"""Autoplay: colony scheduler lifecycle, activation, undo, delivery and failure handling."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from plugin.sdk.plugin import Err, Ok, SdkError

from .agent.delivery.handoff import consultation_handoff
from .configuration import autoplay_budget_settings
from .game.bridge import BridgeQueryTimeout
from .game.state import utc_now
from .tool_contracts import UNKNOWN_WRITE_OUTCOME_CODES, ToolExecutionError

DEFAULT_GOAL = "Prioritize survival and stability."
DEFAULT_CONSTRAINTS = "No cannibalism or live organ harvesting."
REQUIRED_CAPABILITIES = frozenset({"colony_write.transactions.v1", "colony_write.ledger_snapshot.v1",
                                   "colony_events.v1", "colony_pause.v1", "colony_pause.policy.v1"})
ENDPOINT_FAILURE_CODES = frozenset({"model_transport", "model_transport_timeout", "model_first_response_timeout",
                                    "model_incomplete_stream", "model_http_error", "model_stream_error"})
SILENT_REQUEST_PHASES = frozenset({"starting", "connecting", "connected", "tls", "tls_complete",
                                   "response_headers", "first_response"})


def unresponsive_request_deadline(registry, run_id, request_limit):
    """Distinguish a silent endpoint timeout from a run-limited or active stream."""
    if not run_id or not isinstance(request_limit, (int, float)) or isinstance(request_limit, bool):
        return False
    try:
        details = registry.get_details(run_id, event_offset=0, event_limit=100_000) if registry else None
    except Exception:
        return False
    events = details.get("events", []) if isinstance(details, dict) else []
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        data = event.get("data", {})
        if event.get("event") != "model_request_failed" or data.get("error_code") != "model_request_deadline":
            continue
        diagnostics = data.get("diagnostics") or {}
        if (not isinstance(diagnostics, Mapping) or diagnostics.get("first_useful_delta_ms") is not None
                or diagnostics.get("phase") not in SILENT_REQUEST_PHASES):
            return False
        started = next((item for item in reversed(events[:index]) if item.get("event") == "model_request_started"), None)
        timeout = (started.get("data") or {}).get("timeout_seconds") if started else None
        return type(timeout) in (int, float) and timeout >= request_limit
    return False


class Autoplay:
    """Explicit, process-local autoplay for one connected colony; startup never enables it."""

    def __init__(self, *, bridge, agent_settings, frontdesk, workspace, load_tools, config, push_message,
                 agent_audit, logger):
        self._bridge = bridge
        self._agent_settings = agent_settings
        self._frontdesk = frontdesk
        self._workspace = workspace
        self._load_tools = load_tools
        self._config = config
        self._push_message = push_message
        self._agent_audit = agent_audit
        self._logger = logger
        self.scheduler = None
        self.delivery = None
        self.waiters = {}
        self.fault = None
        self.budget = None
        self.settings = {}
        self._mode_lock = asyncio.Lock()
        self._bound_tasks = {}

    def _session(self):
        return getattr(self._bridge(), "session", None)

    def _connection(self):
        return getattr(self._bridge(), "connection_key", None)

    def _push_chat(self, text):
        return self._push_message(source="neko_rimworld", visibility=["chat"], ai_behavior="blind",
                                  parts=[{"type": "text", "text": text}])

    async def panel_state(self) -> dict[str, Any]:
        return {"enabled": self.scheduler is not None, "fault": self.fault,
                "budget": await self.budget.status() if self.budget else None, "settings": self.settings}

    def bind_task(self, task, scheduler) -> None:
        """Cancel ``task`` when ``scheduler`` stops."""
        self._bound_tasks[task] = scheduler
        task.add_done_callback(lambda done: self._bound_tasks.pop(done, None))

    def response_waiter(self, seq, binding):
        """The shared future for frontdesk result ``seq``; None when it belongs to another binding."""
        future = self.waiters.get(seq)
        if future is not None:
            return future if getattr(future, "_autoplay_binding", None) == binding else None
        future = asyncio.get_running_loop().create_future()
        future._autoplay_binding = binding
        self.waiters[seq] = future

        def clear_waiter(done):
            if self.waiters.get(seq) is done:
                self.waiters.pop(seq, None)
        future.add_done_callback(clear_waiter)
        return future

    async def set_mode(self, enabled, goal, constraints, goal_edited, constraints_edited):
        async with self._mode_lock:
            try:
                if enabled and self.scheduler is None:
                    choices = {"goal": goal, "constraints": constraints, "goal_edited": goal_edited,
                               "constraints_edited": constraints_edited}
                    if (any(not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in (goal, constraints))
                            or type(goal_edited) is not bool or type(constraints_edited) is not bool):
                        raise ValueError("Goal and constraints must be nonempty text of at most 2000 characters")
                    if ((not goal_edited and goal != DEFAULT_GOAL) or
                            (not constraints_edited and constraints != DEFAULT_CONSTRAINTS)):
                        raise ValueError("Unedited choices must match the displayed defaults")
                    await self.start(choices)
                elif not enabled:
                    game = self._session()
                    scheduler = self.scheduler
                    try:
                        if game is not None and scheduler is not None:
                            await scheduler.service.call("record", game.colony_id, game.load_epoch, game.game_tick,
                                                         "user", "autoplay.disabled", {})
                    finally:
                        await self.stop()
                return Ok({"enabled": self.scheduler is not None})
            except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
                return Err(SdkError(str(error), code="autoplay_unavailable"))

    async def start(self, choices):
        """Explicit opt-in for the local scheduler; startup never enables it."""
        from .agent.runtime.colony_scheduler import ColonyScheduler
        from .workspace.budget import ColonyBudget

        if self.scheduler is not None:
            raise RuntimeError("Scheduler already started")
        game = self._session()
        agent_settings = self._agent_settings()
        if game is None or not agent_settings.configured:
            raise ValueError("A connected colony and configured model are required")
        service = self._frontdesk()

        def event_binding():
            return (self._connection(), getattr(self._session(), "colony_id", None),
                    getattr(self._session(), "load_epoch", None))

        setup = await self._load_tools()
        if (setup.write_port is None or setup.write_identity is None or setup.write_identity() is None
                or not REQUIRED_CAPABILITIES <= set(setup.write_port.capabilities) or setup.write_catalog is None
                or setup.ledger_query is None or setup.write_fields_query is None):
            raise ValueError("The connected Mod does not provide all autoplay capabilities")
        config = await self._config().dump(timeout=5.0)
        budget_settings = autoplay_budget_settings(config.get("neko_rimworld", {}).get("autoplay", {}))
        if (self.budget is not None and self.budget.game.colony_id != game.colony_id
                and self.budget.pending):
            raise ValueError("A previous colony has an unrecorded usage receipt")
        if self.budget is None or self.budget.game.colony_id != game.colony_id:
            self.budget = ColonyBudget(service, game, budget_settings)
        else:
            self.budget.game = game
            self.budget.settings = budget_settings
            await self.budget.reenable()

        async def scoped_failure(role, result, bound_game=None):
            await self.failure(role, result, bound_game, origin_scheduler=scheduler)

        async def watchdog_notify(bound_game):
            if self.scheduler is not scheduler or not scheduler._business_current(bound_game):
                raise ValueError("The active colony changed before the pause notification")
            return self._push_chat("RimWorld is still paused. Please review the pause and resume when appropriate.")
        scheduler = ColonyScheduler(service, agent_settings, event_provider=self._load_tools, binding=event_binding,
                                    budget=self.budget, failure_handler=scoped_failure,
                                    watchdog_notify=watchdog_notify)
        identity = setup.write_identity()
        current = self._session()
        if (identity is None or tuple(identity[1:]) != (game.colony_id, game.load_epoch) or current is None
                or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch)
                or self._connection() != identity[0]):
            raise ValueError("The active colony changed while enabling autoplay")
        await service.call("observe_epoch", game.colony_id, game.load_epoch, game.game_tick,
                           command_binding_check=lambda: event_binding() == tuple(identity))
        if event_binding() != tuple(identity):
            raise ValueError("The active colony changed while enabling autoplay")

        async def read_pause_state():
            if getattr(setup, "idle_query", None) is None:
                return None, None, "native_read_unavailable"
            try:
                observation = await setup.idle_query("read_situation", {})
                value = observation.to_dict() if hasattr(observation, "to_dict") else dict(observation)
                response = value.get("response", {})
                if (response.get("connection_epoch"), response.get("colony_id"), response.get("load_epoch")) != tuple(identity):
                    return None, None, "native_scope_changed"
                data = response.get("payload", {}).get("data", {})
                state = data.get("clock", {}).get("pause_state")
                scheduler.observe_pause_state(state, tuple(identity))
                attention = {"scope": {key: response.get(key) for key in ("connection_epoch", "colony_id", "load_epoch")},
                             "response_tick": response.get("game_tick"),
                             "snapshot_sequence": response.get("snapshot_sequence"), "sample": data.get("sample"),
                             "attention_snapshot": data.get("attention_snapshot")}
                return (scheduler.pause_state, attention, None) if scheduler.pause_state is not None else (None, None, "native_pause_state_unavailable")
            except Exception as error:
                return None, None, "native_read_" + type(error).__name__

        def startup_tick(state=None):
            current_tick = getattr(self._session(), "game_tick", game.game_tick)
            sampled_tick = state.get("sampled_tick", game.game_tick) if isinstance(state, dict) else game.game_tick
            return max(game.game_tick, current_tick, sampled_tick)

        before, before_attention, problem = await read_pause_state()
        if problem or event_binding() != tuple(identity):
            reason = problem or "native_scope_changed"
            await service.call("record", game.colony_id, game.load_epoch, startup_tick(before), "planner",
                               "autoplay.startup_pause", {"stage": "failed", "outcome": "not_sent", "reason": reason},
                               command_binding_check=lambda: event_binding() == tuple(identity))
            raise ValueError("Startup pause state unavailable: " + reason)
        receipt = None
        if not before["paused"]:
            await service.call("record", game.colony_id, game.load_epoch, startup_tick(before), "planner",
                               "autoplay.startup_pause", {"stage": "requested", "role": "planner"},
                               command_binding_check=lambda: event_binding() == tuple(identity))
            try:
                receipt = await setup.write_port.set_paused(True, role="planner")
            except BaseException as error:
                try:
                    await asyncio.shield(service.call("record", game.colony_id, game.load_epoch, startup_tick(before), "planner",
                                                      "autoplay.startup_pause", {"stage": "failed", "outcome": "unknown",
                                                                                 "error_type": type(error).__name__}))
                except Exception:
                    pass
                raise
            after, after_attention, problem = await read_pause_state()
            if problem or event_binding() != tuple(identity) or not after["paused"]:
                reason = problem or ("native_scope_changed" if event_binding() != tuple(identity) else "native_pause_not_confirmed")
                await service.call("record", game.colony_id, game.load_epoch, startup_tick(before), "planner",
                                   "autoplay.startup_pause", {"stage": "failed", "outcome": "known",
                                                              "reason": reason, "receipt": dict(receipt)},
                                   command_binding_check=lambda: event_binding() == tuple(identity))
                raise ValueError("Startup pause state unavailable: " + reason)
            pause_state = after
            initial_attention = after_attention
        else:
            pause_state = before
            initial_attention = before_attention
        if event_binding() != tuple(identity):
            raise ValueError("The active colony changed while recording startup pause")
        pause_evidence_seq = await service.call("record", game.colony_id, game.load_epoch, startup_tick(pause_state), "planner",
                           "autoplay.startup_pause", {"stage": "result", "receipt": dict(receipt) if receipt else None,
                                                       "preserved_existing": before["paused"], "pause_state": pause_state,
                                                       "initial_attention": initial_attention,
                                                       "attention_available": {"colony_events.v1", "colony_attention.v1"} <= set(setup.write_port.capabilities)},
                           command_binding_check=lambda: event_binding() == tuple(identity))
        scheduler.observe_pause_state(pause_state, tuple(identity), evidence_seq=pause_evidence_seq)
        await service.call("record_autoplay_activation", game.colony_id, game.load_epoch, startup_tick(pause_state), choices,
                           command_binding_check=lambda: event_binding() == tuple(identity))
        scheduler.start()
        scheduler.resume(game)
        self.scheduler = scheduler
        self.fault = None
        self.delivery = asyncio.create_task(self.deliver(scheduler))
        return scheduler

    async def stop(self):
        scheduler = self.scheduler
        tasks = [task for task, owner in list(self._bound_tasks.items())
                 if scheduler is not None and owner is scheduler and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        worker = self.delivery
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            self.delivery = None
        for future in self.waiters.values():
            if not future.done():
                future.cancel()
        scheduler = self.scheduler
        if scheduler is not None:
            await scheduler.close()
            self.scheduler = None

    async def undo(self, seq):
        scheduler = self.scheduler
        if scheduler is None or scheduler.halted:
            return Err(SdkError("Autoplay must be running to undo an action.", code="undo_unavailable"))
        current = self._session()
        if current is None:
            return Err(SdkError("Current colony unavailable.", code="world_changed"))
        bound_game = SimpleNamespace(colony_id=current.colony_id, load_epoch=current.load_epoch,
                                     game_tick=current.game_tick, connection_epoch=self._connection())
        async with scheduler.write_lock:
            return await self.undo_locked(seq, scheduler, bound_game=bound_game)

    async def undo_locked(self, seq, scheduler, *, bound_game=None):
        failure_game = None
        try:
            from .agent.write.service import WriteService
            from .workspace.takeovers import TakeoverControl

            game = self._session()
            work = self._workspace()
            if game is None or work is None:
                raise ValueError("Current colony unavailable")
            failure_game = bound_game or SimpleNamespace(colony_id=game.colony_id, load_epoch=game.load_epoch,
                                                          game_tick=game.game_tick,
                                                          connection_epoch=self._connection())
            game = failure_game
            expected = (game.connection_epoch, game.colony_id, game.load_epoch)

            def check_binding():
                current = self._session()
                return (all(isinstance(value, str) and value for value in expected)
                        and self.scheduler is scheduler and not scheduler.halted and current is not None
                        and (self._connection(), current.colony_id, current.load_epoch) == expected)

            def require_binding():
                if not check_binding():
                    raise ToolExecutionError("world_changed", "The colony connection changed before undo.", recoverable=False)
            require_binding()
            event = await work.call("event", game.colony_id, seq)
            require_binding()
            write = event["payload"].get("write") if event and event["kind"] == "planner.progress" else None
            receipt = write.get("receipt") if isinstance(write, dict) else None
            if (event is None or not isinstance(write, dict) or event["load_epoch"] != game.load_epoch or write.get("stage") != "result"
                    or write.get("mode") != "commit" or not isinstance(receipt, dict)
                    or receipt.get("status") != "committed" or not receipt.get("undo_token")):
                raise ValueError("Current-epoch undo is unavailable")
            setup = await self._load_tools()
            require_binding()
            if setup.write_identity is None or setup.write_identity() != expected:
                raise ToolExecutionError("world_changed", "The undo tool is bound to another connection.", recoverable=False)
            authorization_revision = getattr(work, "authorization_revision", 0)

            def identity():
                return expected if (check_binding() and setup.write_identity() == expected
                                    and getattr(work, "authorization_revision", 0) == authorization_revision) else None
            guard = TakeoverControl(work, setup.ledger_query, identity, setup.write_fields_query,
                                    scoped_provider=getattr(setup, "scoped_ledger_query", None))

            async def record(event_data):
                await work.call("record", game.colony_id, game.load_epoch, game.game_tick,
                                "plugin", "autoplay.undo", {"source_seq": seq, "write": event_data})
            writer = WriteService(setup.write_catalog, [], setup.write_port, identity,
                                  record=record, guard=guard)
            await work.call("record", game.colony_id, game.load_epoch, game.game_tick, "user",
                            "autoplay.undo_requested", {"source_seq": seq})
            require_binding()
            result = await writer.undo({"token": receipt["undo_token"]})
            try:
                message = f"User requested undo of action {seq}; game reported {result.get('status', 'unknown')}. Review the affected task and receipt."
                await scheduler.wake(game, source="plugin", text=message)
                await scheduler.wake_frontdesk(game, message, source="plugin")
            except Exception:
                await self.failure("scheduler", {"audit": {"error_code": "workspace_unavailable"}}, failure_game, origin_scheduler=scheduler)
                return Ok({"status": result.get("status"), "review_wake": "unconfirmed"})
            return Ok({"status": result.get("status")})
        except ToolExecutionError as error:
            if error.code == "undo_outcome_unknown":
                await self.failure("scheduler", {"audit": {"error_code": "undo_outcome_unknown"}}, failure_game, origin_scheduler=scheduler)
                return Err(SdkError("Undo outcome is unknown; check the game and action log before retrying.",
                                    code="undo_outcome_unknown"))
            if error.code == "write_audit_failed":
                outcome = error.details.get("outcome")
                reconciliation = outcome != "not_sent"
                failure = {"audit": {"error_code": "workspace_unavailable"},
                           "write_recording": {"mode": "undo", "stage": "audit", "outcome": outcome or "unknown",
                                               "needs_reconciliation": reconciliation}}
                await self.failure("scheduler", failure, failure_game, origin_scheduler=scheduler)
                message = ("Undo was not sent because its audit record was not saved." if not reconciliation else
                           "Undo outcome or receipt is unconfirmed; check the game and action log before retrying.")
                return Err(SdkError(message,
                                    code=error.code, details=error.details))
            return Err(SdkError(str(error), code=error.code))
        except BridgeQueryTimeout:
            await self.failure("scheduler", {"audit": {"error_code": "undo_outcome_unknown"}}, failure_game, origin_scheduler=scheduler)
            return Err(SdkError("Undo outcome is unknown; check the game and action log before retrying.",
                                code="undo_outcome_unknown"))
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
            return Err(SdkError(str(error), code="undo_unavailable"))

    async def deliver(self, scheduler):
        try:
            await self.delivery_loop(scheduler)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.failure("frontdesk", {"audit": {"error_code": "workspace_unavailable"}}, origin_scheduler=scheduler)

    async def delivery_loop(self, scheduler):
        while True:
            item = await scheduler.next_frontdesk_result()
            ident, result = item["delivery_id"], item["result"]
            terminal = result.get("terminal", {}).get("kind")
            pending = scheduler._pending_frontdesk
            through = item.get("through_seq", ident)
            waiters = [future for seq, future in self.waiters.items()
                       if ident is not None and through is not None and ident <= seq <= through and not future.done()]
            if item.get("colony_id") is not None:
                current = self._session()
                connection_changed = item.get("require_connection") and (
                    not item.get("connection_epoch") or self._connection() != item["connection_epoch"])
                if (current is None or (current.colony_id, current.load_epoch) != (item["colony_id"], item["load_epoch"])
                        or connection_changed):
                    if pending is not None:
                        await scheduler.service.call("record", pending["colony_id"], pending["load_epoch"],
                            pending["game_tick"], "plugin", "autoplay.stale_delivery",
                            {"delivery_id": ident, "reason": "world_changed"})
                    for waiter in waiters:
                        waiter.set_result({"status": "failed", "audit": {"error_code": "world_changed"}})
                    if pending is not None:
                        await scheduler.ack_frontdesk_result(ident)
                    continue
            if pending is not None and terminal != "update_only":
                handoff = consultation_handoff(result, "")
                await scheduler.service.call("record", pending["colony_id"], pending["load_epoch"],
                    pending["game_tick"], "frontdesk", "agent.delivery",
                    {"utterance_seq": int(result["workspace"]["turn_id"]), "summary": handoff["summary"],
                     "delivery": handoff["delivery"], "report": result.get("report"), "prepared_at": utc_now(),
                     "meaning": "Prepared host response; not a receipt of N.E.K.O consumption.", "handoff": handoff})
            if waiters:
                if pending is not None:
                    await scheduler.service.call("record", pending["colony_id"], pending["load_epoch"],
                        pending["game_tick"], "plugin", "autoplay.host_submission",
                        {"delivery_id": ident, "outcome": "user_handoff", "meaning": "Prepared for the waiting host request; not user consumption."})
                for waiter in waiters:
                    waiter.set_result(result)
            elif terminal != "update_only" and not item.get("failed"):
                handoff = consultation_handoff(result, "")
                pending = scheduler._pending_frontdesk
                if pending is not None:
                    events = await scheduler.service.call("events_by_kind", pending["colony_id"], ("autoplay.host_submission",))
                    already_attempted = any(e["kind"] == "autoplay.host_submission"
                                            and e["payload"].get("delivery_id") == ident for e in events)
                    if not already_attempted:
                        # Record the attempt before crossing the SDK boundary. A crash in the
                        # gap leaves the outcome unknown, never a reason for unlimited replay.
                        await scheduler.service.call("record", pending["colony_id"], pending["load_epoch"],
                            pending["game_tick"], "plugin", "autoplay.host_submission",
                            {"delivery_id": ident, "outcome": "attempting", "meaning": "Submission outcome may be unknown; host may drop without a client."})
                        try:
                            submission = self._push_chat(handoff["summary"])
                            outcome = "submitted" if isinstance(submission, dict) and submission.get("submitted") is True else "rejected"
                        except Exception:
                            outcome = "rejected"
                        await scheduler.service.call("record", pending["colony_id"], pending["load_epoch"],
                            pending["game_tick"], "plugin", "autoplay.host_submission_result",
                            {"delivery_id": ident, "outcome": outcome, "meaning": "Local submission only; host may drop without a client."})
            if ident is not None and not item.get("failed") and terminal != "update_only":
                await scheduler.ack_frontdesk_result(ident)

    async def failure(self, role, result, bound_game=None, *, origin_scheduler=None):
        scheduler = self.scheduler
        current = self._session()
        code = (result.get("audit") or {}).get("error_code") or "run_failed"
        origin_connection = getattr(bound_game, "connection_epoch", None)
        observed_connection = self._connection()
        recording = result.get("write_recording")
        needs_reconciliation = isinstance(recording, Mapping) and recording.get("needs_reconciliation") is True
        same_world_reconciliation = (
            (code in UNKNOWN_WRITE_OUTCOME_CODES or needs_reconciliation) and scheduler is not None and origin_scheduler is scheduler
            and bound_game is not None and current is not None
            and (current.colony_id, current.load_epoch) == (bound_game.colony_id, bound_game.load_epoch)
            and isinstance(origin_connection, str) and bool(origin_connection)
            and isinstance(observed_connection, str) and bool(observed_connection)
            and origin_connection != observed_connection
        )
        shared_persistence_failure = (code in {"workspace_unavailable", "planner_result_persistence_failed"}
                                      and scheduler is not None and origin_scheduler is scheduler)
        stale = (origin_scheduler is not None and origin_scheduler is not scheduler) or (
            bound_game is not None and (current is None or
                (current.colony_id, current.load_epoch) != (bound_game.colony_id, bound_game.load_epoch) or
                (getattr(bound_game, "connection_epoch", None) is not None and
                 self._connection() != bound_game.connection_epoch)))
        if stale and not same_world_reconciliation and not shared_persistence_failure:
            source = origin_scheduler or scheduler
            audit = result.get("audit") or {}
            if bound_game is not None:
                stale_binding = (source, getattr(bound_game, "connection_epoch", None),
                                 bound_game.colony_id, bound_game.load_epoch)
                for waiter in self.waiters.values():
                    if getattr(waiter, "_autoplay_binding", None) == stale_binding and not waiter.done():
                        waiter.set_result({"status": "failed", "audit": {"error_code": "world_changed"}})
            if source is not None and bound_game is not None and hasattr(source, "service"):
                try:
                    await source.service.call("record", bound_game.colony_id, bound_game.load_epoch,
                        getattr(bound_game, "game_tick", 0), "plugin", "autoplay.stale_failure",
                        {"role": role, "code": audit.get("error_code") or "run_failed",
                         "phase": audit.get("phase"), "exception_type": audit.get("exception_type"),
                         "run_id": audit.get("run_id"), "wake_seq": audit.get("wake_seq"),
                         "diagnostic_seq": audit.get("diagnostic_seq"), "diagnostic_status": audit.get("diagnostic_status"),
                         "connection_epoch": getattr(bound_game, "connection_epoch", None)})
                except Exception:
                    self._logger().warning("Stale autoplay failure could not be recorded")
            return
        if scheduler is None or (scheduler.halted and not same_world_reconciliation):
            return
        scheduler.halted = True
        if scheduler.budget is not None:
            scheduler.budget.blocked = True
        if same_world_reconciliation:
            current_task = asyncio.current_task()
            for name in ("_worker", "_frontdesk_worker"):
                worker = getattr(scheduler, name, None)
                if worker is not None and worker is not current_task and not worker.done():
                    worker.cancel()
        endpoint = code in ENDPOINT_FAILURE_CODES
        if code == "model_request_deadline":
            endpoint = unresponsive_request_deadline(
                self._agent_audit(), (result.get("audit") or {}).get("run_id"),
                getattr(getattr(scheduler, "settings", None), "request_timeout_seconds", None))
        kind = "endpoint" if endpoint else "budget" if code == "agent_cumulative_budget" else "execution"
        audit = result.get("audit") or {}
        self.fault = {"role": role, "code": code, "kind": kind, "pause": "unknown",
                      "phase": None, "exception_type": None, "run_id": None, "wake_seq": None,
                      "diagnostic_seq": None, "diagnostic_status": None,
                      "origin_connection_epoch": origin_connection or observed_connection,
                      "origin_colony_id": getattr(bound_game, "colony_id", None),
                      "origin_load_epoch": getattr(bound_game, "load_epoch", None)}
        if same_world_reconciliation:
            self.fault["observed_connection_epoch"] = observed_connection
        for key in ("phase", "exception_type", "run_id", "wake_seq", "diagnostic_seq", "diagnostic_status", "start_seq", "context_tick"):
            value = audit.get(key)
            if (isinstance(value, str) and value and len(value) <= 128) or (key in {"wake_seq", "diagnostic_seq", "start_seq", "context_tick"} and type(value) is int):
                self.fault[key] = value
        failure_binding = (scheduler, self._connection(),
                           getattr(current, "colony_id", None), getattr(current, "load_epoch", None))
        for waiter in self.waiters.values():
            binding = getattr(waiter, "_autoplay_binding", failure_binding)
            origin_binding = ((scheduler, origin_connection, bound_game.colony_id, bound_game.load_epoch)
                              if (same_world_reconciliation or shared_persistence_failure) and bound_game is not None else None)
            if (binding == failure_binding or binding == origin_binding) and not waiter.done():
                waiter.set_result({**result, "status": result.get("status", "failed")})
        game = self._session()
        if game is None or (bound_game is not None and (game.colony_id, game.load_epoch) != (bound_game.colony_id, bound_game.load_epoch)):
            self.fault["pause"] = "unconfirmed" if endpoint else "unknown"
            await self.audit_failure_notification(scheduler, bound_game, role, code, kind, "pause unconfirmed")
            return
        try:
            await scheduler.service.call("record", game.colony_id, game.load_epoch, game.game_tick,
                                         "plugin", "autoplay.halted", dict(self.fault))
        except Exception:
            self.fault["record"] = "unconfirmed"
        if endpoint:
            try:
                setup = await self._load_tools()
                identity = setup.write_identity() if setup.write_identity else None
                if (identity is None or tuple(identity[1:]) != (game.colony_id, game.load_epoch)
                        or (bound_game is not None and getattr(bound_game, "connection_epoch", identity[0]) != identity[0])):
                    raise ValueError("The game binding changed")
                await setup.write_port.set_paused(True, role="policy")
                self.fault["pause"] = "requested"
                await scheduler.service.call("record", game.colony_id, game.load_epoch, game.game_tick,
                                             "plugin", "autoplay.policy_pause", {"role": role, "code": code})
            except Exception:
                if self.fault["pause"] != "requested":
                    self.fault["pause"] = "unconfirmed"
                else:
                    self.fault["record"] = "unconfirmed"
        pause_text = "pause requested" if self.fault["pause"] == "requested" else "pause unconfirmed"
        await self.audit_failure_notification(scheduler, bound_game or game, role, code, kind, pause_text)

    async def audit_failure_notification(self, scheduler, game, role, code, kind, pause_text):
        text = (f"RimWorld autoplay: {role} model endpoint failed ({code}); {pause_text}." if kind == "endpoint"
                else f"RimWorld autoplay stopped: {role}, {code}. Game pause state is unknown.")
        payload = {"role": role, "code": code, "kind": kind, "text": text, "ai_behavior": "blind",
                   "origin_connection_epoch": getattr(game, "connection_epoch", None),
                   "scope_kind": "failure_origin", "host_delivery_binding": "unavailable",
                   "outcome_meaning": "Local SDK submission only; user consumption is not confirmed."}

        async def record(phase, outcome=None):
            if game is None or not hasattr(game, "game_tick"):
                self.fault["notification_record"] = "unknown"
                return
            try:
                await scheduler.service.call("record", game.colony_id, game.load_epoch, game.game_tick,
                    "plugin", "autoplay.failure_notification", {**payload, "phase": phase, **({"outcome": outcome} if outcome else {})})
            except Exception:
                self.fault["notification_record"] = "unknown"
        await record("attempting")
        try:
            submission = self._push_chat(text)
            outcome = "submitted" if isinstance(submission, dict) and submission.get("submitted") is True else "rejected"
        except Exception:
            outcome = "unknown"
        self.fault["notification"] = outcome
        await record("result", outcome)

"""Colony scheduler core: role workers, colony binding and planner run lifecycle over the journal.

Journal derives cadence and persists wakes; this service executes their roles.
"""

import asyncio
import copy
import sqlite3
import time
from dataclasses import dataclass
from types import SimpleNamespace

from ...media import ImageInputConfig
from ...message_contracts import private_protocol_redacted
from ...serialization import canonical
from ...tool_contracts import ToolExecutionError
from .audit import AgentAuditLedger
from .dispatch_diagnostics import record_dispatch_error
from .frontdesk_dispatch import run_frontdesk
from .native_event_intake import clear_native_registrations, run_events
from .pause_watchdog import run_pause_watchdog
from .planner_input import assemble_planner_run
from .scheduler_tools import attention_tools, planner_tools
from .session import AgentRunSession

_ATTENTION_CAPABILITIES = frozenset({"colony_events.v1", "colony_attention.v1"})


@dataclass(frozen=True)
class _PlannerResult:
    origin: object
    context_tick: int
    start_seq: int
    cursor: int
    result: dict
    audit: dict

    @property
    def arguments(self):
        return (self.origin.colony_id, self.origin.load_epoch, self.context_tick,
                self.start_seq, self.cursor, self.result, self.audit)


class ColonyScheduler:
    _owner = None

    def __init__(self, service, settings, *, event_provider=None, binding=None, budget=None, failure_handler=None,
                 watchdog_notify=None):
        self.service = service
        self.settings = settings
        self.budget = budget
        self._autoplay_colony = budget.game.colony_id if budget is not None else None
        self.failure_handler = failure_handler
        self.halted = False
        self.write_lock = asyncio.Lock()
        self._event_provider = event_provider
        self._binding = binding
        self._gap_binding = None
        self._pending_gap = None
        self._attempted_binding = object()
        self._retry_at = 0.0
        self._signal = asyncio.Event()
        self._games = {}
        self._worker = None
        self._frontdesk_worker = None
        self._event_worker = None
        self._event_port = None
        self._event_setup = None
        self._event_identity = None
        self._event_cursor = 0
        self._policy_installed = False
        self._installed_policy = None
        self._installed_observations = None
        self._frontdesk_signal = asyncio.Event()
        self._frontdesk_results = asyncio.Queue()
        self._pending_frontdesk = None
        self.error = None
        self.unsaved_result = None
        self._watchdog_notify = watchdog_notify
        self._watchdog_worker = None
        self._watchdog_event = asyncio.Event()
        self._watchdog_interval = 60.0
        self.pause_state = None
        self._pause_episode = None
        self._pause_evidence_seq = None
        self._pause_count = 0
        self._pause_last_wake = None
        self._pause_seen_at = None
        self._pause_waiting = False
        self._pause_notified = False
        self._pause_restored = False
        self._pause_yielded = False
        self._last_yield = None
        self._planner_running = False

    def start(self):
        if self._worker is not None:
            raise RuntimeError("Scheduler already started")
        if ColonyScheduler._owner is not None:
            raise RuntimeError("A planner scheduler already owns this runtime")
        ColonyScheduler._owner = self
        self._worker = asyncio.create_task(self._run())
        self._frontdesk_worker = asyncio.create_task(run_frontdesk(self))
        if self._watchdog_notify is not None:
            self._watchdog_worker = asyncio.create_task(run_pause_watchdog(self))
        if self._event_provider is not None and self._binding is not None:
            self._event_worker = asyncio.create_task(run_events(self))

    def _bind_game(self, game):
        """Snapshot the game identity, keeping it immutable even if the live session object changes.

        A missing connection is taken from the active binding when that binding names the same colony and epoch.
        """
        connection = getattr(game, "connection_epoch", None)
        if self._binding is not None and connection is None:
            try:
                identity = self._binding()
            except Exception:
                identity = None
            if isinstance(identity, tuple) and len(identity) == 3 and identity[1:] == (game.colony_id, game.load_epoch):
                connection = identity[0]
        return SimpleNamespace(colony_id=game.colony_id, load_epoch=game.load_epoch, game_tick=game.game_tick,
                               connection_epoch=connection)

    def _binding_current(self, game):
        current = self.service.game_session()
        if current is None or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch):
            return False
        expected = getattr(game, "connection_epoch", None)
        if self._binding is not None:
            try:
                identity = self._binding()
            except Exception:
                return False
            return (isinstance(expected, str) and bool(expected) and isinstance(identity, tuple)
                    and identity == (expected, game.colony_id, game.load_epoch))
        return True

    def _business_current(self, game):
        return not self.halted and self._binding_current(game)

    def _native_attention_available(self):
        return bool(self._event_port and _ATTENTION_CAPABILITIES <= set(self._event_port.capabilities))

    def _reschedule_current(self, role):
        current = self.service.game_session()
        if current is not None:
            bound = self._bind_game(current)
            if self._binding_current(bound):
                self._games[current.colony_id] = bound
                (self._signal if role == "planner" else self._frontdesk_signal).set()
                return
        signal = self._signal if role == "planner" else self._frontdesk_signal
        asyncio.get_running_loop().call_later(1, signal.set)

    async def _observe_wake_scope(self, game):
        """Confirm the wake targets the active bound colony and record that epoch before recording the wake."""
        current = self.service.game_session()
        if current is None or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch):
            raise ValueError("The active colony changed before wake")
        bound = self._bind_game(game)
        if not self._business_current(bound):
            raise ValueError("The active colony changed before wake")
        await self.service.call("observe_epoch", current.colony_id, current.load_epoch, current.game_tick,
                                command_binding_check=lambda: self._business_current(bound))
        if not self._business_current(bound):
            raise ValueError("The active colony changed before wake")
        return current, bound

    async def wake(self, game, *, source, text, utterance_seq=None):
        if self._worker is None or self._worker.done():
            raise RuntimeError("Scheduler is not running")
        if source not in {"user", "frontdesk", "planner", "game", "plugin"}:
            raise ValueError("Unknown wake source")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Wake reason requires text")
        current, bound = await self._observe_wake_scope(game)
        payload = {"source": source, "text": text}
        if source == "user":
            original = await self.service.call("event", current.colony_id, utterance_seq)
            if (original is None or original["kind"] != "user.utterance"
                    or original["payload"].get("text") != text):
                raise ValueError("User wake must cite the original utterance")
            payload["utterance_seq"] = utterance_seq
        seq = await self.service.call("record", current.colony_id, current.load_epoch, current.game_tick,
                                      "plugin", "planner.wake", payload,
                                      command_binding_check=lambda: self._business_current(bound))
        self._games[game.colony_id] = bound
        self._signal.set()
        return seq

    async def next_frontdesk_result(self):
        """Receive a completed result; persisted run is prepared, not host delivery."""
        return await self._frontdesk_results.get()

    async def ack_frontdesk_result(self, delivery_id):
        pending = self._pending_frontdesk
        if pending is None or pending["delivery_id"] != delivery_id:
            raise ValueError("Unknown or already acknowledged frontdesk result")
        await self.service.call("record", pending["colony_id"], pending["load_epoch"], pending["game_tick"],
                                "frontdesk", "frontdesk.scheduler_consumed",
                                {"through_seq": pending["through_seq"], "delivery_id": delivery_id})
        self._pending_frontdesk = None
        self._frontdesk_signal.set()

    async def wake_frontdesk(self, game, text, *, source="user", request_id=None, purpose=None):
        """Schedule a real user utterance through the existing frontdesk service."""
        if self._worker is None or self._worker.done():
            raise RuntimeError("Scheduler is not running")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("User utterance requires text")
        current, bound = await self._observe_wake_scope(game)
        if source not in {"user", "game", "agent", "frontdesk", "planner", "plugin"}:
            raise ValueError("Unknown wake source")
        if request_id is not None:
            if source != "user" or not isinstance(request_id, str) or not request_id:
                raise ValueError("A request identity is only valid for user messages")
        seq = await self.service.call("record_frontdesk_wake", current.colony_id, current.load_epoch,
                                      current.game_tick, source, text,
                                      command_binding_check=lambda: self._business_current(bound),
                                      **({"request_id": request_id} if request_id is not None else {}),
                                      **({"purpose": purpose} if purpose is not None else {}))
        self._games[current.colony_id] = bound
        self._frontdesk_signal.set()
        return seq

    def resume(self, game):
        """Re-register a colony after restart and replay unconsumed wakes."""
        if self._worker is None or self._worker.done():
            raise RuntimeError("Scheduler is not running")
        self._games[game.colony_id] = self._bind_game(game)
        self._signal.set()
        self._frontdesk_signal.set()

    async def close(self):
        if self._worker is not None:
            for task in (self._worker, self._frontdesk_worker, self._event_worker, self._watchdog_worker):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (self._worker, self._frontdesk_worker, self._event_worker,
                                                     self._watchdog_worker)
                                   if task is not None), return_exceptions=True)
        try:
            try:
                await self._persist_planner_result()
            except Exception as error:
                await self._planner_error(error, self.unsaved_result.origin)
                raise
        finally:
            try:
                await clear_native_registrations(self._event_port, self._event_identity)
            finally:
                self._event_port = None
                self._event_identity = None
                self._worker = None
                self._watchdog_worker = None
                if ColonyScheduler._owner is self:
                    ColonyScheduler._owner = None

    def observe_pause_state(self, state, binding, *, evidence_seq=None):
        """Accept only native pause evidence belonging to the active event binding."""
        required = ("connection_epoch", "colony_id", "load_epoch")
        valid = (isinstance(state, dict) and state.get("status") == "observed"
                 and tuple(state.get(key) for key in required) == tuple(binding)
                 and all(type(state.get(key)) is int and state[key] >= 0
                         for key in ("sampled_tick", "pause_revision", "pause_transition_revision"))
                 and all(type(state.get(key)) is bool for key in
                         ("paused", "speed_paused", "force_paused", "windows_force_paused"))
                 and isinstance(state.get("owner"), str))
        if not valid:
            self.pause_state = None
            self._watchdog_event.set()
            return
        if (self.pause_state is not None and tuple(self.pause_state.get(key) for key in required) == tuple(binding)
                and (state["pause_revision"], state["pause_transition_revision"], state["sampled_tick"]) <
                    (self.pause_state["pause_revision"], self.pause_state["pause_transition_revision"],
                     self.pause_state["sampled_tick"])):
            return
        episode = (*binding, state["pause_revision"], state["pause_transition_revision"])
        if self._pause_episode != episode:
            self._pause_episode = episode
            self._pause_evidence_seq = None
            self._pause_count = 0
            self._pause_last_wake = None
            self._pause_seen_at = time.monotonic()
            self._pause_waiting = False
            self._pause_notified = False
            self._pause_restored = False
            self._pause_yielded = bool(self._last_yield and self._last_yield[:3] == tuple(binding)
                                       and type(evidence_seq) is int and evidence_seq < self._last_yield[3])
        if type(evidence_seq) is int:
            self._pause_evidence_seq = evidence_seq
        self.pause_state = copy.deepcopy(state)
        self._watchdog_event.set()

    async def _run(self):
        try:
            while True:
                await self._signal.wait()
                self._signal.clear()
                for game in list(self._games.values()):
                    if self.halted:
                        break
                    try:
                        async with self.write_lock:
                            if not self._binding_current(game):
                                self._reschedule_current("planner")
                                continue
                            self._planner_running = True
                            try:
                                await self._run_colony(game)
                            finally:
                                self._planner_running = False
                                self._watchdog_event.set()
                    except Exception as error:
                        await self._planner_error(error, game)

        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = error
            raise

    async def _planner_error(self, error, game):
        self.error = error
        completion = self.unsaved_result
        origin = completion.origin if completion is not None else game
        storage_error = isinstance(error, (OSError, sqlite3.Error))
        code = ("workspace_unavailable" if storage_error else "planner_result_persistence_failed"
                if completion is not None else getattr(error, "code", "execution_failed"))
        diagnostic = await record_dispatch_error(self.service, "planner", error, origin, code, completion=completion)
        if self.failure_handler is not None:
            await self.failure_handler("planner", {"audit": diagnostic}, origin)
        elif storage_error and self._worker is not None and not self._worker.done():
            asyncio.get_running_loop().call_later(1, self._signal.set)

    async def _persist_planner_result(self):
        pending = self.unsaved_result
        if pending is None:
            return
        finish_seq = await self.service.call("finish_planner", *pending.arguments)
        self.unsaved_result = None
        if (pending.result.get("terminal") or {}).get("kind") == "yield" and self._business_current(pending.origin):
            binding = (getattr(pending.origin, "connection_epoch", None), pending.origin.colony_id,
                       pending.origin.load_epoch)
            self._last_yield = (*binding, finish_seq)
            self._pause_yielded = bool(self.pause_state is not None and self._pause_episode is not None
                                       and self._pause_episode[:3] == binding and self._pause_evidence_seq is not None
                                       and self._pause_evidence_seq < finish_seq)
            self._pause_waiting = False
            self._watchdog_event.set()
        self._signal.set()
        return finish_seq

    async def _run_colony(self, game):
        await self._persist_planner_result()
        current = self.service.game_session()
        if current is None or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch):
            raise ValueError("The active colony changed before planner execution")
        run_scope = await self._check_autoplay_binding(game)
        scope = run_scope or game
        colony_id, load_epoch, last_tick = current.colony_id, current.load_epoch, current.game_tick

        def business_current():
            return self._business_current(scope)

        def current_tick(*, require_binding=True):
            nonlocal last_tick
            now = self.service.game_session()
            if now is None or (now.colony_id, now.load_epoch) != (colony_id, load_epoch) or (require_binding and not business_current()):
                if require_binding:
                    raise ToolExecutionError("world_changed", "The active colony or epoch changed.", recoverable=False)
                return last_tick
            last_tick = now.game_tick
            return last_tick

        attention = await self.service.call("attention_state", colony_id, load_epoch, last_tick,
                                            command_binding_check=business_current)
        run = await assemble_planner_run(
            self.service, self.settings, colony_id, load_epoch, getattr(scope, "connection_epoch", None),
            attention=attention, pause=copy.deepcopy(self.pause_state), pause_seq=self._pause_evidence_seq,
            current_tick=current_tick, observed_tick=lambda: last_tick, business_current=business_current,
            event_provider=self._event_provider, attention_available=self._native_attention_available)
        if run is None:
            return
        pending = run.pending
        start = await self.service.call("record", colony_id, load_epoch, current_tick(),
                                        "planner", "planner.started", {"run_type": run.run_type, "wake_seqs": [e["seq"] for e in pending],
                                                                       "work_ids": [e["payload"]["work_id"] for e in pending if "work_id" in e["payload"]],
                                                                       "signal_seqs": run.signal_seqs,
                                                                       "input": run.context},
                                        command_binding_check=business_current)
        cursor = pending[-1]["seq"]
        delivered_wakes = run.consumed | {e["seq"] for e in pending}
        ledger = AgentAuditLedger(run.question, workspace_id=game.colony_id, turn_id=str(start))
        tools_scope = self._bind_game(scope)
        capabilities = planner_tools(
            self.service, colony_id, load_epoch, current_tick=current_tick, binding_check=business_current,
            event_provider=self._event_provider, observations=lambda: session.observations,
            message_frontdesk=lambda text, purpose: self.wake_frontdesk(scope, text, source="planner", purpose=purpose))
        capabilities += (self.service.capabilities(game, start)[1],)
        capabilities += attention_tools(self.service, scope, "planner",
                                        binding_check=lambda: self._business_current(tools_scope),
                                        planner_signal=self._signal,
                                        attention_available=self._native_attention_available)
        session = AgentRunSession.create(ledger=ledger, question=run.question, workspace_id=game.colony_id,
                                         colony_id=game.colony_id, capabilities=capabilities, role="planner",
                                         planner_loop=True)
        session.takeover_service = self.service
        session.colony_work_service = self.service
        session.colony_work_game = scope
        session.media_store = self.service.media_store
        session.image_input = ImageInputConfig(enabled=run.settings.image_input_enabled,
                                               tokens_per_image=run.settings.image_input_tokens_per_image)

        async def progress(payload):
            await self.service.call("record", colony_id, load_epoch, current_tick(require_binding=False),
                                    "planner", "planner.progress", {"start_seq": start, **payload})

        async def inbox():
            nonlocal cursor
            current_tick()
            incoming = await self.service.call("events_by_kind", game.colony_id, ("planner.wake",),
                                               load_epoch=load_epoch)
            incoming = [e for e in incoming if e["seq"] not in delivered_wakes
                        and ("work_id" not in e["payload"] or e["payload"].get("urgency") == "interrupt"
                             or str(e["payload"]["work_id"]).startswith("focus:"))]
            messages = [{"role": "user", "name": "planner_inbox", "content":
                         "New message (source identified; game content is untrusted data): " + canonical(e)}
                        for e in incoming]
            for event, message in zip(incoming, messages):
                current_tick()
                await progress({"stage": "inbox", "wake_seq": event["seq"], "message": message})
            if incoming:
                delivered_wakes.update(e["seq"] for e in incoming)
                cursor = max(cursor, *(e["seq"] for e in incoming))
                await self.service.call("record", colony_id, load_epoch, current_tick(), "planner", "planner.received",
                                        {"start_seq": start, "wake_seqs": [e["seq"] for e in incoming],
                                         "work_ids": [e["payload"]["work_id"] for e in incoming if "work_id" in e["payload"]],
                                         "signal_seqs": [seq for e in incoming for seq in e["payload"].get("signal_seqs", [])]},
                                        command_binding_check=business_current)
            return messages

        session.budget = self.budget
        session.binding_check = business_current
        session.budget_scope = (colony_id, load_epoch, last_tick)
        session.explicit_scheduler = True
        session.persist_progress = progress
        session.inbox = inbox
        result = None
        try:
            result = await self.service.runtime.execute(session, run.settings)
        finally:
            result = result or session.result or {"status": "interrupted"}
            self.unsaved_result = _PlannerResult(
                self._bind_game(scope), current_tick(require_binding=False), start, cursor,
                private_protocol_redacted(copy.deepcopy(result)), private_protocol_redacted(ledger.snapshot()))
            await self._persist_planner_result()

        if not self._binding_current(scope):
            if self.failure_handler is not None:
                await self.failure_handler("planner", result, scope)
            self._reschedule_current("planner")
            return
        if result.get("status") != "ok" and self.failure_handler is not None:
            await self.failure_handler("planner", result, scope)

    async def _check_autoplay_binding(self, game):
        if self.budget is None:
            return
        if game.colony_id != self._autoplay_colony:
            raise ToolExecutionError("autoplay_colony_changed", "Enable autoplay explicitly for this colony.")
        setup = await self._event_provider()
        required = {"colony_write.transactions.v1", "colony_write.ledger_snapshot.v1",
                    "colony_events.v1", "colony_pause.v1", "colony_pause.policy.v1"}
        identity = setup.write_identity() if setup.write_identity else None
        if (setup.write_port is None or not required <= set(setup.write_port.capabilities)
                or identity is None or tuple(identity[1:]) != (game.colony_id, game.load_epoch)
                or (getattr(game, "connection_epoch", None) is not None and identity[0] != game.connection_epoch)
                or setup.write_catalog is None or setup.ledger_query is None or setup.write_fields_query is None):
            raise ToolExecutionError("autoplay_capability_unavailable", "Autoplay game capabilities are unavailable.")
        self.budget.game = self._bind_game(game)
        return SimpleNamespace(colony_id=game.colony_id, load_epoch=game.load_epoch,
                               game_tick=game.game_tick, connection_epoch=identity[0])

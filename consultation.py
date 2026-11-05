"""One-shot consultation: a host question answered by the frontdesk or the running autoplay."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from collections.abc import Mapping
from types import SimpleNamespace

from plugin.sdk.plugin import Err, Ok, SdkError

from .agent.delivery.handoff import consultation_handoff
from .game.state import utc_now
from .tool_contracts import ToolExecutionError
from .workspace.session_view import anchor_seq


def _host_request_id(context):
    request_id = context.get("run_id") if isinstance(context, Mapping) else None
    return request_id if isinstance(request_id, str) and request_id else None


class Consultations:
    """Host consultation requests; duplicate host calls share one execution."""

    def __init__(self, *, bridge, agent_settings, autoplay, frontdesk, reports, record_unexpected):
        self._bridge = bridge
        self._agent_settings = agent_settings
        self._autoplay = autoplay
        self._frontdesk = frontdesk
        self._reports = reports
        self._record_unexpected = record_unexpected
        self.tasks = set()
        self._inflight = {}
        self._colony_locks = {}

    def _session(self):
        return getattr(self._bridge(), "session", None)

    def _connection(self):
        return getattr(self._bridge(), "connection_key", None)

    async def cancel_all(self):
        calls = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in calls:
            task.cancel()
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)

    async def consult(self, question, context):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            game = self._session()
            if game is None:
                return await self._shared(question, context, game)
            async with self._colony_locks.setdefault(game.colony_id, asyncio.Lock()):
                current = self._session()
                if current is None or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch):
                    return Err(SdkError("The active colony changed while waiting; retry in the current colony.", code="world_changed"))
                return await self._shared(question, context, game)
        except ToolExecutionError as error:
            if error.code == "world_changed":
                return Err(SdkError("The active colony changed; retry in the current colony.", code="world_changed"))
            raise
        finally:
            self.tasks.discard(task)

    async def _shared(self, question, context, game):
        scheduler = self._autoplay.scheduler
        request_id = context.get("run_id") if isinstance(context, Mapping) else None
        connection = self._connection()
        if (game is None or not isinstance(question, str)
                or not isinstance(request_id, str) or not request_id
                or not isinstance(connection, str) or not connection):
            return await self._run(question, context, game)
        key = (id(scheduler), connection, game.colony_id, game.load_epoch, request_id, question)
        task = self._inflight.get(key)
        if task is None:
            async def run_bound():
                if self._autoplay.scheduler is not scheduler or self._connection() != connection:
                    return Err(SdkError("The colony connection changed; retry in the current colony.", code="world_changed"))
                return await self._run(question, context, game)
            task = asyncio.create_task(run_bound())
            self._inflight[key] = task
            self.tasks.add(task)
            if scheduler is not None:
                self._autoplay.bind_task(task, scheduler)

            def clear_inflight(done):
                self.tasks.discard(done)
                if self._inflight.get(key) is done:
                    self._inflight.pop(key, None)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(clear_inflight)
        return copy.deepcopy(await asyncio.shield(task))

    async def _run(self, question, context, game):
        if not isinstance(question, str) or not question.strip():
            return Err(SdkError("A RimWorld question is required.", code="invalid_question"))
        normalized = question.strip()
        if len(normalized) > 2000:
            return Err(SdkError("The RimWorld question is too long.", code="invalid_question"))
        settings = self._agent_settings()
        if not settings.configured:
            return Err(
                SdkError(
                    "Configure the plugin-owned research model in the Hosted UI before consulting it.",
                    code="agent_not_configured",
                )
            )
        if game is None:
            return Err(SdkError("A connected colony is required for persistent frontdesk work.", code="colony_unavailable"))
        scheduler = self._autoplay.scheduler
        if scheduler is not None:
            return await self._run_with_autoplay(question, normalized, context, game, scheduler)
        try:
            connection = self._connection()
            if not isinstance(connection, str) or not connection:
                return Err(SdkError("The colony connection changed; retry in the current colony.", code="world_changed"))

            def binding_check():
                current = self._session()
                return (current is not None and (current.colony_id, current.load_epoch) == (game.colony_id, game.load_epoch)
                        and self._connection() == connection)
            service = self._frontdesk()
            await service.settle_pending(game.colony_id)
            request_id = _host_request_id(context)
            retained_response = await service.call("retained_response", game.colony_id, game.load_epoch, question, request_id) if request_id else None
            current = self._session()
            if current is None or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch):
                return Err(SdkError("The active colony changed during lookup; retry in the current colony.", code="world_changed"))
            if retained_response is not None:
                payload = retained_response["payload"]
                terminal = (payload.get("result") or {}).get("terminal") or {}
                scheduled = "trigger_seq" in payload or terminal.get("kind") in {"reply", "update_only"}
                if not scheduled:
                    events = await service.call("events_by_kind", game.colony_id,
                                                ("frontdesk.wake", "frontdesk.run_binding"))
                    wake_ids = {event["seq"] for event in events if event["kind"] == "frontdesk.wake"
                                and event["load_epoch"] == retained_response["load_epoch"]
                                and anchor_seq(event) == anchor_seq(retained_response)
                                and event["seq"] < retained_response["seq"]}
                    scheduled = any(event["kind"] == "frontdesk.run_binding"
                                    and event["load_epoch"] == retained_response["load_epoch"]
                                    and event["seq"] < retained_response["seq"]
                                    and event["payload"].get("wake_seq") in wake_ids for event in events)
                if scheduled:
                    return Err(SdkError("This prepared autoplay response has expired; start a new request in the current mode.",
                                        code="world_changed"))
            if retained_response is not None and retained_response.get("delivery_payload"):
                saved_delivery = json.loads(retained_response["delivery_payload"])
                if saved_delivery.get("handoff") is not None:
                    return Ok(saved_delivery["handoff"])
            if retained_response is not None:
                result = copy.deepcopy(retained_response["payload"]["result"])
                result["workspace"] = {"workspace_id": game.colony_id, "turn_id": str(retained_response["payload"]["utterance_seq"]), "state": "waiting", "history_saved": True}
            else:
                result = await service.consult(game, question, settings, request_id=request_id, binding_check=binding_check)
            if not result["workspace"]["history_saved"]:
                return Err(SdkError("The completed response could not be saved; retry the same request.", code="workspace_unavailable"))
        except (OSError, sqlite3.Error, ValueError) as exception:
            incident = self._record_unexpected("workspace", "Persistent work could not be completed.", exception)
            return Err(SdkError("The ongoing work could not be saved or loaded; retry after checking storage.", code="workspace_unavailable", details={"incident_id": incident}))
        if result.get("report", {}).get("saved"):
            result["report"]["export"] = await self._reports.export(result["report"]["report_id"], context)
        handoff = consultation_handoff(result, normalized)
        workspace = result.get("workspace")
        if workspace is not None:
            payload = {
                "utterance_seq": int(workspace["turn_id"]), "summary": handoff["summary"],
                "delivery": handoff["delivery"], "report": result.get("report"),
                "prepared_at": utc_now(), "meaning": "Prepared host response; not a receipt of N.E.K.O consumption.", "handoff": handoff,
            }
            try:
                await service.call("record", game.colony_id, game.load_epoch, game.game_tick, "frontdesk", "agent.delivery", payload)
                service.revision += 1
            except (OSError, sqlite3.Error, RuntimeError):
                return Err(SdkError("The prepared response could not be saved; retry the same request.", code="delivery_unavailable"))
        return Ok(handoff)

    async def _run_with_autoplay(self, question, normalized, context, game, scheduler):
        """Route the question through the running scheduler's frontdesk and wait for its result."""
        if scheduler.halted:
            return Err(SdkError("Autoplay is stopped after a failure; check the panel.", code="autoplay_halted"))
        if scheduler._autoplay_colony != game.colony_id:
            return Err(SdkError("Autoplay was enabled for another colony; enable it here first.", code="world_changed"))
        request_id = _host_request_id(context)
        if request_id:
            retained = await scheduler.service.call("retained_response", game.colony_id, game.load_epoch,
                                                    question, request_id)
            if retained is not None:
                events = await scheduler.service.call("events_by_kind", game.colony_id,
                                                      ("frontdesk.wake", "frontdesk.run_binding", "autoplay.stale_delivery"))
                anchor = anchor_seq(retained)
                wakes = [event for event in events if event["kind"] == "frontdesk.wake"
                         and event["load_epoch"] == retained["load_epoch"]
                         and anchor_seq(event) == anchor and event["seq"] < retained["seq"]]
                wake_seq = wakes[-1]["seq"] if wakes else None
                bindings = [event for event in events if event["kind"] == "frontdesk.run_binding"
                            and event["payload"].get("wake_seq") == wake_seq
                            and event["load_epoch"] == retained["load_epoch"] and event["seq"] < retained["seq"]]
                stale = any(event["kind"] == "autoplay.stale_delivery"
                            and event["payload"].get("delivery_id") == wake_seq for event in events)
                current = self._session()
                connection = self._connection()
                if (wake_seq is None or not bindings or stale or not connection
                        or bindings[-1]["payload"].get("connection") != connection or current is None
                        or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch)):
                    return Err(SdkError("The prepared response belongs to an earlier connection or expired world; start a new request.", code="world_changed"))
                delivery = json.loads(retained["delivery_payload"]) if retained.get("delivery_payload") else {}
                if delivery.get("handoff") is not None:
                    return Ok(delivery["handoff"])
                return Ok(consultation_handoff(retained["payload"]["result"], normalized))
        waiter_connection = self._connection()
        if not isinstance(waiter_connection, str) or not waiter_connection:
            return Err(SdkError("The colony connection changed; retry in the current colony.", code="world_changed"))
        bound_game = SimpleNamespace(colony_id=game.colony_id, load_epoch=game.load_epoch,
                                     game_tick=game.game_tick, connection_epoch=waiter_connection)
        try:
            seq = await scheduler.wake_frontdesk(bound_game, question, request_id=request_id)
        except (ToolExecutionError, ValueError):
            return Err(SdkError("The colony connection changed; retry in the current colony.", code="world_changed"))
        if self._connection() != waiter_connection:
            return Err(SdkError("The colony connection changed; retry in the current colony.", code="world_changed"))
        future = self._autoplay.response_waiter(seq, (scheduler, waiter_connection, game.colony_id, game.load_epoch))
        if future is None:
            return Err(SdkError("The prepared response belongs to another connection.", code="world_changed"))
        result = await asyncio.shield(future)
        if result.get("report", {}).get("saved"):
            result["report"]["export"] = await self._reports.export(result["report"]["report_id"], context)
        return Ok(consultation_handoff(result, normalized))

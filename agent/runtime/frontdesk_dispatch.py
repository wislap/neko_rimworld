"""Frontdesk dispatch: run pending frontdesk wakes and offer each completed result for host delivery."""

import asyncio
import copy
import sqlite3
from types import SimpleNamespace

from ...tool_contracts import ToolExecutionError
from ...workspace.session_view import anchor_seq, trigger_message
from .dispatch_diagnostics import record_dispatch_error
from .scheduler_tools import attention_tools


async def run_frontdesk(scheduler):
    try:
        while True:
            await scheduler._frontdesk_signal.wait()
            scheduler._frontdesk_signal.clear()
            for game in list(scheduler._games.values()):
                if scheduler.halted:
                    break
                try:
                    if not scheduler._binding_current(game):
                        scheduler._reschedule_current("frontdesk")
                        continue
                    await frontdesk_colony(scheduler, game)
                except (OSError, sqlite3.Error) as error:
                    scheduler.error = error
                    diagnostic = await record_dispatch_error(scheduler.service, "frontdesk", error, game,
                                                             "workspace_unavailable")
                    if scheduler.failure_handler is not None:
                        await scheduler.failure_handler("frontdesk", {"audit": diagnostic}, game)
                    else:
                        asyncio.get_running_loop().call_later(1, scheduler._frontdesk_signal.set)
                except Exception as error:
                    scheduler.error = error
                    diagnostic = await record_dispatch_error(scheduler.service, "frontdesk", error, game,
                                                             getattr(error, "code", "execution_failed"))
                    if scheduler.failure_handler is not None:
                        await scheduler.failure_handler("frontdesk", {"audit": diagnostic}, game)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        scheduler.error = error
        raise


async def frontdesk_colony(scheduler, game):
    service = scheduler.service
    if scheduler._pending_frontdesk is not None:
        return
    current = service.game_session()
    if current is None or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch):
        raise ValueError("The active colony changed before frontdesk execution")
    run_scope = await scheduler._check_autoplay_binding(game)
    await service.settle_pending(game.colony_id)
    highwater = await service.call("event_highwater", game.colony_id)
    consumed_events = await service.call("events_by_kind", game.colony_id, ("frontdesk.scheduler_consumed",))
    cursor = max((e["payload"]["through_seq"] for e in consumed_events if e["seq"] <= highwater), default=0)
    wakes = await service.call("events_by_kind", game.colony_id, ("frontdesk.wake",), after_seq=cursor)
    wakes = [e for e in wakes if e["seq"] <= highwater]
    if not wakes:
        return
    # A prepared reply survives a reload and must be acknowledged before
    # any newer wake can advance the consumption cursor.
    for wake in wakes:
        run = await service.call("successful_run", game.colony_id, anchor_seq(wake), through_seq=highwater)
        if run:
            inbox_events = await service.call("events_by_kind", game.colony_id, ("frontdesk.inbox",),
                                              anchor=anchor_seq(wake))
            delivered = [e["payload"]["wake_seq"] for e in inbox_events if e["seq"] <= highwater]
            binding = SimpleNamespace(colony_id=game.colony_id, load_epoch=run["load_epoch"],
                                      game_tick=run["game_tick"])
            binding_events = await service.call("events_by_kind", game.colony_id, ("frontdesk.run_binding",))
            bindings = [e for e in binding_events if e["payload"].get("wake_seq") == wake["seq"] and e["seq"] < run["seq"]]
            await _offer_frontdesk(scheduler, binding, run["game_tick"], wake["seq"],
                                   max([wake["seq"], *delivered]), run["payload"]["result"],
                                   connection=bindings[-1]["payload"]["connection"] if bindings else None)
            return
    stale_events = await service.call("events_by_kind", game.colony_id, ("frontdesk.stale_wake",))
    marked = {e["payload"]["wake_seq"] for e in stale_events if e["seq"] <= highwater}
    for event in wakes:
        if event["load_epoch"] != game.load_epoch and event["seq"] not in marked:
            await service.call("record", game.colony_id, game.load_epoch, current.game_tick,
                               "plugin", "frontdesk.stale_wake", {"wake_seq": event["seq"]})
    pending = [e for e in wakes if e["load_epoch"] == game.load_epoch]
    if not pending:
        return
    initial = pending[0]
    utterance = await service.call("event", game.colony_id, anchor_seq(initial))
    cursor = initial["seq"]
    intent_sources = {utterance["seq"]} if utterance["kind"] == "user.utterance" else set()

    async def inbox():
        nonlocal cursor
        if not scheduler._binding_current(run_scope or game):
            raise ToolExecutionError("world_changed", "The active colony connection changed.", recoverable=False)
        incoming = await service.call("events_by_kind", game.colony_id, ("frontdesk.wake",),
                                      after_seq=cursor, load_epoch=game.load_epoch)
        if not scheduler._binding_current(run_scope or game):
            raise ToolExecutionError("world_changed", "The active colony connection changed.", recoverable=False)
        messages = []
        for event in incoming:
            original = await service.call("event", game.colony_id, anchor_seq(event))
            if not scheduler._binding_current(run_scope or game):
                raise ToolExecutionError("world_changed", "The active colony connection changed.", recoverable=False)
            message = trigger_message(original)
            if original["kind"] == "user.utterance":
                message = {"role": "user", "name": "frontdesk_inbox", "content":
                           "User original words (event " + str(original["seq"]) + "): " + original["payload"]["text"]}
            else:
                message["name"] = "frontdesk_trigger"
            messages.append(message)
            cursor = event["seq"]
            current = service.game_session()
            if current is None or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch):
                raise ToolExecutionError("world_changed", "The active colony or epoch changed.", recoverable=False)
            await service.call("record", game.colony_id, game.load_epoch,
                               current.game_tick, "frontdesk",
                               "frontdesk.inbox", {"wake_seq": cursor, ("utterance_seq" if utterance["kind"] == "user.utterance" else "trigger_seq"): utterance["seq"],
                                                   "message": messages[-1]})
            if original["kind"] == "user.utterance":
                intent_sources.add(original["seq"])
        return messages

    question = (utterance["payload"]["text"] if utterance["kind"] == "user.utterance"
                else trigger_message(utterance)["content"])

    async def wake_planner(text):
        return await scheduler.wake(run_scope or game, source="frontdesk", text=text)

    if run_scope is not None:
        await service.call("record", game.colony_id, game.load_epoch, current.game_tick, "plugin",
                           "frontdesk.run_binding", {"wake_seq": initial["seq"],
                                                      "connection": run_scope.connection_epoch})
    tools_scope = scheduler._bind_game(run_scope or game)
    result = await service.consult(game, question, scheduler.settings,
                                   explicit_update_only=True, inbox=inbox,
                                   utterance_seq=utterance["seq"] if utterance["kind"] == "user.utterance" else None,
                                   trigger_seq=utterance["seq"] if utterance["kind"] == "frontdesk.trigger" else None,
                                   wake_planner=wake_planner, intent_sources=intent_sources,
                                   pending_utterances=[anchor_seq(e) for e in pending],
                                   scheduler_tools=attention_tools(
                                       service, run_scope or game, "frontdesk",
                                       binding_check=lambda: scheduler._business_current(tools_scope),
                                       planner_signal=scheduler._signal,
                                       attention_available=scheduler._native_attention_available,
                                       intent_sources=intent_sources),
                                   scheduler_setup_provider=scheduler._event_provider,
                                   binding_check=lambda: scheduler._business_current(run_scope or game),
                                   **({"budget": scheduler.budget} if scheduler.budget is not None else {}))
    if not scheduler._binding_current(run_scope or game):
        if scheduler.failure_handler is not None:
            await scheduler.failure_handler("frontdesk", result, run_scope or game)
        scheduler._reschedule_current("frontdesk")
        return
    if result.get("status") == "ok" and result.get("workspace", {}).get("history_saved"):
        current = service.game_session()
        tick = current.game_tick if current and (current.colony_id, current.load_epoch) == (
            game.colony_id, game.load_epoch) else game.game_tick
        await _offer_frontdesk(scheduler, game, tick, initial["seq"], cursor, result,
                               connection=run_scope.connection_epoch if run_scope else None)
    else:
        if scheduler.failure_handler is not None:
            await scheduler.failure_handler("frontdesk", result, run_scope or game)
        await scheduler._frontdesk_results.put({"delivery_id": initial["seq"], "result": copy.deepcopy(result),
                                                "failed": True})


async def _offer_frontdesk(scheduler, game, tick, delivery_id, cursor, result, *, connection=None):
    pending = {"delivery_id": delivery_id, "colony_id": game.colony_id,
               "load_epoch": game.load_epoch, "game_tick": tick, "through_seq": cursor}
    scheduler._pending_frontdesk = pending
    await scheduler._frontdesk_results.put({"delivery_id": delivery_id, "through_seq": cursor,
                                            "colony_id": game.colony_id, "load_epoch": game.load_epoch,
                                            "connection_epoch": connection,
                                            "require_connection": scheduler.budget is not None,
                                            "result": copy.deepcopy(result)})
    if result.get("terminal", {}).get("kind") == "update_only":
        await scheduler.ack_frontdesk_result(delivery_id)

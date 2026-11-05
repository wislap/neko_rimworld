"""Native event intake over the negotiated bound port: policy, observation sync and event ingestion."""

import asyncio
import copy
import sqlite3

from ...tool_contracts import ToolExecutionError


async def clear_native_registrations(port, identity):
    """Withdraw installed observations and pause policy while the port is still bound."""
    if port is not None and identity and identity() is not None:
        if "colony_observation.v1" in port.capabilities:
            await port.push_observations({"source": "planner", "observations": []})
        if "colony_pause.v1" in port.capabilities:
            await port.push_policy({"source": "frontdesk", "pause_on_letter_defs": []})


async def sync_observations(scheduler):
    identity = scheduler._event_identity()
    if identity is None:
        return
    connection, colony, epoch = identity
    game = scheduler.service.game_session()
    if game is None or (game.colony_id, game.load_epoch) != (colony, epoch):
        return
    state = await scheduler.service.call("colony_work", colony, epoch, connection)
    tasks = {row["entry_id"] for row in await scheduler.service.call("board", colony)
             if row["kind"] == "task" and row["status"] != "closed"}
    coverage = {row["observation_id"]: row for row in state["observations"]["coverage"]}
    desired = []
    for row in state["observations"]["intents"]:
        if not row["executable"] or row["task_id"] not in tasks:
            continue
        observed = coverage[row["observation_id"]]
        states = observed.get("condition_states", {})
        if all(states.get(condition["id"]) in {"expired", "exhausted"} for condition in row["conditions"]):
            continue
        if (not any(condition["expires_at_tick"] > game.game_tick for condition in row["conditions"])
                and observed.get("receipt_id") is None):
            continue
        # Keep the immutable group until its native terminal event has been
        # ingested; removing it at the tick boundary can lose that evidence.
        desired.append({"observation_id": row["observation_id"], "revision": row["revision"],
                        "conditions": row["conditions"]})
    directory = getattr(scheduler._event_setup, "directory", None) or {}
    limits = directory.get("colony_observation", {}).get("limits", {})
    if (len(desired) > limits.get("observations", 16)
            or sum(len(condition["target_ids"]) for row in desired
                   for condition in row["conditions"]) > limits.get("targets", 64)):
        # No implicit target selection: poll the pending terminal events,
        # then install the full requested set on the next existing cycle.
        return
    signature = (identity, desired)
    if scheduler._installed_observations == signature:
        return
    result = await scheduler._event_port.push_observations({"source": "planner", "observations": desired})
    if scheduler._event_identity() != identity:
        raise ToolExecutionError("world_changed", "Observation registration belongs to an earlier connection",
                                 recoverable=False)
    registrations = result.get("registrations")
    if not isinstance(registrations, list):
        raise ToolExecutionError("observation_ack_invalid", "The native observation response has no registration coverage")
    expected = {(row["observation_id"], row["revision"]) for row in desired}
    actual = {(row.get("observation_id"), row.get("revision")) for row in registrations if isinstance(row, dict)}
    if actual != expected or len(registrations) != len(expected):
        raise ToolExecutionError("observation_ack_invalid", "Native observation ACKs do not match the requested revisions")
    for receipt in registrations:
        seq = await scheduler.service.call("observation_coverage", colony, epoch, game.game_tick, connection, **receipt,
                                           command_binding_check=lambda: scheduler._event_identity() == identity)
        if seq is None:
            scheduler._installed_observations = None
            return
    scheduler._installed_observations = copy.deepcopy(signature)


async def run_events(scheduler):
    """Poll only the negotiated bound port; a change requires a fresh binding."""
    while True:
        poll_identity = None
        try:
            if scheduler._pending_gap is not None:
                colony, epoch, tick, payload, gap_binding = scheduler._pending_gap
                await scheduler.service.call("record", colony, epoch, tick, "plugin", "game.event_gap", payload)
                scheduler._gap_binding = gap_binding
                scheduler._pending_gap = None
            binding = scheduler._binding()
            if (scheduler.pause_state is not None and
                    binding != tuple(scheduler.pause_state.get(key)
                                     for key in ("connection_epoch", "colony_id", "load_epoch"))):
                scheduler.pause_state = None
                scheduler._watchdog_event.set()
            if binding == scheduler._gap_binding:
                await asyncio.sleep(1)
                continue
            if scheduler._event_port is not None and scheduler._event_identity() is None:
                scheduler._event_port = scheduler._event_identity = None
                scheduler._policy_installed = False
                scheduler._attempted_binding = object()
            if (scheduler._event_port is None and binding == scheduler._attempted_binding
                    and asyncio.get_running_loop().time() >= scheduler._retry_at):
                scheduler._attempted_binding = object()
            if scheduler._event_port is None and binding != scheduler._attempted_binding:
                scheduler._attempted_binding = binding
                scheduler._retry_at = asyncio.get_running_loop().time() + 5
                setup = await scheduler._event_provider()
                port, identity = setup.write_port, setup.write_identity
                if port is None or identity is None or identity() is None:
                    scheduler._event_port = scheduler._event_identity = None
                else:
                    scheduler._event_port, scheduler._event_identity = port, identity
                    scheduler._installed_observations = None
                    scheduler._event_setup = setup
                    connection, colony, epoch = identity()
                    scheduler._event_cursor = await scheduler.service.call("game_event_cursor", colony, epoch, connection)
                    if await scheduler.service.call("game_event_gap", colony, epoch, connection):
                        scheduler._gap_binding = binding
                        continue
            if scheduler._event_port is not None and "colony_pause.v1" in scheduler._event_port.capabilities:
                _, colony, _ = scheduler._event_identity()
                policy = await scheduler.service.call("wake_policy", colony)
                desired = policy["pause_on_letter_defs"]
                if not scheduler._policy_installed or desired != scheduler._installed_policy:
                    await scheduler._event_port.push_policy({"source": "frontdesk", "pause_on_letter_defs": desired})
                    scheduler._installed_policy = copy.deepcopy(desired)
                    scheduler._policy_installed = True
            if scheduler._event_port is not None and "colony_observation.v1" in scheduler._event_port.capabilities:
                await sync_observations(scheduler)
            if scheduler._event_port is not None and "colony_events.v1" in scheduler._event_port.capabilities:
                connection, colony, epoch = scheduler._event_identity()
                poll_identity = (connection, colony, epoch)
                game = scheduler.service.game_session()
                if game is None or (game.colony_id, game.load_epoch) != (colony, epoch):
                    raise ValueError("Active game changed during event polling")
                for event in await scheduler._event_port.events(since=scheduler._event_cursor):
                    if scheduler._event_identity() != (connection, colony, epoch):
                        raise ValueError("Game binding changed during event polling")
                    if (event.get("connection_epoch"), event.get("colony_id"), event.get("load_epoch")) != (connection, colony, epoch):
                        raise ValueError("Native event identity does not match its bound port")
                    observed = scheduler.service.game_session()
                    if observed is None or (observed.colony_id, observed.load_epoch) != (colony, epoch):
                        raise ValueError("Active game changed during event polling")
                    changed = await scheduler.service.call(
                        "ingest_game_event", colony, epoch, event["game_tick"], connection, dict(event),
                        attention_available=scheduler._native_attention_available(),
                        observed_tick=observed.game_tick,
                        command_binding_check=lambda: scheduler._event_identity() == poll_identity,
                        diagnostic_binding_identity=lambda: scheduler._event_identity())
                    scheduler._event_cursor = event["cursor"]
                    if changed:
                        if event.get("kind") in {"session_snapshot", "pause_changed"}:
                            scheduler.observe_pause_state(event.get("data", {}).get("pause_state"),
                                                          (connection, colony, epoch), evidence_seq=changed.event_seq)
                        scheduler._games[colony] = scheduler._bind_game(game)
                        if changed.frontdesk_wake:
                            scheduler._frontdesk_signal.set()
                        if changed.planner_wake:
                            scheduler._signal.set()
        except asyncio.CancelledError:
            raise
        except (OSError, sqlite3.Error) as error:
            scheduler.error = error
            if scheduler.failure_handler is not None:
                await scheduler.failure_handler("scheduler", {"audit": {"error_code": "workspace_unavailable"}},
                                                scheduler.service.game_session())
        except ToolExecutionError as error:
            scheduler.error = error
            if error.code == "event_gap" and poll_identity is not None:
                connection, colony, epoch = poll_identity
                scheduler._pending_gap = (colony, epoch, game.game_tick,
                                          {"connection": connection, "after_cursor": scheduler._event_cursor,
                                           "reason": str(error), "details": copy.deepcopy(error.details)}, binding)
                try:
                    await scheduler.service.call("record", colony, epoch, game.game_tick, "plugin",
                                                 "game.event_gap", scheduler._pending_gap[3])
                except (OSError, sqlite3.Error) as record_error:
                    scheduler.error = record_error
                else:
                    scheduler._gap_binding = binding
                    scheduler._pending_gap = None
                if scheduler.failure_handler is not None:
                    await scheduler.failure_handler("scheduler", {"audit": {"error_code": "event_gap"}}, game)
        except Exception as error:
            scheduler.error = error
            if scheduler._event_port is None or scheduler._event_identity() is None:
                scheduler._event_port = scheduler._event_identity = None
                scheduler._policy_installed = False
                scheduler._retry_at = asyncio.get_running_loop().time() + 5
        await asyncio.sleep(1)

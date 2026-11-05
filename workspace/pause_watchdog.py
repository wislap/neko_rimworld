"""Planner wakes and one-time notification for a pause that outlives its owner's episode.

Writers run inside the transaction the journal opened.
"""

from .ledger import PLUGIN


def _matches(ledger, colony_id, load_epoch, episode):
    records = ledger.events_by_kind(colony_id, ("game.event", "autoplay.startup_pause"), load_epoch=load_epoch)
    for event in reversed(records):
        payload = event["payload"]
        if event["kind"] == "game.event":
            native = payload.get("event", {})
            if native.get("kind") not in {"session_snapshot", "pause_changed"}:
                continue
            state = native.get("data", {}).get("pause_state")
            connection = native.get("connection_epoch")
        else:
            if payload.get("stage") != "result":
                continue
            state = payload.get("pause_state")
            connection = state.get("connection_epoch") if isinstance(state, dict) else None
        if connection != episode[0]:
            return False
        if not isinstance(state, dict) or state.get("status") != "observed":
            return False
        return ((state.get("connection_epoch"), state.get("colony_id"), state.get("load_epoch"),
                 state.get("pause_revision"), state.get("pause_transition_revision")) == tuple(episode)
                and state.get("paused") is True and state.get("speed_paused") is True
                and state.get("force_paused") is False and state.get("windows_force_paused") is False
                and state.get("owner") in {"planner", "frontdesk", "reflex", "automatic_letter"})
    return False


def pause_watchdog_history(ledger, colony_id, load_epoch):
    return [event for event in ledger.cadence_events(colony_id)
            if event["load_epoch"] == load_epoch and event["kind"] in
            {"pause.watchdog", "planner.started", "planner.finished"}]


def pause_watchdog_wake(ledger, colony_id, load_epoch, game_tick, episode, count):
    game_tick = ledger.recording_tick(colony_id, load_epoch, game_tick)
    if not _matches(ledger, colony_id, load_epoch, episode):
        return None
    wake = ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "planner.wake",
                         {"source": "plugin", "text": "Pause remains; review whether it should continue.",
                          "pause_watchdog": True})
    ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "pause.watchdog",
                  {"episode": list(episode), "action": "wake", "count": count, "wake_seq": wake})
    return wake


def pause_watchdog_notify(ledger, colony_id, load_epoch, game_tick, episode):
    game_tick = ledger.recording_tick(colony_id, load_epoch, game_tick)
    if not _matches(ledger, colony_id, load_epoch, episode):
        return False
    previous = ledger.events_by_kind(colony_id, ("pause.watchdog",), load_epoch=load_epoch)
    if any(event["payload"].get("episode") == list(episode) and event["payload"].get("action") in {"notify_attempt", "notify"}
           for event in previous):
        return False
    ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "pause.watchdog",
                  {"episode": list(episode), "action": "notify_attempt"})
    return True

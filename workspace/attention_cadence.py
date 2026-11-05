"""Deterministic cadence work and attention leases recorded in the event log.

Writers run inside the transaction the journal opened.
"""

from .attention_policy import load_policy
from .board import current_board
from .cadence import project as cadence_project
from .cadence import validate_declaration
from .ledger import PLUGIN, USER, UTTERANCE_EVENT


def append_cadence(ledger, colony_id, load_epoch, game_tick):
    """Append deterministic cadence work while the caller owns the transaction."""
    game_tick = ledger.recording_tick(colony_id, load_epoch, game_tick)
    events = ledger.cadence_events(colony_id)
    current_epoch = ledger.current_epoch(colony_id)
    if current_epoch is not None and current_epoch != load_epoch:
        return {"active":False, "items":[]}
    policy, _ = load_policy()
    state = cadence_project(events, epoch=load_epoch, now_tick=game_tick, policy=policy)
    for item in state["items"]:
        ledger.append(colony_id, load_epoch, game_tick, PLUGIN, item["kind"], item["payload"])
        if item["kind"] == "cadence.review_due":
            payload = item["payload"]
            ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "planner.wake",
                          {"source": "game", "text": "Cadence review due", "work_id": payload["id"],
                           "urgency": payload.get("urgency", "scheduled"),
                           "signal_seqs": payload.get("signal_seqs", []),
                           "signals": [ledger.event(colony_id, seq) for seq in payload.get("signal_seqs", [])],
                           "review_type": payload["review_type"]})
    return state


def attention_display(ledger, colony_id, load_epoch, connection):
    """Read-only cadence projection at the latest recorded tick; never append work."""
    events = ledger.cadence_events(colony_id)
    current = [event for event in events if event["load_epoch"] == load_epoch]
    if not current:
        return None
    sampled_tick = max(event["game_tick"] for event in current)
    policy, _ = load_policy()
    state = cadence_project(events, epoch=load_epoch, now_tick=sampled_tick, policy=policy)
    if not state.get("active"):
        return None
    by_seq = {event["seq"]: event for event in current}
    reasons = [{**reason, "signal": by_seq.get(reason["event_seq"], {}).get("payload")}
               for reason in state["reasons"]]
    leases = [{"event_seq": seq, **lease} for seq, lease in state["leases"].items()]
    snapshots = [event for event in current if event["kind"] == "attention.signal"
                 and event["payload"].get("edge") == "snapshot"]
    latest_snapshot = snapshots[-1] if snapshots else None
    source = ledger.event(colony_id, latest_snapshot["payload"].get("native_event_seq")) if latest_snapshot else None
    same_scope = bool(source and source["kind"] == "game.event"
                      and source["load_epoch"] == load_epoch and source["payload"].get("connection") == connection)
    partitions = {name: dict(part) for name, part in latest_snapshot["payload"].get("partitions", {}).items()} if same_scope else {}
    if same_scope:
        map_id = latest_snapshot["payload"].get("map_id")
        changes = ledger.events_by_kind(colony_id, ("game.event",), after_seq=source["seq"], load_epoch=load_epoch)
        for event in changes:
            native = event["payload"].get("event", {})
            data = native.get("data", {})
            if (event["payload"].get("connection") == connection and native.get("kind") == "attention_changed"
                    and data.get("edge") == "status_changed" and data.get("map_id") == map_id
                    and data.get("category") in partitions):
                part = data.get("current")
                partitions[data["category"]] = dict(part) if isinstance(part, dict) else {}
                sampled_tick = max(sampled_tick, event["game_tick"])
    baseline_complete = bool(same_scope and len(partitions) == 3 and all(
        part.get("status") == "ok" and not part.get("truncated") for part in partitions.values()))
    any_observed = same_scope and any(part.get("status") in {"ok", "partial"} for part in partitions.values())
    source_status = "observed" if baseline_complete else "partial" if any_observed else "unavailable"
    return {"level": state["level"], "source_status": source_status,
            "reasons": reasons, "next_daily_tick": state["next_daily_tick"],
            "next_escalated_tick": state["next_escalated_tick"], "leases": leases,
            "sampled_tick": sampled_tick, "load_epoch": load_epoch}


def attention_declare(ledger, colony_id, load_epoch, game_tick, actor, level, reference, reason, user_sources=()):
    game_tick = ledger.recording_tick(colony_id, load_epoch, game_tick)
    tasks = {row["entry_id"] for row in current_board(ledger.revisions(colony_id))
             if row["kind"] == "task" and row["author"] == "planner" and row["status"] != "closed"}
    sources = {seq for seq in user_sources if (e := ledger.event(colony_id, seq))
               and e["kind"] == UTTERANCE_EVENT and e["load_epoch"] == load_epoch}
    policy, _ = load_policy()
    payload = validate_declaration(ledger.cadence_events(colony_id), epoch=load_epoch, tick=game_tick,
                                   actor=actor, level=level, reference=reference, reason=reason,
                                   policy=policy, open_tasks=tasks, user_utterances=sources)
    seq = ledger.append(colony_id, load_epoch, game_tick, actor, "cadence.lease_declared", payload)
    append_cadence(ledger, colony_id, load_epoch, game_tick)
    return seq


def attention_revoke(ledger, colony_id, load_epoch, game_tick, reference, user_sources):
    game_tick = ledger.recording_tick(colony_id, load_epoch, game_tick)
    if reference not in user_sources:
        raise ValueError("Revoke requires an utterance received in this run")
    original = ledger.event(colony_id, reference)
    if not original or original["kind"] != UTTERANCE_EVENT or original["load_epoch"] != load_epoch:
        raise ValueError("Revoke requires the original user utterance")
    state = append_cadence(ledger, colony_id, load_epoch, game_tick)
    revoked = []
    for lease in state.get("leases", {}).values():
        if lease["actor"] == "user":
            ledger.append(colony_id, load_epoch, game_tick, USER, "attention.revoked",
                          {"reference": lease["reference"], "utterance_seq": reference})
            revoked.append(lease["reference"])
    append_cadence(ledger, colony_id, load_epoch, game_tick)
    return revoked

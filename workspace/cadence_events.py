"""The canonical cadence input view, shared by runtime and diagnostics.

Raw observations and progress may retain an older sample tick. They are not
cadence inputs. This codec never changes an envelope's scope, sequence or tick.
"""

FORMAT = "cadence_events.v1"
INPUT_KINDS = (
    "autoplay.activation", "autoplay.disabled", "colony.load", "attention.signal",
    "cadence.lease_declared", "cadence.lease_expired", "cadence.changed",
    "cadence.review_due", "task.closed", "attention.revoked", "planner.started",
    "planner.finished", "planner.received", "pause.watchdog",
)
BUSINESS_KINDS = frozenset({
    "autoplay.activation", "autoplay.disabled", "planner.started", "planner.received",
    "pause.watchdog", "task.closed", "cadence.lease_declared", "attention.revoked",
})


def event_view(events):
    result = []
    for event in events:
        kind = event["kind"]
        if kind not in INPUT_KINDS:
            continue
        payload = event["payload"]
        if kind == "planner.finished":
            completed = payload.get("completed")
            if "completed" not in payload:
                completed = ((payload.get("result") or {}).get("terminal") or {}).get("kind") == "yield"
            payload = {"start_seq":payload.get("start_seq"), "completed":completed}
        elif kind == "planner.started":
            payload = {key:payload.get(key) or [] for key in ("wake_seqs", "work_ids", "signal_seqs")}
        result.append({**event, "payload":payload})
    return result

"""Board revisions saved with their event after shape, author and reference checks.

Writers run inside the transaction the journal opened.
"""

from . import observations, work_state
from .attention_cadence import append_cadence
from .board import TASK_TARGET_KINDS, BoardError, check_revision, check_shape
from .ledger import REVISION_EVENT, UTTERANCE_EVENT


def submit(ledger, colony_id, load_epoch, game_tick, actor, submission):
    """Write one board revision and its event, or raise and write nothing."""
    check_shape(submission)
    game_tick = ledger.recording_tick(colony_id, load_epoch, game_tick)
    current = ledger.revision(submission["entry_id"])
    if current is not None and current["colony_id"] != colony_id:
        raise BoardError("Entry ids are unique across colonies; this id belongs to another colony")
    row = check_revision(current, actor, submission)
    _check_references(ledger, colony_id, row)
    if row["kind"] == "task" and row["status"] == "verified":
        plans = [plan for plan in work_state.current(ledger, colony_id, load_epoch)["construction"]["plans"]
                 if plan["task_id"] == row["entry_id"]]
        if plans and not all(plan["construction_complete"] for plan in plans):
            raise BoardError("Construction is verified only from current native completion of every effective adopted element")
    seq = ledger.append(
        colony_id,
        load_epoch,
        game_tick,
        actor,
        REVISION_EVENT,
        {"entry_id": row["entry_id"], "revision": row["revision"]},
    )
    ledger.add_revision(colony_id, seq, row)
    if row["kind"] == "task" and row["status"] == "closed":
        ledger.append(colony_id, load_epoch, game_tick, actor, "task.closed", {"reference": row["entry_id"]})
        observation_events = ledger.events_by_kind(colony_id, observations.KINDS)
        requests = [e for e in observation_events if e["kind"] == "observation.requested"]
        if requests:
            binding = requests[-1]["payload"]["binding"]
            for intent in observations.project(observation_events, binding=binding)["intents"]:
                if intent["active"] and intent["task_id"] == row["entry_id"]:
                    revoke = observations.prepare_revoke(observation_events, observation_id=intent["observation_id"],
                        expected_revision=intent["revision"], reason="planner_task_closed")
                    ledger.append(colony_id, load_epoch, game_tick, revoke["source"], revoke["kind"], revoke["payload"])
        append_cadence(ledger, colony_id, load_epoch, game_tick)
    return seq


def _check_references(ledger, colony_id, row):
    if row["kind"] == "intent" and not ledger.has_event(colony_id, UTTERANCE_EVENT, row["source"]["event_seq"]):
        raise BoardError("An intent cites a user utterance in this colony")
    if row["kind"] == "task":
        for entry_id in (row["parent"], row["source"]["entry_id"]):
            entry = ledger.revision(entry_id)
            if entry is None or entry["colony_id"] != colony_id or entry["kind"] not in TASK_TARGET_KINDS:
                raise BoardError("A task hangs under, and implements, a strategy or intent on this board")

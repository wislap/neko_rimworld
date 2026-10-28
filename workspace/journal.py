"""Event and board writes, plus views rebuilt from the log; the only path into the two tables."""

from .board import TASK_TARGET_KINDS, BoardError, check_revision, check_shape
from .ledger import (
    FRONTDESK,
    LOAD_EVENT,
    PLUGIN,
    RESERVED_EVENTS,
    RETURN_EVENT,
    REVISION_EVENT,
    TAKEOVER_EVENT,
    USER,
    UTTERANCE_EVENT,
    EventLedger,
)

TAKEOVER_FIELDS = frozenset({"object", "field", "value_read", "ai_value", "changeset_id"})


class Journal:
    def __init__(self, path):
        self._ledger = EventLedger(path)

    def close(self):
        self._ledger.close()

    def events(self, colony_id):
        return self._ledger.events(colony_id)

    def record(self, colony_id, load_epoch, game_tick, source, kind, payload):
        """Append an event kind that no typed writer owns; board, load, takeover and return have their own."""
        if kind in RESERVED_EVENTS:
            raise ValueError(f"{kind} events are written by their own operation")
        return self._ledger.append(colony_id, load_epoch, game_tick, source, kind, payload)

    def record_utterance(self, colony_id, load_epoch, game_tick, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A user utterance requires text")
        return self._ledger.append(colony_id, load_epoch, game_tick, USER, UTTERANCE_EVENT, {"text": text})

    def record_load(self, colony_id, load_epoch, game_tick):
        return self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, LOAD_EVENT, {})

    def record_takeover(self, colony_id, load_epoch, game_tick, takeover):
        if not isinstance(takeover, dict) or set(takeover) != TAKEOVER_FIELDS:
            raise ValueError("A takeover records object, field, value_read, ai_value and changeset_id")
        return self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, TAKEOVER_EVENT, takeover)

    def record_return(self, colony_id, load_epoch, game_tick, *, takeover_seq, utterance_seq):
        """Release a takeover, citing the user's own words; a takeover can be released once."""
        with self._ledger.transaction():
            if not self._ledger.has_event(colony_id, TAKEOVER_EVENT, takeover_seq):
                raise BoardError("A return releases an open takeover of this colony")
            if self._ledger.has_return(colony_id, takeover_seq):
                raise BoardError("This takeover is already released")
            if not self._ledger.has_event(colony_id, UTTERANCE_EVENT, utterance_seq):
                raise BoardError("A return cites a user utterance of this colony")
            payload = {"takeover_seq": takeover_seq, "utterance_seq": utterance_seq}
            return self._ledger.append(colony_id, load_epoch, game_tick, FRONTDESK, RETURN_EVENT, payload)

    def submit(self, colony_id, load_epoch, game_tick, actor, submission):
        """Write one board revision and its event atomically, or raise and write nothing."""
        check_shape(submission)
        with self._ledger.transaction():
            current = self._ledger.revision(submission["entry_id"])
            if current is not None and current["colony_id"] != colony_id:
                raise BoardError("Entry ids are unique across colonies; this id belongs to another colony")
            row = check_revision(current, actor, submission)
            self._check_references(colony_id, row)
            seq = self._ledger.append(
                colony_id,
                load_epoch,
                game_tick,
                actor,
                REVISION_EVENT,
                {"entry_id": row["entry_id"], "revision": row["revision"]},
            )
            self._ledger.add_revision(colony_id, seq, row)
            return seq

    def _check_references(self, colony_id, row):
        if row["kind"] == "intent" and not self._ledger.has_event(colony_id, UTTERANCE_EVENT, row["source"]["event_seq"]):
            raise BoardError("An intent cites a user utterance in this colony")
        if row["kind"] == "task":
            for entry_id in (row["parent"], row["source"]["entry_id"]):
                entry = self._ledger.revision(entry_id)
                if entry is None or entry["colony_id"] != colony_id or entry["kind"] not in TASK_TARGET_KINDS:
                    raise BoardError("A task hangs under, and implements, a strategy or intent on this board")

    def board(self, colony_id):
        return current_board(self._ledger.revisions(colony_id))

    def takeovers(self, colony_id):
        return open_takeovers(self._ledger.events(colony_id))

    def review_marks(self, colony_id):
        return review_marks(self._ledger.revisions(colony_id), self._ledger.events(colony_id))


def current_board(revisions):
    """Latest revision of every entry, from board revision rows alone."""
    latest = {}
    for revision in revisions:
        if revision["revision"] > latest.get(revision["entry_id"], {"revision": 0})["revision"]:
            latest[revision["entry_id"]] = revision
    return sorted(latest.values(), key=lambda entry: entry["entry_id"])


def open_takeovers(events):
    """Takeover events not yet released by a return event, from the event log alone."""
    takeovers = {}
    for event in events:
        if event["kind"] == TAKEOVER_EVENT:
            takeovers[event["seq"]] = event
        elif event["kind"] == RETURN_EVENT:
            takeovers.pop(event["payload"]["takeover_seq"], None)
    return list(takeovers.values())


def review_marks(revisions, events):
    """Entries needing review: a load after their latest revision, or a takeover touching their changesets."""
    last_load = 0
    takeovers = []
    for event in events:
        if event["kind"] == LOAD_EVENT:
            last_load = max(last_load, event["seq"])
        elif event["kind"] == TAKEOVER_EVENT:
            takeovers.append(event)
    marks = {}
    for entry in current_board(revisions):
        reasons = []
        if last_load > entry["event_seq"]:
            reasons.append("load")
        if any(t["seq"] > entry["event_seq"] and t["payload"]["changeset_id"] in entry["changesets"] for t in takeovers):
            reasons.append("takeover")
        if reasons:
            marks[entry["entry_id"]] = reasons
    return marks

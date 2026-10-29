"""Event and board writes, plus views rebuilt from the log; the only path into the two tables."""

from .board import TASK_TARGET_KINDS, BoardError, check_revision, check_shape
from .ledger import (
    FRONTDESK,
    LOAD_EVENT,
    PLUGIN,
    RESERVED_EVENTS,
    RETURN_EVENT,
    REVISION_EVENT,
    RUN_EVENT,
    TAKEOVER_EVENT,
    USER,
    UTTERANCE_EVENT,
    EventLedger,
)

TAKEOVER_FIELDS = frozenset({"object", "field", "value_read", "ai_value", "changeset_id"})
RUN_ROLES = frozenset({"assistant", "tool"})
CONTEXT_POLICY_EVENT = "frontdesk.context_policy"
CONTEXT_POLICY_DEFAULTS = {"window_ticks": 180_000, "narrative_threshold": 60_000}


class Journal:
    def __init__(self, path):
        self._ledger = EventLedger(path)

    def close(self):
        self._ledger.close()

    def colonies(self):
        return self._ledger.colonies()

    def events(self, colony_id):
        return self._ledger.events(colony_id)

    def observe_epoch(self, colony_id, load_epoch, game_tick):
        """Record a load once when the observed epoch changes; never rewrite history."""
        previous = self._ledger.latest_epoch(colony_id)
        if previous is not None and previous != load_epoch:
            return self.record_load(colony_id, load_epoch, game_tick)
        return None

    def context_policy(self, colony_id):
        policy = dict(CONTEXT_POLICY_DEFAULTS)
        for event in self._ledger.events_by_kind(colony_id, (CONTEXT_POLICY_EVENT,)):
            policy.update(event["payload"])
        return policy

    def set_context_policy(self, colony_id, load_epoch, game_tick, changes):
        if not isinstance(changes, dict) or not changes or any(
            key not in CONTEXT_POLICY_DEFAULTS or type(value) is not int or not 1 <= value < 2**63
            for key, value in changes.items()
        ):
            raise ValueError("Context policy requires positive integer window_ticks or narrative_threshold")
        return self._ledger.append(colony_id, load_epoch, game_tick, USER, CONTEXT_POLICY_EVENT, changes)

    def retained_response(self, colony_id, load_epoch, question, request_id):
        return self._ledger.retained_response(colony_id, load_epoch, question, request_id)

    def progress(self, colony_id, utterance_seq):
        return self._ledger.progress(colony_id, utterance_seq)

    def progress_metadata(self, colony_id, utterance_seq):
        return self._ledger.progress_metadata(colony_id, utterance_seq)

    def evidence(self, colony_id, observation_id):
        return self._ledger.evidence(colony_id, observation_id)

    def interrupted_events(self, colony_id):
        return self._ledger.interrupted_events(colony_id)

    def context_events(self, colony_id, now_tick, board, window_ticks=180_000, after_run_seq=0):
        pinned = [row["source"]["event_seq"] for row in board if row["kind"] == "intent" and row["status"] != "closed"]
        return self._ledger.context_events(colony_id, now_tick - window_ticks, pinned, after_run_seq)

    def event(self, colony_id, seq):
        return self._ledger.event(colony_id, seq)

    def events_by_kind(self, colony_id, kinds):
        return self._ledger.events_by_kind(colony_id, kinds)

    def latest_event(self, colony_id, kind):
        return self._ledger.latest_event(colony_id, kind)

    def record(self, colony_id, load_epoch, game_tick, source, kind, payload):
        """Append an event kind that no typed writer owns; board, load, takeover and return have their own."""
        if kind in RESERVED_EVENTS:
            raise ValueError(f"{kind} events are written by their own operation")
        return self._ledger.append(colony_id, load_epoch, game_tick, source, kind, payload)

    def record_utterance(self, colony_id, load_epoch, game_tick, text, *, request_id=None):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A user utterance requires text")
        return self._ledger.append(colony_id, load_epoch, game_tick, USER, UTTERANCE_EVENT, {"text": text, "request_id": request_id})

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

    def record_run(self, colony_id, load_epoch, game_tick, role, utterance_seq, messages, *, result=None, audit=None):
        """Append the assistant and tool messages one frontdesk run added, answering a user utterance."""
        if role != FRONTDESK:
            raise ValueError("Only the frontdesk session records runs; the planner has no session")
        if not self._ledger.has_event(colony_id, UTTERANCE_EVENT, utterance_seq):
            raise ValueError("A run answers a user utterance of this colony")
        _check_run_messages(messages)
        payload = {"utterance_seq": utterance_seq, "messages": messages, "result": result, "audit": audit}
        return self._ledger.append(colony_id, load_epoch, game_tick, FRONTDESK, RUN_EVENT, payload)

    def finish_planner(self, colony_id, load_epoch, game_tick, start_seq, cursor, result, audit):
        """Atomically retain the outcome, optional handoff and consumed wake position."""
        with self._ledger.transaction():
            terminal = result.get("terminal")
            if terminal and terminal.get("kind") == "yield":
                self._ledger.append(colony_id, load_epoch, game_tick, "planner", "planner.yield",
                                    {"start_seq": start_seq, "handoff": terminal["handoff"]})
            seq = self._ledger.append(colony_id, load_epoch, game_tick, "planner", "planner.finished",
                                      {"start_seq": start_seq, "result": result, "audit": audit})
            self._ledger.append(colony_id, load_epoch, game_tick, "planner", "planner.consumed",
                                {"through_seq": cursor})
            return seq

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
        return open_takeovers(self._ledger.events_by_kind(colony_id, (TAKEOVER_EVENT, RETURN_EVENT)))

    def review_marks(self, colony_id):
        load = self._ledger.latest_event(colony_id, LOAD_EVENT)
        events = self._ledger.events_by_kind(colony_id, (TAKEOVER_EVENT,))
        if load:
            events.append(load)
        return review_marks(self._ledger.revisions(colony_id), events)


def current_board(revisions):
    """Latest revision of every entry, from board revision rows alone."""
    latest = {}
    for revision in revisions:
        if revision["revision"] > latest.get(revision["entry_id"], {"revision": 0})["revision"]:
            latest[revision["entry_id"]] = revision
    return sorted(latest.values(), key=lambda entry: entry["entry_id"])


def _check_run_messages(messages):
    """Only assistant and tool messages; each tool call is answered by its own tool message right after."""
    if not isinstance(messages, list):
        raise ValueError("A run records its messages as a list")
    pending = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in RUN_ROLES:
            raise ValueError("A run records only assistant and tool messages")
        if message["role"] == "assistant":
            if pending:
                raise ValueError("Each tool call is answered before the next message")
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("An assistant message content is text or null")
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list) or not all(_is_tool_call(call) for call in calls):
                raise ValueError("A tool call names an id and a function name")
            ids = [call["id"] for call in calls]
            if len(set(ids)) != len(ids):
                raise ValueError("Tool call ids are unique within a message")
            pending = set(ids)
        else:
            answer = message.get("tool_call_id")
            if not isinstance(answer, str) or answer not in pending or not isinstance(message.get("content"), str):
                raise ValueError("A tool message answers a tool call of the assistant message before it")
            pending.remove(answer)
    if pending:
        raise ValueError("Each tool call is answered by a tool message")


def _is_tool_call(call):
    function = call.get("function") if isinstance(call, dict) else None
    return (
        isinstance(call, dict)
        and isinstance(call.get("id"), str)
        and bool(call["id"])
        and isinstance(function, dict)
        and isinstance(function.get("name"), str)
        and bool(function["name"])
    )


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

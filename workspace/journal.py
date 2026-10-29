"""Event and board writes, plus views rebuilt from the log; the only path into the two tables."""

from contextlib import nullcontext

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
from .wake_conditions import COMPARISONS, numeric, path_value, validate_condition

TAKEOVER_FIELDS = frozenset({"object", "field", "value_read", "ai_value", "changeset_id"})
RUN_ROLES = frozenset({"assistant", "tool"})
CONTEXT_POLICY_EVENT = "frontdesk.context_policy"
CONTEXT_POLICY_DEFAULTS = {"window_ticks": 180_000, "narrative_threshold": 60_000}
WAKE_POLICY_DEFAULTS = {
    "pause_on_letter_defs": ["ThreatBig", "ThreatSmall", "NegativeEvent"],
    "frontdesk_letter_defs": ["ThreatBig", "ThreatSmall", "NegativeEvent"],
    "frontdesk_choice_letters": True,
    "frontdesk_paused_windows": True,
    "planner_review_ticks": 15_000,
}


def _event_filter(value, expected):
    if isinstance(expected, dict) and set(expected) == {"op", "value"}:
        op, threshold = expected["op"], expected["value"]
        return (op in COMPARISONS and numeric(value) and numeric(threshold)
                and COMPARISONS[op](value, threshold))
    if type(value) is bool or type(expected) is bool:
        return type(value) is type(expected) and value == expected
    if isinstance(value, dict) and isinstance(expected, dict):
        return value.keys() == expected.keys() and all(_event_filter(value[k], expected[k]) for k in value)
    if isinstance(value, list) and isinstance(expected, list):
        return len(value) == len(expected) and all(_event_filter(a, b) for a, b in zip(value, expected))
    return value == expected


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

    def wake_policy(self, colony_id):
        policy = {key: value.copy() if isinstance(value, list) else value
                  for key, value in WAKE_POLICY_DEFAULTS.items()}
        for event in self._ledger.events_by_kind(colony_id, ("scheduler.policy",)):
            policy.update(event["payload"])
        return policy

    def wake_policy_authors(self, colony_id):
        authors = {key: {"source": PLUGIN, "event_seq": None} for key in WAKE_POLICY_DEFAULTS}
        for event in self._ledger.events_by_kind(colony_id, ("scheduler.policy",)):
            for key in event["payload"]:
                authors[key] = {"source": event["source"], "event_seq": event["seq"]}
        return authors

    def set_wake_policy(self, colony_id, load_epoch, game_tick, actor, changes):
        if actor not in {"frontdesk", "planner"} or not isinstance(changes, dict) or not changes:
            raise ValueError("Invalid policy author or changes")
        allowed = ({"pause_on_letter_defs", "frontdesk_letter_defs", "frontdesk_choice_letters", "frontdesk_paused_windows"}
                   if actor == "frontdesk" else {"planner_review_ticks"})
        if not changes.keys() <= allowed:
            raise ValueError("Policy field is not writable by this role")
        for key, value in changes.items():
            if key.endswith("_defs"):
                if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
                    raise ValueError("Letter definitions must be nonempty strings")
            elif key in {"frontdesk_choice_letters", "frontdesk_paused_windows"}:
                if type(value) is not bool:
                    raise ValueError("Choice letter policy must be boolean")
            elif type(value) is not int or not 1 <= value < 2**63:
                raise ValueError("Review interval must be a positive integer")
        with self._ledger.transaction():
            seq = self._ledger.append(colony_id, load_epoch, game_tick, actor, "scheduler.policy", changes)
            if "planner_review_ticks" in changes:
                defaults = [e for e in self._ledger.events_by_kind(colony_id, ("scheduler.default_review",))
                            if e["load_epoch"] == load_epoch]
                if defaults:
                    old_id = defaults[-1]["payload"]["id"]
                    active = {item["id"]: item for item in self.wake_conditions(colony_id, load_epoch)}
                    if old_id in active:
                        self.cancel_wake(colony_id, load_epoch, game_tick, "planner", old_id)
                        new = self.register_wake(colony_id, load_epoch, game_tick, "planner",
                                                 {"kind": "periodic", "interval_ticks": changes["planner_review_ticks"]},
                                                 "Periodic colony review")
                        self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN,
                                            "scheduler.default_review", {"id": new["id"]})
            return seq

    def wake_conditions(self, colony_id, load_epoch):
        active = {}
        for event in self._ledger.events_by_kind(colony_id, ("wake.registered", "wake.cancelled", "wake.evaluated")):
            if event["load_epoch"] != load_epoch:
                continue
            payload = event["payload"]
            if event["kind"] == "wake.registered":
                active[event["seq"]] = {**payload, "id": event["seq"], "next_tick": payload["next_tick"]}
            elif event["kind"] == "wake.cancelled":
                active.pop(payload["id"], None)
            elif payload["id"] in active:
                if payload.get("consume"):
                    active.pop(payload["id"])
                else:
                    active[payload["id"]]["next_tick"] = payload["next_tick"]
        return list(active.values())

    def register_wake(self, colony_id, load_epoch, game_tick, actor, condition, text):
        if actor not in {"frontdesk", "planner"} or not isinstance(text, str) or not text.strip():
            raise ValueError("Wake requires a valid role and reason")
        condition = validate_condition(condition)
        kind = condition["kind"]
        next_tick = (condition["tick"] if kind == "deadline" else
                     game_tick + condition.get("interval_ticks", 0))
        if next_tick >= 2**63:
            raise ValueError("Wake tick exceeds the supported range")
        payload = {"role": actor, "condition": condition, "text": text, "next_tick": next_tick}
        seq = self._ledger.append(colony_id, load_epoch, game_tick, actor, "wake.registered", payload)
        return {"id": seq, **payload}

    def cancel_wake(self, colony_id, load_epoch, game_tick, actor, wake_id):
        active = {item["id"]: item for item in self.wake_conditions(colony_id, load_epoch)}
        if wake_id not in active or active[wake_id]["role"] != actor:
            raise ValueError("Wake is absent or owned by another role")
        return self._ledger.append(colony_id, load_epoch, game_tick, actor, "wake.cancelled", {"id": wake_id})

    def evaluate_wake(self, colony_id, load_epoch, game_tick, wake_id, observation, *, expected_next_tick):
        """Atomically record the sample, its advancement and any resulting inbox wake."""
        with nullcontext() if self._ledger.db.in_transaction else self._ledger.transaction():
            active = {item["id"]: item for item in self.wake_conditions(colony_id, load_epoch)}
            item = active.get(wake_id)
            if item is None or item["next_tick"] != expected_next_tick:
                return False
            condition = item["condition"]
            kind = condition["kind"]
            if kind != "event" and game_tick < item["next_tick"]:
                return False
            value, matched, error = None, False, None
            if kind in {"deadline", "periodic"}:
                matched = True
            elif kind == "event":
                data = observation.get("data", {})
                matched = (observation.get("kind") == condition["event_kind"] and
                           isinstance(data, dict) and all(k in data and _event_filter(data[k], v)
                               for k, v in condition["filters"].items()))
            else:
                try:
                    if "error" in observation or observation.get("response", {}).get("payload", {}).get("status") != "ok":
                        raise ValueError("Read did not return a successful observation")
                    value = path_value(observation, condition["path"])
                    if not numeric(value):
                        raise ValueError("Observation is not a finite number")
                    matched = COMPARISONS[condition["op"]](value, condition["value"])
                except (ValueError, TypeError) as exc:
                    error = str(exc)
            consume = matched and kind in {"deadline", "event", "numeric"}
            busy = kind == "numeric" and observation.get("error", {}).get("code") == "bridge_busy"
            next_tick = (game_tick + condition["interval_ticks"] if kind in {"periodic", "numeric"} and not busy
                         else item["next_tick"])
            payload = {"id": wake_id, "condition": condition, "observation": observation,
                       "value": value, "matched": matched, "error": error, "sample_tick": game_tick,
                       "consume": consume, "next_tick": next_tick}
            evaluation = self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "wake.evaluated", payload)
            if matched:
                from ..serialization import canonical
                text = canonical({"reason": item["text"], "evaluation_seq": evaluation, **payload})
                if item["role"] == "planner":
                    self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "planner.wake",
                                        {"source": "plugin", "text": text, "evaluation_seq": evaluation})
                else:
                    anchor = self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "frontdesk.trigger",
                                                 {"source": "plugin", "text": text, "evaluation_seq": evaluation})
                    self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "frontdesk.wake",
                                        {"source": "plugin", "trigger_seq": anchor})
            return matched

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

    def record_frontdesk_wake(self, colony_id, load_epoch, game_tick, source, text):
        if source not in {"user", "game", "agent", "frontdesk", "planner", "plugin"}:
            raise ValueError("Unknown wake source")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Wake reason requires text")
        with self._ledger.transaction():
            if source == "user":
                anchor = self.record_utterance(colony_id, load_epoch, game_tick, text)
                key = "utterance_seq"
            else:
                anchor = self._ledger.append(colony_id, load_epoch, game_tick,
                                             "mod" if source == "game" else "plugin" if source == "agent" else source,
                                             "frontdesk.trigger", {"source": source, "text": text})
                key = "trigger_seq"
            return self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "frontdesk.wake",
                                       {"source": source, key: anchor})

    def ingest_game_event(self, colony_id, load_epoch, game_tick, connection, event):
        """Persist a native event and its default wakes atomically; the raw row is the cursor."""
        cursor = event.get("cursor")
        if (type(cursor) is not int or cursor < 1 or not isinstance(connection, str) or not connection
                or (event.get("connection_epoch"), event.get("colony_id"), event.get("load_epoch"),
                    event.get("game_tick")) != (connection, colony_id, load_epoch, game_tick)):
            raise ValueError("Invalid native event cursor or sampled identity")
        with self._ledger.transaction():
            last = self._ledger.game_event_cursor(colony_id, load_epoch, connection)
            if cursor <= last:
                previous = self._ledger.game_event_at_cursor(colony_id, load_epoch, connection, cursor)
                if previous and previous["payload"]["event"] == event:
                    return False
                raise ValueError("Native event cursor was reused")
            latest_epoch = self._ledger.latest_epoch(colony_id)
            previous_native = self._ledger.latest_event(colony_id, "game.event")
            previous_epoch = previous_native["load_epoch"] if previous_native else latest_epoch
            epoch_changed = previous_epoch is not None and previous_epoch != load_epoch
            if epoch_changed and latest_epoch != load_epoch:
                start_tick = event.get("data", {}).get("start_tick")
                loaded_at = (start_tick if event.get("kind") == "session_snapshot"
                             and type(start_tick) is int and 0 <= start_tick <= game_tick else game_tick)
                self.record_load(colony_id, load_epoch, loaded_at)
            seq = self._ledger.append(colony_id, load_epoch, game_tick, "mod", "game.event",
                                      {"connection": connection, "event": event})
            defaults = [e for e in self._ledger.events_by_kind(colony_id, ("scheduler.default_review",))
                        if e["load_epoch"] == load_epoch]
            if not defaults:
                default = self.register_wake(colony_id, load_epoch, game_tick, "planner",
                                             {"kind": "periodic", "interval_ticks": self.wake_policy(colony_id)["planner_review_ticks"]},
                                             "Periodic colony review")
                self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "scheduler.default_review", {"id": default["id"]})
            for item in self.wake_conditions(colony_id, load_epoch):
                if item["condition"]["kind"] in {"event", "deadline", "periodic"}:
                    self.evaluate_wake(colony_id, load_epoch, game_tick, item["id"], event,
                                       expected_next_tick=item["next_tick"])
            kind = event.get("kind")
            data = event.get("data", {})
            policy = self.wake_policy(colony_id)
            if epoch_changed or (kind == "pause_changed" and policy["frontdesk_paused_windows"]
                                 and data.get("source") == "window" and data.get("windows_force_paused") is True) or (kind == "letter" and
                    (data.get("def") in policy["frontdesk_letter_defs"]
                     or (policy["frontdesk_choice_letters"] and data.get("is_choice_letter") is True))):
                from ..serialization import canonical
                text = canonical({"event_seq": seq, "event": event})
                anchor = self._ledger.append(colony_id, load_epoch, game_tick, "mod", "frontdesk.trigger",
                                             {"source": "game", "text": text, "event_seq": seq})
                self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "frontdesk.wake",
                                    {"source": "game", "trigger_seq": anchor})
            if epoch_changed:
                self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "planner.wake",
                                    {"source": "game", "text": "World loaded", "event_seq": seq})
            return True

    def game_event_gap(self, colony_id, load_epoch, connection):
        return any(e["load_epoch"] == load_epoch and e["payload"].get("connection") == connection
                   for e in self._ledger.events_by_kind(colony_id, ("game.event_gap",)))

    def game_event_cursor(self, colony_id, load_epoch, connection):
        return self._ledger.game_event_cursor(colony_id, load_epoch, connection)

    def record_load(self, colony_id, load_epoch, game_tick):
        return self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, LOAD_EVENT, {})

    def record_takeover(self, colony_id, load_epoch, game_tick, takeover):
        if not isinstance(takeover, dict) or set(takeover) != TAKEOVER_FIELDS:
            raise ValueError("A takeover records object, field, value_read, ai_value and changeset_id")
        return self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, TAKEOVER_EVENT, takeover)

    def record_return(self, colony_id, load_epoch, game_tick, *, takeover_seq, utterance_seq, accepted_baseline=None):
        """Release a takeover, citing the user's own words; a takeover can be released once."""
        with self._ledger.transaction():
            if not self._ledger.has_event(colony_id, TAKEOVER_EVENT, takeover_seq):
                raise BoardError("A return releases an open takeover of this colony")
            if self._ledger.has_return(colony_id, takeover_seq):
                raise BoardError("This takeover is already released")
            if not self._ledger.has_event(colony_id, UTTERANCE_EVENT, utterance_seq):
                raise BoardError("A return cites a user utterance of this colony")
            payload = {"takeover_seq": takeover_seq, "utterance_seq": utterance_seq}
            if accepted_baseline is not None:
                if not isinstance(accepted_baseline, dict):
                    raise ValueError("Accepted baseline must be an object")
                payload["accepted_baseline"] = accepted_baseline
            return self._ledger.append(colony_id, load_epoch, game_tick, FRONTDESK, RETURN_EVENT, payload)

    def record_run(self, colony_id, load_epoch, game_tick, role, utterance_seq, messages, *, result=None, audit=None):
        """Append the assistant and tool messages one frontdesk run added, answering a user utterance."""
        if role != FRONTDESK:
            raise ValueError("Only the frontdesk session records runs; the planner has no session")
        if not any(self._ledger.has_event(colony_id, kind, utterance_seq) for kind in (UTTERANCE_EVENT, "frontdesk.trigger")):
            raise ValueError("A run answers a user utterance or frontdesk trigger of this colony")
        _check_run_messages(messages)
        key = "utterance_seq" if self._ledger.has_event(colony_id, UTTERANCE_EVENT, utterance_seq) else "trigger_seq"
        payload = {key: utterance_seq, "messages": messages, "result": result, "audit": audit}
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

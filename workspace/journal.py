"""Event and board writes, plus views rebuilt from the log; the only path into the two tables."""

import copy
import json

from .attention_policy import load_policy, normalize_attention_event
from .board import TASK_TARGET_KINDS, BoardError, check_revision, check_shape
from .cadence import project as cadence_project
from .cadence import validate_declaration
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
WAKE_POLICY_DEFAULTS = {
    "pause_on_letter_defs": ["ThreatBig", "ThreatSmall", "NegativeEvent"],
    "frontdesk_letter_defs": ["ThreatBig", "ThreatSmall", "NegativeEvent"],
    "frontdesk_choice_letters": True,
    "frontdesk_paused_windows": True,
}


class Journal:
    def __init__(self, path):
        self._ledger = EventLedger(path)

    def close(self):
        self._ledger.close()

    def budget_status(self, colony_id):
        events = self._ledger.events_by_kind(colony_id, ("budget.reserve", "budget.settle"))
        reservations = {e["payload"]["id"]: e["payload"] for e in events if e["kind"] == "budget.reserve"}
        settlements = {e["payload"]["id"]: e["payload"] for e in events if e["kind"] == "budget.settle"}
        input_total = known_prompt = known_completion = 0
        cost_total = known_cost = reserved_cost = 0.0
        unknown_usage = unknown_cost = unpriced = not_dispatched = attempted = 0
        for ident, item in reservations.items():
            receipt = settlements.get(ident, {})
            if receipt.get("outcome") == "not_dispatched":
                not_dispatched += 1
                continue
            if receipt.get("outcome") == "attempted":
                attempted += 1
            usage = receipt.get("usage", {})
            prompt, output, cached = (usage.get(k) for k in ("prompt_tokens", "completion_tokens", "cached_tokens"))
            def valid(n):
                return type(n) is int and n >= 0
            input_total += prompt if valid(prompt) else item["input_estimate"]
            known_prompt += prompt if valid(prompt) else 0
            known_completion += output if valid(output) else 0
            if not (valid(prompt) and valid(output)):
                unknown_usage += 1
            prices = item["prices"]
            priced = all(prices[k] is not None for k in ("input_price", "output_price", "cache_read_price"))
            exact = priced and valid(prompt) and valid(output) and valid(cached) and cached <= prompt
            if exact:
                cost = ((prompt - cached) * prices["input_price"] + cached * prices["cache_read_price"]
                        + output * prices["output_price"]) / 1_000_000
                known_cost += cost
            else:
                unknown_cost += 1
                if not priced:
                    unpriced += 1
                    continue
                # Use known quantities even if they exceed the reservation. Unknown quantities
                # retain their reservation; unknown cache split uses the higher configured rate.
                input_count = prompt if valid(prompt) else item["input_estimate"]
                output_count = output if valid(output) else item["output_limit"]
                cost = (input_count * max(prices["input_price"], prices["cache_read_price"])
                        + output_count * prices["output_price"]) / 1_000_000
                reserved_cost += cost
            cost_total += cost
        charged = len(reservations) - not_dispatched
        return {"requests": charged, "reservations": len(reservations), "not_dispatched": not_dispatched,
                "attempted": attempted, "dispatch_unknown": charged - attempted,
                "input_tokens": input_total, "cost": cost_total,
                "unknown_usage": unknown_usage, "unknown_cost": unknown_cost,
                "known_prompt_tokens": known_prompt, "known_completion_tokens": known_completion,
                "known_cost": known_cost, "reserved_cost": reserved_cost,
                "unpriced_reservations": unpriced}

    def budget_reserve(self, colony_id, epoch, tick, payload, max_requests, max_input_tokens, max_cost):
        with self._ledger.transaction():
            state = self.budget_status(colony_id)
            if (state["requests"] + 1 > max_requests
                    or state["input_tokens"] + payload["input_estimate"] > max_input_tokens
                    or (max_cost is not None and (payload["reserved_cost"] is None
                        or state["unpriced_reservations"] or state["cost"] + payload["reserved_cost"] > max_cost))):
                return False
            self._ledger.append(colony_id, epoch, tick, PLUGIN, "budget.reserve", payload)
        return True

    def budget_settle(self, colony_id, epoch, tick, payload):
        with self._ledger.transaction():
            events = self._ledger.events_by_kind(colony_id, ("budget.reserve", "budget.settle"))
            if not any(e["kind"] == "budget.reserve" and e["payload"]["id"] == payload["id"] for e in events):
                raise ValueError("Unknown model reservation")
            for event in events:
                if event["kind"] == "budget.settle" and event["payload"]["id"] == payload["id"]:
                    if event["payload"] == payload:
                        return
                    raise ValueError("Model reservation already settled differently")
            self._ledger.append(colony_id, epoch, tick, PLUGIN, "budget.settle", payload)

    def colonies(self):
        return self._ledger.colonies()

    def events(self, colony_id):
        return self._ledger.events(colony_id)

    def observe_epoch(self, colony_id, load_epoch, game_tick):
        """Record a load once when the observed epoch changes; never rewrite history."""
        load = self._ledger.latest_event(colony_id, LOAD_EVENT)
        previous = load["load_epoch"] if load else self._ledger.latest_epoch(colony_id)
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
            if "pause_on_letter_defs" in event["payload"]:
                policy["pause_on_letter_defs"] = event["payload"]["pause_on_letter_defs"]
        return policy

    def wake_policy_authors(self, colony_id):
        authors = {key: {"source": PLUGIN, "event_seq": None} for key in WAKE_POLICY_DEFAULTS}
        for event in self._ledger.events_by_kind(colony_id, ("scheduler.policy",)):
            if "pause_on_letter_defs" in event["payload"]:
                authors["pause_on_letter_defs"] = {"source": event["source"], "event_seq": event["seq"]}
        return authors

    def set_wake_policy(self, colony_id, load_epoch, game_tick, actor, changes):
        if actor != "frontdesk" or not isinstance(changes, dict) or set(changes) != {"pause_on_letter_defs"}:
            raise ValueError("Policy field is not writable by this role")
        definitions = changes["pause_on_letter_defs"]
        if not isinstance(definitions, list) or any(not isinstance(item, str) or not item for item in definitions):
            raise ValueError("Letter definitions must be nonempty strings")
        with self._ledger.transaction():
            return self._ledger.append(colony_id, load_epoch, game_tick, actor, "scheduler.policy", changes)

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

    def intent_source_history(self, colony_id, *, referenced=(), pending=()):
        """Original words for each active intent since its most recent closure.

        Prior sources are history, not independently active permissions; the
        current board revision and later user words determine their meaning.
        """
        histories = {}
        for revision in self._ledger.revisions(colony_id, kind="intent"):
            entry_id = revision["entry_id"]
            if revision["status"] == "closed":
                histories.pop(entry_id, None)
                continue
            history = histories.setdefault(entry_id, [])
            history.append((revision["source"]["event_seq"], revision["revision"]))
        emitted = set(referenced)
        pending = set(pending) - emitted
        result = []
        for entry_id, revisions in histories.items():
            sources = []
            by_id = {}
            for event_seq, revision in revisions:
                if event_seq in by_id:
                    by_id[event_seq]["revisions"].append(revision)
                    continue
                source = {"event_seq": event_seq, "revisions": [revision]}
                event = self._ledger.event(colony_id, event_seq)
                if event is None or event["kind"] != UTTERANCE_EVENT or not isinstance(event["payload"].get("text"), str):
                    if event_seq == revisions[-1][0]:
                        raise ValueError("Current intent source refers to unavailable user words")
                    source["status"] = "unavailable"
                else:
                    source.update(status="pending" if event_seq in pending else "referenced" if event_seq in emitted else "available",
                                  load_epoch=event["load_epoch"], game_tick=event["game_tick"])
                    if event_seq not in emitted and event_seq not in pending:
                        source["text"] = event["payload"]["text"]
                        emitted.add(event_seq)
                by_id[event_seq] = source
                sources.append(source)
            result.append({"entry_id": entry_id, "sources": sources,
                           "meaning": "Original user sources since the last closure; earlier words may have been changed or withdrawn. They are history, not independent current authorization. Referenced words are supplied elsewhere in this input; pending words have not yet been delivered."})
        return result

    def event(self, colony_id, seq):
        return self._ledger.event(colony_id, seq)

    def events_by_kind(self, colony_id, kinds, *, after_seq=0, load_epoch=None, anchor=None):
        return self._ledger.events_by_kind(colony_id, kinds, after_seq=after_seq, load_epoch=load_epoch, anchor=anchor)

    def planner_completion_events(self, colony_id):
        return self._ledger.planner_completion_events(colony_id)

    def action_events(self, colony_id):
        return self._ledger.action_events(colony_id)

    def event_highwater(self, colony_id):
        return self._ledger.event_highwater(colony_id)

    def event_ticks(self, colony_id, seqs):
        return self._ledger.event_ticks(colony_id, seqs)

    def planner_finished_metadata(self, colony_id):
        return self._ledger.planner_finished_metadata(colony_id)

    def successful_run(self, colony_id, anchor, *, through_seq=None):
        return self._ledger.successful_run(colony_id, anchor, through_seq=through_seq)

    def latest_event(self, colony_id, kind):
        return self._ledger.latest_event(colony_id, kind)

    def record(self, colony_id, load_epoch, game_tick, source, kind, payload):
        """Append an event kind that no typed writer owns; board, load, takeover and return have their own."""
        if kind in RESERVED_EVENTS:
            raise ValueError(f"{kind} events are written by their own operation")
        if kind in {"autoplay.disabled", "planner.started", "planner.received"}:
            with self._ledger.transaction():
                seq = self._ledger.append(colony_id, load_epoch, game_tick, source, kind, payload)
                self._append_cadence(colony_id, load_epoch, game_tick)
                return seq
        return self._ledger.append(colony_id, load_epoch, game_tick, source, kind, payload)

    def record_autoplay_activation(self, colony_id, load_epoch, game_tick, choices):
        required = {"goal", "constraints", "goal_edited", "constraints_edited"}
        if not isinstance(choices, dict) or set(choices) != required:
            raise ValueError("Activation requires goal, constraints and their edited flags")
        for name in ("goal", "constraints"):
            value = choices[name]
            if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                raise ValueError(f"Activation {name} must be nonempty text up to 2000 characters")
        if any(type(choices[name]) is not bool for name in ("goal_edited", "constraints_edited")):
            raise ValueError("Activation edited flags must be booleans")
        from ..serialization import canonical

        with self._ledger.transaction():
            policy, policy_hash = load_policy()
            activation = self._ledger.append(colony_id, load_epoch, game_tick, USER, "autoplay.activation", dict(choices))
            self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "attention.policy",
                                {"activation_seq": activation, "version": policy["version"], "sha256": policy_hash})
            wake = self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "planner.wake", {
                "source": "plugin", "text": "Explicit autoplay activation choices: " + canonical(choices),
                "activation_seq": activation, "choices": dict(choices)})
        return wake

    def record_utterance(self, colony_id, load_epoch, game_tick, text, *, request_id=None):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A user utterance requires text")
        return self._ledger.append(colony_id, load_epoch, game_tick, USER, UTTERANCE_EVENT, {"text": text, "request_id": request_id})

    def record_frontdesk_wake(self, colony_id, load_epoch, game_tick, source, text, *, request_id=None, purpose=None):
        if source not in {"user", "game", "agent", "frontdesk", "planner", "plugin"}:
            raise ValueError("Unknown wake source")
        if purpose is not None and (source != "planner" or purpose not in {"user_decision", "urgent_risk"}):
            raise ValueError("Invalid frontdesk message purpose")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Wake reason requires text")
        if source != "user" and request_id is not None:
            raise ValueError("Only original user requests have host request identifiers")
        with self._ledger.transaction():
            if source == "user" and request_id is not None:
                prior = self._ledger.db.execute(
                    "SELECT w.seq FROM events w JOIN events u ON u.seq=json_extract(w.payload,'$.utterance_seq') "
                    "WHERE w.kind='frontdesk.wake' AND w.colony_id=? AND w.load_epoch=? "
                    "AND u.kind=? AND u.colony_id=? AND u.load_epoch=? "
                    "AND json_extract(u.payload,'$.request_id')=? AND json_extract(u.payload,'$.text')=? "
                    "ORDER BY w.seq DESC LIMIT 1",
                    (colony_id, load_epoch, UTTERANCE_EVENT, colony_id, load_epoch, request_id, text),
                ).fetchone()
                if prior is not None:
                    return prior["seq"]
            if source == "user":
                anchor = self.record_utterance(colony_id, load_epoch, game_tick, text, request_id=request_id)
                key = "utterance_seq"
            else:
                anchor = self._ledger.append(colony_id, load_epoch, game_tick,
                                             "mod" if source == "game" else "plugin" if source == "agent" else source,
                                             "frontdesk.trigger", {"source": source, "text": text,
                                                                   **({"purpose": purpose} if purpose else {})})
                key = "trigger_seq"
            return self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "frontdesk.wake",
                                       {"source": source, key: anchor})

    def ingest_game_event(self, colony_id, load_epoch, game_tick, connection, event, *, attention_available=False,
                          observed_tick=None):
        """Persist a native event and its default wakes atomically; the raw row is the cursor."""
        cursor = event.get("cursor")
        if (type(cursor) is not int or cursor < 1 or not isinstance(connection, str) or not connection
                or (event.get("connection_epoch"), event.get("colony_id"), event.get("load_epoch"),
                    event.get("game_tick")) != (connection, colony_id, load_epoch, game_tick)):
            raise ValueError("Invalid native event cursor or sampled identity")
        if observed_tick is not None and (type(observed_tick) is not int or observed_tick < 0):
            raise ValueError("Invalid observed game tick")
        with self._ledger.transaction():
            last = self._ledger.game_event_cursor(colony_id, load_epoch, connection)
            if cursor <= last:
                previous = self._ledger.game_event_at_cursor(colony_id, load_epoch, connection, cursor)
                if previous and previous["payload"]["event"] == event:
                    return False
                raise ValueError("Native event cursor was reused")
            sampled_tick = game_tick
            previous_tick = self._ledger.latest_observed_tick(colony_id, load_epoch)
            game_tick = max(sampled_tick, observed_tick if observed_tick is not None else sampled_tick,
                            previous_tick if previous_tick is not None else sampled_tick)
            latest_load = self._ledger.latest_event(colony_id, LOAD_EVENT)
            latest_epoch = latest_load["load_epoch"] if latest_load else self._ledger.latest_epoch(colony_id)
            previous_native = self._ledger.latest_event(colony_id, "game.event")
            previous_epoch = previous_native["load_epoch"] if previous_native else latest_epoch
            epoch_changed = previous_epoch is not None and previous_epoch != load_epoch
            if epoch_changed and latest_epoch != load_epoch:
                start_tick = event.get("data", {}).get("start_tick")
                loaded_at = (start_tick if event.get("kind") == "session_snapshot"
                             and type(start_tick) is int and 0 <= start_tick <= game_tick else game_tick)
                self.record_load(colony_id, load_epoch, loaded_at)
            seq = self._ledger.append(colony_id, load_epoch, game_tick, "mod", "game.event",
                                      {"connection": connection, "event": event, "native_game_tick": sampled_tick,
                                       "observed_game_tick": game_tick})
            kind = event.get("kind")
            data = event.get("data", {})
            if kind in {"attention_snapshot", "attention_changed"} and attention_available:
                for ordinal, signal in enumerate(normalize_attention_event({"kind": kind, **data})):
                    self._ledger.append(colony_id, load_epoch, game_tick, "mod", "attention.signal",
                                        {**signal, "native_event_seq": seq, "native_game_tick": sampled_tick, "ordinal": ordinal})
            elif kind == "letter":
                self._ledger.append(colony_id, load_epoch, game_tick, "mod", "attention.signal",
                                    {"category": "letter", "edge": "appeared", "source_id": f"letter:{seq}",
                                     "def": data.get("def"), "native_event_seq": seq, "native_game_tick": sampled_tick, "ordinal": 0})
            self._append_cadence(colony_id, load_epoch, game_tick)
            kind = event.get("kind")
            data = event.get("data", {})
            policy = self.wake_policy(colony_id)
            lifecycle = self._ledger.events_by_kind(colony_id, ("autoplay.activation", "autoplay.disabled"))
            autoplay = bool(lifecycle and lifecycle[-1]["kind"] == "autoplay.activation")
            if (epoch_changed and not autoplay) or (kind == "pause_changed" and policy["frontdesk_paused_windows"]
                                 and data.get("source") == "window" and data.get("windows_force_paused") is True) or (kind == "letter" and
                    ((not autoplay and data.get("def") in policy["frontdesk_letter_defs"])
                     or (policy["frontdesk_choice_letters"] and data.get("is_choice_letter") is True))):
                from ..serialization import canonical
                presented_event = copy.deepcopy(event)
                presented_data = presented_event.get("data")
                if isinstance(presented_data, dict):
                    presented_data.pop("monotonic", None)
                    pause_state = presented_data.get("pause_state")
                    if isinstance(pause_state, dict):
                        pause_state.pop("monotonic", None)
                text = canonical({"event_seq": seq, "event": presented_event})
                anchor = self._ledger.append(colony_id, load_epoch, game_tick, "mod", "frontdesk.trigger",
                                             {"source": "game", "text": text, "event_seq": seq})
                self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "frontdesk.wake",
                                    {"source": "game", "trigger_seq": anchor})
            if epoch_changed:
                self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "planner.wake",
                                    {"source": "game", "text": "World loaded", "event_seq": seq})
            return True

    def _append_cadence(self, colony_id, load_epoch, game_tick):
        """Append deterministic cadence work while the caller owns the transaction."""
        policy, _ = load_policy()
        state = cadence_project(self._ledger.cadence_events(colony_id), epoch=load_epoch,
                                now_tick=game_tick, policy=policy)
        for item in state["items"]:
            self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, item["kind"], item["payload"])
            if item["kind"] == "cadence.review_due":
                payload = item["payload"]
                self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "planner.wake",
                                    {"source": "game", "text": "Cadence review due", "work_id": payload["id"],
                                     "urgency": payload.get("urgency", "scheduled"),
                                     "signal_seqs": payload.get("signal_seqs", []),
                                     "signals": [self._ledger.event(colony_id, seq) for seq in payload.get("signal_seqs", [])],
                                     "review_type": payload["review_type"]})
        return state

    def attention_display(self, colony_id, load_epoch, connection):
        """Read-only cadence projection at the latest recorded tick; never append work."""
        events = self._ledger.cadence_events(colony_id)
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
        source = self._ledger.event(colony_id, latest_snapshot["payload"].get("native_event_seq")) if latest_snapshot else None
        same_scope = bool(source and source["kind"] == "game.event"
                          and source["load_epoch"] == load_epoch and source["payload"].get("connection") == connection)
        partitions = {name: dict(part) for name, part in latest_snapshot["payload"].get("partitions", {}).items()} if same_scope else {}
        if same_scope:
            map_id = latest_snapshot["payload"].get("map_id")
            changes = self._ledger.events_by_kind(colony_id, ("game.event",), after_seq=source["seq"], load_epoch=load_epoch)
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

    def attention_state(self, colony_id, load_epoch, game_tick):
        with self._ledger.transaction():
            return self._append_cadence(colony_id, load_epoch, game_tick)

    def attention_declare(self, colony_id, load_epoch, game_tick, actor, level, reference, reason, user_sources=()):
        with self._ledger.transaction():
            tasks = {row["entry_id"] for row in self.board(colony_id)
                     if row["kind"] == "task" and row["author"] == "planner" and row["status"] != "closed"}
            sources = {seq for seq in user_sources if (e := self._ledger.event(colony_id, seq))
                       and e["kind"] == UTTERANCE_EVENT and e["load_epoch"] == load_epoch}
            policy, _ = load_policy()
            payload = validate_declaration(self._ledger.cadence_events(colony_id), epoch=load_epoch, tick=game_tick,
                                           actor=actor, level=level, reference=reference, reason=reason,
                                           policy=policy, open_tasks=tasks, user_utterances=sources)
            seq = self._ledger.append(colony_id, load_epoch, game_tick, actor, "cadence.lease_declared", payload)
            self._append_cadence(colony_id, load_epoch, game_tick)
            return seq

    def _pause_watchdog_matches(self, colony_id, load_epoch, episode):
        records = self._ledger.events_by_kind(colony_id, ("game.event", "autoplay.startup_pause"), load_epoch=load_epoch)
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

    def pause_watchdog_history(self, colony_id, load_epoch):
        return [event for event in self._ledger.cadence_events(colony_id)
                if event["load_epoch"] == load_epoch and event["kind"] in
                {"pause.watchdog", "planner.started", "planner.finished"}]

    def pause_watchdog_wake(self, colony_id, load_epoch, game_tick, episode, count):
        with self._ledger.transaction():
            if not self._pause_watchdog_matches(colony_id, load_epoch, episode):
                return None
            wake = self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "planner.wake",
                                       {"source": "plugin", "text": "Pause remains; review whether it should continue.",
                                        "pause_watchdog": True})
            self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "pause.watchdog",
                                {"episode": list(episode), "action": "wake", "count": count, "wake_seq": wake})
            return wake

    def pause_watchdog_notify(self, colony_id, load_epoch, game_tick, episode):
        with self._ledger.transaction():
            if not self._pause_watchdog_matches(colony_id, load_epoch, episode):
                return False
            previous = self._ledger.events_by_kind(colony_id, ("pause.watchdog",), load_epoch=load_epoch)
            if any(event["payload"].get("episode") == list(episode) and event["payload"].get("action") in {"notify_attempt", "notify"}
                   for event in previous):
                return False
            self._ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "pause.watchdog",
                                {"episode": list(episode), "action": "notify_attempt"})
            return True

    def attention_revoke(self, colony_id, load_epoch, game_tick, reference, user_sources):
        with self._ledger.transaction():
            if reference not in user_sources:
                raise ValueError("Revoke requires an utterance received in this run")
            original = self._ledger.event(colony_id, reference)
            if not original or original["kind"] != UTTERANCE_EVENT or original["load_epoch"] != load_epoch:
                raise ValueError("Revoke requires the original user utterance")
            state = self._append_cadence(colony_id, load_epoch, game_tick)
            revoked = []
            for lease in state.get("leases", {}).values():
                if lease["actor"] == "user":
                    self._ledger.append(colony_id, load_epoch, game_tick, USER, "attention.revoked",
                                        {"reference": lease["reference"], "utterance_seq": reference})
                    revoked.append(lease["reference"])
            self._append_cadence(colony_id, load_epoch, game_tick)
            return revoked

    def game_event_at_cursor(self, colony_id, load_epoch, connection, cursor):
        return self._ledger.game_event_at_cursor(colony_id, load_epoch, connection, cursor)

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
        run_id = audit.get("run_id") if isinstance(audit, dict) else None
        if not isinstance(run_id, str) or not run_id:
            return self._ledger.append(colony_id, load_epoch, game_tick, FRONTDESK, RUN_EVENT, payload)
        with self._ledger.transaction():
            rows = self._ledger.db.execute(
                "SELECT seq,colony_id FROM events WHERE kind=? "
                "AND json_extract(payload,'$.audit.run_id')=? ORDER BY seq", (RUN_EVENT, run_id)
            ).fetchall()
            if rows:
                if len(rows) != 1:
                    raise ValueError("Frontdesk run identity is ambiguous")
                prior = self._ledger.event(rows[0]["colony_id"], rows[0]["seq"])
                same_payload = json.dumps(prior["payload"], ensure_ascii=False, allow_nan=False, sort_keys=True) == json.dumps(
                    payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
                if (prior["colony_id"] != colony_id or prior["load_epoch"] != load_epoch
                        or prior["game_tick"] != game_tick or prior["source"] != FRONTDESK or not same_payload):
                    raise ValueError("Frontdesk run identity conflicts with the saved outcome")
                return prior["seq"]
            return self._ledger.append(colony_id, load_epoch, game_tick, FRONTDESK, RUN_EVENT, payload)

    def finish_planner(self, colony_id, load_epoch, game_tick, start_seq, cursor, result, audit):
        """Atomically retain the outcome, optional handoff and consumed wake position."""
        if type(start_seq) is not int or start_seq <= 0 or type(cursor) is not int or cursor < 0:
            raise ValueError("Planner completion requires a valid start and consumed cursor")
        with self._ledger.transaction():
            terminal = result.get("terminal")
            payload = {"start_seq": start_seq, "result": result, "audit": audit}
            saved = [event for event in self._ledger.events_by_kind(
                colony_id, ("planner.finished",), after_seq=start_seq)
                if event["payload"].get("start_seq") == start_seq]
            if saved:
                if len(saved) != 1:
                    raise ValueError("Planner completion is ambiguous")
                prior = saved[0]
                consumed = self._ledger.event(colony_id, prior["seq"] + 1)

                def matches(event, kind, expected):
                    if (event is None or event["kind"] != kind or event["source"] != "planner"
                            or event["load_epoch"] != load_epoch or event["game_tick"] != game_tick):
                        return False
                    return json.dumps(event["payload"], ensure_ascii=False, allow_nan=False, sort_keys=True) == json.dumps(
                        expected, ensure_ascii=False, allow_nan=False, sort_keys=True)

                if (not matches(prior, "planner.finished", payload)
                        or not matches(consumed, "planner.consumed", {"through_seq": cursor})):
                    raise ValueError("Planner completion conflicts with the saved outcome")
                if terminal and terminal.get("kind") == "yield":
                    handoff = self._ledger.event(colony_id, prior["seq"] - 1)
                    if not matches(handoff, "planner.yield", {"start_seq": start_seq, "handoff": terminal["handoff"]}):
                        raise ValueError("Planner completion conflicts with its saved handoff")
                return prior["seq"]
            if terminal and terminal.get("kind") == "yield":
                self._ledger.append(colony_id, load_epoch, game_tick, "planner", "planner.yield",
                                    {"start_seq": start_seq, "handoff": terminal["handoff"]})
            seq = self._ledger.append(colony_id, load_epoch, game_tick, "planner", "planner.finished",
                                      payload)
            self._ledger.append(colony_id, load_epoch, game_tick, "planner", "planner.consumed",
                                {"through_seq": cursor})
            self._append_cadence(colony_id, load_epoch, game_tick)
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
            if row["kind"] == "task" and row["status"] == "closed":
                self._ledger.append(colony_id, load_epoch, game_tick, actor, "task.closed", {"reference": row["entry_id"]})
                self._append_cadence(colony_id, load_epoch, game_tick)
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
        events = self._ledger.events_by_kind(colony_id, (TAKEOVER_EVENT, REVISION_EVENT))
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
    load_epoch = None
    revision_epochs = {}
    takeovers = []
    for event in events:
        if event["kind"] == LOAD_EVENT:
            if event["seq"] > last_load:
                last_load = event["seq"]
                load_epoch = event.get("load_epoch")
        elif event["kind"] == REVISION_EVENT:
            revision_epochs[event["seq"]] = event.get("load_epoch")
        elif event["kind"] == TAKEOVER_EVENT:
            takeovers.append(event)
    marks = {}
    for entry in current_board(revisions):
        reasons = []
        revision_epoch = revision_epochs.get(entry["event_seq"])
        if last_load and (last_load > entry["event_seq"]
                          or not isinstance(load_epoch, str) or not load_epoch
                          or not isinstance(revision_epoch, str) or not revision_epoch
                          or revision_epoch != load_epoch):
            reasons.append("load")
        if any(t["seq"] > entry["event_seq"] and t["payload"]["changeset_id"] in entry["changesets"] for t in takeovers):
            reasons.append("takeover")
        if reasons:
            marks[entry["entry_id"]] = reasons
    return marks

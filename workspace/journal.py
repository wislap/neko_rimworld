"""Event and board writes, plus views rebuilt from the log; the only path into the two tables.

The journal owns every transaction. The responsibility modules it calls receive the
ledger explicitly and run inside the transaction opened here.
"""

import copy

from ..serialization import canonical
from . import (
    attention_cadence,
    board_submission,
    budget_ledger,
    field_takeovers,
    intent_history,
    native_ingest,
    pause_watchdog,
    role_policies,
    run_records,
    work_commands,
    work_state,
)
from .attention_policy import load_policy
from .board import current_board, review_marks
from .cadence_events import BUSINESS_KINDS
from .ledger import (
    FRONTDESK,
    LOAD_EVENT,
    PLUGIN,
    RESERVED_EVENTS,
    REVISION_EVENT,
    TAKEOVER_EVENT,
    USER,
    UTTERANCE_EVENT,
    EventLedger,
)


class Journal:
    def __init__(self, path):
        self._ledger = EventLedger(path)

    def close(self):
        self._ledger.close()

    def budget_status(self, colony_id):
        return budget_ledger.status(self._ledger, colony_id)

    def budget_reserve(self, colony_id, epoch, tick, payload, max_requests, max_input_tokens, max_cost):
        with self._ledger.transaction():
            return budget_ledger.reserve(self._ledger, colony_id, epoch, tick, payload,
                                         max_requests, max_input_tokens, max_cost)

    def budget_settle(self, colony_id, epoch, tick, payload):
        with self._ledger.transaction():
            budget_ledger.settle(self._ledger, colony_id, epoch, tick, payload)

    def colonies(self):
        return self._ledger.colonies()

    def events(self, colony_id):
        return self._ledger.events(colony_id)

    def observe_epoch(self, colony_id, load_epoch, game_tick):
        return native_ingest.observe_epoch(self._ledger, colony_id, load_epoch, game_tick)

    def context_policy(self, colony_id):
        return role_policies.context_policy(self._ledger, colony_id)

    def wake_policy(self, colony_id):
        return role_policies.wake_policy(self._ledger, colony_id)

    def set_wake_policy(self, colony_id, load_epoch, game_tick, actor, changes):
        with self._ledger.transaction():
            return role_policies.set_wake_policy(self._ledger, colony_id, load_epoch, game_tick, actor, changes)

    def set_context_policy(self, colony_id, load_epoch, game_tick, changes):
        return role_policies.set_context_policy(self._ledger, colony_id, load_epoch, game_tick, changes)

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
        return intent_history.intent_source_history(self._ledger, colony_id, referenced=referenced, pending=pending)

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

    def planner_finished_metadata(self, colony_id, *, through_seq=None):
        return self._ledger.planner_finished_metadata(colony_id, through_seq=through_seq)

    def successful_run(self, colony_id, anchor, *, through_seq=None):
        return self._ledger.successful_run(colony_id, anchor, through_seq=through_seq)

    def latest_event(self, colony_id, kind):
        return self._ledger.latest_event(colony_id, kind)

    def record(self, colony_id, load_epoch, game_tick, source, kind, payload):
        """Append an event kind that no typed writer owns; board, load, takeover and return have their own."""
        if kind in RESERVED_EVENTS:
            raise ValueError(f"{kind} events are written by their own operation")
        if native_ingest.is_native_construction_commit(source, kind, payload):
            with self._ledger.transaction():
                seq = self._ledger.append(colony_id, load_epoch, game_tick, source, kind, payload)
                native_ingest.record_construction_batch(self._ledger, colony_id, load_epoch, game_tick, payload["write"], seq)
                return seq
        if kind in BUSINESS_KINDS:
            with self._ledger.transaction():
                game_tick = self._ledger.recording_tick(colony_id, load_epoch, game_tick)
                seq = self._ledger.append(colony_id, load_epoch, game_tick, source, kind, payload)
                if kind in {"autoplay.disabled", "planner.started", "planner.received"}:
                    attention_cadence.append_cadence(self._ledger, colony_id, load_epoch, game_tick)
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
        with self._ledger.transaction():
            game_tick = self._ledger.recording_tick(colony_id, load_epoch, game_tick)
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
        with self._ledger.transaction():
            return native_ingest.ingest_game_event(self._ledger, colony_id, load_epoch, game_tick, connection, event,
                                                   attention_available=attention_available, observed_tick=observed_tick)

    def ingest_area_manual_snapshot(self, colony_id, load_epoch, game_tick, connection, observation):
        with self._ledger.transaction():
            return native_ingest.ingest_area_manual_snapshot(self._ledger, colony_id, load_epoch, game_tick,
                                                             connection, observation)

    def reconcile_construction(self, colony_id, load_epoch, game_tick, connection_epoch, observation):
        with self._ledger.transaction():
            return native_ingest.reconcile_construction(self._ledger, colony_id, load_epoch, game_tick,
                                                        connection_epoch, observation)

    def attention_display(self, colony_id, load_epoch, connection):
        return attention_cadence.attention_display(self._ledger, colony_id, load_epoch, connection)

    def attention_state(self, colony_id, load_epoch, game_tick):
        with self._ledger.transaction():
            return attention_cadence.append_cadence(self._ledger, colony_id, load_epoch, game_tick)

    def attention_declare(self, colony_id, load_epoch, game_tick, actor, level, reference, reason, user_sources=()):
        with self._ledger.transaction():
            return attention_cadence.attention_declare(self._ledger, colony_id, load_epoch, game_tick, actor, level,
                                                       reference, reason, user_sources)

    def attention_revoke(self, colony_id, load_epoch, game_tick, reference, user_sources):
        with self._ledger.transaction():
            return attention_cadence.attention_revoke(self._ledger, colony_id, load_epoch, game_tick, reference,
                                                      user_sources)

    def pause_watchdog_history(self, colony_id, load_epoch):
        return pause_watchdog.pause_watchdog_history(self._ledger, colony_id, load_epoch)

    def pause_watchdog_wake(self, colony_id, load_epoch, game_tick, episode, count):
        with self._ledger.transaction():
            return pause_watchdog.pause_watchdog_wake(self._ledger, colony_id, load_epoch, game_tick, episode, count)

    def pause_watchdog_notify(self, colony_id, load_epoch, game_tick, episode):
        with self._ledger.transaction():
            return pause_watchdog.pause_watchdog_notify(self._ledger, colony_id, load_epoch, game_tick, episode)

    def game_event_at_cursor(self, colony_id, load_epoch, connection, cursor):
        return self._ledger.game_event_at_cursor(colony_id, load_epoch, connection, cursor)

    def game_event_gap(self, colony_id, load_epoch, connection):
        return any(e["load_epoch"] == load_epoch and e["payload"].get("connection") == connection
                   for e in self._ledger.events_by_kind(colony_id, ("game.event_gap",)))

    def game_event_cursor(self, colony_id, load_epoch, connection):
        return self._ledger.game_event_cursor(colony_id, load_epoch, connection)

    def record_load(self, colony_id, load_epoch, game_tick):
        return native_ingest.record_load(self._ledger, colony_id, load_epoch, game_tick)

    def record_takeover(self, colony_id, load_epoch, game_tick, takeover):
        return field_takeovers.record_takeover(self._ledger, colony_id, load_epoch, game_tick, takeover)

    def record_return(self, colony_id, load_epoch, game_tick, *, takeover_seq, utterance_seq, accepted_baseline=None):
        with self._ledger.transaction():
            return field_takeovers.record_return(self._ledger, colony_id, load_epoch, game_tick, takeover_seq=takeover_seq,
                                                 utterance_seq=utterance_seq, accepted_baseline=accepted_baseline)

    def record_policy_protection(self, colony_id, load_epoch, game_tick, *, utterance_seq, protection, release_seq=None):
        """Save a protection requested in original user words, or explicitly release it."""
        if not isinstance(protection, dict):
            raise ValueError("A policy protection must be an object")
        kind = protection.get("kind")
        required = {"reference": {"kind", "pawn_id"}, "policy_content": {"kind", "policy_id"},
                    "effective_rules": {"kind", "pawn_id"}}.get(kind)
        allowed = required | ({"policy_id"} if kind == "effective_rules" else set()) if required else set()
        if (required is None or not required <= protection.keys() or protection.keys() - allowed
                or any(not isinstance(value, str) or not value.strip() for value in protection.values())):
            raise ValueError("Invalid policy protection kind or identity")
        with self._ledger.transaction():
            if not self._ledger.has_event(colony_id, UTTERANCE_EVENT, utterance_seq):
                raise ValueError("A protection must cite original user words in this colony")
            tick = self._ledger.recording_tick(colony_id, load_epoch, game_tick)
            payload = {"utterance_seq": utterance_seq, "protection": copy.deepcopy(protection)}
            event_kind = "user.policy_protection"
            if release_seq is not None:
                original = self._ledger.event(colony_id, release_seq)
                released = self._ledger.events_by_kind(colony_id, ("user.policy_protection_released",))
                if (original is None or original["kind"] != "user.policy_protection" or original["source"] != FRONTDESK
                        or original["payload"]["protection"] != protection
                        or utterance_seq <= original["seq"]
                        or any(e["payload"].get("protection_seq") == release_seq for e in released)):
                    raise ValueError("Release must cite a matching active protection")
                payload["protection_seq"] = release_seq
                event_kind = "user.policy_protection_released"
            return self._ledger.append(colony_id, load_epoch, tick, FRONTDESK, event_kind, payload)

    def record_run(self, colony_id, load_epoch, game_tick, role, utterance_seq, messages, *, result=None, audit=None):
        with self._ledger.transaction():
            return run_records.record_run(self._ledger, colony_id, load_epoch, game_tick, role, utterance_seq, messages,
                                          result=result, audit=audit)

    def finish_planner(self, colony_id, load_epoch, game_tick, start_seq, cursor, result, audit):
        with self._ledger.transaction():
            return run_records.finish_planner(self._ledger, colony_id, load_epoch, game_tick, start_seq, cursor,
                                              result, audit)

    def submit(self, colony_id, load_epoch, game_tick, actor, submission):
        """Write one board revision and its event atomically, or raise and write nothing."""
        with self._ledger.transaction():
            return board_submission.submit(self._ledger, colony_id, load_epoch, game_tick, actor, submission)

    def board(self, colony_id):
        return current_board(self._ledger.revisions(colony_id))

    def colony_work(self, colony_id, load_epoch, connection_epoch=None):
        return work_state.current(self._ledger, colony_id, load_epoch, connection_epoch)

    def construction_dispatch_authority(self, colony_id, load_epoch, plan_id, element_ids):
        return work_commands.construction_dispatch_authority(self._ledger, colony_id, load_epoch, plan_id, element_ids)

    def colony_work_command(self, colony_id, load_epoch, game_tick, actor, command, arguments,
                            *, connection_epoch=None, supported_kinds=()):
        with self._ledger.transaction():
            return work_commands.colony_work_command(self._ledger, colony_id, load_epoch, game_tick, actor, command,
                                                     arguments, connection_epoch=connection_epoch,
                                                     supported_kinds=supported_kinds)

    def observation_coverage(self, colony_id, load_epoch, game_tick, connection_epoch, **receipt):
        with self._ledger.transaction():
            return work_commands.observation_coverage(self._ledger, colony_id, load_epoch, game_tick, connection_epoch,
                                                      **receipt)

    def record_resource_observation(self, colony_id, load_epoch, game_tick, **arguments):
        with self._ledger.transaction():
            return work_commands.record_resource_observation(self._ledger, colony_id, load_epoch, game_tick, **arguments)

    def takeovers(self, colony_id):
        return field_takeovers.takeovers(self._ledger, colony_id)

    def review_marks(self, colony_id):
        load = self._ledger.latest_event(colony_id, LOAD_EVENT)
        events = self._ledger.events_by_kind(colony_id, (TAKEOVER_EVENT, REVISION_EVENT))
        if load:
            events.append(load)
        return review_marks(self._ledger.revisions(colony_id), events)

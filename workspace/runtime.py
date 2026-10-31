"""Persistent frontdesk execution backed by the colony event journal."""

import asyncio
import copy
import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..agent.context.narrative import SUMMARY_EVENT, select_narrative, summary_message, summary_request
from ..agent.runtime.audit import AgentAuditLedger, private_protocol_redacted
from ..agent.runtime.session import AgentRunSession
from ..serialization import canonical
from ..tool_contracts import ToolExecutionError, ToolSpec
from .journal import Journal
from .ledger import FRONTDESK, RUN_EVENT, UTTERANCE_EVENT
from .session_view import anchor_seq, frontdesk_messages, pinned_utterances, recall, trigger_message


class FrontdeskService:
    def __init__(self, path, runtime, *, game_session):
        self.path = path
        self.journal = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="colony-journal")
        self._recovered = set()
        self.runtime = runtime
        self.game_session = game_session
        self._locks = {}
        self._role_lock = asyncio.Lock()
        self._tasks = set()
        self.active = {}
        self.pending = {}
        self.revision = 0
        self.closed = False
        self._journal_timing_lock = threading.Lock()
        self._journal_timing = {}

    def journal_timing_snapshot(self):
        """Cumulative worker timings; sampled independently of bridge scope."""
        with self._journal_timing_lock:
            return {"sampled_monotonic": time.monotonic(),
                    "meaning": "Service lifetime totals; worker includes guard, Journal, SQLite and serialization. Durations overlap across roles and are not wall time.",
                    "methods": copy.deepcopy(self._journal_timing)}

    async def call(self, method, *args, command_binding_check=None, **kwargs):
        submitted = time.monotonic()
        with self._journal_timing_lock:
            timing = self._journal_timing.setdefault(method, {"submitted": 0, "started": 0, "completed": 0,
                "failed": 0, "await_cancelled": 0, "queue_seconds": 0.0, "worker_seconds": 0.0,
                "queue_max_seconds": 0.0, "worker_max_seconds": 0.0})
            timing["submitted"] += 1

        def invoke():
            begun = time.monotonic()
            with self._journal_timing_lock:
                timing["started"] += 1
                queued = begun - submitted
                timing["queue_seconds"] += queued
                timing["queue_max_seconds"] = max(timing["queue_max_seconds"], queued)
            failed = True
            try:
                result = invoke_journal()
                failed = False
                return result
            finally:
                with self._journal_timing_lock:
                    timing["completed"] += 1
                    timing["failed"] += int(failed)
                    duration = time.monotonic() - begun
                    timing["worker_seconds"] += duration
                    timing["worker_max_seconds"] = max(timing["worker_max_seconds"], duration)

        def invoke_journal():
            if self.journal is None:
                self.journal = Journal(self.path)
            # Business mutations must still belong to the active world when the
            # single journal worker executes them, not merely when queued.
            guarded = {"observe_epoch", "submit", "set_wake_policy", "register_wake", "cancel_wake",
                       "evaluate_wake", "set_context_policy", "record_autoplay_activation",
                       "record_utterance", "record_frontdesk_wake", "record_takeover", "record_return",
                       "ingest_game_event"}
            event_kind = (args[4] if len(args) > 4 else kwargs.get("kind")) if method == "record" else None
            if method in guarded or event_kind in {"planner.wake", "frontdesk.wake"}:
                colony = args[0] if args else kwargs.get("colony_id")
                epoch = args[1] if len(args) > 1 else kwargs.get("load_epoch")
                current = self.game_session()
                if current is None or (current.colony_id, current.load_epoch) != (colony, epoch):
                    raise ToolExecutionError("world_changed", "The active colony or epoch changed before the command executed", recoverable=False)
                if command_binding_check is not None:
                    try:
                        valid = command_binding_check()
                    except Exception:
                        valid = False
                    if not valid:
                        raise ToolExecutionError("world_changed", "The active colony connection changed before the command executed", recoverable=False)
                load = self.journal.latest_event(colony, "colony.load")
                if method not in {"observe_epoch", "ingest_game_event"} and load and load["load_epoch"] != epoch:
                    raise ToolExecutionError("world_changed", "The recorded colony load changed before the command executed", recoverable=False)
            return getattr(self.journal, method)(*args, **kwargs)
        try:
            return await asyncio.get_running_loop().run_in_executor(self._executor, invoke)
        except asyncio.CancelledError:
            with self._journal_timing_lock:
                timing["await_cancelled"] += 1
            raise

    async def _recover_interrupted(self, colony, *, pending_utterances=()):
        if colony in self._recovered:
            return
        events = await self.call("interrupted_events", colony)
        for event in events:
            if event["kind"] not in (UTTERANCE_EVENT, "frontdesk.trigger") or event["seq"] in pending_utterances:
                continue
            progress = [item["payload"] for item in events if item["kind"] == "agent.progress" and anchor_seq(item) == event["seq"]]
            messages = [item["message"] for item in progress if "message" in item]
            _complete_tool_pairs(messages)
            observations = [item["observation"] for item in progress if item.get("observation") is not None]
            await self.call("record_run", colony, event["load_epoch"], event["game_tick"], FRONTDESK, event["seq"], messages, result={"status": "failed", "reason": "process_interrupted", "observations": observations})
        self._recovered.add(colony)

    def capabilities(self, game, utterance_seq, intent_sources=None, *, explicit=False, binding_check=None):
        connection = getattr(game, "connection_epoch", None)
        if binding_check is None and connection is not None:
            def binding_check():
                return getattr(self.game_session(), "connection_epoch", None) == connection
        async def board(args):
            try:
                if args["action"] == "read":
                    return {"entries": await self.call("board", game.colony_id)}
                source_seq = args.get("source_event_seq", utterance_seq)
                if type(source_seq) is not int or (intent_sources is not None and source_seq not in intent_sources):
                    available = (sorted(intent_sources) if intent_sources is not None else
                                 [utterance_seq] if type(utterance_seq) is int else [])
                    raise ToolExecutionError("board_invalid", "An intent must cite user words delivered to this run",
                                             details={"available_user_utterance_seqs": available})
                original = await self.call("event", game.colony_id, source_seq)
                if original is None or original["kind"] != UTTERANCE_EVENT:
                    raise ValueError("An intent requires the original user utterance")
                submission = dict(args["entry"])
                submission.update(kind="intent", parent=None, source={"event_seq": source_seq}, evidence=[], changesets=[])
                seq = await self.call("submit", game.colony_id, game.load_epoch, game.game_tick, FRONTDESK, submission,
                                      command_binding_check=binding_check)
                return {"event_seq": seq, "entries": await self.call("board", game.colony_id)}
            except (ValueError, KeyError) as error:
                raise ToolExecutionError("board_invalid", str(error)) from error

        async def read_original(args):
            try:
                if "evidence_id" in args:
                    evidence = await self.call("evidence", game.colony_id, args["evidence_id"])
                    if evidence is None:
                        raise KeyError("Unknown evidence")
                    return {"original": evidence["payload"]["observation"], "historical": True, "current_observation": False}
                event = await self.call("event", game.colony_id, args["event_seq"])
                if event is None:
                    raise KeyError("Unknown event")
                progress = await self.call("progress", game.colony_id, anchor_seq(event)) if event["kind"] == RUN_EVENT else []
                original = recall([event], args["event_seq"], args.get("tool_call_id"), progress=progress, part=args.get("part", "result"))
                result = {"original": original, "historical": True, "current_observation": False}
                if event["kind"] == RUN_EVENT and args.get("tool_call_id") and args.get("part", "result") == "result":
                    for message in event["payload"]["messages"]:
                        for call in message.get("tool_calls", []):
                            if call.get("id") == args["tool_call_id"]:
                                result["original_tool"] = call.get("function", {}).get("name")
                return result
            except (ValueError, KeyError) as error:
                raise ToolExecutionError("recall_invalid", str(error)) from error

        return (
            ToolSpec("frontdesk_board", "Read the colony board or record the user's intent. This does not modify the game.", {
                "type": "object", "properties": {"action": {"type": "string", "enum": ["read", "write_intent"]}, **({"source_event_seq": {"type": "integer", "description": "Only a user.utterance actually delivered to this run. Historical words, board references, agent runs and non-user triggers do not grant a new intent source. If no user words were delivered, do not write an intent."}} if explicit else {}), "entry": {
                    "type": "object", "properties": {"entry_id": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 0}, "status": {"type": "string", "enum": ["open", "blocked", "closed"]}, "priority": {"type": "integer"}, "review_at_tick": {"type": ["integer", "null"]}, "body": {"type": "string"}},
                    "required": ["entry_id", "expected_revision", "status", "priority", "review_at_tick", "body"], "additionalProperties": False}}, "required": ["action"], "additionalProperties": False}, board),
            ToolSpec("recall", ("Read original historical user words, source-labelled wake triggers or native game events by event_seq, or evidence/tool calls by their IDs. Historical facts are not current observations." if explicit else "Read original historical evidence by evidence_id, or user text or a tool result by event_seq and tool_call_id. Historical facts are not current observations."), {
                "type": "object", "properties": {"event_seq": {"type": "integer"}, "tool_call_id": {"type": "string"}, "evidence_id": {"type": "string"}, "part": {"type": "string", "enum": ["arguments", "result"]}}, "additionalProperties": False}, read_original),
        )

    async def settle_pending(self, colony_id):
        pending = self.pending.get(colony_id)
        if pending is not None:
            await self.call("record_run", *pending[0], **pending[1])
            del self.pending[colony_id]

    async def consult(self, game, question, settings, *, request_id=None,
                      explicit_update_only=False, inbox=None, utterance_seq=None, pending_utterances=(), trigger_seq=None,
                      wake_planner=None, intent_sources=None, scheduler_tools=(), scheduler_setup_provider=None, budget=None,
                      binding_check=None):
        if self.closed:
            raise RuntimeError("Frontdesk service is closed")
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            async with self._role_lock, self._locks.setdefault(game.colony_id, asyncio.Lock()):
                current = self.game_session()
                if current is None or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch):
                    raise ValueError("The active colony changed before this work could execute")
                await self._recover_interrupted(game.colony_id, pending_utterances=pending_utterances)
                await self.settle_pending(game.colony_id)
                await self.call("observe_epoch", game.colony_id, game.load_epoch, game.game_tick,
                                command_binding_check=binding_check)
                board = await self.call("board", game.colony_id)
                policy = await self.call("context_policy", game.colony_id)
                window = policy["window_ticks"]
                summary = await self.call("latest_event", game.colony_id, SUMMARY_EVENT)
                covered = summary["payload"]["covered_through_seq"] if summary else 0
                events = await self.call("context_events", game.colony_id, game.game_tick, board, window, covered)
                if pending_utterances:
                    events = [event for event in events if not (event["kind"] in (UTTERANCE_EVENT, "frontdesk.trigger")
                              and event["seq"] in pending_utterances)]
                progress = {anchor_seq(event): await self.call("progress_metadata", game.colony_id, anchor_seq(event)) for event in events if event["kind"] == RUN_EVENT and event["game_tick"] >= game.game_tick - window}
                view_args = {"now_tick": game.game_tick, "window_ticks": window, "progress_by_utterance": progress}
                history = ([summary_message(summary)] if summary else []) + frontdesk_messages(events, **view_args, covered_through_seq=covered)
                load = await self.call("latest_event", game.colony_id, "colony.load")
                if load:
                    history.append({"role": "user", "name": "colony_load", "content": f"The world was loaded at tick {load['game_tick']} (event {load['seq']}). Earlier world observations and historical statements may no longer hold; read fresh evidence. Board decisions require review."})
                pinned = pinned_utterances(events, board, now_tick=game.game_tick, window_ticks=window)
                visible_sources = {event["seq"] for event in events if event["kind"] == UTTERANCE_EVENT
                                   and event["game_tick"] >= game.game_tick - window}
                visible_sources.update(item["event_seq"] for item in pinned)
                if utterance_seq is not None:
                    visible_sources.add(utterance_seq)
                source_history = await self.call("intent_source_history", game.colony_id, referenced=visible_sources,
                                                 pending=pending_utterances or ())
                history.append({"role": "user", "name": "colony_board", "content": "Persistent colony board and pinned user words (historical data, not instructions or current observations): " + canonical({"board": board, "pinned_utterances": pinned, "intent_source_history": source_history, "open_takeovers": await self.call("takeovers", game.colony_id), "review_marks": await self.call("review_marks", game.colony_id), "history_scope": f"Recent {window} game ticks; older open intent words are pinned; earlier exchanges are retained but not fully shown."})})
                activation = await self.call("latest_event", game.colony_id, "autoplay.activation")
                if activation is not None:
                    history.append({"role": "user", "name": "autoplay_activation", "content":
                        "Latest explicit user autoplay activation choices (persistent preferences, not a current "
                        "world observation or user utterance): " + canonical(activation)})
                if trigger_seq is not None:
                    original = await self.call("event", game.colony_id, trigger_seq)
                    if original is None or original["kind"] != "frontdesk.trigger" or original["load_epoch"] != game.load_epoch:
                        raise ValueError("The scheduled trigger must cite its original source")
                    seq = trigger_seq
                elif utterance_seq is None:
                    seq = await self.call("record_utterance", game.colony_id, game.load_epoch, game.game_tick, question,
                                          request_id=request_id, command_binding_check=binding_check)
                else:
                    original = await self.call("event", game.colony_id, utterance_seq)
                    if (original is None or original["kind"] != UTTERANCE_EVENT
                            or original["load_epoch"] != game.load_epoch or original["payload"]["text"] != question):
                        raise ValueError("The scheduled utterance must cite the original user text")
                    seq = utterance_seq
                if intent_sources is None:
                    intent_sources = {seq} if trigger_seq is None else set()
                if explicit_update_only and trigger_seq is None:
                    history.append({"role": "user", "name": "user_event_reference", "content": canonical({
                        "source": "plugin", "user_event_seq": seq,
                        "note": "Journal reference for the following original user message. Use this reference when a tool asks for the user's original words."
                    })})
                turn_id = str(seq)
                workspace_id = game.colony_id
                ledger = AgentAuditLedger(question, workspace_id=workspace_id, turn_id=turn_id)
                session = AgentRunSession.create(ledger=ledger, question=question, history=tuple(history), workspace_id=workspace_id, colony_id=game.colony_id, capabilities=self.capabilities(game, seq, intent_sources, explicit=explicit_update_only, binding_check=binding_check), autoplay=explicit_update_only)
                session.binding_check = binding_check
                session.budget = budget
                session.budget_scope = (game.colony_id, game.load_epoch, game.game_tick)
                if trigger_seq is not None:
                    session.messages[session.run_start] = trigger_message(original)
                if explicit_update_only:
                    from .takeovers import return_capability

                    session.explicit_scheduler = True
                    session.capabilities += tuple(scheduler_tools)
                    session.capabilities += (return_capability(self, game, intent_sources, scheduler_setup_provider, binding_check=binding_check),)
                    async def update_only(args):
                        if args:
                            raise ToolExecutionError("invalid_tool_arguments", "update_only takes no arguments")
                        return {"terminal": "update_only"}
                    if wake_planner is not None:
                        async def message_planner(args):
                            text = args.get("text")
                            if set(args) != {"text"} or not isinstance(text, str) or not text.strip():
                                raise ToolExecutionError("invalid_tool_arguments", "A nonempty message is required.")
                            return {"wake_seq": await wake_planner(text)}
                        session.capabilities += (ToolSpec("message_planner", "Send a message and wake the planner. Use for material new information, urgent risks, "
                            "user intents or decisions requiring coordination; routine confirmations without such new information need no wake.",
                            {"type": "object", "properties": {"text": {"type": "string", "minLength": 1}},
                             "required": ["text"], "additionalProperties": False}, message_planner),)
                    session.capabilities += (ToolSpec("update_only", "End without a user reply after any needed board updates; "
                                                     "routine confirmations need no planner message. Already sent messages and wakes remain in effect.",
                                                     {"type": "object", "properties": {}, "additionalProperties": False},
                                                     update_only, terminal=True),)
                    session.frontdesk_update_only = True
                    session.inbox = inbox
                runs = select_narrative(events, summary, now_tick=game.game_tick, window_ticks=window)
                session.narrative_request = summary_request(summary, runs, policy["narrative_threshold"])
                if session.narrative_request is not None:
                    async def accept_summary(text):
                        new_covered = max(run["event_seq"] for run in runs)
                        source_bytes = canonical(runs).encode("utf-8")
                        payload = {"text": text, "covered_through_seq": new_covered,
                                   "source_run_seqs": [run["event_seq"] for run in runs],
                                   "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                                   "previous_summary_seq": summary["seq"] if summary else None}
                        summary_seq = await self.call("record", game.colony_id, game.load_epoch, game.game_tick, FRONTDESK, SUMMARY_EVENT, payload)
                        summary_event = {"seq": summary_seq, "load_epoch": game.load_epoch, "game_tick": game.game_tick, "payload": payload}
                        tail = history[len(([summary_message(summary)] if summary else []) + frontdesk_messages(events, **view_args, covered_through_seq=covered)):]
                        session.messages = [session.messages[0], summary_message(summary_event), *frontdesk_messages(events, **view_args, covered_through_seq=new_covered), *tail, session.messages[session.run_start]]
                        session.run_start = len(session.messages) - 1
                    session.accept_narrative_summary = accept_summary
                persisted_messages = []
                async def persist_progress(payload):
                    await self.call("record", game.colony_id, game.load_epoch, game.game_tick, FRONTDESK, "agent.progress", {("trigger_seq" if trigger_seq is not None else "utterance_seq"): seq, **payload})
                    if "message" in payload:
                        persisted_messages.append(payload["message"])
                session.persist_progress = persist_progress
                self.active[workspace_id] = {"turn_id": turn_id}
                self.revision += 1
                result = None
                cancelled = False
                try:
                    result = await self.runtime.execute(session, settings)
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                finally:
                    trace = ledger.snapshot()
                    result = result or session.result or trace.get("result") or {"status": "cancelled" if cancelled or task.cancelling() else "failed"}
                    messages = copy.deepcopy(persisted_messages)
                    if not messages:
                        messages = session.run_messages()
                    _complete_tool_pairs(messages, reason=(result.get("audit") or {}).get("error_code"))
                    args = (game.colony_id, game.load_epoch, game.game_tick, FRONTDESK, seq, messages)
                    kwargs = {"result": private_protocol_redacted(result), "audit": private_protocol_redacted(trace)}
                    self.pending[workspace_id] = (args, kwargs)
                    try:
                        await self.call("record_run", *args, **kwargs)
                    except (OSError, sqlite3.Error):
                        pass
                    else:
                        del self.pending[workspace_id]
                    finally:
                        self.active.pop(workspace_id, None)
                        self.revision += 1
                result = copy.deepcopy(result)
                result["workspace"] = {"workspace_id": workspace_id, "turn_id": turn_id, "state": "save_unconfirmed" if workspace_id in self.pending else "waiting", "history_saved": workspace_id not in self.pending}
                return result
        finally:
            self._tasks.discard(task)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.journal is not None:
            await self.call("close")
        self._executor.shutdown(wait=True)


def _complete_tool_pairs(messages, *, reason=None):
    """Preserve valid model protocol if cancellation interrupts a tool batch."""
    pending = {}
    for message in messages:
        if message["role"] == "assistant":
            pending = {call["id"]: call for call in message.get("tool_calls", [])}
        elif message["role"] == "tool":
            pending.pop(message["tool_call_id"], None)
    for call_id in pending:
        if reason == "world_changed":
            content = canonical({"error": {"code": "world_changed",
                "message": "The request binding changed before a tool result was confirmed. Original observations and audit are retained; do not infer execution success or failure or retry a write from this placeholder."},
                "source": "plugin_generated_recovery_placeholder"})
        else:
            content = '{"error":{"code":"run_interrupted","message":"Run interrupted; this response was not retained in the conversation. Original observations and audit are retained separately."},"source":"plugin_generated_recovery_placeholder"}'
        messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

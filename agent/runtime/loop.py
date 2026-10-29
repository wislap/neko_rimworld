"""Open, prompt-guided ReAct loop owned entirely by this plugin."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from ...message_contracts import PreparedToolCall
from ...tool_contracts import ToolExecutionError, ToolSetup, ToolSpec
from ..context.situation_context import compile_situation, unavailable_situation
from ..delivery.handoff import (
    attach_agent_handoff,
)
from ..model.model_client import AgentModelSettings, ModelClient, OpenAICompatibleModelClient
from ..model.model_requests import MAX_MODEL_ATTEMPTS, _canonical_hash, request_model
from .audit import (
    AgentAuditCapacityError,
    AgentAuditLedger,
    AgentAuditRegistry,
    AgentRunState,
    private_protocol_redacted,
)
from .execution_environment import ExecutionEnvironment
from .prompt import build_runtime_advisory
from .session import AgentRunSession
from .tools import ToolRegistry


class LoggerLike(Protocol):
    def info(self, message: object, *args: object) -> Any: ...

    def warning(self, message: object, *args: object) -> Any: ...

    def error(self, message: object, *args: object) -> Any: ...


ModelFactory = Callable[[AgentModelSettings], ModelClient]


class AgentRuntime:
    """Execute bounded caller-owned sessions; run() prepares a single question."""

    def __init__(
        self,
        *,
        audit_registry: AgentAuditRegistry,
        logger: LoggerLike,
        model_factory: ModelFactory | None = None,
        tools_provider: Callable[[], Awaitable[ToolSetup]],
        report_sink: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None,
    ) -> None:
        self._tools_provider = tools_provider
        self._report_sink = report_sink
        self._audit_registry = audit_registry
        self._logger = logger
        self._model_factory = model_factory or OpenAICompatibleModelClient
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}

    async def run(
        self,
        question: str,
        settings: AgentModelSettings,
        *,
        capabilities: tuple[ToolSpec, ...] = (),
    ) -> dict[str, Any]:
        session = AgentRunSession.create(
            ledger=AgentAuditLedger(question),
            question=question,
            capabilities=capabilities,
        )
        return await self.execute(session, settings)

    async def execute(
        self,
        session: AgentRunSession,
        settings: AgentModelSettings,
    ) -> dict[str, Any]:
        """Execute a prepared session with the existing tools and delivery.

        The ledger must be fresh (one run per ledger); the session may carry
        prior history messages before its own user message. The caller owns
        context preparation and the lifetime of its work. This execution owns
        model/tool calls, deadlines, cancellation and one result.
        No persisted transcript or experimental module is loaded here.
        """
        ledger = session.ledger
        if ledger.state != AgentRunState.CREATED or session.result is not None or ledger.run_id in self._active_tasks:
            raise ValueError("Execution requires a fresh prepared session")
        question = session.question
        self._audit_registry.start(ledger)
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks[ledger.run_id] = task
        try:
            return await self._run_session(question, settings, ledger, session)
        except AgentAuditCapacityError:
            # The terminal event already occupies the last ledger slot. Saving
            # the result/report must not require another append or transition.
            summary = ledger.summary()
            cancelled = task is not None and task.cancelling() > 0
            result = self._attach_handoff(
                {
                    "status": "cancelled" if cancelled else "degraded",
                    "answer": session.fallback_answer
                    or "The consultation reached its audit capacity before completing.",
                    "caveats": [
                        "The audit event limit was reached; collected observations and any draft were retained."
                    ],
                    "observations": session.observations,
                    "freshness": self._freshness(session.observations),
                    "draft": session.submission.snapshot(),
                    "audit": {
                        "run_id": ledger.run_id,
                        "error_code": "audit_capacity_reached",
                        "cycles": summary["cycle"],
                        "model_calls": summary["model_calls"],
                        "tool_calls": summary["tool_calls"],
                        "duration_ms": summary["duration_ms"],
                    },
                },
                session,
            )
            await self._retain_report(result, question, ledger, session)
            ledger.set_result(result)
            self._logger.warning("[agent] audit capacity reached run_id={}", ledger.run_id)
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            self._active_tasks.pop(ledger.run_id, None)
            self._audit_registry.finish(ledger)

    async def _run_session(
        self,
        question: str,
        settings: AgentModelSettings,
        ledger: AgentAuditLedger,
        session: AgentRunSession,
    ) -> dict[str, Any]:
        try:
            try:
                async with asyncio.timeout(settings.run_budget_seconds):
                    result = await self._run_loop(question, settings, ledger, session)
            except TimeoutError:
                result = self._finish_degraded(
                    ledger,
                    reason="The internal agent reached its absolute run deadline.",
                    error_code="agent_run_budget",
                    observations=session.observations,
                    fallback_answer=session.fallback_answer,
                )
            result["draft"] = session.submission.snapshot()
            result = self._attach_handoff(result, session)
            if result.get("status")=="ok" and session.delivery_issue:
                result["status"]="degraded"
                result.setdefault("caveats",[]).append("The game connection changed during this consultation; retained facts and partial answer are preserved.")
                result.setdefault("audit",{})["error_code"]=session.delivery_issue
            ledger.record("handoff_built", result.get("delivery"))
            await self._retain_report(result, question, ledger, session)
            self._log_handoff(ledger.run_id, result)
            ledger.set_result(result)
            return result
        except asyncio.CancelledError:
            if ledger.state not in {AgentRunState.CANCELLED, AgentRunState.COMPLETED, AgentRunState.FAILED}:
                ledger.transition(AgentRunState.CANCELLED, "run_cancelled")
            cancelled = self._attach_handoff(
                {
                    "status": "cancelled",
                    "answer": session.submission.full_text
                    or "The consultation was cancelled before a complete report was submitted.",
                    "observations": session.observations,
                    "draft": session.submission.snapshot(),
                    "audit": {"run_id": ledger.run_id, "error_code": "agent_cancelled"},
                },
                session,
            )
            await self._retain_report(cancelled, question, ledger, session)
            ledger.set_result(cancelled)
            self._logger.info("[agent] run cancelled run_id={}", ledger.run_id)
            raise
        except AgentAuditCapacityError:
            raise
        except Exception as exception:
            initialization_error = (
                isinstance(exception, ToolExecutionError)
                and exception.code in {"semantic_surface_unavailable", "invalid_catalog"}
                and ledger.summary()["model_calls"] == 0
            )
            self._logger.error(
                "[agent] unexpected run failure run_id={} error_type={}",
                ledger.run_id,
                type(exception).__name__,
            )
            if ledger.state in {AgentRunState.COMPLETED, AgentRunState.CANCELLED, AgentRunState.FAILED}:
                result = {
                    "status": "degraded",
                    "answer": session.submission.full_text
                    or "The internal RimWorld agent ended in an inconsistent terminal state.",
                    "caveats": ["The run audit contains the last valid state transition."],
                    "observations": session.observations,
                    "freshness": None,
                    "audit": {"run_id": ledger.run_id, "error_code": "agent_internal"},
                }
            else:
                result = self._finish_degraded(
                    ledger,
                    reason=str(exception) if initialization_error else "The internal RimWorld agent failed unexpectedly.",
                    error_code=exception.code if initialization_error else "agent_internal",
                    observations=session.observations,
                    fallback_answer=session.fallback_answer,
                    already_degraded=ledger.state == AgentRunState.DEGRADED,
                )
            result["draft"] = session.submission.snapshot()
            result = self._attach_handoff(result, session)
            if result.get("status")=="ok" and session.delivery_issue:
                result["status"]="degraded"
                result.setdefault("caveats",[]).append("The game connection changed during this consultation; retained facts and partial answer are preserved.")
                result.setdefault("audit",{})["error_code"]=session.delivery_issue
            ledger.record("handoff_built", result.get("delivery"))
            await self._retain_report(result, question, ledger, session)
            self._log_handoff(ledger.run_id, result)
            ledger.set_result(result)
            return result

    @staticmethod
    def _attach_handoff(result, session):
        draft = session.submission
        # Reuse only an explicitly authored callback paired with this retained
        # report. Never summarize tool facts or attach a newer candidate's text
        # to an older rendered report on a degraded path.
        if (
            isinstance(result, Mapping) and not result.get("agent_text")
            and result.get("answer") == draft.full_text
            and draft.full_text and draft.agent_text
            and draft.rendered_revision == draft.agent_revision
            and draft.rendered_revision is not None
        ):
            result = {**result, "agent_text": draft.agent_text,
                      "submission_mode": "retained_submission"}
        if session.model_receipt_unconfirmed:
            if session.received_model_turn is not None:
                result = {**result, "received_model_response": private_protocol_redacted(session.received_model_turn.as_audit_value()),
                          "model_response_recording": "unconfirmed"}
            elif session.received_model_error is not None:
                result = {**result, "received_model_error": private_protocol_redacted(session.received_model_error),
                          "model_response_recording": "unconfirmed"}
        if session.write_outcomes:
            result = {**result, "write_outcomes": copy.deepcopy(session.write_outcomes)}
        if session.write_record_issue:
            stage = session.write_record_issue
            if stage == "prepared":
                message = "The write was not sent because its preparation record could not be saved."
            elif stage == "result" and session.write_outcomes and "receipt" in session.write_outcomes[-1]:
                receipt = session.write_outcomes[-1]["receipt"]
                if (session.write_outcomes[-1].get("mode") == "commit"
                        and isinstance(receipt, Mapping) and receipt.get("status") == "committed"):
                    message = "The game confirmed the write was committed, but recording was not confirmed. The known receipt is retained."
                else:
                    message = "The game returned a write receipt, but recording it was not confirmed. The known receipt is retained."
            else:
                message = "The write may have been applied, but its outcome and recording are not confirmed."
            result = {**result, "answer": message, "caveats": [*result.get("caveats", []), message]}
            result.pop("agent_text", None)
            result.pop("delivery", None)
            result.pop("submission_mode", None)
            if result.get("status") != "cancelled":
                result["status"] = "degraded"
            result.setdefault("audit", {})["error_code"] = "write_record_issue"
        if session.planner_terminal or result.get("terminal", {}).get("kind") == "update_only":
            return result
        attached = attach_agent_handoff(result)
        if (
            isinstance(result, Mapping) and result.get("answer") == draft.full_text
            and draft.full_text and draft.rendered_revision == draft.revision
        ):
            attached["delivery"]["claim_warning_count"] = len(draft.claim_diagnostics)
        return attached

    async def cancel_all(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._active_tasks.values() if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _log_handoff(self, run_id: str, result: Mapping[str, Any]) -> None:
        delivery = result.get("delivery")
        projection = delivery if isinstance(delivery, Mapping) else {}
        if projection.get("error_code"):
            self._logger.error(
                "[agent] handoff degraded run_id={} error_code={} error_type={}",
                run_id,
                projection.get("error_code"),
                projection.get("error_type"),
            )
            return
        self._logger.info(
            "[agent] handoff built run_id={} mode={} tokens={}/{}",
            run_id,
            projection.get("submission_mode"),
            projection.get("handoff_tokens"),
            projection.get("token_budget"),
        )

    async def _retain_report(
        self,
        result: dict[str, Any],
        question: str,
        ledger: AgentAuditLedger,
        session: AgentRunSession,
    ) -> None:
        if session.situation_context is not None:
            result["situation_context"] = copy.deepcopy(session.situation_context)
        if session.knowledge or session.knowledge_omitted:
            result["knowledge"] = session.knowledge
            result["knowledge_omitted"] = session.knowledge_omitted
        # The result already contains detached observations. Retain it for the
        # caller even if cancellation is raised, without another large copy.
        # The public audit may deliberately bound large text fields.
        session.result = result
        if session.planner_terminal or self._report_sink is None or not session.submission.full_text:
            return
        record = {
            "report_id": ledger.run_id,
            "source_run_id": ledger.run_id,
            "state": "complete" if result.get("status") == "ok" else "draft",
            "question": question,
            "full_text": session.submission.full_text,
            "agent_text": session.submission.agent_text if result.get("status") == "ok" else None,
            "draft_errors": session.submission.snapshot()["errors"],
            "submission": session.submission.snapshot(),
            "observations": session.observations,
            "knowledge": session.knowledge,
            "knowledge_omitted": session.knowledge_omitted,
            "evidence_views": copy.deepcopy(session.evidence_views),
            "situation_context": copy.deepcopy(session.situation_context),
        }
        if session.workspace_id is not None:
            record["workspace_id"] = session.workspace_id
        try:
            metadata = await asyncio.wait_for(self._report_sink(record), timeout=3.0)
            result["report"] = {**metadata, "saved": True}
        except Exception as error:
            result["report"] = {"report_id": ledger.run_id, "saved": False, "error_code": "report_save_unconfirmed"}
            self._logger.error(
                "[agent] report save unconfirmed run_id={} error_type={}", ledger.run_id, type(error).__name__
            )

    def snapshot(self, *, include_latest_details: bool = True) -> dict[str, Any]:
        return self._audit_registry.snapshot(include_latest_details=include_latest_details)

    async def _bind_situation(self, provider, session):
        try:
            snapshot = await provider.snapshot()
        except ToolExecutionError as error:
            # A busy/failed optional read is isolated. Connection/world changes,
            # invalid admission, cancellation and execution deadlines still abort.
            if not error.recoverable or error.code not in {"query_timeout", "bridge_busy", "unsupported"}:
                raise
            session.ledger.record("situation_source_failed", {"error_code": error.code, "message": str(error)})
            context = unavailable_situation(error.code, str(error))
        else:
            observation = snapshot.observation.to_dict()
            response = observation["response"]
            identity = (response["colony_id"], response["load_epoch"], response.get("connection_epoch"))
            if session.colony_id is not None and response["colony_id"] != session.colony_id:
                raise ToolExecutionError("world_changed", "Situation snapshot belongs to another colony.", recoverable=False)
            session.interaction.sample_identity = identity
            session.observations.append(observation)
            if session.persist_progress is not None:
                await session.persist_progress({"stage": "situation", "observation": observation})
            payload = response["payload"]
            if payload["status"] in {"ok", "partial"}:
                context = compile_situation(snapshot)
            else:
                code = payload.get("error_code") or "situation_unavailable"
                if payload["status"] == "stale_session" or code in {
                    "world_changed", "connection_changed", "invalid_catalog", "game_offline", "protocol_error",
                    "stale_session", "session_stale", "invalid_arguments", "object_unavailable", "unsupported_game_implementation",
                }:
                    raise ToolExecutionError(code, payload.get("message") or code, recoverable=False)
                context = unavailable_situation(code, payload.get("message") or code, observation=observation)
                session.ledger.record("situation_source_failed", {"observation_id": observation["observation_id"], "error_code": code})
        session.situation_context = context
        # The caller's question stays last. Dynamic background never becomes an
        # instruction or an automatically repeated persistent-history attachment.
        session.messages.insert(session.run_start, {
            "role": "user", "name": "game_situation", "content": context["content"],
        })
        session.run_start += 1
        session.ledger.record("situation_context_bound", context)

    async def _run_loop(
        self,
        question: str,
        settings: AgentModelSettings,
        ledger: AgentAuditLedger,
        session: AgentRunSession,
    ) -> dict[str, Any]:
        model = self._model_factory(settings)
        if session.narrative_request is not None:
            messages = session.narrative_request
            requested = await request_model(
                model, settings, ledger, session, messages, [], finalizing=True, reserve=0,
                research_deadline=settings.run_budget_seconds, cycle=0,
                request_sha256=_canonical_hash({"messages": messages, "tools": []}),
                logger=self._logger, max_attempts=MAX_MODEL_ATTEMPTS,
            )
            if requested.error is not None:
                return self._finish_degraded(ledger, reason=str(requested.error), error_code=requested.error.code,
                                             observations=session.observations, fallback_answer=session.fallback_answer,
                                             already_degraded=ledger.state == AgentRunState.DEGRADED)
            turn = requested.turn
            if turn is None or turn.tool_calls or not turn.content.strip():
                return self._finish_degraded(ledger, reason="Narrative summary was empty or invalid.",
                                             error_code="narrative_summary_invalid", observations=session.observations,
                                             fallback_answer=session.fallback_answer)
            ledger.transition(AgentRunState.MODEL_RESPONDED, "model_response_received", turn.as_audit_value())
            await session.accept_narrative_summary(turn.content)
        setup = await self._tools_provider()

        def submission_updated(snapshot, event, details):
            ledger.set_draft(snapshot)
            ledger.record(event, details)

        environment = ExecutionEnvironment(
            setup,
            session,
            submission_updated,
        )
        if setup.situation_provider is not None:
            await self._bind_situation(setup.situation_provider, session)
        reserve = min(settings.finalization_reserve_seconds, settings.run_budget_seconds / 2)
        research_deadline = settings.run_budget_seconds - reserve
        finalizing = False
        while True:
            elapsed = ledger.elapsed_seconds
            remaining = settings.run_budget_seconds - elapsed
            if remaining <= 0:
                return self._finish_degraded(
                    ledger,
                    reason="The internal agent used its available run time and returned the evidence collected so far.",
                    error_code="agent_run_budget",
                    observations=session.observations,
                    fallback_answer=session.fallback_answer,
                )

            cycle = ledger.begin_cycle()
            if reserve > 0 and elapsed >= research_deadline and not finalizing:
                finalizing = True
            if finalizing and not getattr(session, "finalization_started", False):
                session.finalization_started = True
                ledger.record(
                    "research_closed", {"remaining_seconds": remaining, "research_deadline": research_deadline}
                )
                session.interaction.finalizing = True
            tools = environment.registry()
            ledger.record("interaction_state", session.interaction.snapshot())
            advisory = build_runtime_advisory(
                cycle=cycle,
                elapsed_seconds=elapsed,
                remaining_seconds=remaining,
                model_calls=session.model_calls,
                tool_calls=session.tool_calls,
                finalizing=finalizing,
                finalization_reserve_seconds=settings.finalization_reserve_seconds,
                autoplay_role=session.role if session.explicit_scheduler else None,
            )
            if session.inbox is not None:
                session.messages.extend(await session.inbox())
            request_messages = session.request_messages(advisory)
            repair_messages = session.length_repair_messages()
            if repair_messages is not None:
                tools = ToolRegistry()
                tools.register(session.interaction.submission(answer_only=True))
                request_messages = repair_messages
                ledger.record("length_repair_context", {"game_evidence_replayed": False, "saved_detail_changed": False})
            tool_schemas = tools.schemas()
            request_sha256 = _canonical_hash({"messages": request_messages, "tools": tool_schemas})
            requested = await request_model(
                model,
                settings,
                ledger,
                session,
                request_messages,
                tool_schemas,
                finalizing=finalizing,
                reserve=reserve,
                research_deadline=research_deadline,
                cycle=cycle,
                request_sha256=request_sha256,
                logger=self._logger,
                max_attempts=MAX_MODEL_ATTEMPTS,
            )
            finalizing = requested.finalizing
            if requested.error is not None:
                return self._finish_degraded(
                    ledger,
                    reason=str(requested.error),
                    error_code=requested.error.code,
                    observations=session.observations,
                    fallback_answer=session.fallback_answer,
                    already_degraded=ledger.state == AgentRunState.DEGRADED,
                )
            turn = requested.turn

            if turn is None:
                continue
            ledger.transition(
                AgentRunState.MODEL_RESPONDED,
                "model_response_received",
                turn.as_audit_value(),
            )
            session.append_assistant(turn)
            if session.persist_progress is not None:
                await session.persist_progress({"stage": "assistant", "message": turn.as_message()})

            if turn.tool_calls:
                ledger.transition(
                    AgentRunState.TOOL_DISPATCHING,
                    "tool_batch_started",
                    {"count": len(turn.tool_calls)},
                )
                tool_messages: list[dict[str, Any]] = []
                submitted_response: Mapping[str, Any] | None = None
                terminal_name = None
                isolated_batch = len(turn.tool_calls) > 1 and any(tools.is_exclusive(c.name) for c in turn.tool_calls)
                for call in turn.tool_calls:
                    session.tool_calls += 1
                    ledger.record(
                        "tool_call_requested",
                        {
                            "call_id": call.call_id,
                            "name": call.name,
                            "raw_arguments": call.raw_arguments,
                        },
                    )
                    prepared = tools.prepare(call)
                    if isolated_batch or len(turn.tool_calls) > 1 and tools.is_terminal(call.name):
                        prepared = tools.error_result(
                            call,
                            "terminal_tool_must_be_alone",
                            "This state-changing or final tool must be called alone, in a separate tool batch.",
                            recoverable=True,
                        )
                    if not isinstance(prepared, PreparedToolCall):
                        result = prepared
                        ledger.record(
                            "tool_failed",
                            {
                                "call_id": result.call_id,
                                "name": result.name,
                                "result": result.value,
                            },
                        )
                    else:
                        ledger.record(
                            "tool_arguments_validated",
                            {
                                "call_id": call.call_id,
                                "name": call.name,
                                "arguments": prepared.arguments,
                            },
                        )
                        ledger.record("tool_started", {"call_id": call.call_id, "name": call.name})
                        tool_available = (
                            research_deadline
                            if reserve > 0 and not tools.is_terminal(call.name)
                            else settings.run_budget_seconds
                        ) - ledger.elapsed_seconds
                        try:
                            if tool_available <= 0:
                                raise TimeoutError()
                            async with asyncio.timeout(tool_available):
                                result = await tools.execute(prepared)
                        except TimeoutError:
                            result = tools.error_result(
                                call,
                                "research_deadline",
                                "Research time ended; this read did not complete. Finish with obtained evidence and identify unknowns.",
                                recoverable=False,
                            )
                            finalizing = reserve > 0
                        if session.write_record_issue:
                            return self._finish_degraded(
                                ledger, reason="Write recording failed; this run stopped before any further action.",
                                error_code="write_record_issue", observations=session.observations,
                            )
                        if result.observation is not None:
                            if (
                                session.colony_id is not None
                                and result.observation["response"]["colony_id"] != session.colony_id
                            ):
                                result = tools.error_result(
                                    call, "world_changed", "The active colony changed.", recoverable=False
                                )
                            else:
                                session.observations.append(result.observation)
                        session.retain_knowledge(result)
                        ledger.record(
                            "tool_completed" if result.ok else "tool_failed",
                            {
                                "call_id": result.call_id,
                                "name": result.name,
                                "duration_ms": result.duration_ms,
                                "result": result.value,
                            },
                        )
                    if not result.ok and result.value.get("error", {}).get("code") == "world_changed":
                        session.invalidated_error = "world_changed"
                    if not result.ok and result.value.get("error", {}).get("code") == "connection_changed":
                        session.delivery_issue = "connection_changed"
                    if session.invalidated_error:
                        ledger.record("world_observations_invalidated", {"error_code": session.invalidated_error})
                        return self._finish_degraded(
                            ledger,
                            reason="The loaded world changed. Observations from this run are stale; read the world again.",
                            error_code=session.invalidated_error,
                            observations=session.observations,
                            fallback_answer="The loaded world changed; observations from this run are stale and must be read again.",
                        )
                    tool_message = session.tool_message(result)
                    if session.persist_progress is not None:
                        await session.persist_progress({"stage": "tool", "message": tool_message, "tool_call_id": result.call_id, "tool_result": result.value, "observation": result.observation})
                    tool_messages.append(tool_message)
                    if session.evidence_views and session.evidence_views[-1]["call_id"] == result.call_id:
                        ledger.record("model_evidence_compiled", session.evidence_views[-1])
                    if result.terminal and result.ok:
                        data_value = result.value.get("data")
                        if isinstance(data_value, Mapping) and submitted_response is None:
                            submitted_response = data_value
                            terminal_name = result.name
                for message in tool_messages:
                    session.append_tool_message(message)
                ledger.transition(
                    AgentRunState.TOOL_RESULTS_READY,
                    "tool_batch_completed",
                    {"count": len(tool_messages)},
                )
                if submitted_response is not None:
                    if session.planner_terminal:
                        handoff = submitted_response["handoff"]
                        result = self._finish_success(ledger, answer=handoff, agent_text=handoff,
                            submission_mode="yield", observations=session.observations, delivery={})
                        result["terminal"] = {"kind": "yield", "handoff": handoff}
                        return result
                    if session.frontdesk_update_only and terminal_name == "update_only":
                        result = self._finish_success(ledger, answer="", agent_text="",
                            submission_mode="update_only", observations=session.observations, delivery={})
                        result["terminal"] = {"kind": "update_only"}
                        return result
                    result = self._complete_submission(ledger, session, submitted_response)
                    if session.frontdesk_update_only:
                        result["terminal"] = {"kind": "reply"}
                    return result
                continue

            if turn.content:
                session.append_recovery(
                    "Finish with planner_yield and a handoff." if session.planner_terminal else
                    "The consultation is not submitted. Continue any necessary research, then finish through submit_response. Ordinary assistant text does not complete this request."
                )
                ledger.record("explicit_submission_required", {"finish_reason": turn.finish_reason})
                continue

            recovery = (
                "Your previous response contained neither final text nor a valid tool call. "
                "Continue the task, using the available tool only if it is materially useful."
            )
            session.append_recovery(recovery)
            ledger.record("recovery_prompt_appended", {"message": recovery})

    def _complete_submission(self, ledger, session, submission):
        """Commit the accepted text submission to the terminal result."""
        session.submission.full_text = submission["full_text"]
        session.submission.agent_text = submission["agent_text"]
        return self._finish_success(
            ledger,
            answer=session.submission.full_text,
            agent_text=session.submission.agent_text,
            submission_mode="submit_response",
            observations=session.observations,
            delivery=submission["delivery"],
        )


    def _finish_success(
        self,
        ledger: AgentAuditLedger,
        *,
        answer: str,
        agent_text: str,
        submission_mode: str,
        observations: list[dict[str, Any]],
        delivery: Mapping[str, Any],
    ) -> dict[str, Any]:
        ledger.transition(
            AgentRunState.FINALIZING,
            "final_received",
            {
                "answer": answer,
                "agent_text": agent_text,
                "submission_mode": submission_mode,
                "answer_sha256": hashlib.sha256(answer.encode("utf-8", "replace")).hexdigest(),
                "agent_text_sha256": hashlib.sha256(agent_text.encode("utf-8", "replace")).hexdigest(),
            },
        )
        ledger.transition(AgentRunState.COMPLETED, "run_completed")
        summary = ledger.summary()
        return {
            "status": "ok",
            "answer": answer,
            "agent_text": agent_text,
            "submission_mode": submission_mode,
            "delivery": dict(delivery),
            "caveats": [],
            "observations": observations,
            "freshness": self._freshness(observations),
            "audit": {
                "run_id": ledger.run_id,
                "cycles": summary["cycle"],
                "model_calls": summary["model_calls"],
                "tool_calls": summary["tool_calls"],
                "duration_ms": summary["duration_ms"],
            },
        }

    def _finish_degraded(
        self,
        ledger: AgentAuditLedger,
        *,
        reason: str,
        error_code: str,
        observations: list[dict[str, Any]],
        fallback_answer: str = "",
        already_degraded: bool = False,
    ) -> dict[str, Any]:
        if not already_degraded:
            ledger.transition(
                AgentRunState.DEGRADED,
                "run_degraded",
                {"error_code": error_code, "reason": reason},
            )
        answer = fallback_answer.strip() or "The internal RimWorld agent could not complete a normal answer."
        ledger.transition(
            AgentRunState.FINALIZING,
            "degraded_final_created",
            {"answer": answer, "error_code": error_code},
        )
        ledger.transition(AgentRunState.COMPLETED, "run_completed", {"status": "degraded"})
        summary = ledger.summary()
        return {
            "status": "degraded",
            "answer": answer,
            "caveats": [reason],
            "observations": observations,
            "freshness": self._freshness(observations),
            "audit": {
                "run_id": ledger.run_id,
                "cycles": summary["cycle"],
                "model_calls": summary["model_calls"],
                "tool_calls": summary["tool_calls"],
                "duration_ms": summary["duration_ms"],
                "error_code": error_code,
            },
        }

    @staticmethod
    def _freshness(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not observations:
            return None
        last = observations[-1]
        response = last["response"]
        return {
            "received_at": last["received_at"],
            "colony_id": response["colony_id"],
            "load_epoch": response["load_epoch"],
            "snapshot_sequence": response["snapshot_sequence"],
            "game_tick": response["game_tick"],
        }


__all__ = ["AgentRuntime"]

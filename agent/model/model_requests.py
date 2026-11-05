"""Model attempt admission, retries and phase expiry; no tool vocabulary."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass

from ...game.state import redact_text
from ...media import ImageInputConfig, MediaError, prepare_model_messages
from ...message_contracts import AssistantTurn
from ...tool_contracts import binding_is_current
from ..runtime.audit import AgentRunState
from .model_client import ModelClientError
from .usage import input_reservation

MAX_MODEL_ATTEMPTS = 3
MODEL_RETRY_DELAYS_SECONDS = (0.5, 1.5)
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_RETRYABLE_PROVIDER_MARKERS = (
    "overload",
    "rate_limit",
    "rate limit",
    "server_error",
    "temporar",
    "timeout",
    "try again",
    "unavailable",
    "upstream",
)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _model_retry_reason(error: ModelClientError) -> str | None:
    diagnostics = error.diagnostics
    if diagnostics.get("first_useful_delta_ms") is not None:
        return None
    if error.code in {"model_transport", "model_transport_timeout", "model_first_response_timeout"}:
        return error.code
    if error.code == "model_incomplete_stream":
        return error.code
    if error.code == "model_http_error" and diagnostics.get("http_status") in _RETRYABLE_HTTP_STATUSES:
        return f"http_{diagnostics['http_status']}"
    if error.code != "model_stream_error":
        return None
    provider_error = diagnostics.get("provider_error")
    provider = provider_error if isinstance(provider_error, Mapping) else {}
    marker_text = " ".join(str(provider.get(key) or "").lower() for key in ("code", "type", "message"))
    return "provider_transient" if any(marker in marker_text for marker in _RETRYABLE_PROVIDER_MARKERS) else None


@dataclass(frozen=True, slots=True)
class ModelRequestResult:
    turn: AssistantTurn | None
    finalizing: bool
    error: ModelClientError | None = None


async def request_model(
    model,
    settings,
    ledger,
    session,
    request_messages,
    tool_schemas,
    *,
    finalizing,
    reserve,
    research_deadline,
    cycle,
    request_sha256,
    logger,
    max_attempts=MAX_MODEL_ATTEMPTS,
):
    attempt = 1
    turn = None
    while True:
        if not binding_is_current(session.binding_check):
            return ModelRequestResult(None, finalizing, ModelClientError(
                "world_changed", "The active colony connection changed before model dispatch."))
        attempt_remaining = settings.run_budget_seconds - ledger.elapsed_seconds
        available = attempt_remaining if finalizing or reserve == 0 else research_deadline - ledger.elapsed_seconds
        if available <= 0 and not finalizing and reserve > 0:
            finalizing = True
            break
        if session.model_calls >= settings.max_model_calls:
            return ModelRequestResult(None, finalizing, ModelClientError("agent_request_budget", "The model request limit was reached."))
        messages = request_messages
        if session.budget is not None:
            try:
                state = await session.budget.status(session.budget_scope)
            except BaseException:
                session.budget.blocked = True
                raise
            messages = [*request_messages, {"role": "system", "content":
                "Cumulative model budget (local estimates and reservations, not a provider bill): "
                + json.dumps(state, ensure_ascii=False, sort_keys=True)}]
        try:
            wire_messages, attachments, image_tokens = prepare_model_messages(
                messages, getattr(session, "media_store", None), getattr(session, "image_input", ImageInputConfig()))
        except MediaError as error:
            return ModelRequestResult(None, finalizing, ModelClientError(error.code, str(error)))
        request_sha256 = _canonical_hash({"messages": wire_messages, "tools": tool_schemas})
        attempt_id = uuid.uuid4().hex
        encoded = json.dumps({"messages": messages, "tools": tool_schemas}, ensure_ascii=False, separators=(",", ":"), default=str)
        allocation = input_reservation(encoded, image_tokens)
        text_estimate, estimate = allocation["text_tokens"], allocation["tokens"]
        if session.input_tokens_estimated + estimate > settings.max_input_tokens:
            return ModelRequestResult(None, finalizing, ModelClientError("agent_input_budget", "The local cumulative input token estimate would exceed its limit."))
        request_timeout = min(
            settings.request_timeout_seconds,
            max(0.001, available),
        )
        request_event = {
            "input_budget": {"tokens": estimate, "cumulative": session.input_tokens_estimated + estimate, "method": allocation["method"]},
            "model": settings.model,
            "base_url": settings.base_url,
            "timeout_seconds": request_timeout,
            "messages": messages,
            "attempt_id": attempt_id,
            "dispatch_state": "prepared_not_dispatched",
            "tools": tool_schemas,
            "request_sha256": request_sha256,
            "attempt": attempt,
            "max_attempts": max_attempts,
        }
        if attachments:
            request_event["attachments"] = attachments
            request_event["input_budget"].update(text_tokens=text_estimate, image_tokens_reserved=image_tokens,
                                                  image_reservation_method="explicit_per_image")
            # Pin the original bytes before the persisted request can refer to them.
            session.media_store.retain("request:" + attempt_id, attachments)
        persist = getattr(session, "persist_progress", None)
        if persist is not None:
            # Preserve each actual attempt before dispatch, including retries.
            # A failed journal write must not leave an unrecorded model request.
            try:
                await persist({"stage": "model_request", "model_request": copy.deepcopy({
                    key: value for key, value in request_event.items() if key != "base_url"
                })})
            except BaseException:
                if session.budget is not None:
                    session.budget.blocked = True
                raise
        budget = session.budget
        reservation = None
        if budget is not None:
            reservation = await budget.reserve(session.role, allocation["conservative_tokens"],
                settings.max_output_tokens, scope=session.budget_scope, attempt_id=attempt_id,
                metadata={"run_id": ledger.run_id, "cycle": cycle, "attempt": attempt,
                          "request_sha256": request_sha256})
            if reservation is None:
                return ModelRequestResult(None, finalizing, ModelClientError(
                    "agent_cumulative_budget", "The cumulative model budget cannot admit another request."))
        if persist is not None and budget is not None:
            try:
                await persist({"stage": "model_request_admitted", "attempt_id": attempt_id,
                               "dispatch_state": "admitted_not_dispatched"})
            except BaseException:
                budget.blocked = True
                await asyncio.shield(budget.settle(reservation, {}, outcome="not_dispatched"))
                raise
        session.received_model_turn = None
        session.response_audit_pending = False
        session.received_model_error = None
        turn = None
        settlement_started = False
        dispatched = False
        settled_without_dispatch = False
        admission_pending = False
        try:
            request_scope = getattr(model, "request_scope", None)
            async with request_scope() if callable(request_scope) else nullcontext():
                if budget is not None:
                    admission_pending = True
                    admitted = await budget.begin_dispatch(reservation)
                    admission_pending = False
                    if not admitted:
                        settled_without_dispatch = True
                        return ModelRequestResult(None, finalizing, ModelClientError(
                            "agent_cumulative_budget", "The cumulative model budget cannot admit another request."))
                if not binding_is_current(session.binding_check):
                    raise ModelClientError("world_changed", "The active colony connection changed before model dispatch.")
                if attempt == 1 and ledger.state != AgentRunState.MODEL_REQUESTING:
                    ledger.transition(AgentRunState.MODEL_REQUESTING, "model_request_started", request_event)
                else:
                    ledger.record("model_request_started", request_event)
                session.input_tokens_estimated += estimate
                session.model_calls += 1
                if attachments:
                    session.image_tokens_reserved += image_tokens
                session.last_request_media = copy.deepcopy(attachments)
                dispatched = True
                async with asyncio.timeout(request_timeout):
                    turn = await model.complete(wire_messages, tool_schemas, timeout=request_timeout)
                    session.received_model_turn = turn
                    session.response_audit_pending = True
                    session.model_receipt_unconfirmed = True
            if budget is not None and not settled_without_dispatch and not admission_pending:
                settlement_started = True
                await budget.settle(reservation, turn.usage,
                                    response={"audit": turn.as_audit_value()})
                session.model_receipt_unconfirmed = False
            elif budget is None:
                session.model_receipt_unconfirmed = False
            break
        except asyncio.CancelledError:
            if budget is not None and not settled_without_dispatch and not admission_pending:
                if turn is None:
                    await asyncio.shield(budget.settle(reservation, getattr(model, "last_usage", {}) if dispatched else {},
                                                       outcome="attempted" if dispatched else "not_dispatched"))
                elif not settlement_started:
                    # A response can arrive before an asynchronous request scope
                    # finishes. Retain it even if cancellation interrupts scope exit.
                    session.received_model_turn = turn
                    session.model_receipt_unconfirmed = True
                    settlement_started = True
                    await asyncio.shield(budget.settle(reservation, turn.usage,
                                                        response={"audit": turn.as_audit_value()}))
                    session.model_receipt_unconfirmed = False
            ledger.record(
                "model_request_cancelled",
                {
                    "attempt": attempt,
                    "diagnostics": getattr(model, "last_diagnostics", {}),
                    "usage": getattr(model, "last_usage", {}),
                },
            )
            raise
        except (ModelClientError, TimeoutError) as exception:
            received_scope_failure = turn is not None
            if isinstance(exception, TimeoutError):
                exception = ModelClientError(
                    "model_request_deadline",
                    "The model request reached its phase deadline.",
                    diagnostics=getattr(model, "last_diagnostics", {}),
                    usage=getattr(model, "last_usage", {}),
                )
            if budget is not None and not settled_without_dispatch and not admission_pending:
                session.received_model_error = {"code": exception.code, "message": str(exception),
                                                "diagnostics": copy.deepcopy(exception.diagnostics),
                                                "usage": copy.deepcopy(exception.usage)}
                session.model_receipt_unconfirmed = True
                if turn is not None:
                    session.received_model_turn = turn
                    await budget.settle(reservation, turn.usage,
                                        response={"audit": turn.as_audit_value(), "error": session.received_model_error})
                else:
                    await budget.settle(reservation, exception.usage if dispatched else {},
                                        response={"error": session.received_model_error},
                                        outcome="attempted" if dispatched else "not_dispatched")
                session.model_receipt_unconfirmed = False
            if received_scope_failure:
                # The response is retained for audit but cannot be returned to
                # the model loop after the request scope failed.
                turn = None
            if not received_scope_failure and reserve > 0 and not finalizing and ledger.elapsed_seconds >= research_deadline:
                ledger.record(
                    "research_request_expired",
                    {"error_code": exception.code, "diagnostics": exception.diagnostics, "usage": exception.usage},
                )
                finalizing = True
                break
            retry_reason = None if received_scope_failure else _model_retry_reason(exception)
            retry_delay = MODEL_RETRY_DELAYS_SECONDS[min(attempt - 1, len(MODEL_RETRY_DELAYS_SECONDS) - 1)]
            remaining_after_failure = settings.run_budget_seconds - ledger.elapsed_seconds
            retry_available = (
                remaining_after_failure if finalizing or reserve == 0 else research_deadline - ledger.elapsed_seconds
            )
            retry_budget_floor = 3.0
            will_retry = (
                retry_reason is not None
                and attempt < max_attempts
                and retry_available > retry_delay + retry_budget_floor
            )
            failure = {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "error_code": exception.code,
                "message": str(exception),
                "diagnostics": exception.diagnostics,
                "usage": exception.usage,
                "retryable": retry_reason is not None,
                "retry_reason": retry_reason,
                "will_retry": will_retry,
            }
            provider_error = exception.diagnostics.get("provider_error")
            provider = provider_error if isinstance(provider_error, Mapping) else {}
            log_method = logger.warning if will_retry else logger.error
            log_method(
                "[agent] model request {} run_id={} cycle={} attempt={}/{} error_code={} request_id={} "
                "phase={} provider_code={} provider_type={} provider_param={} provider_message={}",
                "retry scheduled" if will_retry else "failed",
                ledger.run_id,
                cycle,
                attempt,
                max_attempts,
                exception.code,
                redact_text(exception.diagnostics.get("request_id"), 128),
                redact_text(exception.diagnostics.get("phase"), 64),
                redact_text(provider.get("code"), 128),
                redact_text(provider.get("type"), 128),
                redact_text(provider.get("param"), 128),
                redact_text(provider.get("message"), 512),
            )
            if will_retry:
                ledger.record("model_request_attempt_failed", failure)
                ledger.record(
                    "model_retry_scheduled",
                    {
                        "failed_attempt": attempt,
                        "next_attempt": attempt + 1,
                        "delay_seconds": retry_delay,
                        "retry_reason": retry_reason,
                        "remaining_seconds": round(remaining_after_failure, 3),
                    },
                )
                await asyncio.sleep(retry_delay)
                attempt += 1
                continue
            if (
                retry_reason is not None
                and attempt < max_attempts
                and reserve > 0
                and not finalizing
                and remaining_after_failure > 0
            ):
                ledger.record("research_request_failed", failure)
                finalizing = True
                break
            ledger.transition(AgentRunState.DEGRADED, "model_request_failed", failure)
            return ModelRequestResult(None, finalizing, error=exception)
        except Exception:
            if budget is not None and not settlement_started and not settled_without_dispatch and not admission_pending:
                if turn is not None:
                    settlement_started = True
                    await asyncio.shield(budget.settle(reservation, turn.usage,
                                                        response={"audit": turn.as_audit_value()}))
                    session.model_receipt_unconfirmed = False
                elif not dispatched:
                    await asyncio.shield(budget.settle(reservation, {}, outcome="not_dispatched"))
            raise

    return ModelRequestResult(turn, finalizing)

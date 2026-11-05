"""Async, session-bound write access through the existing authenticated bridge."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import threading
from collections.abc import Mapping
from typing import Any

from ..tool_contracts import ReceiptLost, ToolExecutionError
from .bridge import BridgeEventGap, BridgeQueryTimeout, BridgeServer
from .protocol import SessionDescriptor


class BridgeGameWritePort:
    """A port bound to the connection and loaded colony selected by its caller.

    The bridge checks this binding under its connection lock when a worker starts,
    and checks it again before sending. An ambiguous failure is never retried here.
    """

    def __init__(self, bridge: BridgeServer, connection: str, session: SessionDescriptor) -> None:
        if not connection or session is None:
            raise ValueError("An active connection and session are required.")
        self._bridge = bridge
        self._connection = connection
        self._session = session
        self._capabilities = frozenset(getattr(bridge, "capabilities", ()))
        self._events_subscribed = False

    @property
    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    async def _call(self, method: str, *args: Any, audit=None, **arguments: Any) -> Mapping[str, Any]:
        cancel_event = threading.Event()
        loop = asyncio.get_running_loop()
        def dispatch(request_id, timeout, cancellation):
            if audit is None:
                return
            future = asyncio.run_coroutine_threadsafe(audit("queued", request_id, None), loop)
            try:
                future.result(timeout=timeout)
            except concurrent.futures.TimeoutError as exc:
                future.cancel()
                raise ToolExecutionError("write_audit_failed", "Write audit timed out before dispatch.", recoverable=False) from exc
        try:
            result = await asyncio.to_thread(
                getattr(self._bridge, method), *args,
                expected_connection=self._connection, expected_session=self._session, cancel_event=cancel_event,
                **arguments,
                **({"audit_dispatch": dispatch} if audit is not None else {}),
            )
        except asyncio.CancelledError:
            # The bridge removes requests that have not crossed the dispatch boundary.
            # Once dispatched, cancellation cannot establish the write outcome.
            self._bridge.cancel_write(cancel_event)
            raise
        if audit is not None:
            await audit("envelope", result.request_id, result.to_dict())
        payload = result.payload
        if payload.status != "ok":
            raise ToolExecutionError(payload.error_code or payload.status or "write_failed", payload.message or "Game write failed.")
        return copy.deepcopy(payload.data)

    async def dry_run(self, changeset: Mapping[str, Any], *, audit=None) -> Mapping[str, Any]:
        return await self._changeset("dry_run", changeset, audit=audit)

    async def commit(self, changeset: Mapping[str, Any], *, audit=None) -> Mapping[str, Any]:
        return await self._changeset("commit", changeset, audit=audit)

    async def _changeset(self, method: str, changeset: Mapping[str, Any], *, audit=None) -> Mapping[str, Any]:
        body = copy.deepcopy(dict(changeset))
        if body.get("load_epoch") != self._session.load_epoch:
            raise ToolExecutionError("stale_epoch", "Changeset does not belong to the bound game session.")
        try:
            return await self._call(method, body, audit=audit)
        except BridgeQueryTimeout as exc:
            if method != 'commit':
                raise
            raise ReceiptLost(body['idempotency_key']) from exc

    async def undo(self, token: str, *, audit=None) -> Mapping[str, Any]:
        return await self._call("undo", token, audit=audit)

    async def set_paused(self, paused: bool, *, role: str, expected_owner: str | None = None) -> Mapping[str, Any]:
        if role == "policy" and (not paused or "colony_pause.policy.v1" not in self._capabilities):
            raise ToolExecutionError("unsupported", "Policy pause is unavailable.")
        if expected_owner is not None:
            if "colony_pause.resume_guard.v1" not in self._capabilities:
                raise ToolExecutionError("unsupported", "Guarded pause recovery is unavailable.")
            return await self._call("set_paused", paused, role, expected_owner=expected_owner)
        return await self._call("set_paused", paused, role)

    async def events(self, since: int = 0) -> list[Mapping[str, Any]]:
        if "colony_events.v1" not in self._capabilities:
            raise ToolExecutionError("unsupported", "Game event delivery is not available.")
        if not self._events_subscribed:
            raise ToolExecutionError("events_not_subscribed", "Enable event delivery with push_policy first.")
        try:
            return self._bridge.game_events(since, expected_connection=self._connection, expected_session=self._session)
        except BridgeEventGap as exc:
            raise ToolExecutionError("event_gap", str(exc), recoverable=False, details=exc.details) from exc

    async def push_policy(self, policy: Mapping[str, Any]) -> Mapping[str, Any]:
        if "colony_pause.v1" not in self._capabilities:
            raise ToolExecutionError("unsupported", "Game push policies are not available.")
        result = await self._call("push_policy", copy.deepcopy(dict(policy)))
        self._events_subscribed = True
        return result

    async def push_observations(self, policy: Mapping[str, Any]) -> Mapping[str, Any]:
        """Replace this load's selected native observations and return coverage ACKs."""
        if "colony_observation.v1" not in self._capabilities:
            raise ToolExecutionError("unsupported", "Native business observation is not available.")
        body = copy.deepcopy(dict(policy))
        from .observation_contract import validate_observation_request

        validate_observation_request(body)
        result = await self._call("push_policy", body)
        self._events_subscribed = True
        return result

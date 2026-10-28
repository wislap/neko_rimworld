"""Async, session-bound write access through the existing authenticated bridge."""

from __future__ import annotations

import asyncio
import copy
import threading
from collections.abc import Mapping
from typing import Any

from ..tool_contracts import ToolExecutionError
from .bridge import BridgeServer
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

    async def _call(self, method: str, *args: Any) -> Mapping[str, Any]:
        cancel_event = threading.Event()
        try:
            result = await asyncio.to_thread(
                getattr(self._bridge, method), *args,
                expected_connection=self._connection, expected_session=self._session, cancel_event=cancel_event,
            )
        except asyncio.CancelledError:
            # The bridge removes requests that have not crossed the dispatch boundary.
            # Once dispatched, cancellation cannot establish the write outcome.
            self._bridge.cancel_write(cancel_event)
            raise
        payload = result.payload
        if payload.status != "ok":
            raise ToolExecutionError(payload.error_code or payload.status or "write_failed", payload.message or "Game write failed.")
        return copy.deepcopy(payload.data)

    async def dry_run(self, changeset: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._changeset("dry_run", changeset)

    async def commit(self, changeset: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._changeset("commit", changeset)

    async def _changeset(self, method: str, changeset: Mapping[str, Any]) -> Mapping[str, Any]:
        body = copy.deepcopy(dict(changeset))
        if body.get("load_epoch") != self._session.load_epoch:
            raise ToolExecutionError("stale_epoch", "Changeset does not belong to the bound game session.")
        return await self._call(method, body)

    async def undo(self, token: str) -> Mapping[str, Any]:
        return await self._call("undo", token)

    async def set_paused(self, paused: bool, *, role: str) -> Mapping[str, Any]:
        return await self._call("set_paused", paused, role)

    async def events(self, since: int = 0) -> list[Mapping[str, Any]]:
        raise ToolExecutionError("unsupported", "Game event delivery is not available.")

    async def push_policy(self, policy: Mapping[str, Any]) -> Mapping[str, Any]:
        raise ToolExecutionError("unsupported", "Game push policies are not available.")

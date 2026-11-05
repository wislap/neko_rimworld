"""Cumulative model-attempt accounting backed by the append-only event journal."""
from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass

from ..message_contracts import private_protocol_redacted


@dataclass(frozen=True, slots=True)
class BudgetSettings:
    max_requests: int = 100_000
    max_input_tokens: int = 1_000_000_000
    max_cost: float | None = None
    input_price: float | None = None
    output_price: float | None = None
    cache_read_price: float | None = None

    def __post_init__(self):
        for name in ("max_requests", "max_input_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_cost", "input_price", "output_price", "cache_read_price"):
            value = getattr(self, name)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be a nonnegative finite number or null")


class ColonyBudget:
    def __init__(self, service, game, settings):
        self.service, self.game, self.settings = service, game, settings
        self.pending = {}
        self.scopes = {}
        self.blocked = False
        self._lock = asyncio.Lock()

    async def status(self, scope=None):
        colony = scope[0] if scope else self.game.colony_id
        state = await self.service.call("budget_status", colony)
        state.update(blocked=self.blocked,
                     requests_remaining=max(0, self.settings.max_requests - state["requests"]),
                     input_tokens_remaining=max(0, self.settings.max_input_tokens - state["input_tokens"]),
                     cost_limit=self.settings.max_cost,
                     cost_remaining=(None if self.settings.max_cost is None or state["unpriced_reservations"]
                                     else max(0, self.settings.max_cost - state["cost"])))
        return state

    async def reserve(self, role, estimate, max_output_tokens, *, scope=None, attempt_id=None, metadata=None):
        async with self._lock:
            if self.blocked:
                return None
            ident = attempt_id or uuid.uuid4().hex
            scope = tuple(scope or (self.game.colony_id, self.game.load_epoch, self.game.game_tick))
            self.scopes[ident] = scope
            prices = {name: getattr(self.settings, name) for name in ("input_price", "output_price", "cache_read_price")}
            cost = None
            if all(value is not None for value in prices.values()):
                maximum_input_price = max(prices["input_price"], prices["cache_read_price"])
                cost = (estimate * maximum_input_price + max_output_tokens * prices["output_price"]) / 1_000_000
            payload = {"id": ident, "role": role, "input_estimate": estimate, "output_limit": max_output_tokens,
                       "reserved_cost": cost, "prices": prices, **(metadata or {})}
            try:
                task = asyncio.create_task(self.service.call("budget_reserve", *scope, payload,
                    self.settings.max_requests, self.settings.max_input_tokens, self.settings.max_cost))
                try:
                    accepted = await asyncio.shield(task)
                except asyncio.CancelledError:
                    # The executor may have committed even though its waiter was cancelled.
                    accepted = await task
                    if accepted:
                        receipt = {"id": ident, "usage": {}, "outcome": "not_dispatched"}
                        self.pending[ident] = (scope, receipt)
                        await asyncio.shield(self.service.call("budget_settle", *scope, receipt))
                        self.pending.pop(ident, None)
                    raise
            except BaseException:
                self.blocked = True
                raise
            if not accepted:
                self.blocked = True
                return None
            if self.blocked:
                await self._not_dispatched_locked(ident)
                return None
            return ident

    async def _not_dispatched_locked(self, ident):
        scope = self.scopes[ident]
        receipt = {"id": ident, "usage": {}, "outcome": "not_dispatched"}
        self.pending[ident] = (scope, receipt)
        try:
            await self.service.call("budget_settle", *scope, receipt)
        except BaseException:
            self.blocked = True
            raise
        self.pending.pop(ident, None)

    async def begin_dispatch(self, ident):
        try:
            async with self._lock:
                if self.blocked:
                    await self._not_dispatched_locked(ident)
                    return False
                # No await between this return and starting the model coroutine.
                return True
        except BaseException:
            self.blocked = True
            self.pending.setdefault(ident, (self.scopes[ident],
                                    {"id": ident, "usage": {}, "outcome": "not_dispatched"}))
            raise

    async def reenable(self):
        async with self._lock:
            try:
                for ident, (scope, receipt) in list(self.pending.items()):
                    await self.service.call("budget_settle", *scope, receipt)
                    self.pending.pop(ident, None)
            except BaseException:
                self.blocked = True
                raise
            self.blocked = False

    async def settle(self, ident, usage, *, response=None, outcome="attempted"):
        # Keep the receipt before waiting: cancellation while another role holds
        # the lock must not discard a response that has already arrived.
        scope = self.scopes[ident]
        receipt = {"id": ident, "usage": dict(usage), "outcome": outcome}
        if response is not None:
            receipt["response"] = private_protocol_redacted(response)
        self.pending[ident] = (scope, receipt)
        try:
            async with self._lock:
                await self.service.call("budget_settle", *scope, receipt)
                self.pending.pop(ident, None)
        except BaseException:
            self.blocked = True
            raise

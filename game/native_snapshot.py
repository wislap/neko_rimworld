"""Connection-owned caches of sampled native background reads; no model tools or durable state."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass

from .observation import Observation


@dataclass(frozen=True, slots=True)
class NativeSnapshot:
    observation: Observation
    cache_hit: bool
    checked_tick: int | None


class NativeSnapshotProvider:
    """One native read reused while the same loaded world is younger than ``refresh_ticks``."""

    operation: str
    version: str
    refresh_ticks: int
    label: str

    def __init__(self, query, current_session=None):
        self._query = query
        self._current_session = current_session
        self._lock = asyncio.Lock()
        self._cached: Observation | None = None

    async def snapshot(self) -> NativeSnapshot:
        # Serial callers recheck the cache after a single in-flight native read.
        async with self._lock:
            current = self._current_session() if self._current_session else None
            tick = getattr(current, "game_tick", None)
            cached = self._cached
            if cached is not None and current is not None:
                response = cached.value["response"]
                sampled = response["payload"]["data"]["sample"]["finished_tick"]
                if (
                    (response["colony_id"], response["load_epoch"])
                    == (current.colony_id, current.load_epoch)
                    and 0 <= tick - sampled < self.refresh_ticks
                ):
                    return NativeSnapshot(Observation(cached.to_dict()), True, tick)
            observation = await self._query({"operation": self.operation, "arguments": {}})
            if not isinstance(observation, Observation):
                raise ValueError(f"{self.label} source did not return an Observation")
            response = observation.value["response"]
            if current is not None and (response["colony_id"], response["load_epoch"]) != (current.colony_id, current.load_epoch):
                from ..tool_contracts import ToolExecutionError
                raise ToolExecutionError("world_changed", f"{self.label} response belongs to a different loaded world", recoverable=False)
            payload = response["payload"]
            data = payload.get("data")
            if payload.get("status") not in {"ok", "partial"}:
                # Retain the real failed envelope; the runtime can disclose a
                # source failure without disabling otherwise valid research.
                return NativeSnapshot(Observation(observation.to_dict()), False, tick)
            if not isinstance(data, dict) or data.get("format") != self.version:
                raise ValueError(f"{self.label} source returned an invalid snapshot")
            sampled = data.get("sample", {}).get("finished_tick")
            if type(sampled) is not int:
                raise ValueError(f"{self.label} source omitted its sampling tick")
            self._cached = Observation(copy.deepcopy(observation.value))
            return NativeSnapshot(Observation(observation.to_dict()), False, tick)

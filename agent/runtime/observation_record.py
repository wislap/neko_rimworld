"""Retain one native observation on a run session, projecting construction evidence.

This lives apart from both the session dataclass and the semantic adapter so each
can call it without importing the other: the loop (which gets a tool's observation)
and the semantic adapter (which reads natively itself) share one entry, so
construction facts follow every native read rather than only loop-returned ones.
"""

from __future__ import annotations

from typing import Any

# Native reads whose results are construction/area evidence for the colony work ledger.
CONSTRUCTION_OBSERVATIONS = frozenset({"inspect_construction_intents", "inspect_construction", "inspect_areas"})


async def record_observation(session: Any, observation: dict[str, Any], *, source: str) -> None:
    """Append the observation; when it is native construction evidence, reconcile the ledger.

    A failed ledger projection stops the run as a write-record issue. Sessions
    without a colony work service (read-only probes, test doubles) only retain
    the read.
    """
    session.observations.append(observation)
    service = getattr(session, "colony_work_service", None)
    if (service is None or source != "native"
            or observation.get("request", {}).get("operation") not in CONSTRUCTION_OBSERVATIONS):
        return
    response = observation["response"]
    try:
        await service.call(
            "reconcile_construction", response["colony_id"], response["load_epoch"], response["game_tick"],
            response["connection_epoch"], observation, command_binding_check=getattr(session, "binding_check", None))
    except BaseException:
        session.write_record_issue = "construction_evidence"
        session.write_record_context = {"mode": "reconcile_construction",
                                        "observation_id": observation.get("observation_id")}
        raise

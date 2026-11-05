"""Scope and source references for the planner's dated purpose assessment."""

import copy
import hashlib

from ..serialization import canonical


def _map_ids(value):
    result = set()
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {"map_id", "mapId"} and isinstance(item, str) and item:
                    result.add(item)
                elif isinstance(item, (dict, list)):
                    stack.append(item)
        elif isinstance(node, list):
            stack.extend(item for item in node if isinstance(item, (dict, list)))
    return result


def evidence_ref(observation, *, identity, map_id, minimum_tick=0, allow_failed=False):
    response = observation.get("response", {})
    if tuple(response.get(key) for key in ("connection_epoch", "colony_id", "load_epoch")) != tuple(identity):
        raise ValueError("Purpose evidence must belong to this current connection, colony and epoch")
    tick = response.get("game_tick")
    if type(tick) is not int or tick < minimum_tick:
        raise ValueError("Purpose evidence must follow the current plan state")
    request, payload = observation.get("request", {}), response.get("payload", {})
    status = payload.get("status")
    if status not in {"ok", "partial"} and not allow_failed:
        raise ValueError("A supported or unmet purpose judgment requires native returned facts")
    data = payload.get("data") or {}
    maps = _map_ids(data)
    binding = "observed"
    if map_id not in maps:
        arguments = request.get("arguments", {})
        requested = _map_ids(arguments)
        if isinstance(arguments, dict) and isinstance(arguments.get("map"), str):
            requested.add(arguments["map"])
        if not allow_failed or maps or map_id not in requested:
            raise ValueError("Purpose evidence must explicitly cover the adopted plan map")
        binding = "requested_unknown"
    reference = {"observation_id": observation["observation_id"], "operation": request.get("operation"),
                 "map_id": map_id, "map_binding": binding, "map_ids": sorted(maps), "game_tick": tick,
                 "sha256": hashlib.sha256(canonical(observation).encode("utf-8")).hexdigest()}
    for key in ("sample", "coverage", "bounds"):
        if key in data:
            reference[key] = copy.deepcopy(data[key])
    return reference

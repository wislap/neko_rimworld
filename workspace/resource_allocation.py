"""Explicit, soft resource allocations and three separate satisfaction readings."""

from copy import deepcopy

from .ledger import is_integer

FORMAT = "resource_allocation.v1"
KINDS = frozenset({"resource.allocation_selected", "resource.observed"})


def _identity(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _quantities(value, name):
    if not isinstance(value, dict) or any(not isinstance(key, str) or not key or not is_integer(count) or count < 0 for key, count in value.items()):
        raise ValueError(f"{name} must map resource ids to nonnegative integer quantities")
    return deepcopy(value)


def _inputs(snapshot, demands):
    if not isinstance(snapshot, dict):
        raise ValueError("A resource observation is required")
    for name in ("map_id", "observation_id", "load_epoch"):
        _identity(snapshot.get(name), name)
    if not isinstance(snapshot.get("scope"), dict) or not snapshot["scope"]:
        raise ValueError("Resource observations require an explicit resource scope")
    if not is_integer(snapshot.get("game_tick")) or snapshot["game_tick"] < 0:
        raise ValueError("A resource observation requires its native sample tick")
    _quantities(snapshot.get("quantities"), "snapshot.quantities")
    if type(snapshot.get("complete")) is not bool:
        raise ValueError("Resource coverage completeness is required")
    ids = set()
    for demand in demands:
        identity = _identity(demand.get("id"), "demand id")
        if identity in ids:
            raise ValueError("Demand ids must be unique")
        ids.add(identity)
        if not is_integer(demand.get("revision")) or demand["revision"] < 1:
            raise ValueError("Demand revision must be positive")
        _identity(demand.get("window"), "demand window")
        _quantities(demand.get("resources"), "demand.resources")
        if demand.get("relation", "competing") not in {"competing", "alternative", "sequential"}:
            raise ValueError("Unsupported demand relation")
        if demand.get("relation") in {"alternative", "sequential"}:
            _identity(demand.get("group_id"), "related demand group_id")


def _reading(required, snapshot):
    unknown, shortfalls = [], {}
    for resource, count in required.items():
        if resource not in snapshot["quantities"] and not snapshot["complete"]:
            unknown.append(resource)
        else:
            shortfalls[resource] = max(0, count - snapshot["quantities"].get(resource, 0))
    return {"required": deepcopy(required), "shortfalls": shortfalls, "unknown": unknown,
            "satisfied": None if unknown else not any(shortfalls.values())}


def _total(rows):
    result = {}
    for quantities in rows:
        for resource, count in quantities.items():
            result[resource] = result.get(resource, 0) + count
    return result


def assess(snapshot, demands, *, selection=None):
    """Compare a current observation without choosing a priority or reallocating.

    Sequential demands in the same consumption window still consume resources;
    different windows are shown separately. Alternatives require an explicit
    choice and are never all counted as simultaneous obligations.
    """
    demands = list(demands)
    _inputs(snapshot, demands)
    independent = [{"demand_id": row["id"], "revision": row["revision"], "window": row["window"],
                    **_reading(row["resources"], snapshot)} for row in demands]
    joint, competition = [], []
    for window in dict.fromkeys(row["window"] for row in demands):
        rows = [row for row in demands if row["window"] == window]
        alternatives = {}
        for row in rows:
            if row.get("relation") == "alternative":
                alternatives.setdefault(row["group_id"], []).append(row["id"])
        combined = _reading(_total(row["resources"] for row in rows if row.get("relation") != "alternative"), snapshot)
        joint.append({"window": window, **combined, "satisfied": None if alternatives else combined["satisfied"],
                      "status": "choice_required" if alternatives else "unknown" if combined["satisfied"] is None else "satisfied" if combined["satisfied"] else "shortfall",
                      "alternative_groups": alternatives})
        for resource in combined["required"]:
            claimants = [row["id"] for row in rows if row.get("relation") != "alternative" and row["resources"].get(resource, 0)]
            if len(claimants) > 1 and (resource in combined["unknown"] or combined["shortfalls"].get(resource, 0)):
                competition.append({"window": window, "resource": resource, "demand_ids": claimants,
                                    "shortfall": combined["shortfalls"].get(resource), "status": "unallocated"})
    selected, discrepancies = None, []
    if selection is not None:
        choice = selection.get("payload", selection)
        old_snapshot = choice["snapshot"]
        refs = {row["id"]: row for row in choice["demands"]}
        current = {row["id"]: row for row in demands}
        for identity in set(refs) | set(current):
            if identity not in refs or identity not in current or refs[identity] != current[identity]:
                discrepancies.append({"kind": "demand_changed", "demand_id": identity})
        for field in ("map_id", "load_epoch"):
            if snapshot[field] != old_snapshot[field]:
                discrepancies.append({"kind": "scope_changed", "field": field, "selected": old_snapshot[field], "current": snapshot[field]})
        if snapshot["scope"] != old_snapshot["scope"]:
            discrepancies.append({"kind": "resource_scope_changed", "selected": deepcopy(old_snapshot["scope"]), "current": deepcopy(snapshot["scope"])})
        if snapshot["quantities"] != old_snapshot["quantities"] or snapshot["complete"] != old_snapshot["complete"]:
            discrepancies.append({"kind": "stock_changed", "selected_observation_id": old_snapshot["observation_id"], "current_observation_id": snapshot["observation_id"]})
        shares = choice["shares"]
        total = _total(shares.values())
        current_stock = _reading(total, snapshot)
        selected = {"allocation_id": choice["allocation_id"], "revision": choice["revision"], "window": choice["window"],
                    "shares": deepcopy(shares), "original_observation_id": old_snapshot["observation_id"],
                    "current_stock": current_stock, "demands": [], "stale": bool(discrepancies), "native_lock": False}
        for identity, demand in current.items():
            if demand["window"] != choice["window"]:
                continue
            share = shares.get(identity, {})
            remaining = {resource: max(0, count - share.get(resource, 0)) for resource, count in demand["resources"].items()}
            covers_demand = not any(remaining.values())
            selected["demands"].append({"demand_id": identity, "share": deepcopy(share), "remaining": remaining,
                                        "allocation_covers_demand": covers_demand,
                                        "satisfied": None if discrepancies or current_stock["satisfied"] is not True else covers_demand})
    return {"independent": independent, "joint": joint, "selected": selected,
            "competition": competition, "discrepancies": discrepancies}


def project(events, *, load_epoch=None):
    decisions = {}
    for event in events:
        payload = event.get("payload", {})
        if event.get("kind") == "resource.allocation_selected" and payload.get("format") == FORMAT:
            row = deepcopy(payload)
            previous = decisions.get(row["allocation_id"], {})
            row["history"] = [*previous.get("history", []), {"kind": event["kind"], "seq": event.get("seq"), "revision": row["revision"], "observation_id": row["snapshot"]["observation_id"]}]
            row["current_snapshot"] = deepcopy(row["snapshot"])
            row["current_demands"] = deepcopy(row["demands"])
            decisions[row["allocation_id"]] = row
        elif event.get("kind") == "resource.observed" and payload.get("format") == FORMAT:
            row = decisions.get(payload["allocation_id"])
            if row is not None and row["revision"] == payload["selection_revision"]:
                row["current_snapshot"] = deepcopy(payload["snapshot"])
                if payload.get("demands") is not None:
                    row["current_demands"] = deepcopy(payload["demands"])
                row["history"].append({"kind": event["kind"], "seq": event.get("seq"), "revision": row["revision"], "observation_id": payload["snapshot"]["observation_id"]})
    for row in decisions.values():
        row["assessment"] = assess(row["current_snapshot"], row["current_demands"], selection=row)
        historical = load_epoch is not None and row["current_snapshot"]["load_epoch"] != load_epoch
        row["evidence_status"] = "historical" if historical else "current_epoch"
        if historical:
            assessment = row["assessment"]
            assessment["discrepancies"].append({"kind": "load_epoch_changed", "selected": row["current_snapshot"]["load_epoch"], "current": load_epoch})
            assessment["selected"]["stale"] = True
            readings = [*assessment["independent"], *assessment["joint"], assessment["selected"]["current_stock"], *assessment["selected"]["demands"]]
            for reading in readings:
                reading["historical_satisfied"] = reading["satisfied"]
                reading["satisfied"] = None
            for reading in assessment["joint"]:
                reading["status"] = "historical"
    return {"decisions": list(decisions.values())}


def prepare_observation(events, *, allocation_id, expected_revision, snapshot, demands=None):
    """Bind a trusted fresh stock read to the still-current allocation revision.

    The read does not create a choice or infer revised requirements. A caller
    may supply explicitly revised demands; omission retains recorded demands.
    """
    current = next((row for row in project(events)["decisions"] if row["allocation_id"] == allocation_id), None)
    if current is None or type(expected_revision) is not int or current["revision"] != expected_revision:
        raise ValueError("Resource observation allocation revision conflict")
    if demands is not None:
        demands = list(demands)
    _inputs(snapshot, current["current_demands"] if demands is None else demands)
    if snapshot["map_id"] != current["snapshot"]["map_id"]:
        raise ValueError("Resource observations cannot change the allocation map")
    return {"kind": "resource.observed", "source": "plugin", "payload": {
        "format": FORMAT, "allocation_id": allocation_id, "selection_revision": expected_revision,
        "snapshot": deepcopy(snapshot), "demands": deepcopy(demands)}}


def prepare_selection(events, *, allocation_id, expected_revision, snapshot, demands, shares, window, reason, task_id=None):
    _identity(allocation_id, "allocation_id")
    _identity(window, "window")
    _identity(reason, "reason")
    demands = list(demands)
    _inputs(snapshot, demands)
    current = next((row for row in project(events)["decisions"] if row["allocation_id"] == allocation_id), None)
    if type(expected_revision) is not int or expected_revision != (current["revision"] if current else 0):
        raise ValueError("Resource allocation revision conflict")
    eligible = {row["id"]: row for row in demands if row["window"] == window}
    if not isinstance(shares, dict) or not shares or any(identity not in eligible for identity in shares):
        raise ValueError("Shares must name demands in the selected window")
    alternatives = {}
    for identity, quantities in shares.items():
        _quantities(quantities, "shares")
        demand = eligible[identity]
        if any(count > demand["resources"].get(resource, 0) for resource, count in quantities.items()):
            raise ValueError("A share exceeds its demand")
        if demand.get("relation") == "alternative" and any(quantities.values()):
            if demand["group_id"] in alternatives:
                raise ValueError("Choose at most one alternative per group")
            alternatives[demand["group_id"]] = identity
    reading = _reading(_total(shares.values()), snapshot)
    if reading["satisfied"] is not True:
        raise ValueError("Chosen shares require known available stock")
    payload = {"format": FORMAT, "allocation_id": allocation_id, "task_id": task_id, "revision": expected_revision + 1,
               "snapshot": deepcopy(snapshot), "demands": deepcopy(demands), "shares": deepcopy(shares),
               "window": window, "reason": reason, "native_lock": False}
    return {"kind": "resource.allocation_selected", "source": "planner", "payload": payload}

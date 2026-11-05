"""Translate complete native cell observations into construction area facts.

Coverage can satisfy a drawn area without proving who created its native zone.
No thing identity, ownership, operating purpose, or unobserved absence is inferred.
The caller owns world/connection validation and appends prepare_fact arguments.
"""

from collections.abc import Mapping

from ..agent.world.spatial_observation import SpatialObservationError, observation_cells

NATIVE_KINDS = {
    "roof": "build_roof",
    "remove_roof": "no_roof",
    "home": "home",
    "allowed": "allowed",
    "stockpile": "stockpile",
    "growing": "growing",
}


def _points(rows):
    if not isinstance(rows, list):
        return None
    points = set()
    for row in rows:
        if not isinstance(row, Mapping) or type(row.get("x")) is not int or type(row.get("z")) is not int:
            return None
        point = (row["x"], row["z"])
        if point in points:
            return None
        points.add(point)
    return points


def facts(state, data, *, operation, observation_id, load_epoch):
    """Return keyword arguments for construction.prepare_fact, without mutations.

    inspect_construction establishes physical roof presence/absence from every
    required visible cell. inspect_areas establishes only complete native area
    coverage, with the row's real area_cells/zone_cells identity. A designation
    cannot demote a fresh physical built fact; a later spatial absence can.
    """
    if not isinstance(data, Mapping) or operation not in {"inspect_construction", "inspect_areas"}:
        return []
    sample = data.get("sample") or {}
    if isinstance(sample, Mapping) and sample.get("load_epoch", load_epoch) != load_epoch:
        return []
    if operation == "inspect_construction":
        if (
            data.get("format") != "construction_observation.v1"
            or data.get("coverage", {}).get("cells_complete") is not True
        ):
            return []
        try:
            raw_cells = observation_cells(data)
        except SpatialObservationError:
            return []
        if _points(raw_cells) is None:
            return []
        observed = {(row["x"], row["z"]): row for row in raw_cells}
        native_rows = []
    else:
        if data.get("complete") is not True or not isinstance(data.get("objects"), list):
            return []
        native_rows = []
        seen = set()
        for row in data["objects"]:
            if not isinstance(row, Mapping) or not isinstance(row.get("id"), str) or not row["id"]:
                return []
            if row["id"] in seen:
                return []
            seen.add(row["id"])
            flags = row.get("write_flags", {}).get("cells")
            selected_scope = None
            if isinstance(flags, list):
                selected_scope = _points(flags)
                if selected_scope is None or any(type(cell.get("enabled")) is not bool for cell in flags):
                    continue
                points = {(cell["x"], cell["z"]) for cell in flags if cell["enabled"]}
            else:
                points = _points(row.get("cells"))
            if (
                row.get("map_id") == data.get("map_id")
                and row.get("complete") is True
                and row.get("status") == "observed"
                and points is not None
            ):
                native_rows.append((row, points, selected_scope))
        native_rows.sort(key=lambda entry: entry[0]["id"])
        observed = {}
    prepared = []
    for current in state["plans"]:
        plan = current["plan"]
        if not isinstance(data.get("map_id"), str) or plan["map_id"] != data["map_id"]:
            continue
        projected = {row["id"]: row for row in current["elements"]}
        origin = plan["origin"]
        for area in plan.get("areas", []):
            kind = area.get("kind")
            if kind not in NATIVE_KINDS or area["id"] not in projected:
                continue
            local = _points(area.get("cells"))
            if not local:
                continue
            needed = {(x + origin["x"], z + origin["z"]) for x, z in local}
            prior = projected[area["id"]]
            identity = None
            identities = None
            if operation == "inspect_construction":
                if kind not in {"roof", "remove_roof"}:
                    continue
                cells = [observed.get(point) for point in needed]
                if any(
                    row is None
                    or row.get("known") is not True
                    or "roof" not in row
                    or row["roof"] is not None
                    and (not isinstance(row["roof"], str) or not row["roof"])
                    for row in cells
                ):
                    continue
                fulfilled = all((row["roof"] is not None) == (kind == "roof") for row in cells)
                stage = "built" if fulfilled else "missing"
            else:
                candidates = [(row, points, scope) for row, points, scope in native_rows
                              if row.get("kind") == NATIVE_KINDS[kind] and (scope is None or needed <= scope)]
                previous_identity = (prior.get("fact") or {}).get("field_identity") or {}
                if not previous_identity:
                    previous_scopes = (prior.get("fact") or {}).get("field_identities") or []
                    previous_identity = previous_scopes[0] if previous_scopes else {}
                matches = [(row, points) for row, points, _ in candidates if needed <= points]
                if matches:
                    row = next(
                        (row for row, _ in matches if row["id"] == previous_identity.get("object")), matches[0][0]
                    )
                    stage = "designated" if kind in {"roof", "remove_roof"} else "built"
                    if stage == "designated" and prior.get("fresh") is True and prior.get("stage") == "built":
                        continue
                else:
                    # A selected complete row can invalidate its prior coverage.
                    # An unreturned row cannot establish absence: inspect_areas
                    # may have selected IDs even when its selected page is complete.
                    row = next((row for row, _, _ in candidates if row["id"] == previous_identity.get("object")), None)
                    if row is None or kind in {"roof", "remove_roof"}:
                        continue
                    stage = "missing"
                if kind in {"stockpile", "growing"}:
                    identity = {"object": row["id"], "field": "zone_cells"}
                else:
                    native_fields = row.get("cell_fields", [])
                    selected = [field for field in native_fields if isinstance(field, Mapping)
                                and (field.get("x"), field.get("z")) in needed
                                and field.get("object") == row["id"]
                                and isinstance(field.get("field"), str) and field["field"]]
                    if (_points(selected) == needed and len({field["field"] for field in selected}) == len(needed)):
                        identities = [{"object": field["object"], "field": field["field"]} for field in selected]
            prepared.append(
                {
                    "plan_id": current["plan_id"],
                    "element_id": area["id"],
                    "stage": stage,
                    "object_id": None,
                    "observation_id": observation_id,
                    "load_epoch": load_epoch,
                    "field_identity": identity,
                    "field_identities": identities,
                }
            )
    return prepared

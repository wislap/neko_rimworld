"""Deterministic construction geometry over explicitly chosen plans and native data.

This module does not choose a site, issue an order, or simulate native eligibility.
Plan coordinates are local to origin; observation coordinates are world cells.
"""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Mapping

from ...game.spatial_observation import SpatialObservationError, observation_cells
from ...serialization import identity_digest

PLAN_FORMAT = "construction_plan.v1"
CHECK_FORMAT = "construction_check.v1"
MAX_SPAN = 64
MAX_PLAN_SPAN = MAX_SPAN - 2  # Reserve the exterior ring in one supported native observation.
MAX_ELEMENTS = 4096
MAX_ROOMS = 64
SIDES = frozenset({"north", "east", "south", "west"})


class ConstructionPlanError(ValueError):
    """The selected plan cannot be represented without guessing its meaning."""


def _integer(value, name, *, minimum=None, maximum=None):
    if type(value) is not int or not -(2**31) <= value < 2**31:
        raise ConstructionPlanError(f"{name} must be a 32-bit integer")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ConstructionPlanError(f"{name} is outside its supported range")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ConstructionPlanError(f"{name} must be nonempty text of at most 256 characters")
    return value


def _point(value, name):
    if not isinstance(value, Mapping):
        raise ConstructionPlanError(f"{name} must be an x/z object")
    return (_integer(value.get("x"), name + ".x"), _integer(value.get("z"), name + ".z"))


def _cell(x, z):
    return {"x": x, "z": z}


def _rect(value, name, *, minimum=1):
    if not isinstance(value, Mapping):
        raise ConstructionPlanError(f"{name} must contain x, z, width and height")
    x, z = _point(value, name)
    w = _integer(value.get("width"), name + ".width", minimum=minimum, maximum=MAX_SPAN)
    h = _integer(value.get("height"), name + ".height", minimum=minimum, maximum=MAX_SPAN)
    if x + w > 2**31 or z + h > 2**31:
        raise ConstructionPlanError(f"{name} extends beyond the coordinate range")
    return x, z, w, h


def _cells(rect, *, interior=False):
    x, z, w, h = rect
    padding = 1 if interior else 0
    return {(a, b) for a in range(x + padding, x + w - padding) for b in range(z + padding, z + h - padding)}


def _boundary(rect):
    return _cells(rect) - _cells(rect, interior=True)


def _catalog(catalog):
    rows = catalog.get("definitions") if isinstance(catalog, Mapping) else catalog
    if not isinstance(rows, (list, tuple)):
        raise ConstructionPlanError("A native construction catalog is required")
    result = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ConstructionPlanError("Catalog definitions must be objects")
        name = _text(row.get("definition"), "definition")
        if name in result:
            raise ConstructionPlanError("Catalog contains an ambiguous definition: " + name)
        result[name] = row
    return result


def _rotate(point, rotation):
    x, z = point
    return ((x, z), (z, -x), (-x, -z), (-z, x))[rotation]


def occupied_cells(position, size, rotation=0):
    """Match GenAdj.OccupiedRect's rotated even-size anchor convention.

    Rotating an even dimension changes the anchor by a cell in the south/west
    directions. This follows the supported native implementation, rather than
    merely swapping width and height around an unchanged center.
    """
    x, z = _point(position, "position")
    w, h = _point(size, "size")
    _integer(w, "size.x", minimum=1, maximum=MAX_SPAN)
    _integer(h, "size.z", minimum=1, maximum=MAX_SPAN)
    _integer(rotation, "rotation", minimum=0, maximum=3)
    if rotation % 2:
        w, h = h, w
    shift_x, shift_z = ((0, 0), (0, -1), (-1, -1), (-1, 0))[rotation]
    if w % 2 == 0:
        x += shift_x
    if h % 2 == 0:
        z += shift_z
    return {
        (a, b)
        for a in range(x - (w - 1) // 2, x - (w - 1) // 2 + w)
        for b in range(z - (h - 1) // 2, z - (h - 1) // 2 + h)
    }


def _element(raw, definitions):
    if not isinstance(raw, Mapping):
        raise ConstructionPlanError("Elements must be objects")
    item = copy.deepcopy(dict(raw))
    identifier = _text(item.get("id"), "element.id")
    name = _text(item.get("definition"), "element.definition")
    definition = definitions.get(name)
    if definition is None:
        raise ConstructionPlanError("Definition is absent from the native catalog: " + name)
    position = _point(item.get("position"), "element.position")
    rotation = _integer(item.get("rotation", 0), "element.rotation", minimum=0, maximum=3)
    layer = item.get("layer", definition.get("layer", "building"))
    if layer not in {"building", "floor", "conduit"}:
        raise ConstructionPlanError("Element layer must be building, floor or conduit")
    if definition.get("layer") not in (None, layer):
        raise ConstructionPlanError("Element layer differs from native definition: " + identifier)
    if "stuff" in item and item["stuff"] is not None:
        _text(item["stuff"], "element.stuff")
    else:
        item.pop("stuff", None)
    footprint = occupied_cells(_cell(*position), definition.get("size"), rotation)
    item.update(
        id=identifier,
        definition=name,
        position=_cell(*position),
        rotation=rotation,
        layer=layer,
        footprint=[_cell(*p) for p in sorted(footprint)],
        passability=definition.get("passability"),
        is_door=definition.get("is_door"),
        is_room_boundary=definition.get("is_room_boundary"),
        holds_roof=definition.get("holds_roof"),
        native_eligibility="not_checked",
    )
    item.pop("interaction_cell", None)
    item.pop("interaction_cells", None)
    offsets = definition.get("interaction_offsets")
    if offsets is None:
        offsets = [definition.get("interaction_offset")] if definition.get("has_interaction_cell") is True else []
    if not isinstance(offsets, list) or len(offsets) > MAX_SPAN**2:
        raise ConstructionPlanError("Native interaction_offsets must be a bounded list")
    if offsets:
        rotated = {_rotate(_point(p, "interaction_offset"), rotation) for p in offsets}
        item["interaction_cells"] = [_cell(position[0] + x, position[1] + z) for x, z in sorted(rotated)]
        if len(item["interaction_cells"]) == 1:
            item["interaction_cell"] = item["interaction_cells"][0]
    rooms = item.get("room_ids", [])
    if not isinstance(rooms, list) or not all(isinstance(r, str) and r.strip() for r in rooms):
        raise ConstructionPlanError("room_ids must be room identities")
    item["room_ids"] = sorted(set(rooms))
    return item


def _door(room, raw):
    if not isinstance(raw, Mapping) or raw.get("side") not in SIDES:
        raise ConstructionPlanError("A room door requires north, east, south or west")
    x, z, w, h = _rect(room["bounds"], "room.bounds", minimum=3)
    side = raw["side"]
    length = w if side in {"north", "south"} else h
    offset = _integer(raw.get("offset"), "door.offset", minimum=1, maximum=length - 2)
    pos = {
        "south": (x + offset, z),
        "north": (x + offset, z + h - 1),
        "west": (x, z + offset),
        "east": (x + w - 1, z + offset),
    }[side]
    item = {
        "id": raw.get("id", f"{room['id']}:door:{side}:{offset}"),
        "definition": raw.get("definition", "Door"),
        "position": _cell(*pos),
        "rotation": 0 if side in {"north", "south"} else 1,
        "role": "door",
        "room_ids": [room["id"]],
    }
    stuff = raw.get("stuff", room.get("wall", {}).get("stuff"))
    if stuff is not None:
        item["stuff"] = stuff
    return item


def _area(raw):
    if not isinstance(raw, Mapping):
        raise ConstructionPlanError("Areas must be objects")
    item = copy.deepcopy(dict(raw))
    _text(item.get("id"), "area.id")
    kind = item.get("kind")
    if kind not in {"roof", "remove_roof", "home", "allowed", "stockpile", "growing"}:
        raise ConstructionPlanError("Unsupported area kind")
    if "cells" in item:
        if not isinstance(item["cells"], list) or len(item["cells"]) > MAX_SPAN**2:
            raise ConstructionPlanError("Area cells must be a bounded list")
        cells = {_point(p, "area.cell") for p in item["cells"]}
    elif "bounds" in item:
        cells = _cells(_rect(item["bounds"], "area.bounds"))
    else:
        raise ConstructionPlanError("An area requires cells or bounds")
    if not cells:
        raise ConstructionPlanError("An area must contain at least one cell")
    item["cells"] = [_cell(*p) for p in sorted(cells)]
    item.pop("bounds", None)
    return item


def compile_plan(plan, catalog):
    """Expand an explicitly selected room plan into deterministic local elements.

    Room bounds include walls. Door offsets count from the lower x/z corner,
    excluding corners. No material, room size, location or furniture is selected.
    """
    if not isinstance(plan, Mapping) or plan.get("format", PLAN_FORMAT) != PLAN_FORMAT:
        raise ConstructionPlanError("Unsupported construction plan format")
    definitions = _catalog(catalog)
    result = copy.deepcopy(dict(plan))
    _text(result.get("map_id"), "map_id")
    origin = _point(result.get("origin"), "origin")
    result["origin"] = _cell(*origin)
    rooms = result.get("rooms", [])
    raw_elements = result.get("elements", [])
    raw_areas = result.get("areas", [])
    if not isinstance(rooms, list) or len(rooms) > MAX_ROOMS:
        raise ConstructionPlanError("rooms must be a bounded list")
    if not isinstance(raw_elements, list) or len(raw_elements) > MAX_ELEMENTS or not isinstance(raw_areas, list):
        raise ConstructionPlanError("elements and areas must be bounded lists")
    room_ids = set()
    generated = {}
    doors = []
    areas = []
    for room in rooms:
        if not isinstance(room, Mapping):
            raise ConstructionPlanError("Rooms must be objects")
        identifier = _text(room.get("id"), "room.id")
        if identifier in room_ids:
            raise ConstructionPlanError("Room IDs must be unique")
        room_ids.add(identifier)
        rect = _rect(room.get("bounds"), "room.bounds", minimum=3)
        if result.get("compiled") is True:
            continue
        wall = room.get("wall", {})
        if not isinstance(wall, Mapping):
            raise ConstructionPlanError("room.wall must be a definition/material object")
        for x, z in sorted(_boundary(rect)):
            key = f"wall@{x},{z}"
            item = {
                "id": key,
                "definition": wall.get("definition", "Wall"),
                "position": _cell(x, z),
                "rotation": 0,
                "role": "wall",
                "room_ids": [identifier],
            }
            if wall.get("stuff") is not None:
                item["stuff"] = wall["stuff"]
            if key in generated:
                previous = generated[key]
                if (previous["definition"], previous.get("stuff")) != (item["definition"], item.get("stuff")):
                    raise ConstructionPlanError("Shared walls request conflicting definitions or materials")
                previous["room_ids"].append(identifier)
            else:
                generated[key] = item
        room_doors = room.get("doors", [])
        if not isinstance(room_doors, list):
            raise ConstructionPlanError("room.doors must be a list")
        doors.extend(_door(room, d) for d in room_doors)
        if room.get("roof") is True:
            areas.append(
                {
                    "id": identifier + ":roof",
                    "kind": "roof",
                    "cells": [_cell(*p) for p in sorted(_cells(rect, interior=True))],
                }
            )
        floor = room.get("floor")
        if floor is not None:
            if not isinstance(floor, Mapping):
                raise ConstructionPlanError("room.floor must specify its definition")
            for x, z in sorted(_cells(rect, interior=True)):
                generated[f"{identifier}:floor@{x},{z}"] = {
                    "id": f"{identifier}:floor@{x},{z}",
                    "definition": floor.get("definition"),
                    "position": _cell(x, z),
                    "rotation": 0,
                    "layer": "floor",
                    "room_ids": [identifier],
                }
    for door in doors:
        x, z = _point(door["position"], "door.position")
        wall = generated.pop(f"wall@{x},{z}", None)
        if wall:
            door["room_ids"] = sorted(set(door["room_ids"] + wall["room_ids"]))
        if door["id"] in generated:
            raise ConstructionPlanError("Door IDs must be unique")
        generated[door["id"]] = door
    for raw in raw_elements:
        if not isinstance(raw, Mapping):
            raise ConstructionPlanError("Elements must be objects")
        identifier = _text(raw.get("id"), "element.id")
        if identifier in generated:
            raise ConstructionPlanError("Explicit element ID collides with a generated element")
        generated[identifier] = raw
    if len(generated) > MAX_ELEMENTS:
        raise ConstructionPlanError("Expanded plan exceeds the element limit")
    result["elements"] = [_element(raw, definitions) for _, raw in sorted(generated.items())]
    result["areas"] = [_area(a) for a in [*areas, *raw_areas]]
    area_ids = [a["id"] for a in result["areas"]]
    if len(area_ids) != len(set(area_ids)):
        raise ConstructionPlanError("Area IDs must be unique")
    if set(area_ids) & generated.keys():
        raise ConstructionPlanError("Area and construction element IDs must be distinct")
    positions = {_point(c, "footprint") for item in result["elements"] for c in item["footprint"]}
    positions.update(_point(c, "interaction_cell") for item in result["elements"] for c in item.get("interaction_cells", []))
    positions.update(_point(c, "area.cell") for area in result["areas"] for c in area["cells"])
    if not positions:
        raise ConstructionPlanError("Plan must contain construction elements or areas")
    min_x, min_z = min(x for x, _ in positions), min(z for _, z in positions)
    max_x, max_z = max(x for x, _ in positions), max(z for _, z in positions)
    if max_x - min_x + 1 > MAX_PLAN_SPAN or max_z - min_z + 1 > MAX_PLAN_SPAN:
        raise ConstructionPlanError(
            "Plan footprint exceeds 62 by 62 cells (native observation reserves an exterior ring)"
        )
    for x, z in positions:
        _integer(x + origin[0], "world.x")
        _integer(z + origin[1], "world.z")
    result.update(
        format=PLAN_FORMAT,
        compiled=True,
        rooms=copy.deepcopy(rooms),
        bounds={"min_x": min_x, "min_z": min_z, "width": max_x - min_x + 1, "height": max_z - min_z + 1},
    )
    return result


def observation_bounds(plan):
    """World scope including the exterior ring, bounded by native's 64-cell limit."""
    if not isinstance(plan, Mapping) or plan.get("format") != PLAN_FORMAT or plan.get("compiled") is not True:
        raise ConstructionPlanError("observation_bounds requires a compiled plan")
    ox, oz = _point(plan.get("origin"), "origin")
    bounds = plan["bounds"]
    min_x = _integer(bounds["min_x"] + ox - 1, "observation.min_x")
    min_z = _integer(bounds["min_z"] + oz - 1, "observation.min_z")
    width = _integer(bounds["width"] + 2, "observation.width", minimum=1, maximum=MAX_SPAN)
    height = _integer(bounds["height"] + 2, "observation.height", minimum=1, maximum=MAX_SPAN)
    _integer(min_x + width - 1, "observation.max_x")
    _integer(min_z + height - 1, "observation.max_z")
    return {"min_x": min_x, "min_z": min_z, "width": width, "height": height}


def _neighbors(cell):
    x, z = cell
    return ((x - 1, z), (x + 1, z), (x, z - 1), (x, z + 1))


def _flood(start, allowed):
    if start not in allowed:
        return set()
    seen = {start}
    pending = deque([start])
    while pending:
        cell = pending.popleft()
        for other in _neighbors(cell):
            if other in allowed and other not in seen:
                seen.add(other)
                pending.append(other)
    return seen


def inspect_layout(plan, observation=None):
    """Check chosen geometry; a passed geometry check never certifies native placement."""
    if not isinstance(plan, Mapping) or plan.get("format") != PLAN_FORMAT or plan.get("compiled") is not True:
        raise ConstructionPlanError("inspect_layout requires a compiled plan")
    issues = []
    checks = {"geometry": "passed", "connectivity": "passed", "observation": "not_checked"}
    origin = _point(plan["origin"], "origin")

    def issue(code, reason, *, identifiers=(), cells=(), severity="error"):
        issues.append(
            {
                "code": code,
                "severity": severity,
                "element_ids": sorted(set(identifiers)),
                "cells": [_cell(*p) for p in sorted(set(cells))],
                "reason": reason,
            }
        )

    owners = {}
    blockers = set()
    doors = set()
    enclosure = set()
    unknown_enclosure = set()
    layer_owners = {"building": owners, "floor": {}, "conduit": {}}
    for item in plan["elements"]:
        footprint = {_point(c, "footprint") for c in item["footprint"]}
        layer = item["layer"]
        target = layer_owners[layer]
        for cell in footprint:
            other = target.get(cell)
            if other:
                issue(
                    "footprint_overlap",
                    "Chosen elements occupy the same construction layer",
                    identifiers=(other["id"], item["id"]),
                    cells=(cell,),
                )
            else:
                target[cell] = item
        if item.get("is_door") is True:
            doors.update(footprint)
        elif item.get("passability") == "Impassable":
            blockers.update(footprint)
        elif layer == "building" and item.get("passability") not in {"Standable", "PassThroughOnly"}:
            issue(
                "passability_unknown",
                "Catalog does not establish planned passage",
                identifiers=(item["id"],),
                cells=footprint,
                severity="unknown",
            )
        if layer == "building":
            if item.get("is_room_boundary") is True or item.get("is_door") is True:
                enclosure.update(footprint)
            elif item.get("is_room_boundary") is not False:
                unknown_enclosure.update(footprint)
    for item in plan["elements"]:
        positions = {_point(p, "interaction_cell") for p in item.get("interaction_cells", [])}
        if positions and positions <= blockers:
            issue(
                "interaction_blocked",
                "Planned impassable objects block every native interaction cell",
                identifiers=(item["id"], *(owners[p]["id"] for p in positions)),
                cells=positions,
            )
    checks["geometry"] = (
        "failed" if any(i["severity"] == "error" for i in issues) else ("inconclusive" if issues else "passed")
    )

    bound = plan["bounds"]
    min_x, min_z = bound["min_x"] - 1, bound["min_z"] - 1
    max_x = bound["min_x"] + bound["width"]
    max_z = bound["min_z"] + bound["height"]
    allowed = {(x, z) for x in range(min_x, max_x + 1) for z in range(min_z, max_z + 1)} - blockers
    if observation is not None:
        if not isinstance(observation, Mapping) or observation.get("map_id") != plan["map_id"]:
            raise ConstructionPlanError("Observation map differs from the chosen plan")
        coverage = observation.get("coverage", {})
        try:
            cells = observation_cells(observation)
        except SpatialObservationError as exc:
            raise ConstructionPlanError(str(exc)) from exc
        observed = {}
        for row in cells:
            pos = _point(row, "observed.cell")
            if pos in observed:
                raise ConstructionPlanError("Observation repeats a cell")
            observed[pos] = row
        requested = {(x + origin[0], z + origin[1]) for x, z in allowed}
        unknown = {
            p
            for p in requested
            if p not in observed
            or observed[p].get("known") is not True
            or type(observed[p].get("walkable")) is not bool
        }
        for (x, z), row in observed.items():
            # Unknown cells stay optimistically traversable, so they cannot create
            # a false definitive failure. Their existence makes the result inconclusive.
            if row.get("known") is True and row.get("walkable") is False:
                allowed.discard((x - origin[0], z - origin[1]))
        if (
            unknown
            or coverage.get("cells_complete") is not True
            or coverage.get("objects_complete") is not True
            or coverage.get("visibility_complete") is not True
        ):
            checks["observation"] = "inconclusive"
            issue(
                "observation_incomplete",
                "Unknown or unobserved cells cannot establish complete planned connectivity",
                cells=((x - origin[0], z - origin[1]) for x, z in unknown),
                severity="unknown",
            )
        else:
            checks["observation"] = "passed"
    room_interiors = {}
    for room in plan.get("rooms", []):
        rect = _rect(room["bounds"], "room.bounds", minimum=3)
        interior = _cells(rect, interior=True)
        for other_id, other_interior in room_interiors.items():
            if interior & other_interior:
                issue(
                    "room_interiors_overlap",
                    "Selected rooms overlap rather than share a boundary",
                    identifiers=(room["id"], other_id),
                    cells=interior & other_interior,
                )
        room_interiors[room["id"]] = interior
        boundary = _boundary(rect)
        openings = boundary & doors
        missing = boundary - enclosure - unknown_enclosure
        uncertain = boundary & unknown_enclosure - enclosure
        if uncertain:
            issue(
                "room_boundary_unknown",
                "Native catalog does not establish room separation at these perimeter cells",
                identifiers=(room["id"],),
                cells=uncertain,
                severity="unknown",
            )
        if missing:
            issue(
                "room_boundary_open",
                "A selected room perimeter has unclosed cells",
                identifiers=(room["id"],),
                cells=missing,
            )
        if not openings:
            issue("room_has_no_door", "A selected room has no door on its boundary", identifiers=(room["id"],))
            continue
        entrances = {p for door in openings for p in _neighbors(door) if p in interior and p in allowed}
        if not entrances:
            issue(
                "door_entry_blocked",
                "No interior approach to the room door remains",
                identifiers=(room["id"],),
                cells=openings,
            )
            continue
        connected = set()
        for entry in entrances:
            connected |= _flood(entry, (interior | openings) & allowed)
        for item in plan["elements"]:
            footprint = {_point(c, "footprint") for c in item["footprint"]}
            if (
                item["layer"] != "building"
                or not footprint & interior
                or item.get("is_door") is True
                or item.get("is_room_boundary") is True
            ):
                continue
            if "interaction_cells" in item:
                access = {_point(p, "interaction_cell") for p in item["interaction_cells"]}
            else:
                access = {n for cell in footprint for n in _neighbors(cell)} | (footprint & allowed)
            if not access & connected:
                issue(
                    "facility_unreachable",
                    "No planned interior route reaches this facility's use location",
                    identifiers=(room["id"], item["id"]),
                    cells=footprint,
                )
    # Perimeter-to-exterior connectivity is geometric, not an actor-specific native route.
    outside = (
        {(x, z) for x in range(min_x, max_x + 1) for z in (min_z, max_z)}
        | {(x, z) for x in (min_x, max_x) for z in range(min_z, max_z + 1)}
    ) & allowed
    reachable = set()
    for start in outside:
        if start not in reachable:
            reachable |= _flood(start, allowed)
    for door in doors:
        if door not in reachable:
            issue(
                "door_disconnected_from_exterior",
                "Selected door has no modeled route to the observed footprint edge",
                cells=(door,),
            )
    for item in plan["elements"]:
        access = {_point(p, "interaction_cell") for p in item.get("interaction_cells", [])}
        already_reported = any(i["code"] == "facility_unreachable" and item["id"] in i["element_ids"] for i in issues)
        if access and not access & reachable and not already_reported:
            issue(
                "facility_unreachable",
                "No modeled exterior route reaches a native interaction cell",
                identifiers=(item["id"],),
                cells=access,
            )
    checks["connectivity"] = (
        "failed"
        if any(
            i["severity"] == "error" and i["code"] not in {"footprint_overlap", "interaction_blocked"} for i in issues
        )
        else "passed"
    )
    if any(i["severity"] == "unknown" for i in issues) and checks["connectivity"] == "passed":
        checks["connectivity"] = "inconclusive"
    status = (
        "failed"
        if any(i["severity"] == "error" for i in issues)
        else ("inconclusive" if any(v in {"inconclusive", "not_checked"} for v in checks.values()) else "passed")
    )
    return {
        "format": CHECK_FORMAT,
        "plan_sha256": identity_digest(plan),
        "observation_sha256": identity_digest(observation) if observation is not None else None,
        "status": status,
        "checks": checks,
        "issues": issues,
        "native_eligibility": "not_checked",
        "roof_support": "not_checked",
        "meaning": "Geometry and observed passage only; future native jobs, roof support and actor-specific eligibility are not simulated.",
    }

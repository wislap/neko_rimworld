"""Read native ordinary JSON cell groups without changing observation evidence.

Each group carries its full cell properties and explicit world-coordinate pairs.
There is no dictionary, identifier translation, external state, or inferred cell.
Legacy flat cells remain readable for existing software fixtures and native reads.
"""

from collections.abc import Mapping
from copy import deepcopy


class SpatialObservationError(ValueError):
    """The observation has an ambiguous or malformed cell representation."""


def _coordinate(value):
    if type(value) is not int or not -(2**31) <= value < 2**31:
        raise SpatialObservationError("Observed coordinates must be 32-bit integer world cells")
    return value


def observation_cells(data):
    """Expand explicit cells, preserving every property and detached nested value.

    Exactly one of cells/cell_groups must be present. Missing cells stay missing;
    known/status/roof and extension properties are never defaulted or rewritten.
    Duplicate coordinates are rejected across the whole observation, including
    overlaps between groups. Scope/sample/coverage remain in the untouched input.
    """
    if not isinstance(data, Mapping) or ("cells" in data) == ("cell_groups" in data):
        raise SpatialObservationError("Observation requires exactly one of cells or cell_groups")
    result = []
    seen = set()

    def append(properties, x, z):
        point = (_coordinate(x), _coordinate(z))
        if point in seen:
            raise SpatialObservationError("Observation coordinates must be unique")
        seen.add(point)
        row = deepcopy(dict(properties))
        row.update(x=point[0], z=point[1])
        result.append(row)

    if "cells" in data:
        cells = data["cells"]
        if not isinstance(cells, list):
            raise SpatialObservationError("Observation cells must be a list")
        for row in cells:
            if not isinstance(row, Mapping):
                raise SpatialObservationError("Observed cells must be objects")
            append(row, row.get("x"), row.get("z"))
    else:
        groups = data["cell_groups"]
        if not isinstance(groups, list):
            raise SpatialObservationError("Observation cell_groups must be a list")
        for group in groups:
            if not isinstance(group, Mapping) or "x" in group or "z" in group:
                raise SpatialObservationError("Cell groups require properties and _xz world-coordinate pairs")
            coordinates = group.get("_xz")
            if not isinstance(coordinates, list) or not coordinates:
                raise SpatialObservationError("Each cell group requires a nonempty _xz list")
            properties = {key: value for key, value in group.items() if key != "_xz"}
            for point in coordinates:
                if not isinstance(point, list) or len(point) != 2:
                    raise SpatialObservationError("Group coordinates must be explicit [world_x, world_z] pairs")
                append(properties, *point)
                if len(result) > 64 * 64:
                    raise SpatialObservationError("Construction cell groups exceed the native 64 by 64 scope")
    return result

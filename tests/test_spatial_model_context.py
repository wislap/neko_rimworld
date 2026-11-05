import copy
import json

from plugin.plugins.neko_rimworld.agent.context.context_pruning import ContextPruner


def test_full_native_scope_fits_model_view_without_losing_cell_facts():
    cells = [{"x": x, "z": z, "known": True, "status": "observed", "terrain": "Soil", "roof": None,
        "terrain_layers": {"top": "Soil", "foundation": None, "temporary": None}, "walkable": True,
        "roof_within_support_range": False, "zone_id": None, "area_ids": []} for z in range(64) for x in range(64)]
    cells[61] = {"x": 61, "z": 0, "known": False, "status": "fogged"}
    cells[90]["unknown_extension"] = {"hazard": None, "status": "unavailable"}
    data = {"format": "construction_observation.v1", "map_id": "Map_1", "cells": cells,
        "objects": [{"id": "Wall_1", "stage": "blueprint"}], "coverage": {"atomic": False, "cells_complete": True, "unknown_cells": 1},
        "sample": {"started_tick": 1, "finished_tick": 9}, "observation_id": "original"}
    source = {"ok": True, "tool": "inspect_construction", "data": data}
    original = copy.deepcopy(source)
    view = ContextPruner().compile(source, max_view_chars=131072)
    projected = json.loads(view.content)["data"]
    restored = []
    for group in projected.pop("cell_groups"):
        positions = group.pop("_xz")
        restored.extend({**copy.deepcopy(group), "x": x, "z": z} for x, z in positions)
    projected.pop("cell_groups_meaning")
    projected["cells"] = sorted(restored, key=lambda cell: (cell["z"], cell["x"]))
    assert projected == data
    assert source == original
    assert view.view_chars < view.source_chars / 5


def test_unknown_formats_and_cell_extensions_are_not_reinterpreted():
    source = {"ok": True, "tool": "inspect_construction", "data": {"format": "future-format", "cells": [{"x": 1, "z": 1}]}}
    assert json.loads(ContextPruner().compile(source).content) == source
    source["data"]["format"] = "construction_observation.v1"
    source["data"]["cells"][0]["_xz"] = "extension-owned"
    assert json.loads(ContextPruner().compile(source).content) == source
    source["data"]["cells"][0].pop("_xz")
    source["data"]["cell_groups_meaning"] = "native extension-owned meaning"
    assert json.loads(ContextPruner().compile(source).content) == source

import copy
import json

import pytest

from plugin.plugins.neko_rimworld.agent.world.construction_geometry import compile_plan, inspect_layout
from plugin.plugins.neko_rimworld.serialization import identity_digest
from plugin.plugins.neko_rimworld.agent.world.spatial_observation import SpatialObservationError, observation_cells
from plugin.plugins.neko_rimworld.agent.world.spatial_rendering import build_render_spec, render_scene_svg
from plugin.plugins.neko_rimworld.game import protocol as wire
from plugin.plugins.neko_rimworld.workspace import construction
from plugin.plugins.neko_rimworld.workspace.area_evidence import facts as area_facts


def grouped(data):
    result = copy.deepcopy(data)
    groups = {}
    for row in result.pop("cells"):
        properties = {key: value for key, value in row.items() if key not in {"x", "z"}}
        key = json.dumps(properties, sort_keys=True)
        groups.setdefault(key, {**properties, "_xz": []})["_xz"].append([row["x"], row["z"]])
    result["cell_groups"] = list(groups.values())
    return result


def observation(width=7, height=7):
    return {
        "format": "construction_observation.v1",
        "map_id": "Map_1",
        "bounds": {"min_x": 9, "min_z": 19, "width": width, "height": height},
        "sample": {"load_epoch": "load-1", "started_tick": 120, "finished_tick": 121, "atomic": False},
        "coverage": {"cells_complete": True, "objects_complete": True, "visibility_complete": True, "unknown_cells": 0},
        "objects": [],
        "future_scope": {"retain": [None, False, 0]},
        "cells": [
            {
                "x": x,
                "z": z,
                "known": True,
                "status": "observed",
                "terrain": "Soil",
                "roof": "RoofConstructed",
                "walkable": True,
                "roof_within_support_range": True,
                "area_ids": ["Area_Home_1", "Area_BuildRoof_1"],
                "zone_id": None,
                "extension": {"null": None, "flags": [False, True]},
            }
            for z in range(19, 19 + height)
            for x in range(9, 9 + width)
        ],
    }


def plan():
    catalog = [
        {
            "definition": "Wall",
            "size": {"x": 1, "z": 1},
            "layer": "building",
            "passability": "Impassable",
            "is_room_boundary": True,
            "is_door": False,
        },
        {
            "definition": "Door",
            "size": {"x": 1, "z": 1},
            "layer": "building",
            "passability": "PassThroughOnly",
            "is_room_boundary": True,
            "is_door": True,
        },
    ]
    return compile_plan(
        {
            "map_id": "Map_1",
            "origin": {"x": 10, "z": 20},
            "rooms": [
                {
                    "id": "room",
                    "bounds": {"x": 0, "z": 0, "width": 5, "height": 5},
                    "roof": True,
                    "doors": [{"id": "entrance", "side": "south", "offset": 2}],
                }
            ],
        },
        catalog,
    )


def test_expansion_preserves_unknown_null_extensions_and_does_not_mutate_evidence():
    data = observation()
    data["cells"][0] = {"x": 9, "z": 19, "known": False, "status": "fogged", "extension": {"value": [None, False]}}
    compressed = grouped(data)
    original = copy.deepcopy(compressed)
    assert observation_cells(compressed) == data["cells"]
    expanded = observation_cells(compressed)
    expanded[0]["extension"]["value"].append("changed")
    expanded[1]["extension"]["flags"].append("changed")
    assert compressed == original
    assert "changed" not in expanded[2]["extension"]["flags"]
    assert "roof" not in expanded[0]
    assert compressed["sample"] == data["sample"] and compressed["bounds"] == data["bounds"]


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"cells": [], "cell_groups": []},
        {"cells": None},
        {"cell_groups": None},
        {"cell_groups": [{}]},
        {"cell_groups": [{"_xz": []}]},
        {"cell_groups": [{"x": 1, "_xz": [[1, 2]]}]},
        {"cell_groups": [{"_xz": [[1]]}]},
        {"cell_groups": [{"_xz": [[True, 2]]}]},
        {"cell_groups": [{"_xz": [[1, 2**31]]}]},
        {"cell_groups": [{"_xz": [[1, 2], [1, 2]]}]},
        {"cell_groups": [{"known": True, "_xz": [[1, 2]]}, {"known": False, "_xz": [[1, 2]]}]},
        {"cells": [{"x": 1, "z": 2}, {"x": 1, "z": 2}]},
        {"cells": [{"x": 1}]},
    ],
)
def test_ambiguous_missing_invalid_and_duplicate_coordinates_are_rejected(data):
    with pytest.raises(SpatialObservationError):
        observation_cells(data)


def test_grouped_and_flat_geometry_drawing_and_roof_facts_are_equivalent():
    flat = observation()
    compressed = grouped(flat)
    compiled = plan()
    first, second = inspect_layout(compiled, flat), inspect_layout(compiled, compressed)
    assert first["status"] == second["status"] == "passed"
    assert {k: v for k, v in first.items() if k != "observation_sha256"} == {
        k: v for k, v in second.items() if k != "observation_sha256"
    }
    first_spec, second_spec = build_render_spec(flat, compiled, first), build_render_spec(compressed, compiled, second)
    assert {k: v for k, v in first_spec.items() if k != "source_sha256"} == {
        k: v for k, v in second_spec.items() if k != "source_sha256"
    }
    # The actual original response remains the source identity, even when its
    # expanded cell meaning is equal. Only provenance labels differ in the SVG.
    assert second_spec["source_sha256"] == identity_digest(compressed) != first_spec["source_sha256"]
    normalized = (
        render_scene_svg(compressed, compiled, second)
        .replace(identity_digest(compressed), identity_digest(flat))
        .replace(identity_digest(compressed)[:16], identity_digest(flat)[:16])
    )
    assert normalized == render_scene_svg(flat, compiled, first)
    adoption = construction.prepare_adoption(
        [], plan_id="house", task_id="task", plan=compiled, source={}, load_epoch="load-1"
    )
    state = construction.project([{"seq": 1, "load_epoch": "load-1", **adoption}], load_epoch="load-1")
    arguments = {"operation": "inspect_construction", "observation_id": "obs", "load_epoch": "load-1"}
    assert area_facts(state, compressed, **arguments) == area_facts(state, flat, **arguments)
    assert area_facts(state, compressed, **arguments)[0]["stage"] == "built"


def test_unknown_and_missing_group_properties_remain_inconclusive_not_fabricated():
    flat = observation()
    needed = next(row for row in flat["cells"] if (row["x"], row["z"]) == (11, 21))
    needed.clear()
    needed.update(x=11, z=21, known=False, status="fogged")
    compressed = grouped(flat)
    compiled = plan()
    assert inspect_layout(compiled, compressed)["status"] == "inconclusive"
    assert (
        next(row for row in build_render_spec(compressed)["cells"] if (row["x"], row["z"]) == (11, 21))["known"]
        is False
    )
    current = {
        "plans": [{"plan_id": "house", "plan": compiled, "elements": [{"id": "room:roof", "stage": "unobserved"}]}]
    }
    args = {"operation": "inspect_construction", "observation_id": "obs", "load_epoch": "load-1"}
    assert area_facts(current, compressed, **args) == []
    needed.update(known=True, walkable=True)
    assert area_facts(current, grouped(flat), **args) == []  # missing roof is not an explicit null roof


def envelope(data):
    return {
        "v": 2,
        "type": "query_result",
        "connection_epoch": "1" * 32,
        "colony_id": "2" * 32,
        "load_epoch": "3" * 32,
        "seq": 1,
        "request_id": "4" * 32,
        "game_tick": 121,
        "snapshot_sequence": 2,
        "payload": {"status": "ok", "issues": [], "data": data},
    }


def test_full_64_scope_grouped_response_passes_real_protocol_codec_node_budget():
    flat = observation(64, 64)
    compressed = grouped(flat)
    raw = json.dumps(envelope(compressed), separators=(",", ":")).encode()
    assert len(raw) < wire.MAX_MOD_TO_PLUGIN_FRAME_BYTES
    decoded = wire.decode_envelope(raw, direction=wire.Direction.MOD_TO_PLUGIN, authenticated=True)
    assert observation_cells(decoded.payload.data) == flat["cells"]
    encoded = wire.serialize_envelope(decoded)
    assert wire.decode_envelope(encoded, direction=wire.Direction.MOD_TO_PLUGIN, authenticated=True) == decoded
    flat_raw = json.dumps(envelope(flat), separators=(",", ":")).encode()
    assert len(flat_raw) < wire.MAX_MOD_TO_PLUGIN_FRAME_BYTES  # The rejected limit is nodes, not bytes.
    with pytest.raises(wire.ProtocolError, match="too many items"):
        wire.decode_envelope(
            flat_raw,
            direction=wire.Direction.MOD_TO_PLUGIN,
            authenticated=True,
        )

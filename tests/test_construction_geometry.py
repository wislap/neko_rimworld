import copy

import pytest

from plugin.plugins.neko_rimworld.agent.world.construction_geometry import (
    ConstructionPlanError,
    apply_plan_patch,
    compile_plan,
    inspect_layout,
    observation_bounds,
    occupied_cells,
)


@pytest.fixture
def catalog():
    return {
        "format": "construction_catalog.v1",
        "definitions": [
            {
                "definition": "Wall",
                "size": {"x": 1, "z": 1},
                "passability": "Impassable",
                "is_door": False,
                "is_room_boundary": True,
                "layer": "building",
            },
            {
                "definition": "Door",
                "size": {"x": 1, "z": 1},
                "passability": "PassThroughOnly",
                "is_door": True,
                "is_room_boundary": True,
                "layer": "building",
            },
            {
                "definition": "Bench",
                "size": {"x": 3, "z": 1},
                "passability": "Impassable",
                "is_door": False,
                "is_room_boundary": False,
                "has_interaction_cell": True,
                "interaction_offset": {"x": 0, "z": -1},
                "layer": "building",
            },
            {"definition": "Tile", "size": {"x": 1, "z": 1}, "layer": "floor"},
            {"definition": "Conduit", "size": {"x": 1, "z": 1}, "layer": "conduit"},
            {
                "definition": "SyntheticRoofHolder",
                "size": {"x": 1, "z": 1},
                "layer": "building",
                "passability": "Impassable",
                "holds_roof": True,
                "is_door": False,
                "is_room_boundary": False,
            },
        ],
    }


@pytest.fixture
def house():
    return {
        "format": "construction_plan.v1",
        "map_id": "map-1",
        "origin": {"x": 100, "z": 200},
        "rooms": [
            {
                "id": "bedroom",
                "bounds": {"x": 0, "z": 0, "width": 6, "height": 6},
                "wall": {"definition": "Wall", "stuff": "GraniteBlocks"},
                "doors": [{"id": "entrance", "side": "south", "offset": 2}],
                "roof": True,
                "floor": {"definition": "Tile"},
            }
        ],
    }


def observation(plan, *, fog=None, blocked=None):
    b = plan["bounds"]
    ox, oz = plan["origin"]["x"], plan["origin"]["z"]
    cells = [
        {"x": x + ox, "z": z + oz, "known": (x, z) != fog, "walkable": (x, z) != blocked}
        for x in range(b["min_x"] - 1, b["min_x"] + b["width"] + 1)
        for z in range(b["min_z"] - 1, b["min_z"] + b["height"] + 1)
    ]
    return {
        "format": "construction_observation.v1",
        "map_id": plan["map_id"],
        "cells": cells,
        "coverage": {"cells_complete": True, "objects_complete": True, "visibility_complete": True},
    }


def codes(result):
    return {item["code"] for item in result["issues"]}


def test_house_expands_floor_roof_and_does_not_mutate_input(house, catalog):
    original = copy.deepcopy(house)
    compiled = compile_plan(house, catalog)
    assert house == original
    assert len(compiled["elements"]) == 20 + 16
    assert len(compiled["areas"][0]["cells"]) == 16
    assert compiled["elements"] == compile_plan(compiled, catalog)["elements"]
    result = inspect_layout(compiled, observation(compiled))
    assert result["status"] == "passed"
    assert result["native_eligibility"] == result["roof_support"] == "not_checked"


def test_shared_walls_keep_identity_and_merge_room_ownership(house, catalog):
    house["rooms"].append(
        {
            "id": "kitchen",
            "bounds": {"x": 5, "z": 0, "width": 6, "height": 6},
            "wall": {"definition": "Wall", "stuff": "GraniteBlocks"},
            "doors": [{"id": "passage", "side": "west", "offset": 3}],
        }
    )
    compiled = compile_plan(house, catalog)
    items = {item["id"]: item for item in compiled["elements"]}
    assert items["wall@5,2"]["room_ids"] == ["bedroom", "kitchen"]
    assert items["passage"]["room_ids"] == ["bedroom", "kitchen"]
    assert "wall@5,3" not in items
    assert inspect_layout(compiled, observation(compiled))["status"] == "passed"
    house["rooms"][1]["wall"]["stuff"] = "WoodLog"
    with pytest.raises(ConstructionPlanError, match="Shared walls"):
        compile_plan(house, catalog)


def test_footprint_rotation_and_native_interaction_offset(house, catalog):
    assert occupied_cells({"x": 0, "z": 0}, {"x": 2, "z": 3}, 1) == {
        (-1, -1),
        (0, -1),
        (1, -1),
        (-1, 0),
        (0, 0),
        (1, 0),
    }
    assert occupied_cells({"x": 0, "z": 0}, {"x": 2, "z": 2}, 2) == {(-1, -1), (-1, 0), (0, -1), (0, 0)}
    house["elements"] = [{"id": "work", "definition": "Bench", "position": {"x": 3, "z": 3}, "rotation": 1}]
    item = next(i for i in compile_plan(house, catalog)["elements"] if i["id"] == "work")
    assert item["interaction_cell"] == {"x": 2, "z": 3}
    assert item["footprint"] == [{"x": 3, "z": 2}, {"x": 3, "z": 3}, {"x": 3, "z": 4}]


def test_layer_coexistence_and_full_footprint_collision(house, catalog):
    house["elements"] = [
        {"id": "work", "definition": "Bench", "position": {"x": 3, "z": 3}},
        {"id": "wire", "definition": "Conduit", "position": {"x": 3, "z": 3}},
    ]
    compiled = compile_plan(house, catalog)
    assert "footprint_overlap" not in codes(inspect_layout(compiled))
    house["elements"].append({"id": "obstacle", "definition": "Wall", "position": {"x": 4, "z": 3}})
    assert "footprint_overlap" in codes(inspect_layout(compile_plan(house, catalog)))


def test_patch_does_not_regenerate_wall_and_preserves_unaffected_ids(house, catalog):
    first = compile_plan(house, catalog)
    changed = apply_plan_patch(first, {"remove": ["wall@0,2"]}, catalog)
    assert "wall@0,2" not in {item["id"] for item in changed["elements"]}
    assert len(changed["elements"]) == len(first["elements"]) - 1
    assert "room_boundary_open" in codes(inspect_layout(changed))
    with pytest.raises(ConstructionPlanError):
        apply_plan_patch(first, {"remove": [{}]}, catalog)


def test_blocked_native_interaction_and_door_are_found(house, catalog):
    house["elements"] = [
        {"id": "work", "definition": "Bench", "position": {"x": 3, "z": 3}},
        {"id": "obstacle", "definition": "Wall", "position": {"x": 3, "z": 2}},
        {"id": "doorstop", "definition": "Wall", "position": {"x": 2, "z": 1}},
    ]
    result = inspect_layout(compile_plan(house, catalog))
    assert {"interaction_blocked", "door_entry_blocked"} <= codes(result)


def test_observation_unknown_is_inconclusive_without_false_failure(house, catalog):
    plan = compile_plan(house, catalog)
    result = inspect_layout(plan, observation(plan, fog=(2, 1)))
    assert result["status"] == "inconclusive"
    assert "door_entry_blocked" not in codes(result)
    assert "observation_incomplete" in codes(result)
    assert inspect_layout(plan, observation(plan, blocked=(2, 1)))["status"] == "failed"
    assert inspect_layout(plan)["status"] == "inconclusive"


def test_model_role_cannot_turn_native_wall_into_door(house, catalog):
    house["rooms"][0]["doors"][0]["definition"] = "Wall"
    assert "room_has_no_door" in codes(inspect_layout(compile_plan(house, catalog)))


def test_facility_on_other_side_of_internal_wall_is_unreachable(house, catalog):
    house["elements"] = [
        {"id": f"partition-{x}", "definition": "Wall", "position": {"x": x, "z": 2}, "role": "wall"}
        for x in range(1, 5)
    ] + [{"id": "work", "definition": "Bench", "position": {"x": 3, "z": 4}}]
    assert "facility_unreachable" in codes(inspect_layout(compile_plan(house, catalog)))


def test_default_door_ids_do_not_depend_on_list_order(house, catalog):
    house["rooms"][0]["doors"] = [{"side": "north", "offset": 2}, {"side": "south", "offset": 2}]
    original = compile_plan(house, catalog)
    house["rooms"][0]["doors"].reverse()
    assert original["elements"] == compile_plan(house, catalog)["elements"]


def test_room_overlap_is_reported(house, catalog):
    house["rooms"].append({"id": "overlap", "bounds": {"x": 1, "z": 1, "width": 4, "height": 4}, "doors": []})
    assert "room_interiors_overlap" in codes(inspect_layout(compile_plan(house, catalog)))


def test_impassable_roof_holder_does_not_enclose_room(house, catalog):
    compiled = compile_plan(house, catalog)
    changed = apply_plan_patch(
        compiled,
        {
            "upsert": [
                {"id": "wall@0,2", "definition": "SyntheticRoofHolder", "position": {"x": 0, "z": 2}, "role": "wall"}
            ]
        },
        catalog,
    )
    assert "room_boundary_open" in codes(inspect_layout(changed))
    house["rooms"][0]["wall"]["definition"] = "SyntheticRoofHolder"
    assert "room_boundary_open" in codes(inspect_layout(compile_plan(house, catalog)))


def test_missing_native_room_boundary_fact_is_inconclusive(house, catalog):
    del catalog["definitions"][0]["is_room_boundary"]
    result = inspect_layout(compile_plan(house, catalog), observation(compile_plan(house, catalog)))
    assert result["status"] == "inconclusive"
    assert "room_boundary_unknown" in codes(result)
    assert "room_boundary_open" not in codes(result)


def test_largest_supported_plan_reserves_native_exterior_observation_ring(house, catalog):
    house["rooms"][0]["bounds"].update(width=62, height=3)
    plan = compile_plan(house, catalog)
    assert observation_bounds(plan) == {"min_x": 99, "min_z": 199, "width": 64, "height": 5}
    assert inspect_layout(plan, observation(plan))["status"] == "passed"
    house["rooms"][0]["bounds"]["width"] = 63
    with pytest.raises(ConstructionPlanError, match="exterior ring"):
        compile_plan(house, catalog)


def test_multiple_native_interaction_cells_require_one_reachable_seat(house, catalog):
    catalog["definitions"].append(
        {
            "definition": "SchoolDesk",
            "size": {"x": 2, "z": 1},
            "layer": "building",
            "passability": "PassThroughOnly",
            "is_room_boundary": False,
            "is_door": False,
            "has_interaction_cell": False,
            "interaction_offset": None,
            "interaction_offsets": [{"x": 1, "z": -1}, {"x": 0, "z": -1}],
        }
    )
    house["rooms"][0]["bounds"].update(width=7, height=7)
    house["rooms"][0]["doors"][0]["offset"] = 1
    house["elements"] = [
        {"id": "desk", "definition": "SchoolDesk", "position": {"x": 3, "z": 3}, "role": "wall"},
        {"id": "seat-stop-1", "definition": "Wall", "position": {"x": 3, "z": 2}},
        {"id": "seat-stop-2", "definition": "Wall", "position": {"x": 4, "z": 2}},
    ]
    plan = compile_plan(house, catalog)
    desk = next(item for item in plan["elements"] if item["id"] == "desk")
    assert desk["interaction_cells"] == [{"x": 3, "z": 2}, {"x": 4, "z": 2}]
    assert "interaction_cell" not in desk
    assert {"interaction_blocked", "facility_unreachable"} <= codes(inspect_layout(plan, observation(plan)))
    house["elements"].pop()
    plan = compile_plan(house, catalog)
    assert inspect_layout(plan, observation(plan))["status"] == "passed"


def test_standalone_facility_checks_current_observed_use_location(catalog):
    plan = compile_plan(
        {
            "map_id": "map-1",
            "origin": {"x": 0, "z": 0},
            "elements": [{"id": "work", "definition": "Bench", "position": {"x": 0, "z": 0}}],
        },
        catalog,
    )
    result = inspect_layout(plan, observation(plan, blocked=(0, -1)))
    assert "facility_unreachable" in codes(result)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(origin={"x": True, "z": 0}),
        lambda p: p["rooms"][0].update(bounds={"x": 0, "z": 0, "width": 65, "height": 6}),
        lambda p: p["rooms"][0]["doors"][0].update(offset=0),
        lambda p: p.update(elements=[{"id": "x", "definition": "Missing", "position": {"x": 1, "z": 1}}]),
    ],
)
def test_invalid_plan_rejected(house, catalog, mutation):
    mutation(house)
    with pytest.raises(ConstructionPlanError):
        compile_plan(house, catalog)

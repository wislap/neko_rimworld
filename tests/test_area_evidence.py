import copy

import pytest

from plugin.plugins.neko_rimworld.workspace import construction
from plugin.plugins.neko_rimworld.workspace.area_evidence import facts

EPOCH = "load-1"


def state(kind="roof", *, stage="unobserved", identity=None):
    return {
        "plans": [
            {
                "plan_id": "house",
                "plan": {
                    "format": "construction_plan.v1",
                    "compiled": True,
                    "map_id": "Map_1",
                    "origin": {"x": 10, "z": 20},
                    "elements": [],
                    "areas": [{"id": "room:area", "kind": kind, "cells": [{"x": 0, "z": 0}, {"x": 1, "z": 0}]}],
                },
                "elements": [
                    {
                        "id": "room:area",
                        "stage": stage,
                        "fresh": stage != "unobserved",
                        "fact": {"field_identity": identity} if identity else None,
                    }
                ],
            }
        ]
    }


def roof_observation(roof="RoofConstructed"):
    return {
        "format": "construction_observation.v1",
        "map_id": "Map_1",
        "coverage": {"cells_complete": True},
        "cells": [{"x": x, "z": 20, "known": True, "roof": roof} for x in (10, 11)],
    }


def area_observation(kind="build_roof", *, identity="Area_BuildRoof_1", cells=None):
    selected = cells if cells is not None else [{"x": x, "z": 20} for x in (10, 11, 12)]
    return {
        "map_id": "Map_1",
        "complete": True,
        "objects": [
            {
                "id": identity,
                "map_id": "Map_1",
                "kind": kind,
                "status": "observed",
                "complete": True,
                "cells": selected,
                "cell_fields": [{**cell, "object": identity, "field": f"area_flag:{cell['x']}:{cell['z']}"} for cell in selected],
            }
        ],
    }


def read(current, observation, operation="inspect_construction"):
    return facts(current, observation, operation=operation, observation_id="obs-1", load_epoch=EPOCH)


@pytest.mark.parametrize(
    "kind,roof,expected",
    [
        ("roof", "RoofConstructed", "built"),
        ("roof", None, "missing"),
        ("remove_roof", None, "built"),
        ("remove_roof", "RoofConstructed", "missing"),
    ],
)
def test_spatial_roof_facts_require_actual_all_cell_roof_state(kind, roof, expected):
    current, observation = state(kind), roof_observation(roof)
    original = copy.deepcopy((current, observation))
    result = read(current, observation)
    assert len(result) == 1 and result[0]["stage"] == expected
    assert result[0]["object_id"] is None and result[0]["field_identity"] is None
    assert result[0]["observation_id"] == "obs-1" and result[0]["load_epoch"] == EPOCH
    assert (current, observation) == original


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(map_id="Map_2"),
        lambda d: d["coverage"].update(cells_complete=False),
        lambda d: d["cells"][0].update(known=False),
        lambda d: d["cells"][0].pop("roof"),
        lambda d: d["cells"].pop(),
        lambda d: d["cells"].append(d["cells"][0]),
        lambda d: d["cells"][0].update(roof=False),
        lambda d: d.update(sample={"load_epoch": "old-load"}),
    ],
)
def test_incomplete_unknown_or_wrong_world_cannot_create_roof_fact(mutation):
    observation = roof_observation()
    mutation(observation)
    assert read(state(), observation) == []


@pytest.mark.parametrize(
    "kind,native,expected,field",
    [
        ("roof", "build_roof", "designated", "per_cell"),
        ("remove_roof", "no_roof", "designated", "per_cell"),
        ("home", "home", "built", "per_cell"),
        ("allowed", "allowed", "built", "per_cell"),
        ("stockpile", "stockpile", "built", "zone_cells"),
        ("growing", "growing", "built", "zone_cells"),
    ],
)
def test_complete_area_coverage_uses_native_identity_without_claiming_ownership(kind, native, expected, field):
    result = read(state(kind), area_observation(native, identity="RealNativeArea_7"), "inspect_areas")
    assert len(result) == 1 and result[0]["stage"] == expected
    assert result[0]["object_id"] is None
    if field == "per_cell":
        assert result[0]["field_identity"] is None
        assert result[0]["field_identities"] == [
            {"object": "RealNativeArea_7", "field": "area_flag:10:20"},
            {"object": "RealNativeArea_7", "field": "area_flag:11:20"},
        ]
    else:
        assert result[0]["field_identity"] == {"object": "RealNativeArea_7", "field": field}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(complete=False),
        lambda d: d["objects"][0].update(complete=False),
        lambda d: d["objects"][0].update(map_id="Map_2"),
        lambda d: d["objects"][0].update(status="unknown"),
        lambda d: d["objects"][0].pop("cells"),
        lambda d: d["objects"][0].update(kind="growing"),
        lambda d: d["objects"][0]["cells"].pop(0),
    ],
)
def test_incomplete_or_unmatched_area_rows_do_not_compile_absence(mutation):
    observation = area_observation("home")
    mutation(observation)
    assert read(state("home"), observation, "inspect_areas") == []


def test_weaker_designation_does_not_demote_fresh_physical_roof():
    assert read(state(stage="built"), area_observation(), "inspect_areas") == []
    assert read(state(stage="built"), roof_observation(None))[0]["stage"] == "missing"
    stale = state(stage="built")
    stale["plans"][0]["elements"][0]["fresh"] = False
    assert read(stale, area_observation(), "inspect_areas")[0]["stage"] == "designated"


def test_known_area_row_losing_coverage_marks_missing_but_unreturned_row_does_not():
    identity = {"object": "Area_Allowed_7", "field": "area_cells"}
    current = state("allowed", stage="built", identity=identity)
    narrowed = area_observation("allowed", identity=identity["object"], cells=[{"x": 10, "z": 20}])
    assert read(current, narrowed, "inspect_areas")[0]["stage"] == "missing"
    narrowed["objects"] = []
    assert read(current, narrowed, "inspect_areas") == []


def test_different_rows_are_not_unioned_into_one_plan_area():
    observation = area_observation("allowed", cells=[{"x": 10, "z": 20}])
    other = copy.deepcopy(observation["objects"][0])
    other.update(id="other", cells=[{"x": 11, "z": 20}])
    observation["objects"].append(other)
    assert read(state("allowed"), observation, "inspect_areas") == []


def test_physical_roof_fact_round_trip_can_complete_and_later_invalidate_house():
    plan = state()["plans"][0]["plan"]
    adoption = construction.prepare_adoption(
        [], plan_id="house", task_id="task-1", plan=plan, source={}, load_epoch=EPOCH
    )
    events = [{"seq": 1, "load_epoch": EPOCH, **adoption}]
    for native in (roof_observation(), roof_observation(None)):
        projected = construction.project(events, load_epoch=EPOCH)
        arguments = read(projected, native)[0]
        spec = construction.prepare_fact(events, **arguments)
        assert spec["payload"]["evidence_kind"] == "cells"
        events.append({"seq": len(events) + 1, "load_epoch": EPOCH, **spec})
        current = construction.project(events, load_epoch=EPOCH)["plans"][0]
        assert current["construction_complete"] == (native["cells"][0]["roof"] is not None)
        assert current["purpose_status"] == "not_evaluated"


def test_area_coverage_round_trip_accepts_actual_native_area_field_identity():
    plan = state("stockpile")["plans"][0]["plan"]
    spec = construction.prepare_adoption([], plan_id="house", task_id="task-1", plan=plan, source={}, load_epoch=EPOCH)
    events = [{"seq": 1, "load_epoch": EPOCH, **spec}]
    arguments = read(
        construction.project(events, load_epoch=EPOCH),
        area_observation("stockpile", identity="Zone_Stockpile_7"),
        "inspect_areas",
    )[0]
    fact = construction.prepare_fact(events, **arguments)
    events.append({"seq": 2, "load_epoch": EPOCH, **fact})
    current = construction.project(events, load_epoch=EPOCH)["plans"][0]
    assert current["construction_complete"] is True
    assert current["elements"][0]["object_id"] is None
    assert current["purpose_status"] == "not_evaluated"


def test_local_flag_reads_keep_explicit_scope_and_native_per_cell_identities():
    observation = area_observation("home", identity="Area_Home_7")
    row = observation["objects"][0]
    row.pop("cells")
    row["write_flags"] = {"native_instance": "instance", "cells": [
        {"x": 10, "z": 20, "enabled": True, "manual_id": None},
        {"x": 11, "z": 20, "enabled": True, "manual_id": None},
    ]}
    result = read(state("home"), observation, "inspect_areas")[0]
    assert result["stage"] == "built" and result["field_identity"] is None
    assert result["field_identities"] == [
        {"object": "Area_Home_7", "field": "area_flag:10:20"},
        {"object": "Area_Home_7", "field": "area_flag:11:20"},
    ]
    row["write_flags"]["cells"].pop()
    assert read(state("home"), observation, "inspect_areas") == []


def test_whole_area_coverage_does_not_invent_an_area_cells_ownership_alias():
    observation = area_observation("home")
    observation["objects"][0].pop("cell_fields")
    result = read(state("home"), observation, "inspect_areas")[0]
    assert result["stage"] == "built" and result["field_identity"] is None and result["field_identities"] is None

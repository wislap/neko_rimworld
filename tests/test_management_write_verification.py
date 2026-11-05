"""Post-commit verification uses later native facts for each new result shape."""

import copy

import pytest

from ..agent.write.verification import _equal, _single, verify_op
from ..game.write_catalog import catalog_digest

BOUND = ("connection", "colony", "a" * 32)
OPERATIONS = ("set_production_bill", "add_production_bill", "copy_production_bill", "remove_production_bill",
              "reorder_production_bills", "set_food_policy", "set_food_policy_content", "create_food_policy",
              "copy_food_policy", "delete_food_policy", "create_zone", "create_allowed_area", "set_zone_cells", "set_area_cells",
              "remove_zone", "remove_allowed_area", "place_blueprint", "cancel_blueprint", "restart_blueprint",
              "set_deconstruct", "set_mine", "set_cut_plant", "set_area_cell_flags")


def food_filter():
    return {"allowed_defs": ["MealFine", "MealSimple"], "special_filters": {"AllowRotten": False},
            "hitpoints": [0.0, 1.0], "quality": ["Awful", "Legendary"], "mental_break_chance": [0.0, 1.0]}


def food_content(instance="policy-instance", label="Food"):
    return {"native_instance": instance, "label": label, "filter": food_filter(), "users": [], "users_complete": True}


def production_settings():
    return {"suspended": True, "repeat_mode": "TargetCount", "repeat_count": 1, "target_count": 10,
            "pause_when_satisfied": True, "unpause_when_you_have": 5, "include_equipped": False,
            "include_tainted": False, "hp_range": {"min": 0.0, "max": 1.0},
            "quality_range": {"min": "Awful", "max": "Legendary"}, "limit_to_allowed_stuff": False,
            "ingredient_search_radius": 3.0, "allowed_skill_range": {"min": 0, "max": 20},
            "worker": {"kind": "any"}, "store": {"mode": "DropOnFloor", "storage_id": None},
            "include_storage": None, "ingredient_filter": {"definitions": ["Cloth", "Synthread"],
                "special_filters": [{"definition": "AllowRotten", "allowed": False}],
                "hit_points": {"min": 0.0, "max": 1.0}, "mental_break_chance": {"min": 0.0, "max": 1.0},
                "quality": {"min": "Awful", "max": "Legendary"}}}


def bill_row(identifier="Bill_new"):
    return {"bill_id": identifier, "bill_kind": "production", "recipe": "Make_Parka",
            "runtime_type": "RimWorld.Bill_ProductionWithUft", "supported_standard_production": True,
            **production_settings(), "presentation": {"custom_name": "Winter", "style": None,
                "precept_id": None, "global_style": False, "graphic_index": None}, "unfinished": None}


def descriptor(name, operation, extract, **targets):
    value_type = "boolean" if name in {"copy_production_bill", "remove_production_bill", "cancel_blueprint", "set_deconstruct", "set_mine", "set_cut_plant", "remove_zone", "remove_allowed_area"} else "object"
    properties = {"op": {"const": name}, "value": {"type": value_type}, **{key: {"type": "string"} for key in targets}}
    return {"op": name, "schema": {"type": "object", "additionalProperties": False,
        "properties": properties, "required": list(properties)}, "expect_schema": {"type": "object"},
        "observation": {"read": {"tool": "look", "native_operation": operation, "arguments": {}}, "extract": extract}}


def case(name):
    extract = {"kind": "field", "objects_path": ["objects"], "identity_path": ["id"], "status_path": ["status"],
               "status_value": "observed", "object_id": {"arg": "worktable"}, "path": ["bills"]}
    op = {"op": name, "worktable": "Table_1", "map": "Map_0", "value": {}, "expect": {}}
    operation = "inspect_production"
    data = {"complete": True, "objects": [{"id": "Table_1", "map_id": "Map_0", "status": "observed",
            "complete": True, "bills": [bill_row()]}]}
    fields = [{"object": "Table_1", "field": "production_bills", "op": 0}]
    if name in {"add_production_bill", "copy_production_bill", "remove_production_bill", "reorder_production_bills"}:
        op["expect"] = {"bill_ids": []}
        if name == "add_production_bill":
            op["value"] = {"recipe": "Make_Parka", "settings": {"ingredient_search_radius": 3, "suspended": True}}
        elif name == "copy_production_bill":
            op.update(bill="Bill_source", value=True)
            op["expect"].update(bill_ids=["Bill_source"], source={"bill_id": "Bill_source", "recipe": "Make_Parka", "state_fingerprint": "fp"})
            data["objects"][0]["bills"].insert(0, bill_row("Bill_source"))
        elif name == "remove_production_bill":
            op.update(bill="Bill_old", value=False, expect={"bill_ids": ["Bill_old", "Bill_new"], "bill_id": "Bill_old"})
        else:
            op.update(value={"bill_ids": ["Bill_new"]}, expect={"bill_ids": ["Bill_new"]})
            extract.update(kind="ordered_ids", id_field="bill_id", expect_field="bill_ids", complete_path=["complete"], complete_value=True)
        ids = [row["bill_id"] for row in data["objects"][0]["bills"]]
        committed = {"bill_ids": ids}
        if name in {"add_production_bill", "copy_production_bill"}:
            row = data["objects"][0]["bills"][-1]
            committed["created"] = {"bill_id": "Bill_new", "recipe": row["recipe"], "runtime_type": row["runtime_type"],
                "settings": production_settings(), "presentation": row["presentation"], "unfinished_id": None}
        elif name == "remove_production_bill":
            committed["removed"] = "Bill_old"
    elif name == "set_production_bill":
        op.update(bill="Bill_new", value={"ingredient_search_radius": 3, "hp_range": {"min": 0.123456789, "max": 1},
                                         "worker": {"kind": "any"}}, expect={})
        data["objects"][0]["bills"][0]["hp_range"]["min"] = _single(0.123456789)
        extract.update(kind="object_fields", key_field="bill_id", key={"arg": "bill"}, row_require={"bill_kind": "production", "supported_standard_production": True})
        fields = [{"object": "Bill_new", "field": "bill:" + key, "op": 0} for key in op["value"]]
        committed = {key: data["objects"][0]["bills"][0][key] for key in op["value"]}
    elif name in {"set_deconstruct", "set_mine", "set_cut_plant"}:
        operation, designation = "inspect_state", name.removeprefix("set_")
        op = {"op": name, "map": "Map_0", "thing": "Thing_1", "value": True, "expect": False}
        extract.update(object_id={"arg": "thing"}, identity_path=["target", "id"],
                       status_path=["facets", "condition", "status"], path=["facets", "condition", "construction_designations", designation],
                       match=[{"path": ["target", "map"], "value": {"arg": "map"}}])
        data = {"objects": [{"target": {"id": "Thing_1", "map": "Map_0"},
                             "facets": {"condition": {"status": "observed", "construction_designations": {designation: True}}}}]}
        fields, committed = [{"object": "Thing_1", "field": "designation:" + designation, "op": 0}], True
    elif name == "set_food_policy":
        operation = "inspect_state"
        op = {"op": name, "pawn": "Pawn_A", "value": {"policy_id": "Food_1", "native_instance": "policy-instance"}, "expect": {}}
        extract.update(object_id={"arg": "pawn"}, identity_path=["target", "id"], status_path=["facets", "work", "status"], path=["facets", "work", "food_policy_key"])
        data = {"objects": [{"target": {"id": "Pawn_A"}, "facets": {"work": {"status": "observed", "food_policy_key": op["value"]}}}]}
        fields, committed = [{"object": "Pawn_A", "field": "food_policy", "op": 0}], op["value"]
    elif name in {"create_food_policy", "copy_food_policy", "delete_food_policy", "set_food_policy_content"}:
        operation = "inspect_food_policies"
        op = {"op": name, "policy": "new" if name == "create_food_policy" else "Food_1", "value": {"label": "Food"},
              "expect": {"policy_ids": ["Food_0"]}}
        created = food_content()
        row = {"id": "Food_1", "policy_id": "Food_1", "status": "observed", **created, "write_content": created}
        data = {"status": "ok", "complete": True, "users_complete": True, "structure": {"policy_ids": ["Food_0", "Food_1"]}, "objects": [row]}
        fields = [{"object": "food_policies", "field": "food_policy_structure", "op": 0}]
        extract.update(object_id={"arg": "policy"}, path=["write_content"])
        committed = {"policy_ids": ["Food_0", "Food_1"], "created": {"policy_id": "Food_1", "content": created}}
        if name == "copy_food_policy":
            op["policy"] = "Food_0"
            op["expect"].update(source_id="Food_0", source=food_content("source-instance", "Original"))
        elif name == "delete_food_policy":
            op.update(value={"impact": {"mode": "fixed_users", "users": []}}, expect={"policy_ids": ["Food_0", "Food_1"], "policy_id": "Food_1", "policy": created})
            data.update(structure={"policy_ids": ["Food_0"]}, objects=[])
            committed = {"policy_ids": ["Food_0"], "policy_id": "Food_1", "policy": None}
        elif name == "set_food_policy_content":
            op["value"] = {"label": "Food", "filter": food_filter(), "impact": {"mode": "dynamic_shared"}}
            op["expect"] = food_content()
            fields, committed = [{"object": "Food_1", "field": "food_policy_content", "op": 0}], created
    elif name in {"create_zone", "create_allowed_area", "set_zone_cells", "set_area_cells", "set_area_cell_flags"}:
        operation = "inspect_areas"
        cells = [{"x": 2, "z": 1}, {"x": 1, "z": 1}]
        op = {"op": name, "map": "Map_0", "value": {"cells": cells}, "expect": {"native_instance": "zone-instance", "cells": []}}
        structure = {"map_id": "Map_0", "zone_ids": ["Zone_1"], "allowed_area_ids": []}
        written = {"native_instance": "zone-instance", "cells": list(reversed(cells))}
        row = {"id": "Zone_1", "lookup_id": "Zone_1", "map_id": "Map_0", "kind": "stockpile", "label": "Goods", "hidden": False,
               **written, "settings": {"priority": "Normal", "filter": food_filter()}}
        committed = copy.deepcopy(row)
        row.update(status="observed", complete=True, write_cells=written)
        data = {"status": "ok", "complete": True, "map_id": "Map_0", "structure": structure, "objects": [row]}
        if name == "set_area_cell_flags":
            op["area"] = "home"
            op["value"] = {"cells": [{"x": 2, "z": 1, "enabled": False}, {"x": 1, "z": 1, "enabled": True}]}
            op["expect"]["cells"] = [{"x": 2, "z": 1, "enabled": True, "manual_id": None}, {"x": 1, "z": 1, "enabled": False, "manual_id": None}]
            written = {"native_instance": "zone-instance", "cells": [{**cell, "manual_id": None} for cell in reversed(op["value"]["cells"])]}
            row = {"id": "Area_Home", "lookup_id": "home", "map_id": "Map_0", "kind": "home", "label": "Home",
                   "status": "observed", "complete": True, "write_flags": written}
            data["objects"] = [row]
            extract.update(object_id={"arg": "area"}, identity_path=["lookup_id"], path=["write_flags"],
                           match=[{"path": ["map_id"], "value": {"arg": "map"}}])
            committed = {f"area_flag:{cell['x']}:{cell['z']}": {"native_instance": "zone-instance", "enabled": cell["enabled"], "manual_id": cell["manual_id"]}
                         for cell in written["cells"]}
            fields = [{"object": "Area_Home", "field": field, "op": 0} for field in committed]
        elif name in {"create_zone", "create_allowed_area"}:
            op["value"].update(label="Goods")
            op["expect"] = {"map_id": "Map_0", "zone_ids": [], "allowed_area_ids": []}
            if name == "create_zone":
                op["value"]["kind"] = "stockpile"
            else:
                structure.update(zone_ids=[], allowed_area_ids=["Zone_1"])
                row.update(kind="allowed", users=[], users_complete=True, color={"r": 0.2, "g": 0.4, "b": 1.0, "a": 1.0})
                row.pop("settings")
                row.pop("hidden")
                committed = {key: value for key, value in row.items() if key not in {"lookup_id", "status", "complete", "write_cells"}}
            fields = [{"object": "Map_0", "field": "zone_structure" if name == "create_zone" else "area_structure", "op": 0}]
            committed = {**structure, "created": committed}
        else:
            target = "zone" if name == "set_zone_cells" else "area"
            op[target] = "Zone_1"
            extract.update(object_id={"arg": target}, identity_path=["lookup_id"], path=["write_cells"],
                           match=[{"path": ["map_id"], "value": {"arg": "map"}}])
            fields, committed = [{"object": "Zone_1", "field": "zone_cells" if target == "zone" else "area_cells", "op": 0}], written
            if name == "set_area_cells":
                op["area"] = "Area_1"
                op["expect"]["manual_revision"] = "manual-revision"
                written["manual_revision"] = "manual-revision"
                row.update(id="Area_1", lookup_id="Area_1", kind="allowed", manual_ids=[], manual_ids_complete=True)
                structure.update(zone_ids=[], allowed_area_ids=["Area_1"])
                committed = {f"area_flag:{cell['x']}:{cell['z']}": {"native_instance": "zone-instance", "enabled": True, "manual_id": None}
                             for cell in written["cells"]}
                fields = [{"object": "Area_1", "field": field, "op": 0} for field in committed]
    elif name in {"remove_zone", "remove_allowed_area"}:
        operation, zone = "inspect_areas", name == "remove_zone"
        target, changed, unchanged = ("zone", "zone_ids", "allowed_area_ids") if zone else ("area", "allowed_area_ids", "zone_ids")
        original = {"id": "Region_1", "map_id": "Map_0", "native_instance": "original-instance", "cells": [{"x": 1, "z": 1}],
                    "label": "Old region", "kind": "stockpile" if zone else "allowed"}
        original.update({"references_complete": True, "bill_references": []} if zone else {"users_complete": True, "users": []})
        if zone:
            original.update(storage_group_id=None, storage_group_members=[], storage_group_members_complete=True)
        previous = {"map_id": "Map_0", changed: ["Region_1", "Region_2"], unchanged: [], "removed_id": "Region_1", "removed": original}
        op = {"op": name, "map": "Map_0", target: "Region_1", "value": False, "expect": previous}
        structure = {"map_id": "Map_0", changed: ["Region_2"], unchanged: []}
        data = {"status": "ok", "map_id": "Map_0", "complete": True, "structure_complete": True,
                "structure": structure, "objects": [], "missing_ids": ["Region_1"]}
        fields = [{"object": "Map_0", "field": ("zone_removed:" if zone else "area_removed:") + "Region_1", "op": 0}]
        committed = {**structure, "removed_id": "Region_1", "removed": None}
    else:
        operation = "preview_construction"
        op = {"op": name, "map": "Map_0", "value": {"plan_id": "house", "element_id": "wall-1", "definition": "Wall",
              "x": 1, "z": 2, "rotation": 0, "stuff": "WoodLog", "kind": "building"}, "expect": {}}
        identity = {"object": "Map_0", "field": "construction:element-hash"}
        row = {"status": "observed", "plan_id": "house", "element_id": "wall-1", "map_id": "Map_0", "element": op["value"], "source_id": "Blueprint_1",
               "field_identity": identity, "stage": "blueprint", "lineage": ["Blueprint_1"], "provenance": "native_creation",
               "target": {"id": "Blueprint_1", **{key: value for key, value in op["value"].items() if key in {"definition", "x", "z", "rotation", "stuff"}}}}
        data = {"format": "construction_intents.v1", "map_id": "Map_0", "complete": True, "objects": [row]}
        fields, committed = [{**identity, "op": 0}], row
        if name == "cancel_blueprint":
            prior = copy.deepcopy(row)
            op = {"op": name, "map": "Map_0", "plan_id": "house", "element_id": "wall-1", "value": False, "expect": prior}
            row.update(stage="model_cancelled", target=None)
            row["cancel_guard"] = copy.deepcopy(row)
            committed = row["cancel_guard"]
        elif name == "restart_blueprint":
            prior = copy.deepcopy(row)
            prior.update(stage="destroyed", target=None)
            op["expect"] = {"intent": prior, "site": {}}
            row.update(source_id="Blueprint_2", lineage=["Blueprint_2"])
            row["target"]["id"] = "Blueprint_2"
    desc = descriptor(name, operation, extract, **{key: value for key, value in op.items() if key not in {"op", "value", "expect"}})
    if name == "add_production_bill":
        desc["schema"]["properties"]["value"]["properties"] = {"recipe": {"type": "string"}, "settings": {"type": "object"}}
    if name in {"set_deconstruct", "set_mine", "set_cut_plant"}:
        desc["expect_schema"] = {"type": "boolean"}
    if name in {"place_blueprint", "cancel_blueprint", "restart_blueprint"}:
        desc["verification_read"] = {"tool": "inspect_construction_intents", "native_operation": "inspect_construction_intents",
                                     "arguments": {"map": {"arg": "map"},
                                                   "plan_id": {"arg": "plan_id"} if name == "cancel_blueprint" else {"value_path": ["plan_id"]},
                                                   "ids": [{"arg": "element_id"} if name == "cancel_blueprint" else {"value_path": ["element_id"]}], "limit": 1}}
    catalog = {"capability": "colony_write.transactions.v1", "version": "colony_write.v1", "modes": ["dry_run", "commit", "undo"], "operations": [desc]}
    catalog["catalog_id"] = catalog_digest(catalog)
    readbacks = ([{**field, "value": committed[field["field"][5:]]} for field in fields] if name == "set_production_bill"
                 else [{**field, "value": committed[field["field"]]} for field in fields] if name in {"set_area_cell_flags", "set_area_cells"}
                 else [{**field, "value": committed} for field in fields])
    for row in readbacks:
        row.pop("op")
    receipt = {"status": "committed", "load_epoch": BOUND[2], "changeset_id": "change", "readback": copy.deepcopy(readbacks)}
    observation = {"observation_id": "later-native", "request": {"operation": "inspect_construction_intents" if name in {"place_blueprint", "cancel_blueprint", "restart_blueprint"} else operation, "arguments": {}},
        "response": {"type": "query_result", "request_id": "native-read", "connection_epoch": BOUND[0], "colony_id": BOUND[1],
            "load_epoch": BOUND[2], "game_tick": 11, "payload": {"status": "ok", "issues": [], "data": copy.deepcopy(data)}}}
    if name in {"remove_zone", "remove_allowed_area"}:
        observation["request"]["arguments"] = {"map": "Map_0", "ids": ["Region_1"], "limit": 100}
    elif name == "set_area_cell_flags":
        observation["request"]["arguments"] = {"map": "Map_0", "ids": ["home"], "cells": copy.deepcopy(op["value"]["cells"])}
    return catalog, op, observation, receipt, fields


def verified(values):
    directory, op, observation, receipt, fields = values
    return verify_op(directory, op, observation, *BOUND, 10, receipt, fields)


@pytest.mark.parametrize("name", OPERATIONS)
def test_new_native_configuration_and_structure_results_are_verifiable(name):
    assert verified(case(name)), name


@pytest.mark.parametrize("name", OPERATIONS)
@pytest.mark.parametrize("problem", ["wrong_epoch", "same_tick", "incomplete", "missing_receipt_field", "duplicate_receipt_field"])
def test_original_later_complete_scope_and_every_readback_remain_required(name, problem):
    values = case(name)
    _, _, observation, receipt, _ = values
    if problem == "wrong_epoch":
        observation["response"]["load_epoch"] = "b" * 32
    elif problem == "same_tick":
        observation["response"]["game_tick"] = 10
    elif problem == "incomplete":
        observation["response"]["payload"]["data"]["complete"] = False
    elif problem == "missing_receipt_field":
        receipt["readback"].pop()
    else:
        receipt["readback"].append(copy.deepcopy(receipt["readback"][0]))
    assert not verified(values)


@pytest.mark.parametrize("name", ["add_production_bill", "copy_production_bill"])
@pytest.mark.parametrize("problem", ["settings", "recipe", "presentation", "runtime", "uft_binding", "same_id"])
def test_bill_creation_requires_current_exact_new_bill_and_configuration(name, problem):
    values = case(name)
    _, op, observation, _, _ = values
    bill = observation["response"]["payload"]["data"]["objects"][0]["bills"][-1]
    if problem == "settings":
        bill["target_count"] += 1
    elif problem == "recipe":
        bill["recipe"] = "WrongRecipe"
    elif problem == "presentation":
        bill["presentation"]["custom_name"] = "Changed"
    elif problem == "runtime":
        bill["runtime_type"] = "Special.Bill_Production"
    elif problem == "uft_binding":
        bill["unfinished"] = {"id": "Uft_1", "bound_bill_id": "another-bill"}
    else:
        op["expect"]["bill_ids"].append("Bill_new")
    assert not verified(values)


def test_initial_settings_cannot_be_proved_only_by_receipt_and_later_defaults():
    values = case("add_production_bill")
    values[1]["value"]["settings"]["target_count"] = 30
    assert not verified(values)


def test_new_copy_operation_requires_its_complete_creation_readback():
    values = case("copy_production_bill")
    created = values[3]["readback"][0]["value"]["created"]
    created.pop("settings")
    assert not verified(values)


def test_current_bill_creation_contract_cannot_downgrade_to_an_old_minimal_receipt():
    values = case("add_production_bill")
    values[1]["value"].pop("settings")
    created = values[3]["readback"][0]["value"]["created"]
    for key in ("runtime_type", "settings", "presentation", "unfinished_id"):
        created.pop(key)
    assert not verified(values)


@pytest.mark.parametrize("name", ["set_food_policy", "set_food_policy_content", "create_food_policy", "copy_food_policy", "set_zone_cells", "set_area_cells", "create_zone", "create_allowed_area"])
def test_reused_native_id_with_different_instance_is_not_the_same_effect(name):
    values = case(name)
    _, _, observation, _, _ = values
    row = observation["response"]["payload"]["data"]["objects"][0]
    if name == "set_food_policy":
        row["facets"]["work"]["food_policy_key"]["native_instance"] = "replacement"
    elif name in {"set_zone_cells", "set_area_cells"}:
        row["write_cells"]["native_instance"] = "replacement"
    else:
        row["native_instance"] = "replacement"
        if "write_content" in row:
            row["write_content"]["native_instance"] = "replacement"
    assert not verified(values)


def test_single_precision_normalization_never_equates_bool_and_number():
    assert _equal({"value": 3}, {"value": 3.0})
    assert not _equal({"value": 1}, {"value": True})
    values = case("set_production_bill")
    values[1]["value"]["ingredient_search_radius"] = True
    assert not verified(values)


@pytest.mark.parametrize("stage", ["frame", "built"])
def test_construction_accepts_only_native_lineage_and_exact_current_element(stage):
    values = case("place_blueprint")
    row = values[2]["response"]["payload"]["data"]["objects"][0]
    row.update(stage=stage, provenance="native_transition", lineage=["Blueprint_1", "Frame_1", "Building_1"])
    row["target"]["id"] = "Building_1"
    assert verified(values)
    row["source_id"] = "lookalike"
    assert not verified(values)


@pytest.mark.parametrize("operation", ["place_blueprint", "restart_blueprint"])
@pytest.mark.parametrize("stage", ["blueprint", "frame", "built"])
def test_native_door_orientation_accepts_only_bound_rotation_policy(operation, stage):
    values = case(operation)
    _, op, observation, receipt, _ = values
    observed = observation["response"]["payload"]["data"]["objects"][0]
    committed = receipt["readback"][0]["value"]
    op["value"]["definition"] = "Door"
    if operation == "restart_blueprint":
        op["expect"]["intent"]["element"]["definition"] = "Door"
    for native in (observed, committed):
        native["element"]["definition"] = "Door"
        native["rotation_policy"] = "native_door"
        native["target"].update(definition="Door", rotation=1,
                                occupied_rect={"min_x": 1, "max_x": 1, "min_z": 2, "max_z": 2})
    if stage != "blueprint":
        observed.update(stage=stage, provenance="native_transition", lineage=[observed["source_id"], "next"])
        observed["target"]["id"] = "next"
    assert verified(values)

    for malformed in (True, 1.0, 4, None):
        observed["target"]["rotation"] = malformed
        assert not verified(values)
    observed["target"]["rotation"] = 1
    observed["target"]["occupied_rect"]["max_x"] = 2
    assert not verified(values)
    observed["target"]["occupied_rect"]["max_x"] = 1
    observed["target"]["definition"] = "Wall"
    assert not verified(values)
    observed["target"]["definition"] = "Door"
    committed.pop("rotation_policy")
    assert not verified(values)


def test_fixed_construction_rotation_remains_exact():
    values = case("place_blueprint")
    observed = values[2]["response"]["payload"]["data"]["objects"][0]
    observed["target"]["rotation"] = 1
    assert not verified(values)


@pytest.mark.parametrize("stage", ["unknown", "destroyed", "user_cancelled", "missing"])
def test_construction_missing_or_cancelled_is_never_current_placement(stage):
    values = case("place_blueprint")
    values[2]["response"]["payload"]["data"]["objects"][0]["stage"] = stage
    assert not verified(values)


@pytest.mark.parametrize("problem", ["another_generation", "still_present", "user_cancelled", "wrong_plan"])
def test_cancel_verification_proves_exact_original_generation_no_longer_exists(problem):
    values = case("cancel_blueprint")
    row = values[2]["response"]["payload"]["data"]["objects"][0]
    if problem == "another_generation":
        row["source_id"] = "AnotherBlueprint"
    elif problem == "still_present":
        row["target"] = {"id": "Blueprint_1"}
    elif problem == "user_cancelled":
        row["stage"] = "user_cancelled"
    else:
        row["plan_id"] = "another-house"
    assert not verified(values)


@pytest.mark.parametrize("problem", ["same_generation", "unknown_original", "changed_element"])
def test_restart_verification_requires_new_generation_of_explicit_cancelled_element(problem):
    values = case("restart_blueprint")
    original = values[1]["expect"]["intent"]
    if problem == "same_generation":
        original["source_id"] = "Blueprint_2"
    elif problem == "unknown_original":
        original["stage"] = "unknown"
    else:
        original["element"]["x"] += 1
    assert not verified(values)


def test_completed_floor_requires_actual_terrain_evidence_from_native_transition():
    values = case("place_blueprint")
    _, op, observation, receipt, _ = values
    op["value"].update(kind="floor", definition="Tile", stuff=None)
    row = observation["response"]["payload"]["data"]["objects"][0]
    row["element"] = copy.deepcopy(op["value"])
    receipt["readback"][0]["value"]["element"] = copy.deepcopy(op["value"])
    row.update(stage="floor_complete", target=None, lineage=["Blueprint_1", "Frame_1"], previous_object_id="Frame_1",
               provenance="native_transition", floor_observed=True)
    assert verified(values)
    row["floor_observed"] = False
    assert not verified(values)


@pytest.mark.parametrize("name", ["remove_zone", "remove_allowed_area"])
@pytest.mark.parametrize("problem", ["no_native_miss", "structure_uncovered", "same_id_reused", "collateral_removal",
                                    "other_collection_changed", "references_unknown", "receipt_still_present", "other_selector", "wrong_map"])
def test_region_removal_needs_explicit_native_absence_and_exact_map_structure(name, problem):
    values = case(name)
    _, op, observation, receipt, _ = values
    data = observation["response"]["payload"]["data"]
    changed, unchanged = ("zone_ids", "allowed_area_ids") if name == "remove_zone" else ("allowed_area_ids", "zone_ids")
    if problem == "no_native_miss":
        data["missing_ids"] = []
    elif problem == "structure_uncovered":
        data.pop("structure_complete")
    elif problem == "same_id_reused":
        data["structure"][changed].append("Region_1")
        data["objects"].append({"id": "Region_1", "native_instance": "replacement-instance"})
    elif problem == "collateral_removal":
        data["structure"][changed] = []
    elif problem == "other_collection_changed":
        data["structure"][unchanged].append("NewRegion")
    elif problem == "references_unknown":
        op["expect"]["removed"]["references_complete" if name == "remove_zone" else "users_complete"] = False
    elif problem == "receipt_still_present":
        receipt["readback"][0]["value"]["removed"] = copy.deepcopy(op["expect"]["removed"])
    elif problem == "other_selector":
        observation["request"]["arguments"]["ids"] = ["another-region"]
    else:
        data["map_id"] = "Map_1"
    assert not verified(values)


@pytest.mark.parametrize("name", ["set_deconstruct", "set_mine", "set_cut_plant"])
def test_preparation_designation_requires_unchanged_original_map_and_bool(name):
    values = case(name)
    row = values[2]["response"]["payload"]["data"]["objects"][0]
    row["target"]["map"] = "Map_1"
    assert not verified(values)
    row["target"]["map"] = "Map_0"
    row["facets"]["condition"]["construction_designations"][name.removeprefix("set_")] = 1
    assert not verified(values)


@pytest.mark.parametrize("problem", ["unselected_cell", "wrong_map", "reused_instance", "wrong_bool", "whole_area_substitute", "different_coordinates", "no_selector", "duplicate_cell"])
def test_local_area_flags_verify_only_selected_native_cells(problem):
    values = case("set_area_cell_flags")
    _, _, observation, _, _ = values
    row = observation["response"]["payload"]["data"]["objects"][0]
    flags = row["write_flags"]
    if problem == "unselected_cell":
        flags["cells"].append({"x": 3, "z": 1, "enabled": True})
    elif problem == "wrong_map":
        row["map_id"] = "Map_1"
    elif problem == "reused_instance":
        flags["native_instance"] = "replacement-instance"
    elif problem == "wrong_bool":
        flags["cells"][0]["enabled"] = 1
    elif problem == "whole_area_substitute":
        row["write_cells"] = row.pop("write_flags")
    elif problem == "different_coordinates":
        observation["request"]["arguments"]["cells"][0]["x"] = 99
    elif problem == "no_selector":
        observation["request"]["arguments"].pop("cells")
    else:
        flags["cells"].append(copy.deepcopy(flags["cells"][0]))
    assert not verified(values)


def test_removed_stockpile_requires_complete_unshared_native_group_scope():
    values = case("remove_zone")
    values[1]["expect"]["removed"].update(storage_group_id="SharedGroup_1", storage_group_members=None,
                                           storage_group_members_complete=False)
    assert not verified(values)


@pytest.mark.parametrize("name", ["set_area_cell_flags", "set_area_cells"])
@pytest.mark.parametrize("problem", ["missing_cell", "extra_cell", "wrong_area_id", "wrong_instance", "wrong_value", "old_composite_field"])
def test_area_ownership_readbacks_cover_every_exact_stable_cell(name, problem):
    values = case(name)
    _, _, _, receipt, fields = values
    if problem == "missing_cell":
        fields.pop()
        receipt["readback"].pop()
    elif problem == "extra_cell":
        fields.append({"object": fields[0]["object"], "field": "area_flag:3:1", "op": 0})
        receipt["readback"].append({"object": fields[0]["object"], "field": "area_flag:3:1",
                                   "value": {"native_instance": "zone-instance", "enabled": True, "manual_id": None}})
    elif problem == "wrong_area_id":
        fields[0]["object"] = "home"
        receipt["readback"][0]["object"] = "home"
    elif problem == "wrong_instance":
        receipt["readback"][0]["value"]["native_instance"] = "replacement-instance"
    elif problem == "wrong_value":
        receipt["readback"][0]["value"]["enabled"] = not receipt["readback"][0]["value"]["enabled"]
    else:
        fields[:] = [{"object": fields[0]["object"], "field": "area_flags:old-composite", "op": 0}]
        receipt["readback"][:] = [{"object": fields[0]["object"], "field": "area_flags:old-composite",
                                  "value": {"native_instance": "zone-instance", "cells": values[1]["value"]["cells"]}}]
    assert not verified(values)


def full_area_delta_case():
    values = case("set_area_cells")
    values[1]["expect"]["cells"] = [{"x": 1, "z": 1}, {"x": 3, "z": 1}]
    values[4][:] = [{"object": "Area_1", "field": "area_flag:2:1", "op": 0},
                   {"object": "Area_1", "field": "area_flag:3:1", "op": 0}]
    values[3]["readback"] = [{"object": "Area_1", "field": "area_flag:2:1", "value": {"native_instance": "zone-instance", "enabled": True, "manual_id": None}},
                             {"object": "Area_1", "field": "area_flag:3:1", "value": {"native_instance": "zone-instance", "enabled": False, "manual_id": None}}]
    return values


def test_full_area_uses_only_symmetric_difference_with_true_and_false_cells():
    values = full_area_delta_case()
    assert verified(values)
    values[3]["readback"][1]["value"]["enabled"] = True
    assert not verified(values)


def test_full_area_does_not_claim_unchanged_cells_or_no_change_transactions():
    values = full_area_delta_case()
    values[4].append({"object": "Area_1", "field": "area_flag:1:1", "op": 0})
    values[3]["readback"].append({"object": "Area_1", "field": "area_flag:1:1", "value": {"native_instance": "zone-instance", "enabled": True, "manual_id": None}})
    assert not verified(values)
    values = case("set_area_cells")
    values[1]["expect"]["cells"] = copy.deepcopy(values[1]["value"]["cells"])
    assert not verified(values)


def test_local_area_noop_cells_still_have_individual_ownership_and_optional_read_flags():
    values = case("set_area_cell_flags")
    values[1]["expect"]["cells"] = [{**cell, "manual_id": None} for cell in values[1]["value"]["cells"]]
    for cell in values[2]["request"]["arguments"]["cells"]:
        cell.pop("enabled")
    assert verified(values)


@pytest.mark.parametrize("count,expected", [(64, True), (65, False)])
def test_area_native_field_limit_is_respected(count, expected):
    values = case("set_area_cell_flags")
    cells = [{"x": index, "z": 1, "enabled": True} for index in range(count)]
    values[1]["value"]["cells"] = copy.deepcopy(cells)
    values[1]["expect"]["cells"] = [{**cell, "enabled": False, "manual_id": None} for cell in cells]
    values[2]["request"]["arguments"]["cells"] = copy.deepcopy(cells)
    values[2]["response"]["payload"]["data"]["objects"][0]["write_flags"]["cells"] = [{**cell, "manual_id": None} for cell in cells]
    values[4][:] = [{"object": "Area_Home", "field": f"area_flag:{cell['x']}:{cell['z']}", "op": 0} for cell in cells]
    values[3]["readback"] = [{"object": field["object"], "field": field["field"], "value": {"native_instance": "zone-instance", "enabled": True, "manual_id": None}}
                             for field in values[4]]
    assert verified(values) is expected


def local_manual_generation_case():
    values = case("set_area_cell_flags")
    for cell in values[1]["expect"]["cells"]:
        cell["manual_id"] = "1" * 32
    for cell in values[2]["response"]["payload"]["data"]["objects"][0]["write_flags"]["cells"]:
        cell["manual_id"] = "1" * 32
    for row in values[3]["readback"]:
        row["value"]["manual_id"] = "1" * 32
    return values


def test_local_area_verification_preserves_the_observed_manual_generation():
    assert verified(local_manual_generation_case())
    values = local_manual_generation_case()
    flags = values[2]["response"]["payload"]["data"]["objects"][0]["write_flags"]["cells"]
    # The effective bool is unchanged; a different manual generation still
    # invalidates the old read and cannot be hidden by changing the receipt.
    flags[0]["manual_id"] = "2" * 32
    assert not verified(values)
    coordinate = f"area_flag:{flags[0]['x']}:{flags[0]['z']}"
    next(row for row in values[3]["readback"] if row["field"] == coordinate)["value"]["manual_id"] = "2" * 32
    assert not verified(values)


@pytest.mark.parametrize("part", ["current", "expected", "receipt", "desired", "invalid_id"])
def test_local_area_manual_generation_is_required_and_never_model_managed(part):
    values = local_manual_generation_case()
    if part == "current":
        values[2]["response"]["payload"]["data"]["objects"][0]["write_flags"]["cells"][0].pop("manual_id")
    elif part == "expected":
        values[1]["expect"]["cells"][0].pop("manual_id")
    elif part == "receipt":
        values[3]["readback"][0]["value"].pop("manual_id")
    elif part == "desired":
        values[1]["value"]["cells"][0]["manual_id"] = "1" * 32
    else:
        values[2]["response"]["payload"]["data"]["objects"][0]["write_flags"]["cells"][0]["manual_id"] = True
    assert not verified(values)


def full_manual_generation_case():
    values = full_area_delta_case()
    row = values[2]["response"]["payload"]["data"]["objects"][0]
    row["manual_ids"] = [{"x": 2, "z": 1, "manual_id": "1" * 32}, {"x": 3, "z": 1, "manual_id": "2" * 32}]
    values[3]["readback"][0]["value"]["manual_id"] = "1" * 32
    values[3]["readback"][1]["value"]["manual_id"] = "2" * 32
    return values


def test_full_mask_verifies_generation_of_both_added_and_cleared_cells():
    values = full_manual_generation_case()
    assert verified(values)
    # Cell (3,1) is absent from the active mask, but its retained generation
    # remains part of the proof instead of being inferred to be null.
    row = values[2]["response"]["payload"]["data"]["objects"][0]
    row["manual_ids"][1]["manual_id"] = "3" * 32
    assert not verified(values)


@pytest.mark.parametrize("problem", ["missing_complete", "incomplete", "duplicate_coordinate", "changed_revision", "missing_revision",
                                    "missing_expected_revision", "bad_manual_id", "receipt_generation_mismatch"])
def test_full_mask_generation_proof_cannot_be_dropped_or_inferred_from_partial_ids(problem):
    values = full_manual_generation_case()
    row = values[2]["response"]["payload"]["data"]["objects"][0]
    if problem == "missing_complete":
        row.pop("manual_ids_complete")
    elif problem == "incomplete":
        row["manual_ids_complete"] = False
    elif problem == "duplicate_coordinate":
        row["manual_ids"].append(copy.deepcopy(row["manual_ids"][0]))
    elif problem == "changed_revision":
        row["write_cells"]["manual_revision"] = "new-manual-revision"
    elif problem == "missing_revision":
        row["write_cells"].pop("manual_revision")
    elif problem == "missing_expected_revision":
        values[1]["expect"].pop("manual_revision")
    elif problem == "bad_manual_id":
        row["manual_ids"][0]["manual_id"] = 1
    else:
        values[3]["readback"][0]["value"]["manual_id"] = None
    assert not verified(values)

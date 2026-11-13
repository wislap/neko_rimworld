import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugin.plugins.neko_rimworld.agent.write.service import WriteService, _substitute
from plugin.plugins.neko_rimworld.game.query_tools import COLONY_OPERATIONS, make_game_tools
from plugin.plugins.neko_rimworld.game.write_catalog import catalog_digest
from plugin.plugins.neko_rimworld.game.write_contract import validate_request
from plugin.plugins.neko_rimworld.tool_contracts import ToolExecutionError

from .test_catalog_tools import _public_source
from .test_write_service import IDENTITY, Port


async def test_management_directory_is_additive_and_preserves_requested_operation():
    original = COLONY_OPERATIONS
    source = _public_source()
    source["colony_management"] = {"version": "colony_management.v1", "operations": [
        {"operation": name, "parameters": {"type": "object", "properties": {}}}
        for name in ("inspect_production", "construction_catalog", "inspect_construction_intents")
    ]}
    source["colony_observation"] = {"version": "colony_observation.v1", "supported_kinds": ["construction"]}
    calls = []

    async def query(args):
        calls.append(copy.deepcopy(args))
        return args

    setup = make_game_tools(source, query)
    tool = next(tool for tool in setup.tools if tool.name == "inspect_construction_intents")
    await tool.handler({"map": "Map_1", "ids": ["wall1"]})
    assert calls == [{"operation": "inspect_construction_intents", "arguments": {"map": "Map_1", "ids": ["wall1"]}}]
    assert setup.directory["colony_observation"]["supported_kinds"] == ["construction"]
    assert COLONY_OPERATIONS is original and len(original) == 9
    source["colony_management"]["operations"].append(source["colony_management"]["operations"][0])
    with pytest.raises(ToolExecutionError):
        make_game_tools(source, query)


def construction_directory():
    descriptor = {"op": "place_blueprint", "schema": {"type": "object", "additionalProperties": False,
        "properties": {"op": {"const": "place_blueprint"}, "map": {"type": "string"}, "value": {"type": "object"}},
        "required": ["op", "map", "value"]}, "expect_schema": {"type": "object"},
        "observation": {"read": {"tool": "look", "native_operation": "preview_construction", "arguments": {
            "object": {"kind": "construction_site", "map": {"arg": "map"}}, "elements": [{"value_path": []}]}},
            "extract": {"kind": "field", "object_id": {"value_path": ["element_id"]}, "objects_path": ["elements"],
                "identity_path": ["element_id"], "path": ["state"], "status_path": ["status"], "status_value": "eligible",
                "match": [{"path": ["element"], "value": {"value_path": []}}, {"path": ["map_id"], "value": {"arg": "map"}}]}}}
    directory = {"version": "colony_write.v1", "capability": "colony_write.transactions.v1", "modes": ["dry_run", "commit", "undo"], "operations": [descriptor]}
    directory["catalog_id"] = catalog_digest(directory)
    return directory


async def test_blueprint_evidence_uses_native_eligibility_exact_element_and_map():
    value = {"element_id": "wall1", "definition": "Wall", "x": 10, "z": 11, "rotation": 0, "kind": "building"}
    op = {"op": "place_blueprint", "map": "Map_1", "value": value}
    row = {"element_id": "wall1", "map_id": "Map_1", "element": copy.deepcopy(value), "status": "eligible", "state": {"stage": "absent", "site": []}}
    observation = {"observation_id": "native-preview", "request": {"operation": "preview_construction"}, "response": {
        **dict(zip(("connection_epoch", "colony_id", "load_epoch"), IDENTITY)), "payload": {"status": "ok", "issues": [], "data": {"elements": [row]}}}}
    port = Port()
    service = WriteService(construction_directory(), [observation], port, lambda: IDENTITY)
    await service.dry_run({"entry_id": "task", "ops": [op]})
    assert port.calls[-1]["ops"][0]["expect"] == row["state"]
    assert _substitute({"value_path": []}, op) == value
    for changed in ({**op, "map": "Map_2"}, {**op, "value": {**value, "x": 99}}):
        with pytest.raises(ToolExecutionError) as exc:
            await service.dry_run({"entry_id": "task", "ops": [changed]})
        assert exc.value.code == "evidence_required"
    row["status"] = "blocked"
    with pytest.raises(ToolExecutionError):
        await service.dry_run({"entry_id": "task", "ops": [op]})


async def test_field_evidence_honors_native_manual_source_coverage_prerequisite():
    from .test_write_service import catalog, observation, op

    directory = catalog()
    extract = directory["operations"][0]["observation"]["extract"]
    extract["require"] = [{"path": ["manual_ids_complete"], "value": True}]
    directory["catalog_id"] = catalog_digest(directory)
    original = observation()
    row = original["response"]["payload"]["data"]["objects"][0]
    row["manual_ids_complete"] = False
    port = Port()
    service = WriteService(directory, [original], port, lambda: IDENTITY)
    with pytest.raises(ToolExecutionError) as error:
        await service.dry_run({"entry_id": "task", "ops": [op()]})
    assert error.value.code == "evidence_required" and port.calls == []
    row["manual_ids_complete"] = True
    await service.dry_run({"entry_id": "task", "ops": [op()]})
    assert len(port.calls) == 1


def test_new_write_shapes_keep_old_configuration_checks():
    base = {"changeset_id": "change", "author": "planner", "entry_id": "task", "load_epoch": "a" * 32, "idempotency_key": "key"}
    cases = [
        {"op": "create_food_policy", "policy": "new", "expect": {"policy_ids": []}, "value": {"label": "Meals"}},
        {"op": "set_food_policy_content", "policy": "FoodRestriction_1", "expect": {"label": "Meals", "filter": {}, "users": [], "users_complete": True}, "value": {"label": "Meals", "filter": {}, "impact": {"mode": "dynamic_shared"}}},
        {"op": "remove_production_bill", "map": "Map_1", "worktable": "Table1", "bill": "Bill_1", "expect": {"bill_id": "Bill_1"}, "value": False},
        {"op": "add_production_bill", "worktable": "Table1", "expect": {"bill_ids": []}, "value": {"recipe": "MakeShirt", "settings": {"target_count": 3}}},
    ]
    for op in cases:
        validate_request("commit", {**base, "ops": [{**op, "evidence": "observed"}]})
    with pytest.raises(ValueError):
        validate_request("commit", {**base, "ops": [{"op": "set_bill", "bill": "Bill_1", "expect": {"target_count": 1}, "value": {"suspended": True}, "evidence": "observed"}]})


def test_complete_cell_edits_too_large_for_wire_never_enter_dispatch():
    from plugin.plugins.neko_rimworld.game.bridge import BridgePayloadTooLarge, BridgeServer
    from plugin.plugins.neko_rimworld.game.protocol import SessionDescriptor

    server = BridgeServer("x" * 32, port=0)
    server._ready_epoch = "1" * 32
    server._session = SessionDescriptor("2" * 32, "a" * 32, 1, 0)
    server._capabilities = frozenset({"colony_write.transactions.v1"})
    cells = [{"x": index, "z": 100} for index in range(1024)]
    changeset = {"changeset_id": "change", "author": "planner", "entry_id": "task", "load_epoch": "a" * 32,
        "idempotency_key": "key", "ops": [{"op": "set_area_cells", "map": "Map_1", "area": "Area_" + str(i),
            "expect": {"native_instance": "original", "cells": cells}, "value": {"cells": cells}, "evidence": "observed"} for i in range(3)]}
    audit = []
    with pytest.raises(BridgePayloadTooLarge):
        server.commit(changeset, audit_dispatch=lambda *args: audit.append(args))
    assert not server._pending and server._outbound.empty() and not audit


async def test_selected_ledger_read_is_capability_bound_without_changing_legacy_read():
    from plugin.plugins.neko_rimworld.game.observation import Observation
    from plugin.plugins.neko_rimworld.game.query_tools import CatalogTools

    from .test_catalog_tools import response

    directory = json.loads((Path(__file__).parent / "fixtures/colony-write-v1/catalog.json").read_text(encoding="utf-8"))
    bridge = SimpleNamespace(connection_key="1" * 32, session=SimpleNamespace(colony_id="colony", load_epoch="a" * 32),
        capabilities=frozenset({"colony_write.transactions.v1", "colony_write.ledger_snapshot.v1", "colony_write.ledger_snapshot.selection.v1"}))
    calls = []

    async def query(arguments):
        calls.append(copy.deepcopy(arguments))
        raw = response().to_dict()
        raw.update(connection_epoch=bridge.connection_key, colony_id="colony", load_epoch="a" * 32)
        source = _public_source()
        source["colony_write"] = directory
        raw["payload"] = {"status": "ok", "issues": [], "data": source}
        return Observation({"response": raw})

    provider = CatalogTools(lambda: bridge, query)
    setup = await provider.load()
    fields = [{"object": "Area_1", "field": "area_flag:10:10"}]
    await setup.scoped_ledger_query(fields)
    assert calls[-1] == {"operation": "write_ledger_snapshot", "arguments": {"fields": fields}}
    await setup.ledger_query()
    assert calls[-1] == {"operation": "write_ledger_snapshot", "arguments": {}}
    bridge.capabilities -= {"colony_write.ledger_snapshot.selection.v1"}
    setup = await provider.load()
    assert setup.scoped_ledger_query is None and setup.ledger_query is not None

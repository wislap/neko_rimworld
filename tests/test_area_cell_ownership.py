"""Area ownership follows each actual native field across overlapping patches."""

import copy
from types import SimpleNamespace

import pytest

from ..agent.runtime.audit import AgentAuditLedger
from ..agent.runtime.colony_work import work_tools
from ..agent.runtime.session import AgentRunSession
from ..agent.world.construction_geometry import compile_plan
from ..game.observation import Observation as NativeObservation
from ..tool_contracts import ToolExecutionError, ToolSetup
from ..workspace.journal import Journal as NativeJournal
from ..workspace.runtime import FrontdeskService
from ..workspace.takeovers import TakeoverControl, return_capability
from .test_colony_work_integration import CATALOG, PLAN, seed
from .test_takeover_control import Journal, Observation


def cell(x, z, enabled=True):
    return {"x": x, "z": z, "enabled": enabled}


@pytest.mark.asyncio
async def test_overlapping_patch_keeps_the_protected_cell_identity():
    journal = Journal()
    rows = [{"object": "Area_Home_1", "field": "area_flag:10:20", "value": True, "current_value": False,
             "current_status": "observed", "may_change_by_game": False, "changeset_id": "first", "tick": 1},
            {"object": "Area_Home_1", "field": "area_flag:11:20", "value": True, "current_value": True,
             "current_status": "observed", "may_change_by_game": False, "changeset_id": "first", "tick": 1}]

    async def ledger():
        return Observation({"ledger_status": "valid", "entries": rows})

    async def fields(operation, arguments):
        selected = arguments["changeset"]["ops"][0]["value"]["cells"]
        return Observation({"fields": [{"object": "Area_Home_1", "field": f"area_flag:{item['x']}:{item['z']}", "op": 0}
                                        for item in selected]})

    control = TakeoverControl(journal, ledger, lambda: ("c", "x", "e"), fields)
    for chosen in ([cell(10, 20), cell(11, 20)], [cell(10, 20), cell(12, 20)]):
        with pytest.raises(ToolExecutionError) as error:
            await control.guard_changeset({"ops": [{"op": "set_area_cell_flags", "value": {"cells": chosen}}]})
        assert error.value.code == "field_taken_over"
        assert error.value.details["fields"] == [{"object": "Area_Home_1", "field": "area_flag:10:20"}]
    opened = await journal.call("takeovers", "x")
    assert [(item["payload"]["object"], item["payload"]["field"]) for item in opened] == [
        ("Area_Home_1", "area_flag:10:20")]
    # Changing the patch's other cells does not broaden protection to the area.
    await control.guard_changeset({"ops": [{"op": "set_area_cell_flags", "value": {"cells": [cell(11, 20), cell(12, 20)]}}]})


@pytest.mark.asyncio
async def test_new_planner_value_is_compared_with_the_latest_same_cell_ledger_entry():
    journal = Journal()
    rows = [{"object": "Area_BuildRoof_1", "field": "area_flag:10:20", "value": False, "current_value": False,
             "current_status": "observed", "may_change_by_game": False, "changeset_id": "second", "tick": 2},
            {"object": "Area_BuildRoof_1", "field": "area_flag:11:20", "value": True, "current_value": False,
             "current_status": "observed", "may_change_by_game": False, "changeset_id": "first", "tick": 1}]

    async def ledger():
        return Observation({"ledger_status": "valid", "entries": rows})

    async def fields(operation, arguments):
        return Observation({"fields": [{"object": "Area_BuildRoof_1", "field": "area_flag:10:20", "op": 0},
                                        {"object": "Area_BuildRoof_1", "field": "area_flag:12:20", "op": 0}]})

    control = TakeoverControl(journal, ledger, lambda: ("c", "x", "e"), fields)
    await control.guard_changeset({"ops": [{"op": "set_area_cell_flags", "value": {"cells": [cell(10, 20), cell(12, 20)]}}]})
    opened = await journal.call("takeovers", "x")
    assert [event["payload"]["field"] for event in opened] == ["area_flag:11:20"]
    assert not [event for event in journal.events if event["kind"] == "user.takeover" and event["payload"]["field"] == "area_flag:10:20"]


@pytest.mark.asyncio
async def test_real_manual_snapshot_closes_stream_delay_and_only_actual_cells_need_return(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = NativeJournal(path)
    seed(journal)
    plan = copy.deepcopy(PLAN)
    plan["areas"] = [{"id": "home", "kind": "home", "cells": [{"x": 0, "z": 0}, {"x": 1, "z": 0}]}]
    journal.colony_work_command("colony", "epoch", 10, "planner", "adopt", {
        "plan_id": "house", "task_id": "task", "plan": compile_plan(plan, CATALOG), "source": {}})
    journal.close()
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=20, connection_epoch="connection")
    service = FrontdeskService(path, None, game_session=lambda: game)
    instance, changeset = "a" * 32, "b" * 32
    baseline = {"native_instance": instance, "enabled": True, "manual_id": None}
    rows = [{"object": "Area_Home_1", "field": f"area_flag:{x}:20", "value": copy.deepcopy(baseline),
             "current_value": copy.deepcopy(baseline), "current_status": "observed", "may_change_by_game": True,
             "changeset_id": changeset, "tick": 10, "author": "planner"} for x in (10, 11)]

    def identity():
        return ("connection", "colony", "epoch")

    def native(operation, data):
        return NativeObservation({"observation_id": "native-snapshot", "request": {"operation": operation, "arguments": {}},
            "response": {"connection_epoch": "connection", "colony_id": "colony", "load_epoch": "epoch",
                         "game_tick": game.game_tick, "payload": {"status": "ok", "data": copy.deepcopy(data)}}})

    scoped_calls = []

    async def ledger():
        raise AssertionError("A targeted-cap guard must not load the whole colony ledger")

    async def scoped(fields):
        scoped_calls.append(copy.deepcopy(fields))
        requested = {(field["object"], field["field"]) for field in fields}
        selected = [row for row in rows if (row["object"], row["field"]) in requested]
        return native("write_ledger_snapshot", {"ledger_status": "valid", "complete": True, "entry_count": len(selected),
            "entries": selected, "manual_sources_complete": True, "scope": {"kind": "fields", "fields": fields}})

    async def fields(operation, arguments):
        selected = arguments["changeset"]["ops"][0]["value"]["cells"]
        return native(operation, {"complete": True, "field_count": len(selected), "fields": [
            {"object": "Area_Home_1", "field": f"area_flag:{item['x']}:{item['z']}", "op": 0} for item in selected]})

    def manual(index, manual_id, enabled):
        old = copy.deepcopy(rows[index]["current_value"])
        new = {"native_instance": instance, "enabled": enabled, "manual_id": manual_id}
        change = {"manual_id": manual_id, "manual_order": rows[index].get("manual_source", {}).get("manual_order", 0) + 1, "origin_load_epoch": "epoch", "source": "user_designator", "map_id": "map", "object": "Area_Home_1",
                  "field": rows[index]["field"], "cell": {"x": 10 + index, "z": 20}, "area_kind": "home",
                  "designator": "Designator_AreaHomeClear", "old_value": old, "new_value": new,
                  "ownership": {"value": copy.deepcopy(rows[index]["value"]), "changeset_id": changeset}, "tick": game.game_tick}
        rows[index].update(current_value=new, manual_source=change)
        return change

    def patch(*xs):
        return {"entry_id": "task", "ops": [{"op": "set_area_cell_flags", "map": "map", "area": "Area_Home_1",
                                               "value": {"cells": [cell(x, 20) for x in xs]}}]}

    control = TakeoverControl(service, ledger, identity, fields, scoped_provider=scoped)
    # Ordinary automatic changes remain unclassified as user control.
    rows[0]["current_value"]["enabled"] = False
    await control.guard_changeset(patch(10, 12))
    assert await service.call("takeovers", "colony") == []
    rows[0]["current_value"] = copy.deepcopy(baseline)
    source = manual(0, "1" * 32, False)
    with pytest.raises(ToolExecutionError) as error:
        await control.guard_changeset(patch(10, 12))
    assert error.value.code == "field_taken_over"
    assert await service.call("game_event_cursor", "colony", "epoch", "connection") == 0
    assert service.authorization_revision == 1
    taken = await service.call("takeovers", "colony")
    assert [event["payload"]["field"] for event in taken] == ["area_flag:10:20"]
    state = (await service.call("colony_work", "colony", "epoch", "connection"))["construction"]["plans"][0]
    area = next(element for element in state["elements"] if element["id"] == "home")
    assert area["stop"]["scopes"] == [{"object": "Area_Home_1", "field": "area_flag:10:20"}]
    await control.guard_changeset(patch(11, 12))  # same area, unaffected fields remain available

    setup = SimpleNamespace(ledger_query=ledger, write_identity=identity, write_fields_query=fields, scoped_ledger_query=scoped)
    async def setup_provider():
        return setup

    words = await service.call("record_utterance", "colony", "epoch", 20, "把实际接管的A格交还", command_binding_check=lambda: True)
    returned = await return_capability(service, game, {words}, setup_provider, binding_check=lambda: True).handler({
        "takeover_seq": taken[0]["seq"], "utterance_seq": words})
    assert returned["field"] == "area_flag:10:20"
    assert scoped_calls[-1] == [{"object": "Area_Home_1", "field": "area_flag:10:20"}]
    await control.guard_changeset(patch(10, 12))
    # The delayed stream copy has the same native generation and cannot reopen it.
    await service.call("ingest_game_event", "colony", "epoch", 20, "connection", {
        "cursor": 1, "kind": "area_manual_changed", "connection_epoch": "connection", "colony_id": "colony",
        "load_epoch": "epoch", "game_tick": 20,
        "data": {"cursor": 1, "kind": "area_manual_changed", "source": "user_designator", "changes": [source]}},
        command_binding_check=lambda: True)
    assert await service.call("takeovers", "colony") == []
    assert len(await service.call("events_by_kind", "colony", ("area.manual",))) == 1
    game.game_tick = 21
    manual(0, "2" * 32, True)
    with pytest.raises(ToolExecutionError) as error:
        await control.guard_changeset(patch(10, 12))
    assert error.value.code == "field_taken_over"
    state = (await service.call("colony_work", "colony", "epoch", "connection"))["construction"]["plans"][0]
    area = next(element for element in state["elements"] if element["id"] == "home")
    assert area["stop"]["native_action_id"] == "2" * 32
    assert not area["stop"].get("released_by")
    await service.close()


@pytest.mark.asyncio
async def test_engineering_area_orders_use_bounded_positive_local_patches(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = NativeJournal(path)
    seed(journal)
    plan = copy.deepcopy(PLAN)
    plan["areas"] = [
        {"id": "home", "kind": "home", "cells": [{"x": index % 10, "z": index // 10} for index in range(65)]},
        {"id": "roof", "kind": "roof", "cells": [{"x": 0, "z": 0}, {"x": 1, "z": 0}]},
    ]
    journal.colony_work_command("colony", "epoch", 10, "planner", "adopt", {
        "plan_id": "house", "task_id": "task", "plan": compile_plan(plan, CATALOG), "source": {}})
    journal.close()
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=20, connection_epoch="connection")
    service = FrontdeskService(path, None, game_session=lambda: game)
    session = AgentRunSession.create(ledger=AgentAuditLedger("House"), question="House", colony_id="colony", role="planner")
    session.colony_work_service, session.colony_work_game = service, game
    session.binding_check = lambda: True
    data = {"map_id": "map", "complete": True, "objects": [
        {"id": "Area_Home_1", "lookup_id": "home", "map_id": "map", "kind": "home", "status": "observed", "complete": True,
         "write_flags": {"native_instance": "instance", "cells": [cell(10, 20), cell(99, 99)]}},
        {"id": "Area_Roof_1", "lookup_id": "build_roof", "map_id": "map", "kind": "build_roof", "status": "observed", "complete": True,
         "write_flags": {"native_instance": "instance", "cells": [cell(10, 20, False), cell(11, 20, False)]}},
        {"id": "Area_NoRoof_1", "lookup_id": "no_roof", "map_id": "map", "kind": "no_roof", "status": "observed", "complete": True,
         "write_flags": {"native_instance": "instance", "cells": [cell(10, 20), cell(11, 20, False)]}},
    ]}
    session.observations = [{"observation_id": "areas", "request": {"operation": "inspect_areas", "arguments": {}},
        "response": {"connection_epoch": "connection", "colony_id": "colony", "load_epoch": "epoch", "game_tick": 20,
                     "payload": {"status": "ok", "data": data}}}]
    setup = ToolSetup((), directory={"colony_management": {}}, write_identity=lambda: ("connection", "colony", "epoch"))
    result = await work_tools(setup, session)[0].handler({"action": "orders", "arguments": {
        "task_id": "task", "plan_id": "house", "element_ids": [], "area_ids": ["home", "roof"], "areas_observation_id": "areas"}})
    assert [batch["field_count"] for batch in result["batches"]] == [64, 1]
    assert all(op["op"] == "set_area_cell_flags" for op in result["operations"])
    assert all(item["enabled"] is True for op in result["operations"] for item in op["value"]["cells"])
    assert all(len(op["value"]["cells"]) <= 64 for op in result["operations"])
    assert not [op for op in result["operations"] if op["area"] == "Area_NoRoof_1"]
    assert result["uncovered"][0]["cells"] == [{"x": 10, "z": 20}]
    home = next(op for op in result["operations"] if op["area"] == "Area_Home_1")
    assert not [point for point in home["value"]["cells"] if (point["x"], point["z"]) in {(10, 20), (99, 99)}]
    assert home["value"]["cells"] == sorted(home["value"]["cells"], key=lambda point: (point["z"], point["x"]))
    assert result["batches"][0]["area_reads"][0]["arguments"]["ids"] == ["Area_Home_1"]
    assert not result["writes_dispatched"]
    await service.close()


@pytest.mark.asyncio
async def test_unowned_observed_cell_manual_aba_can_be_explicitly_returned_before_first_write(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=20, connection_epoch="connection")
    service = FrontdeskService(tmp_path / "journal.sqlite", None, game_session=lambda: game)
    await service.call("observe_epoch", "colony", "epoch", 20, command_binding_check=lambda: True)
    instance = "a" * 32
    before_lookup = {"native_instance": instance, "enabled": False, "manual_id": None}
    manual_id = "2" * 32
    final_value = {"native_instance": instance, "enabled": False, "manual_id": manual_id}
    sources = []

    def identity():
        return ("connection", "colony", "epoch")

    def native(operation, data):
        return NativeObservation({"observation_id": "unowned-native", "request": {"operation": operation, "arguments": {}},
            "response": {"connection_epoch": "connection", "colony_id": "colony", "load_epoch": "epoch", "game_tick": game.game_tick,
                         "payload": {"status": "ok", "data": copy.deepcopy(data)}}})

    async def ledger():
        return native("write_ledger_snapshot", {"ledger_status": "valid", "complete": True, "entry_count": 0,
                                               "entries": [], "manual_sources": sources, "manual_sources_complete": True})

    async def fields(operation, arguments):
        return native(operation, {"complete": True, "field_count": 1, "fields": [
            {"object": "Area_Home_1", "field": "area_flag:10:20", "op": 0}]})

    change = {"entry_id": "task", "ops": [{"op": "set_area_cell_flags", "map": "map", "area": "Area_Home_1",
                                             "value": {"cells": [cell(10, 20)]}}]}
    control = TakeoverControl(service, ledger, identity, fields)
    await control.guard_changeset(change)
    # A real native observed fence reports manual false->true->false even though
    # the final flag equals the old lookup. There has been no planner commit.
    sources.append({"manual_id": manual_id, "manual_order": 2, "origin_load_epoch": "epoch", "source": "user_designator", "map_id": "map", "object": "Area_Home_1",
        "field": "area_flag:10:20", "cell": {"x": 10, "z": 20}, "area_kind": "home", "designator": "Designator_AreaHome",
        "old_value": {"native_instance": instance, "enabled": True, "manual_id": "1" * 32}, "new_value": final_value,
        "ownership": None, "tick": 20, "current_status": "observed", "current_value": final_value})
    assert before_lookup["enabled"] == final_value["enabled"]
    with pytest.raises(ToolExecutionError) as error:
        await control.guard_changeset(change)
    assert error.value.code == "field_taken_over"
    takeovers = await service.call("takeovers", "colony")
    assert len(takeovers) == 1 and takeovers[0]["payload"]["ai_value"] is None
    assert takeovers[0]["payload"]["changeset_id"] is None
    words = await service.call("record_utterance", "colony", "epoch", 20, "把A格交给模型继续", command_binding_check=lambda: True)
    setup = SimpleNamespace(ledger_query=ledger, write_identity=identity, write_fields_query=fields)

    async def setup_provider():
        return setup

    result = await return_capability(service, game, {words}, setup_provider, binding_check=lambda: True).handler({
        "takeover_seq": takeovers[0]["seq"], "utterance_seq": words})
    assert result["manual_id"] == manual_id and result["prior_ai_ownership"] is False
    returns = await service.call("events_by_kind", "colony", ("user.return",))
    baseline = returns[-1]["payload"]["accepted_baseline"]
    assert "field" not in baseline and baseline["manual_area"]["current_value"] == final_value
    await control.guard_changeset(change)
    assert await service.call("takeovers", "colony") == []
    assert len(await service.call("events_by_kind", "colony", ("area.manual",))) == 1
    await service.close()


@pytest.mark.asyncio
async def test_returned_latest_of_three_same_tick_generations_ignores_unseen_older_events(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=20, connection_epoch="connection")
    service = FrontdeskService(tmp_path / "journal.sqlite", None, game_session=lambda: game)
    await service.call("observe_epoch", "colony", "epoch", 20, command_binding_check=lambda: True)
    instance = "a" * 32
    sources = []
    for order in (1, 2, 3):
        source = {"manual_id": str(order) * 32, "manual_order": order, "origin_load_epoch": "epoch", "source": "user_designator", "map_id": "map",
            "object": "Area_Home_1", "field": "area_flag:10:20", "cell": {"x": 10, "z": 20}, "area_kind": "home",
            "designator": "RimWorld.Dialog_ManageAreas.DoAreaRow", "tick": 20, "ownership": None,
            "old_value": {"native_instance": instance, "enabled": order % 2 == 0, "manual_id": str(order - 1) * 32 if order > 1 else None},
            "new_value": {"native_instance": instance, "enabled": order % 2 == 1, "manual_id": str(order) * 32}}
        sources.append(source)

    async def ledger():
        return NativeObservation({"observation_id": "latest-three", "request": {"operation": "write_ledger_snapshot", "arguments": {}},
            "response": {"connection_epoch": "connection", "colony_id": "colony", "load_epoch": "epoch", "game_tick": 20,
                "payload": {"status": "ok", "data": {"ledger_status": "valid", "complete": True, "entry_count": 0, "entries": [],
                    "manual_sources_complete": True, "manual_sources": [{**sources[2], "current_status": "observed", "current_value": sources[2]["new_value"]}]}}}})

    def identity():
        return ("connection", "colony", "epoch")

    control = TakeoverControl(service, ledger, identity, None)
    await control.snapshot()
    taken = await service.call("takeovers", "colony")
    assert len(taken) == 1
    words = await service.call("record_utterance", "colony", "epoch", 20, "交还这个格子", command_binding_check=lambda: True)
    setup = SimpleNamespace(ledger_query=ledger, write_identity=identity, write_fields_query=None)

    async def setup_provider():
        return setup

    await return_capability(service, game, {words}, setup_provider, binding_check=lambda: True).handler({
        "takeover_seq": taken[0]["seq"], "utterance_seq": words})
    assert await service.call("takeovers", "colony") == []
    for cursor, source in enumerate(sources[:2], 1):
        result = await service.call("ingest_game_event", "colony", "epoch", 20, "connection", {
            "cursor": cursor, "kind": "area_manual_changed", "connection_epoch": "connection", "colony_id": "colony", "load_epoch": "epoch",
            "game_tick": 20, "data": {"kind": "area_manual_changed", "cursor": cursor, "source": "user_designator", "changes": [source]}},
            command_binding_check=lambda: True)
        assert not result.authorization_changed
        assert await service.call("takeovers", "colony") == []
    archived = await service.call("events_by_kind", "colony", ("area.manual",))
    assert [event["payload"]["disposition"] for event in archived] == ["applied", "superseded", "superseded"]
    assert len(await service.call("events_by_kind", "colony", ("game.event",))) == 2
    await service.close()


@pytest.mark.asyncio
async def test_old_save_fresh_return_and_new_action_use_the_native_origin_epoch_branch(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=20, connection_epoch="connection")
    service = FrontdeskService(tmp_path / "journal.sqlite", None, game_session=lambda: game)
    await service.call("observe_epoch", "colony", "epoch", 20, command_binding_check=lambda: True)
    instance = "a" * 32

    def source(order, identifier, *, origin="epoch", enabled=True):
        return {"manual_id": identifier, "manual_order": order, "origin_load_epoch": origin, "source": "user_designator",
            "map_id": "map", "object": "Area_Home_1", "field": "area_flag:10:20", "cell": {"x": 10, "z": 20}, "area_kind": "home",
            "designator": "RimWorld.Dialog_ManageAreas.DoAreaRow", "tick": 20, "ownership": None,
            "old_value": {"native_instance": instance, "enabled": not enabled, "manual_id": None},
            "new_value": {"native_instance": instance, "enabled": enabled, "manual_id": identifier}}

    current = [source(3, "3" * 32)]

    def identity():
        return (game.connection_epoch, game.colony_id, game.load_epoch)

    async def ledger():
        marker = current[0]
        return NativeObservation({"observation_id": "restore-native", "request": {"operation": "write_ledger_snapshot", "arguments": {}},
            "response": {"connection_epoch": game.connection_epoch, "colony_id": game.colony_id, "load_epoch": game.load_epoch,
                "game_tick": game.game_tick, "payload": {"status": "ok", "data": {"ledger_status": "valid", "complete": True,
                    "entry_count": 0, "entries": [], "manual_sources_complete": True,
                    "manual_sources": [{**marker, "current_status": "observed", "current_value": marker["new_value"]}]}}}})

    control = TakeoverControl(service, ledger, identity, None)
    await control.snapshot()
    taken = await service.call("takeovers", "colony")
    game.load_epoch = "restored-epoch"
    await service.call("observe_epoch", "colony", game.load_epoch, 20, command_binding_check=lambda: True)
    current[0] = source(1, "1" * 32)  # an older saved source from the original branch
    words = await service.call("record_utterance", "colony", game.load_epoch, 20, "按当前状态交还A格", command_binding_check=lambda: True)
    setup = SimpleNamespace(ledger_query=ledger, write_identity=identity, write_fields_query=None)

    async def setup_provider():
        return setup

    returned = await return_capability(service, game, {words}, setup_provider, binding_check=lambda: True).handler({
        "takeover_seq": taken[0]["seq"], "utterance_seq": words})
    assert returned["manual_id"] == "1" * 32
    assert await service.call("takeovers", "colony") == []
    # A real action in the restored loaded world has a new origin branch. Its
    # saved counter can be lower than a future source already in the journal.
    current[0] = source(2, "4" * 32, origin=game.load_epoch, enabled=False)
    await control.snapshot()
    reopened = await service.call("takeovers", "colony")
    assert len(reopened) == 1 and reopened[0]["seq"] != taken[0]["seq"]
    applied = await service.call("events_by_kind", "colony", ("area.manual",))
    assert applied[-1]["payload"]["origin_load_epoch"] == game.load_epoch
    assert applied[-1]["payload"]["manual_order"] == 2 and applied[-1]["payload"]["disposition"] == "applied"
    await service.close()

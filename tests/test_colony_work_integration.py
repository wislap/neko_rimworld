"""The ordinary work runtime uses one journal, native identity and planner authority."""

import copy
from types import SimpleNamespace

import pytest

from ..agent.runtime.audit import AgentAuditLedger
from ..agent.runtime.colony_scheduler import ColonyScheduler
from ..agent.runtime.colony_work import work_tools
from ..agent.runtime.frontdesk_service import FrontdeskService
from ..agent.runtime.native_event_intake import sync_observations
from ..agent.runtime.session import AgentRunSession
from ..agent.world.construction_geometry import compile_plan, observation_bounds
from ..agent.world.semantic_tools import SemanticInteraction
from ..game.observation import Observation as NativeObservation
from ..media import MediaStore, prepare_model_messages
from ..tool_contracts import ToolExecutionError, ToolSetup
from ..workspace.construction_evidence import native_element
from ..workspace.journal import Journal
from ..workspace.takeovers import TakeoverControl
from ..workspace.views import WorkspaceViews
from .test_agent import turn
from .test_capability_composition import Script
from .test_media_input import dispatch
from .test_workspace_journal import row

CATALOG = {"format": "construction_catalog.v1", "definitions": [
    {"definition": "Wall", "size": {"x": 1, "z": 1}, "layer": "building", "passability": "Impassable"}]}
PLAN = {"format": "construction_plan.v1", "map_id": "map", "origin": {"x": 10, "z": 20},
        "elements": [{"id": "wall", "definition": "Wall", "position": {"x": 0, "z": 0}, "rotation": 0}]}


def seed(journal):
    journal.observe_epoch("colony", "epoch", 10)
    journal.submit("colony", "epoch", 10, "planner", row("strategy", "strategy", "open"))
    journal.submit("colony", "epoch", 10, "planner", row("task", "task", "open", parent="strategy", source={"entry_id": "strategy"}))
    return compile_plan(PLAN, CATALOG)


def adopt(journal):
    plan = seed(journal)
    journal.colony_work_command("colony", "epoch", 10, "planner", "adopt", {
        "task_id": "task", "plan_id": "house", "plan": plan, "source": {}})
    return plan


def observation(operation, data, *, ident="native", epoch="epoch", connection="connection", tick=11):
    return {"observation_id": ident, "request": {"operation": operation, "arguments": {}},
            "response": {"connection_epoch": connection, "colony_id": "colony", "load_epoch": epoch,
                         "game_tick": tick, "payload": {"status": "ok", "data": data}}}


def native_intent(plan, *, stage="blueprint", object_id="blueprint", previous=None, lineage=()):
    element = plan["elements"][0]
    value = {"plan_id": "house", "element_id": "wall", "kind": "building", "definition": element["definition"],
             "x": plan["origin"]["x"], "z": plan["origin"]["z"], "rotation": 0}
    return {"format": "construction_intents.v1", "map_id": "map", "complete": True,
            "objects": [{"plan_id": "house", "element_id": "wall", "map_id": "map", "element": value, "stage": stage,
                         "target": {"id": object_id, **{key: value.get(key) for key in ("definition", "x", "z", "rotation", "stuff")}} if object_id else None,
                         "source_id": "blueprint",
                         "field_identity": {"object": "map", "field": "construction:opaque-native-field"},
                         "provenance": "native_transition" if previous else "native_creation",
                         "previous_object_id": previous, "lineage": list(lineage)}]}


def committed(op, native, *, identifier, tick):
    receipt = {"status": "committed", "changeset_id": identifier, "load_epoch": "epoch", "readback": [
        {**native["field_identity"], "value": native}]}
    return {"stage": "result", "mode": "commit", "origin": "native", "outcome": "known",
            "transport_state": "confirmed_receipt", "changeset_id": identifier,
            "changeset": {"entry_id": "task", "ops": [op]}, "receipt": receipt,
            "envelope": {"connection_epoch": "connection", "colony_id": "colony", "load_epoch": "epoch",
                         "game_tick": tick, "payload": {"status": "ok", "data": copy.deepcopy(receipt)}}}


def test_native_commit_cancel_explicit_restart_and_late_old_generation(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    plan = adopt(journal)
    native = native_intent(plan, lineage=["blueprint"])["objects"][0]
    op = {"op": "place_blueprint", "map": "map", "value": native_element("house", plan, plan["elements"][0])}
    journal.record("colony", "epoch", 11, "planner", "planner.progress", {
        "write": committed(op, native, identifier="place", tick=11)})
    state = journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]
    assert state["placement_complete"] and not state["construction_complete"]
    journal.colony_work_command("colony", "epoch", 12, "planner", "intent", {
        "task_id": "task", "plan_id": "house", "element_ids": ["wall"], "intent": "cancelled", "reason": "Change sequence"})
    cancelled = copy.deepcopy(native)
    cancelled.update(stage="model_cancelled", target=None)
    journal.record("colony", "epoch", 13, "planner", "planner.progress", {"write": committed(
        {"op": "cancel_blueprint", "map": "map", "plan_id": "house", "element_id": "wall", "value": False},
        cancelled, identifier="cancel", tick=13)})
    state = journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]
    assert state["elements"][0]["stage"] == "model_cancelled"
    journal.colony_work_command("colony", "epoch", 14, "planner", "intent", {
        "task_id": "task", "plan_id": "house", "element_ids": ["wall"], "intent": "continue", "reason": "Explicitly restart the same target"})
    restarted = native_intent(plan, object_id="new-blueprint", lineage=["new-blueprint"])["objects"][0]
    restarted["source_id"] = "new-blueprint"
    journal.record("colony", "epoch", 15, "planner", "planner.progress", {"write": committed(
        {**op, "op": "restart_blueprint", "expect": {"intent": cancelled, "site": {}}}, restarted,
        identifier="restart", tick=15)})
    state = journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]
    assert state["elements"][0]["object_id"] == "new-blueprint"
    late = {"format": "construction_intents.v1", "map_id": "map", "objects": [cancelled]}
    journal.reconcile_construction("colony", "epoch", 16, "connection", observation("inspect_construction_intents", late))
    assert journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]["elements"][0]["intent"] == "continue"
    journal.close()


def semantic_reader(session, operation, data):
    async def handler(_request):
        return NativeObservation(observation(operation, data))
    return SimpleNamespace(original={operation: SimpleNamespace(handler=handler)}, session=session,
                           setup=SimpleNamespace(observation_source="native"), sample_identity=None)


@pytest.mark.asyncio
async def test_semantic_construction_read_reaches_the_colony_work_ledger(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path)
    plan = adopt(journal)
    journal.close()
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=11, connection_epoch="connection")
    service = FrontdeskService(path, None, game_session=lambda: game)
    session = AgentRunSession.create(ledger=AgentAuditLedger("House"), question="House", colony_id="colony", role="planner")
    session.binding_check = lambda: True
    session.colony_work_service, session.colony_work_game = service, game
    built = native_intent(plan, stage="built", object_id="building", previous="blueprint", lineage=["blueprint", "building"])
    reader = semantic_reader(session, "inspect_construction_intents", built)
    await SemanticInteraction.execute_native(reader, {"map": "map"}, tool_name="inspect_construction_intents")
    assert [row["request"]["operation"] for row in session.observations] == ["inspect_construction_intents"]
    await service.close()
    journal = Journal(path)
    state = journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]
    assert state["elements"][0]["stage"] == "built"
    assert state["construction_complete"]
    journal.close()


@pytest.mark.asyncio
async def test_failed_construction_projection_stops_as_a_write_record_issue():
    class Rejecting:
        async def call(self, *_args, **_kwargs):
            raise ValueError("Native lifecycle cannot move backwards")
    session = AgentRunSession.create(ledger=AgentAuditLedger("House"), question="House", colony_id="colony", role="planner")
    session.colony_work_service = Rejecting()
    reader = semantic_reader(session, "inspect_construction_intents", {"format": "construction_intents.v1", "objects": []})
    with pytest.raises(ValueError):
        await SemanticInteraction.execute_native(reader, {"map": "map"}, tool_name="inspect_construction_intents")
    assert session.write_record_issue == "construction_evidence"
    assert session.write_record_context == {"mode": "reconcile_construction", "observation_id": "native"}


def test_roof_requires_real_cell_completion_and_later_loss_removes_house_completion(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    seed(journal)
    raw = copy.deepcopy(PLAN)
    raw["areas"] = [{"id": "roof", "kind": "roof", "cells": [{"x": 0, "z": 0}]}]
    plan = compile_plan(raw, CATALOG)
    journal.colony_work_command("colony", "epoch", 10, "planner", "adopt", {
        "task_id": "task", "plan_id": "house", "plan": plan, "source": {}})
    journal.reconcile_construction("colony", "epoch", 11, "connection", observation("inspect_construction_intents",
        native_intent(plan, stage="built", object_id="building", lineage=["blueprint", "building"])))
    assert not journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]["construction_complete"]
    spatial = {"format": "construction_observation.v1", "map_id": "map", "coverage": {"cells_complete": True},
               "cells": [{"x": 10, "z": 20, "known": True, "roof": "RoofConstructed"}]}
    journal.reconcile_construction("colony", "epoch", 12, "connection", observation("inspect_construction", spatial))
    state = journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]
    assert state["construction_complete"] and state["purpose_status"] == "not_evaluated"
    spatial["cells"][0]["roof"] = None
    journal.reconcile_construction("colony", "epoch", 13, "connection", observation("inspect_construction", spatial))
    assert not journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]["construction_complete"]
    journal.close()


def test_adoption_is_explicit_planner_work_and_readonly_views_replay_same_state(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path)
    compiled = seed(journal)
    result = journal.colony_work_command("colony", "epoch", 10, "frontdesk", "suggest", {
        "suggestion_id": "candidate", "plan": compiled, "reason": "Requested house", "attachments": []})
    assert result["colony_work"]["construction"]["plans"] == []
    with pytest.raises(ValueError, match="Only the colony planner"):
        journal.colony_work_command("colony", "epoch", 10, "frontdesk", "adopt", {
            "task_id": "task", "plan_id": "house", "plan": compiled, "source": {"event_seq": result["event_seq"]}})
    journal.colony_work_command("colony", "epoch", 10, "planner", "adopt", {
        "task_id": "task", "plan_id": "house", "plan": compiled, "source": {"event_seq": result["event_seq"]}})
    expected = journal.colony_work("colony", "epoch", "connection")
    assert expected["construction"]["plans"][0]["construction_complete"] is False
    journal.close()
    assert WorkspaceViews(path).colony_work("colony", "epoch", "connection") == expected


def test_native_lineage_completion_reload_and_revision_are_distinct(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    plan = adopt(journal)
    first = observation("inspect_construction_intents", native_intent(plan))
    journal.reconcile_construction("colony", "epoch", 11, "connection", first)
    changed = native_intent(plan, stage="built", object_id="other")
    # Same site and definition do not authorize ownership inheritance.
    journal.reconcile_construction("colony", "epoch", 12, "connection", observation("inspect_construction_intents", changed))
    assert journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]["elements"][0]["object_id"] == "blueprint"
    built = native_intent(plan, stage="built", object_id="building", previous="frame", lineage=["blueprint", "frame", "building"])
    journal.reconcile_construction("colony", "epoch", 13, "connection", observation("inspect_construction_intents", built))
    assert journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]["construction_complete"]
    assert not journal.colony_work("colony", "reload", "connection2")["construction"]["plans"][0]["construction_complete"]
    revised = copy.deepcopy(plan)
    revised["elements"][0]["position"]["x"] += 1
    journal.colony_work_command("colony", "epoch", 14, "planner", "revise", {
        "task_id": "task", "plan_id": "house", "expected_revision": 1, "plan": revised})
    assert not journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]["construction_complete"]
    journal.close()


def test_native_door_orientation_preserves_blueprint_frame_and_built_progress(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    seed(journal)
    design = copy.deepcopy(PLAN)
    design["elements"][0]["definition"] = "Door"
    catalog = {**CATALOG, "definitions": [{**CATALOG["definitions"][0], "definition": "Door", "is_door": True}]}
    plan = compile_plan(design, catalog)
    journal.colony_work_command("colony", "epoch", 10, "planner", "adopt", {
        "task_id": "task", "plan_id": "house", "plan": plan, "source": {}})
    op = {"op": "place_blueprint", "map": "map", "value": native_element("house", plan, plan["elements"][0])}

    lineage = []
    for tick, stage, object_id, rotation in ((11, "blueprint", "blueprint", 1),
                                            (12, "frame", "frame", 0), (13, "built", "door", 1)):
        previous = lineage[-1] if lineage else None
        lineage.append(object_id)
        raw = native_intent(plan, stage=stage, object_id=object_id, previous=previous, lineage=lineage)
        row = raw["objects"][0]
        row["rotation_policy"] = "native_door"
        row["target"].update(rotation=rotation, occupied_rect={"min_x": 10, "max_x": 10, "min_z": 20, "max_z": 20})
        if stage == "blueprint":
            journal.record("colony", "epoch", tick, "planner", "planner.progress", {
                "write": committed(op, row, identifier="door-place", tick=tick)})
        else:
            journal.reconcile_construction("colony", "epoch", tick, "connection",
                                           observation("inspect_construction_intents", raw, tick=tick))
        state = journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]
        assert state["elements"][0]["stage"] == stage
        assert state["elements"][0]["object_id"] == object_id
        assert state["placement_complete"]
        assert state["construction_complete"] == (stage == "built")
        assert state["plan"]["elements"][0]["rotation"] == 0
        assert row["target"]["rotation"] == rotation
    journal.close()


def test_native_cancel_survives_revision_until_matching_user_return(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    plan = adopt(journal)
    journal.reconcile_construction("colony", "epoch", 11, "connection",
        observation("inspect_construction_intents", native_intent(plan)))
    journal.reconcile_construction("colony", "epoch", 12, "connection",
        observation("inspect_construction_intents", native_intent(plan, stage="user_cancelled", object_id=None)))
    takeover = journal.record_takeover("colony", "epoch", 12, {
        "object": "map", "field": "construction:opaque-native-field", "value_read": "cancelled", "ai_value": "blueprint", "changeset_id": "cs"})
    revised = copy.deepcopy(plan)
    revised["elements"][0]["position"]["x"] += 1
    journal.colony_work_command("colony", "epoch", 13, "planner", "revise", {
        "task_id": "task", "plan_id": "house", "expected_revision": 1, "plan": revised})
    args = {"task_id": "task", "plan_id": "house", "element_ids": ["wall"], "intent": "continue", "reason": "User returned control"}
    with pytest.raises(ValueError, match="matching user return"):
        journal.colony_work_command("colony", "epoch", 14, "planner", "intent", args)
    words = journal.record_utterance("colony", "epoch", 14, "可以重新规划这面墙")
    release = journal.record_return("colony", "epoch", 14, takeover_seq=takeover, utterance_seq=words)
    journal.colony_work_command("colony", "epoch", 14, "planner", "intent", {**args, "source_seq": release})
    assert journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]["elements"][0]["intent"] == "continue"
    journal.close()


def request_watch(journal):
    journal.record_autoplay_activation("colony", "epoch", 10, {
        "goal": "House", "constraints": "Keep safe", "goal_edited": True, "constraints_edited": True})
    journal.colony_work_command("colony", "epoch", 10, "planner", "observe", {
        "observation_id": "watch", "task_id": "task", "expected_revision": 0,
        "conditions": [{"id": "walls", "kind": "construction", "plan_id": "house", "map_id": "map",
                        "target_ids": ["wall"], "max_samples": 10, "expires_at_tick": 1000}]},
        connection_epoch="connection", supported_kinds=["construction"])


def test_native_ack_facts_merge_one_task_wake_and_cursor_replays_idempotently(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    plan = adopt(journal)
    request_watch(journal)
    assert journal.colony_work("colony", "epoch", "connection")["observations"]["coverage"][0]["status"] == "pending"
    journal.observation_coverage("colony", "epoch", 10, "connection", observation_id="watch", revision=1,
        status="registered", covered_condition_ids=["walls"], receipt_id="receipt")
    def event(cursor):
        return {"cursor": cursor, "connection_epoch": "connection", "colony_id": "colony", "load_epoch": "epoch",
                "game_tick": 10 + cursor, "kind": "observation", "data": {
                    "kind": "observation", "cursor": cursor,
                    "observation_id": "watch", "revision": 1, "condition_id": "walls", "receipt_id": "receipt",
                    "status": "changed", "sample_tick": 10 + cursor, "observation": native_intent(plan)}}
    assert journal.ingest_game_event("colony", "epoch", 11, "connection", event(1)).planner_wake
    assert not journal.ingest_game_event("colony", "epoch", 11, "connection", event(1))
    assert not journal.ingest_game_event("colony", "epoch", 12, "connection", event(2)).planner_wake
    assert len([e for e in journal.events("colony") if e["kind"] == "planner.wake" and e["payload"].get("review_type") == "observation"]) == 1
    assert journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]["placement_complete"]
    assert journal.colony_work("colony", "epoch", "new-connection")["observations"]["coverage"][0]["status"] == "pending_rebind"
    journal.close()


def test_closing_task_and_revising_plan_retire_the_old_native_watch(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    plan = adopt(journal)
    request_watch(journal)
    journal.colony_work_command("colony", "epoch", 11, "planner", "revise", {
        "task_id": "task", "plan_id": "house", "expected_revision": 1, "plan": plan}, connection_epoch="connection")
    state = journal.colony_work("colony", "epoch", "connection")
    assert not state["observations"]["intents"][0]["active"]
    assert state["observations"]["intents"][0]["reason"] == "construction_plan_revised"
    journal.colony_work_command("colony", "epoch", 12, "planner", "observe", {
        "observation_id": "watch", "task_id": "task", "expected_revision": 1,
        "conditions": [{"id": "walls", "kind": "construction", "plan_id": "house", "map_id": "map", "target_ids": ["wall"],
                        "max_samples": 10, "expires_at_tick": 1000}]}, connection_epoch="connection", supported_kinds=["construction"])
    journal.submit("colony", "epoch", 13, "planner", row("task", "task", "closed", expected=1,
        parent="strategy", source={"entry_id": "strategy"}))
    assert journal.colony_work("colony", "epoch", "connection")["observations"]["coverage"][0]["status"] == "inactive"
    assert journal.observation_coverage("colony", "epoch", 13, "connection", observation_id="watch", revision=2,
        status="registered", covered_condition_ids=["walls"], receipt_id="late-receipt") is None
    journal.close()


@pytest.mark.asyncio
async def test_scheduler_restores_acknowledged_intent_and_revokes_without_second_planner(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10, connection_epoch="connection")
    service = FrontdeskService(tmp_path / "journal.sqlite", None, game_session=lambda: game)
    journal = Journal(tmp_path / "journal.sqlite")
    adopt(journal)
    request_watch(journal)
    journal.close()
    class Port:
        calls = []
        async def push_observations(self, request):
            self.calls.append(copy.deepcopy(request))
            return {"registrations": [{"observation_id": row["observation_id"], "revision": row["revision"],
                "status": "registered", "covered_condition_ids": [c["id"] for c in row["conditions"]],
                "receipt_id": "r"} for row in request["observations"]]}
    scheduler = ColonyScheduler(service, None)
    scheduler._event_port = Port()
    scheduler._event_identity = lambda: ("connection", "colony", "epoch")
    await sync_observations(scheduler)
    await sync_observations(scheduler)
    assert len(scheduler._event_port.calls) == 1
    assert (await service.call("colony_work", "colony", "epoch", "connection"))["observations"]["coverage"][0]["status"] == "covered"
    game.game_tick = 1001
    await sync_observations(scheduler)
    assert len(scheduler._event_port.calls) == 1  # native expiry evidence must arrive before removal
    await service.call("ingest_game_event", "colony", "epoch", 1001, "connection", {
        "cursor": 1, "connection_epoch": "connection", "colony_id": "colony", "load_epoch": "epoch", "game_tick": 1001,
        "kind": "observation", "data": {"kind": "observation", "cursor": 1, "observation_id": "watch", "revision": 1,
            "condition_id": "walls", "receipt_id": "r", "status": "expired", "sample_tick": 1001, "observation": {}}},
        command_binding_check=lambda: True)
    await sync_observations(scheduler)
    assert scheduler._event_port.calls[-1]["observations"] == []
    await service.call("colony_work_command", "colony", "epoch", 11, "planner", "revoke", {
        "task_id": "task", "observation_id": "watch", "expected_revision": 1, "reason": "Done"},
        connection_epoch="connection", command_binding_check=lambda: True)
    await sync_observations(scheduler)
    assert scheduler._event_port.calls[-1]["observations"] == []
    await service.close()


@pytest.mark.asyncio
async def test_resources_use_real_selected_source_buckets_and_show_joint_competition(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path)
    seed(journal)
    journal.close()
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=12, connection_epoch="connection")
    service = FrontdeskService(path, None, game_session=lambda: game)
    session = AgentRunSession.create(ledger=AgentAuditLedger("Stocks"), question="Stocks", colony_id="colony", role="planner")
    session.binding_check = lambda: True
    session.colony_work_service, session.colony_work_game = service, game
    session.observations = [observation("inspect_resources", {
        "scope": {"map_id": "map", "population": "visible physical and held plus public traders"},
        "rows": [{"def_name": "WoodLog", "coverage": "complete_in_scope", "sources": [
            {"source": "spawned_ground", "units": 10}, {"source": "trader_stock", "units": 100}]}]}, ident="stock")]
    tool = work_tools(ToolSetup((), directory={"colony_management": {}}, write_identity=lambda: ("connection", "colony", "epoch")), session)[0]
    demands = [{"id": name, "revision": 1, "window": "today", "resources": {"WoodLog": 8}} for name in ("house", "fuel")]
    reading = await tool.handler({"action": "assess_resources", "arguments": {
        "snapshot_observation_id": "stock", "resource_sources": ["spawned_ground"], "demands": demands}})
    assert reading["snapshot"]["quantities"] == {"WoodLog": 10}
    assert all(row["satisfied"] for row in reading["assessment"]["independent"])
    assert reading["assessment"]["competition"]
    await tool.handler({"action": "resources", "arguments": {
        "task_id": "task", "allocation_id": "wood", "expected_revision": 0, "snapshot_observation_id": "stock",
        "resource_sources": ["spawned_ground"], "demands": demands, "shares": {"house": {"WoodLog": 8}},
        "window": "today", "reason": "Prioritize shelter"}})
    await service.call("record_autoplay_activation", "colony", "epoch", 12, {
        "goal": "Shelter", "constraints": "Safe", "goal_edited": True, "constraints_edited": True}, command_binding_check=lambda: True)
    await service.call("colony_work_command", "colony", "epoch", 12, "planner", "observe", {
        "task_id": "task", "observation_id": "stocks", "expected_revision": 0, "conditions": [
            {"id": "wood", "kind": "resources", "map_id": "map", "target_ids": ["WoodLog"], "max_samples": 10,
             "expires_at_tick": 1000}]}, connection_epoch="connection", supported_kinds=["resources"], command_binding_check=lambda: True)
    await service.call("observation_coverage", "colony", "epoch", 12, "connection", observation_id="stocks", revision=1,
        status="registered", covered_condition_ids=["wood"], receipt_id="stock-receipt", command_binding_check=lambda: True)
    resource_event = {"connection_epoch": "connection", "colony_id": "colony", "load_epoch": "epoch", "game_tick": 13,
        "cursor": 1, "kind": "observation", "data": {"kind": "observation", "cursor": 1, "observation_id": "stocks",
            "revision": 1, "condition_id": "wood", "receipt_id": "stock-receipt", "status": "changed", "sample_tick": 13,
            "observation": {"map_id": "map", "complete": True, "objects": [{
                "def_name": "WoodLog", "definition": "WoodLog", "status": "observed", "coverage": "complete_in_scope",
                "scope": {"map_id": "map", "population": "visible physical and held plus public traders"},
                "sources": [{"source": "spawned_ground", "units": 4}, {"source": "trader_stock", "units": 100}]}]}}}
    await service.call("ingest_game_event", "colony", "epoch", 13, "connection", resource_event, command_binding_check=lambda: True)
    current = (await service.call("colony_work", "colony", "epoch", "connection"))["resources"]["decisions"][0]
    assert current["snapshot"]["quantities"] == {"WoodLog": 10}
    assert current["current_snapshot"]["quantities"] == {"WoodLog": 4}
    assert not current["assessment"]["independent"][0]["satisfied"]
    state = await service.call("colony_work", "colony", "reloaded", "connection2")
    assert state["resources"]["decisions"][0]["evidence_status"] == "historical"
    assert state["resources"]["decisions"][0]["assessment"]["selected"]["stale"]
    with pytest.raises(ToolExecutionError, match="model-authored"):
        await tool.handler({"action": "resources", "arguments": {"snapshot": {}}})
    await service.close()


@pytest.mark.asyncio
async def test_policy_impact_uses_native_dependents_and_durable_user_protection(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path)
    seed(journal)
    words = journal.record_utterance("colony", "epoch", 11, "不要改变A现在吃饭的规则")
    journal.record_policy_protection("colony", "epoch", 11, utterance_seq=words,
                                     protection={"kind": "effective_rules", "pawn_id": "A"})
    journal.close()
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=12, connection_epoch="connection")
    service = FrontdeskService(path, None, game_session=lambda: game)
    session = AgentRunSession.create(ledger=AgentAuditLedger("Policy"), question="Policy", colony_id="colony", role="planner")
    session.binding_check = lambda: True
    session.colony_work_service, session.colony_work_game = service, game
    session.observations = [observation("inspect_food_policies", {"objects": [
        {"policy_id": "food", "users": ["A", "B"], "users_complete": True}]}, ident="policy")]
    tool = work_tools(ToolSetup((), directory={"colony_management": {}}, write_identity=lambda: ("connection", "colony", "epoch")), session)[0]
    await tool.handler({"action": "policy", "arguments": {
        "task_id": "task", "impact_id": "impact", "expected_revision": 0,
        "permission": {"policy_id": "food", "scope_id": "food_policy:food", "mode": "shared"},
        "current_observation_id": "policy"}})
    state = await service.call("colony_work", "colony", "epoch", "connection")
    assessment = state["policies"]["impacts"][0]["assessment"]
    assert not assessment["allowed"] and assessment["status"] == "protected"
    assert assessment["actual_members"] == ["A", "B"]
    with pytest.raises(ToolExecutionError, match="members and protections"):
        await tool.handler({"action": "policy", "arguments": {"current": {}, "protections": []}})
    await service.close()


@pytest.mark.asyncio
async def test_orders_are_bounded_namespace_correct_and_guarded_by_adopted_revision(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path)
    seed(journal)
    expanded = copy.deepcopy(PLAN)
    expanded["elements"] = [{"id": "wall" + str(index), "definition": "Wall",
                             "position": {"x": index % 10, "z": index // 10}, "rotation": 0} for index in range(65)]
    compiled = compile_plan(expanded, CATALOG)
    journal.colony_work_command("colony", "epoch", 10, "planner", "adopt", {
        "task_id": "task", "plan_id": "house", "plan": compiled, "source": {}})
    journal.close()
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=12, connection_epoch="connection")
    service = FrontdeskService(path, None, game_session=lambda: game)
    session = AgentRunSession.create(ledger=AgentAuditLedger("Orders"), question="Orders", colony_id="colony", role="planner")
    session.binding_check = lambda: True
    session.colony_work_service, session.colony_work_game = service, game
    def identity():
        return ("connection", "colony", "epoch")
    tool = work_tools(ToolSetup((), directory={"colony_management": {}}, write_identity=identity), session)[0]
    result = await tool.handler({"action": "orders", "arguments": {
        "plan_id": "house", "task_id": "task", "element_ids": [row["id"] for row in compiled["elements"]]}})
    assert [batch["operation_count"] for batch in result["batches"]] == [64, 1]
    assert not result["writes_dispatched"]
    op = result["operations"][0]
    assert op["value"] == native_element("house", compiled, compiled["elements"][0])
    guard = TakeoverControl(service, None, identity, None)
    await guard._guard_construction({"entry_id": "task", "ops": [op]})
    changed = copy.deepcopy(op)
    changed["value"]["x"] += 1
    with pytest.raises(ToolExecutionError, match="current adopted revision"):
        await guard._guard_construction({"entry_id": "task", "ops": [changed]})
    with pytest.raises(ToolExecutionError, match="Adopt"):
        await guard._guard_construction({"entry_id": "different-task", "ops": [op]})
    await service.close()


@pytest.mark.asyncio
async def test_ordinary_disabled_visual_preview_retains_png_and_next_request_remains_text(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10, connection_epoch="connection")
    service = FrontdeskService(tmp_path / "journal.sqlite", None, game_session=lambda: game)
    session = AgentRunSession.create(ledger=AgentAuditLedger("A house"), question="A house", colony_id="colony")
    session.binding_check = lambda: True
    session.colony_work_service, session.colony_work_game = service, game
    session.media_store = MediaStore(tmp_path / "media")
    plan = compile_plan(PLAN, CATALOG)
    bounds = observation_bounds(plan)
    spatial = {"format": "construction_observation.v1", "map_id": "map", "bounds": bounds,
               "objects": [], "coverage": {"cells_complete": True, "objects_complete": True, "visibility_complete": True},
               "cells": [{"x": x, "z": z, "known": True, "walkable": True, "terrain": "Soil"}
                         for x in range(bounds["min_x"], bounds["min_x"] + bounds["width"])
                         for z in range(bounds["min_z"], bounds["min_z"] + bounds["height"])]}
    session.observations = [observation("construction_catalog", CATALOG, ident="catalog"),
                            observation("inspect_construction", spatial, ident="spatial")]
    setup = ToolSetup((), directory={"colony_management": {}}, write_identity=lambda: ("connection", "colony", "epoch"))
    result = await work_tools(setup, session)[0].handler({"action": "preview", "arguments": {
        "plan": PLAN, "catalog_observation_id": "catalog", "spatial_observation_id": "spatial"}})
    assert result["attachments"] and result["render"]["format"] == "construction_render.v1"
    assert result["actual_image_input"] is False
    assert session.attachments and not session.active_media
    prepared, manifest, reserved = prepare_model_messages(session.request_messages("Continue"), session.media_store, session.image_input)
    assert not manifest
    assert reserved == 0
    assert not any(isinstance(m.get("content"), list) for m in prepared)
    model = Script([turn("可继续文本方案")])
    dispatched = await dispatch(session, model)
    assert dispatched.error is None and len(model.requests) == 1
    assert not any(isinstance(m.get("content"), list) for m in model.requests[0]["messages"])
    with pytest.raises(ToolExecutionError, match="Only|role|planner"):
        await work_tools(setup, session)[0].handler({"action": "observe", "arguments": {}})
    await service.close()

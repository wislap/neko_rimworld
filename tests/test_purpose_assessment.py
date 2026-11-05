"""Purpose is a dated planner judgment grounded in original native observations."""

import copy
from types import SimpleNamespace

import pytest

from ..agent.runtime.audit import AgentAuditLedger
from ..agent.runtime.colony_work import work_tools
from ..agent.runtime.session import AgentRunSession
from ..tool_contracts import ToolExecutionError, ToolSetup
from ..workspace.journal import Journal
from ..workspace.runtime import FrontdeskService
from .test_colony_work_integration import adopt, native_intent, observation


def evidence(*, epoch="epoch", map_id="map", tick=15):
    return observation("inspect_construction", {"format": "construction_observation.v1", "map_id": map_id,
        "sample": {"started_tick": 14, "finished_tick": 15, "atomic": False},
        "coverage": {"cells_complete": True}, "cells": [{"x": 10, "z": 20, "known": True, "roof": None}]},
        ident="purpose-native", epoch=epoch, tick=tick)


def arguments():
    return {"plan_id": "house", "task_id": "task", "expected_revision": 1, "goal": "Provide a roofed bedroom",
            "judgment": "unmet", "findings": ["The selected native cell has no roof."], "evidence_ids": ["purpose-native"]}


def test_purpose_refs_preserve_sample_and_do_not_certify_construction(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    plan = adopt(journal)
    original = evidence()
    journal.record("colony", "epoch", 15, "planner", "planner.progress", {
        "observation_source": "native", "observation": original})
    result = journal.colony_work_command("colony", "epoch", 20, "planner", "assess_purpose", arguments(), connection_epoch="connection")
    current = result["colony_work"]["construction"]["plans"][0]
    assessment = current["purpose_assessment"]
    assert current["purpose_status"] == "unmet" and not current["construction_complete"]
    assert assessment["source"] == "planner_assessment" and assessment["assessed_tick"] == 20
    assert assessment["evidence_refs"][0]["game_tick"] == 15
    assert assessment["evidence_refs"][0]["sample"] == {"started_tick": 14, "finished_tick": 15, "atomic": False}
    assert len(assessment["evidence_refs"][0]["sha256"]) == 64
    journal.reconcile_construction("colony", "epoch", 21, "connection", observation("inspect_construction_intents", native_intent(plan)))
    changed = journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]
    assert changed["purpose_status"] == "stale"
    assert changed["purpose_assessment"]["judgment"] == "unmet"
    journal.close()


@pytest.mark.parametrize("problem", ["unretained", "offline", "old_epoch", "other_map", "old_sample"])
def test_purpose_rejects_unqualified_native_references(tmp_path, problem):
    journal = Journal(tmp_path / "journal.sqlite")
    adopt(journal)
    original = evidence(epoch="old" if problem == "old_epoch" else "epoch",
                        map_id="other" if problem == "other_map" else "map", tick=9 if problem == "old_sample" else 15)
    if problem != "unretained":
        journal.record("colony", "epoch", 15, "planner", "planner.progress", {
            "observation_source": "offline_captured_graph" if problem == "offline" else "native", "observation": original})
    with pytest.raises(ValueError):
        journal.colony_work_command("colony", "epoch", 20, "planner", "assess_purpose", arguments(), connection_epoch="connection")
    assert journal.colony_work("colony", "epoch", "connection")["construction"]["plans"][0]["purpose_status"] == "not_evaluated"
    journal.close()


def test_new_load_allows_fresh_purpose_evidence_without_reusing_old_world_facts(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    plan = adopt(journal)
    journal.reconcile_construction("colony", "epoch", 15, "connection", observation("inspect_construction_intents", native_intent(plan)))
    journal.observe_epoch("colony", "reloaded", 20)
    original = evidence(epoch="reloaded", tick=21)
    original["response"]["payload"]["data"]["sample"] = {"started_tick": 20, "finished_tick": 21, "atomic": False}
    journal.record("colony", "reloaded", 21, "planner", "planner.progress", {"observation_source": "native", "observation": original})
    journal.colony_work_command("colony", "reloaded", 22, "planner", "assess_purpose", arguments(), connection_epoch="connection")
    current = journal.colony_work("colony", "reloaded", "connection")["construction"]["plans"][0]
    assert current["purpose_status"] == "unmet" and not current["construction_complete"]
    assert current["purpose_assessment"]["evidence_refs"][0]["sample"]["finished_tick"] == 21
    journal.close()


@pytest.mark.asyncio
async def test_tool_requires_current_run_originals_and_rejects_model_authored_facts(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path)
    adopt(journal)
    original = evidence()
    journal.record("colony", "epoch", 15, "planner", "planner.progress", {"observation_source": "native", "observation": original})
    journal.close()
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=20, connection_epoch="connection")
    service = FrontdeskService(path, None, game_session=lambda: game)
    session = AgentRunSession.create(ledger=AgentAuditLedger("Bedroom"), question="Bedroom", colony_id="colony", role="planner")
    session.colony_work_service, session.colony_work_game = service, game
    session.binding_check = lambda: True
    setup = ToolSetup((), directory={"colony_management": {}}, write_identity=lambda: ("connection", "colony", "epoch"))
    tool = work_tools(setup, session)[0]
    with pytest.raises(ToolExecutionError, match="this run"):
        await tool.handler({"action": "assess_purpose", "arguments": arguments()})
    session.observations = [copy.deepcopy(original)]
    with pytest.raises(ToolExecutionError, match="original native"):
        await tool.handler({"action": "assess_purpose", "arguments": {**arguments(), "observations": [original]}})
    result = await tool.handler({"action": "assess_purpose", "arguments": arguments()})
    assert result["colony_work"]["construction"]["plans"][0]["purpose_status"] == "unmet"
    await service.close()

"""Shared edits cross the real Journal and WriteService pre-dispatch guard."""

import copy
from types import SimpleNamespace

import pytest

from ..agent.write.service import WriteService
from ..game.observation import Observation
from ..game.write_catalog import catalog_digest
from ..tool_contracts import ToolExecutionError
from ..workspace.journal import Journal
from ..workspace.takeovers import TakeoverControl, policy_protection_capability

BOUND = ("connection", "colony", "a" * 32)


class Service:
    def __init__(self, journal):
        self.journal = journal

    async def call(self, name, *args, **kwargs):
        check = kwargs.pop("command_binding_check", None)
        if check is not None and not check():
            raise ToolExecutionError("world_changed", "changed")
        return getattr(self.journal, name)(*args, **kwargs)


def observed(data, operation="inspect_food_policies"):
    return Observation({"observation_id": "native-policy-read", "request": {"operation": operation, "arguments": {}},
        "response": {"connection_epoch": BOUND[0], "colony_id": BOUND[1], "load_epoch": BOUND[2], "game_tick": 10,
                     "payload": {"status": "ok", "issues": [], "data": copy.deepcopy(data)}}})


def content(users):
    return {"label": "Simple", "filter": {"allowed_defs": ["MealSimple"]}, "users": list(users), "users_complete": True}


def native_data(users):
    return {"complete": True, "users_complete": True, "objects": [{"id": "Food_1", "policy_id": "Food_1",
        "status": "observed", **content(users), "write_content": content(users)}]}


def directory():
    operation = {"op": "set_food_policy_content", "schema": {"type": "object", "additionalProperties": False,
        "required": ["op", "policy", "value"], "properties": {"op": {"const": "set_food_policy_content"},
        "policy": {"type": "string"}, "value": {"type": "object"}}}, "expect_schema": {"type": "object"},
        "observation": {"read": {"tool": "look", "native_operation": "inspect_food_policies", "arguments": {}},
            "extract": {"kind": "field", "object_id": {"arg": "policy"}, "objects_path": ["objects"],
                "identity_path": ["id"], "status_path": ["status"], "path": ["write_content"]}}}
    value = {"version": "colony_write.v1", "capability": "colony_write.transactions.v1",
             "modes": ["dry_run", "commit", "undo"], "operations": [operation]}
    value["catalog_id"] = catalog_digest(value)
    return value


class Port:
    def __init__(self):
        self.calls = []

    async def commit(self, changeset):
        self.calls.append(changeset)
        return {"status": "committed", "changeset_id": changeset["changeset_id"], "load_epoch": BOUND[2],
                "undo_token": "receipt", "readback": []}


@pytest.fixture
def harness(tmp_path):
    journal = Journal(tmp_path / "colony.sqlite3")
    service = Service(journal)
    for entry_id, kind, parent in (("strategy", "strategy", None), ("task", "task", "strategy")):
        journal.submit(BOUND[1], BOUND[2], 0, "planner", {"entry_id": entry_id, "expected_revision": 0,
            "kind": kind, "status": "open", "priority": 1, "parent": parent,
            "source": {} if parent is None else {"entry_id": parent}, "evidence": [], "changesets": [],
            "review_at_tick": None, "body": "Manage food"})
    state = native_data(["Pawn_A"])
    async def ledger():
        return observed({"ledger_status": "valid", "complete": True, "entry_count": 0, "entries": []}, "write_ledger_snapshot")
    async def fields(operation, arguments):
        if operation == "inspect_food_policies":
            return observed(state)
        data = {"complete": True, "field_count": 1, "fields": [{"object": "Food_1", "field": "food_policy_content", "op": 0}]}
        if operation == "write_undo_fields":
            data["status"] = "available"
        return observed(data, operation)
    control = TakeoverControl(service, ledger, lambda: BOUND, fields)
    port = Port()
    writer = WriteService(directory(), [observed(native_data(["Pawn_A"]))], port, lambda: BOUND, guard=control)
    yield SimpleNamespace(journal=journal, service=service, control=control, state=state, writer=writer, port=port)
    journal.close()


def edit(*, mode="fixed_users", users=("Pawn_A",)):
    impact = {"mode": mode}
    if mode == "fixed_users":
        impact["users"] = list(users)
    return {"entry_id": "task", "ops": [{"op": "set_food_policy_content", "policy": "Food_1",
        "value": {"label": "New", "filter": {"allowed_defs": ["MealFine"]}, "impact": impact}}]}


def protect(h, protection):
    utterance = h.journal.record_utterance(BOUND[1], BOUND[2], 1, "Keep this person's actual food rules unchanged")
    seq = h.journal.record_policy_protection(BOUND[1], BOUND[2], 1, utterance_seq=utterance, protection=protection)
    return utterance, seq


async def test_effective_rules_blocks_real_write_before_dispatch_even_dynamic(harness):
    h = harness
    protect(h, {"kind": "effective_rules", "pawn_id": "Pawn_A"})
    for mode in ("fixed_users", "dynamic_shared"):
        with pytest.raises(ToolExecutionError) as error:
            await h.writer.commit(edit(mode=mode))
        assert error.value.code == "policy_effect_protected"
    assert h.port.calls == []
    assert h.journal.events_by_kind(BOUND[1], ("takeover.policy_snapshot",))[-1]["payload"]["valid"] is True


async def test_reference_only_does_not_freeze_shared_content(harness):
    h = harness
    protect(h, {"kind": "reference", "pawn_id": "Pawn_A"})
    assert (await h.writer.commit(edit()))["status"] == "committed"
    assert len(h.port.calls) == 1


async def test_policy_content_protection_and_undo(harness):
    h = harness
    protect(h, {"kind": "policy_content", "policy_id": "Food_1"})
    with pytest.raises(ToolExecutionError) as error:
        await h.writer.commit(edit())
    assert error.value.code == "policy_effect_protected"
    with pytest.raises(ToolExecutionError) as error:
        await h.control.guard_undo("receipt")
    assert error.value.code == "policy_effect_protected"
    assert not h.port.calls


async def test_fixed_membership_drift_blocks_and_explicit_dynamic_admits(harness):
    h = harness
    h.state.update(native_data(["Pawn_A", "Pawn_B"]))
    with pytest.raises(ToolExecutionError) as error:
        await h.writer.commit(edit())
    assert error.value.code == "policy_scope_conflict"
    assert not h.port.calls
    assert (await h.writer.commit(edit(mode="dynamic_shared")))["status"] == "committed"


async def test_new_shared_user_effective_protection_blocks_dynamic_edit(harness):
    h = harness
    protect(h, {"kind": "effective_rules", "pawn_id": "Pawn_B"})
    h.state.update(native_data(["Pawn_A", "Pawn_B"]))
    with pytest.raises(ToolExecutionError) as error:
        await h.writer.commit(edit(mode="dynamic_shared"))
    assert error.value.code == "policy_effect_protected"
    assert not h.port.calls


async def test_incomplete_native_users_never_grants_scope(harness):
    h = harness
    h.state["users_complete"] = False
    with pytest.raises(ToolExecutionError) as error:
        await h.writer.commit(edit(mode="dynamic_shared"))
    assert error.value.code == "policy_scope_unknown"
    assert not h.port.calls


@pytest.mark.parametrize("problem", ["wrong_epoch", "missing_user_coverage", "duplicate_policy", "duplicate_user"])
async def test_membership_scope_requires_original_complete_native_binding(harness, problem):
    h = harness
    original = h.control.fields_provider
    async def changed(operation, arguments):
        value = await original(operation, arguments)
        if operation != "inspect_food_policies":
            return value
        raw = value.to_dict()
        data = raw["response"]["payload"]["data"]
        if problem == "wrong_epoch":
            raw["response"]["load_epoch"] = "b" * 32
        elif problem == "missing_user_coverage":
            data["objects"][0].pop("users_complete")
        elif problem == "duplicate_policy":
            data["objects"] *= 2
        else:
            data["objects"][0]["users"] *= 2
        return Observation(raw)
    h.control.fields_provider = changed
    with pytest.raises(ToolExecutionError) as error:
        await h.writer.commit(edit(mode="dynamic_shared"))
    assert error.value.code == "policy_scope_unknown"
    assert not h.port.calls


async def test_frontdesk_protection_requires_delivered_words_and_release_is_durable(harness):
    h = harness
    protection = {"kind": "effective_rules", "pawn_id": "Pawn_A"}
    utterance = h.journal.record_utterance(BOUND[1], BOUND[2], 1, "Do not alter A's food rules")
    game = SimpleNamespace(colony_id=BOUND[1], load_epoch=BOUND[2], game_tick=1)
    tool = policy_protection_capability(h.service, game, {utterance}, binding_check=lambda: True)
    with pytest.raises(ToolExecutionError):
        await tool.handler({"utterance_seq": utterance + 1, "protection": protection})
    saved = await tool.handler({"utterance_seq": utterance, "protection": protection})
    later = h.journal.record_utterance(BOUND[1], BOUND[2], 2, "You may change A's food rules now")
    release_tool = policy_protection_capability(h.service, game, {later}, binding_check=lambda: True)
    await release_tool.handler({"utterance_seq": later, "protection": protection, "release_seq": saved["event_seq"]})
    assert (await h.writer.commit(edit()))["status"] == "committed"
    with pytest.raises(ValueError):
        h.journal.record_policy_protection(BOUND[1], BOUND[2], 3, utterance_seq=later, protection=protection,
                                           release_seq=saved["event_seq"])


async def test_planner_scope_and_forged_protections_never_release_user_protection(harness):
    h = harness
    protect(h, {"kind": "effective_rules", "pawn_id": "Pawn_A"})
    h.journal.record(BOUND[1], BOUND[2], 2, "planner", "policy.impact_selected",
                     {"protections": [], "assessment": {"allowed": True}})
    with pytest.raises(ValueError):
        h.journal.record(BOUND[1], BOUND[2], 2, "planner", "user.policy_protection_released", {"protection_seq": 1})
    with pytest.raises(ToolExecutionError) as error:
        await h.writer.commit(edit(mode="dynamic_shared"))
    assert error.value.code == "policy_effect_protected"
    assert not h.port.calls


@pytest.mark.parametrize("kind", ["reference", "effective_rules"])
async def test_assignment_protection_is_enforced_separately_from_shared_content(harness, kind):
    h = harness
    protect(h, {"kind": kind, "pawn_id": "Pawn_A"})
    with pytest.raises(ToolExecutionError) as error:
        await h.control._guard_policy_effects([{"op": "set_food_policy", "pawn": "Pawn_A",
            "expect": {"policy_id": "Food_1", "native_instance": "old"},
            "value": {"policy_id": "Food_2", "native_instance": "new"}}])
    assert error.value.code == "policy_effect_protected"

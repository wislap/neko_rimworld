import copy
import json

import pytest
from plugin.plugins.neko_rimworld.agent.context.context_pruning import ContextPruner


def source():
    content = {"native_instance": "native-one", "label": "Normal", "filter": {"allowed_defs": ["Rice"], "special_filters": {"AllowFresh": True}},
               "users": ["Pawn_1"], "users_complete": True}
    structure = {"policy_ids": ["FoodPolicy_1"]}
    row = {**copy.deepcopy(content), "id": "FoodPolicy_1", "policy_id": "FoodPolicy_1", "status": "observed", "is_default": True,
        "write_content": copy.deepcopy(content), "copy_expect": {**structure, "source_id": "FoodPolicy_1", "source": copy.deepcopy(content)},
        "delete_expect": {**structure, "policy_id": "FoodPolicy_1", "policy": copy.deepcopy(content)}, "unknown_extension": {"state": None}}
    return {"ok": True, "tool": "inspect_food_policies", "data": {"status": "ok", "complete": True, "structure": structure, "objects": [row],
        "sample": {"started_tick": 100, "finished_tick": 101, "atomic": False}, "coverage": {"users_complete": True}}}


@pytest.mark.parametrize("tool", ["inspect_food_policies", "look"])
def test_guard_aliases_reconstruct_exactly_from_retained_same_sample(tool):
    original = source()
    original["tool"] = tool
    before = copy.deepcopy(original)
    view = ContextPruner().compile(original, arguments={"object": {"kind": "food_policy"}})
    result = json.loads(view.content)
    row = result["data"]["objects"][0]
    assert not {"write_content", "copy_expect", "delete_expect"} & row.keys()
    content = {key: copy.deepcopy(row[key]) for key in ("native_instance", "label", "filter", "users", "users_complete")}
    row["write_content"] = copy.deepcopy(content)
    row["copy_expect"] = {**result["data"]["structure"], "source_id": row["policy_id"], "source": copy.deepcopy(content)}
    row["delete_expect"] = {**result["data"]["structure"], "policy_id": row["policy_id"], "policy": copy.deepcopy(content)}
    assert result == original == before
    assert len(view.omissions) == 3


@pytest.mark.parametrize("alias", ["write_content", "copy_expect", "delete_expect"])
def test_changed_or_extended_guard_never_disappears(alias):
    original = source()
    original["data"]["objects"][0][alias]["unknown_extension"] = {"status": "unavailable"}
    projected = json.loads(ContextPruner().compile(original).content)
    assert projected["data"]["objects"][0][alias] == original["data"]["objects"][0][alias]

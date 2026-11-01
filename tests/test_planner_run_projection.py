import copy

import pytest

from plugin.plugins.neko_rimworld.agent.runtime.prompt import build_system_prompt
from plugin.plugins.neko_rimworld.workspace.views import planner_run_input


def inputs():
    return {
        "board": [
            {"entry_id": "strategy", "kind": "strategy", "status": "open"},
            {"entry_id": "intent", "kind": "intent", "status": "open"},
            {"entry_id": "task", "kind": "task", "status": "committed", "changesets": ["change"]},
            {"entry_id": "done", "kind": "task", "status": "closed", "changesets": []},
        ],
        "activation": {"seq": 1, "payload": {"goal": "survive"}},
        "attention": {"level": "urgent", "facts": {"alert": {"seq": 9}}},
        "signals": [{"seq": 9, "kind": "attention.signal", "payload": {"source_id": "alert"}}],
        "wake_reasons": [{"seq": 10, "payload": {"work_id": "interrupt:9"}}],
        "history": [{"seq": 8, "kind": "cadence.changed", "game_tick": 20, "payload": {"level": "urgent"}}],
        "daily_history": [{"seq": 7, "kind": "attention.signal", "game_tick": 15, "payload": {"edge": "disappeared"}}],
        "intent_utterances": [{"seq": 2, "payload": {"text": "Keep everyone safe"}}],
        "intent_source_history": [{"seq": 2}],
        "takeovers": [{"field": "priority"}],
        "handoff": {"seq": 4, "payload": {"handoff": "continue"}},
        "commit_records": [{"entry_id": "task", "changeset_id": "change", "game_tick": 12}],
    }


@pytest.mark.parametrize("run_type", ["startup", "daily", "signal", "escalated"])
def test_run_input_preserves_required_context_and_signal_identity(run_type):
    data = inputs()
    original = copy.deepcopy(data)
    context, used = planner_run_input(run_type=run_type, **data)
    assert used == ([9, 7] if run_type == "daily" else [9])
    assert context["run_type"] == run_type
    assert context["intent_utterances"] == data["intent_utterances"]
    assert context["takeovers"] == data["takeovers"]
    assert context["strategy"] == [data["board"][0]]
    assert context["wake_reasons"] == data["wake_reasons"]
    assert context["pending_verification"] == [{"entry_id": "task", "changesets": [{"changeset_id": "change", "commit_tick": 12}]}]
    assert ("board" in context) == (run_type in {"startup", "daily"})
    assert context["activation_choices"] == data["activation"]
    assert ("cadence_history" in context) == (run_type == "daily")
    if run_type == "daily":
        assert context["daily_signals"] == data["daily_history"]
    context["attention"]["level"] = "changed"
    assert data == original


def test_empty_changes_and_unknown_commit_tick_are_explicit():
    data = inputs()
    data.update(history=[], daily_history=[], signals=[], commit_records=[])
    context, used = planner_run_input(run_type="daily", **data)
    assert context["changes_since_last_run"] == [] and used == []
    assert context["pending_verification"][0]["changesets"][0]["commit_tick"] is None


def test_readonly_prompt_unchanged_and_autoplay_roles_use_current_tools():
    prompt = build_system_prompt()
    assert prompt.startswith("You are the private read-only RimWorld domain agent inside N.E.K.O.\n")
    planner = build_system_prompt(role="planner", autoplay=True)
    frontdesk = build_system_prompt(role="frontdesk", autoplay=True)
    assert "user_decision or urgent_risk" in planner
    assert "pending verification list" in planner and "planner-owned pause" in planner
    assert "attention_request" in frontdesk and "ordinary game letters" in frontdesk


def test_included_signal_sequences_deduplicate_across_all_actual_inputs():
    data = inputs()
    signal = data["signals"][0]
    recorded = {"seq": 11, "kind": "attention.signal", "game_tick": 25, "payload": {"edge": "disappeared"}}
    data["history"] = [dict(signal, game_tick=20), recorded]
    data["daily_history"] = [recorded, dict(signal, game_tick=20)]
    context, used = planner_run_input(run_type="daily", **data)
    assert [event["seq"] for event in context["attention_signals"]] == [9]
    assert [event["seq"] for event in context["changes_since_last_run"]] == [9, 11]
    assert [event["seq"] for event in context["daily_signals"]] == [11, 9]
    assert used == [9, 11]

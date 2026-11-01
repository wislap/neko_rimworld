import pytest

from plugin.plugins.neko_rimworld.workspace.journal import Journal


def test_pause_policy_is_rebuilt_and_role_bound(tmp_path):
    path = tmp_path / "events.sqlite"
    journal = Journal(path)
    journal.set_wake_policy("colony", "epoch", 10, "frontdesk", {"pause_on_letter_defs": []})
    with pytest.raises(ValueError):
        journal.set_wake_policy("colony", "epoch", 10, "planner", {"planner_review_ticks": 9000})
    with pytest.raises(ValueError):
        journal.set_wake_policy("colony", "epoch", 10, "planner", {"pause_on_letter_defs": []})
    journal.close()
    journal = Journal(path)
    assert journal.wake_policy("colony")["pause_on_letter_defs"] == []
    assert "planner_review_ticks" not in journal.wake_policy("colony")
    assert journal.wake_policy_authors("colony")["pause_on_letter_defs"]["source"] == "frontdesk"
    journal.close()


def test_retired_scheduler_events_cannot_be_forged(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    for kind in ("scheduler.policy", "scheduler.default_review", "wake.registered", "wake.cancelled", "wake.evaluated"):
        with pytest.raises(ValueError):
            journal.record("colony", "epoch", 1, "planner", kind, {})
    journal.close()


def test_retired_wake_events_remain_readable_without_reactivation(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    payload = {"role": "planner", "condition": {"kind": "periodic", "interval_ticks": 15000}, "text": "old"}
    seq = journal._ledger.append("colony", "epoch", 1, "planner", "wake.registered", payload)
    row = journal.event("colony", seq)
    assert row["kind"] == "wake.registered" and row["payload"] == payload
    assert journal.events_by_kind("colony", ("wake.registered",))[0]["payload"] == payload
    assert not any(event["kind"] == "planner.wake" for event in journal.events("colony"))
    journal.close()


def test_historical_retired_policy_does_not_change_current_delivery(tmp_path):
    from .test_attention_journal import CHOICES, native

    journal = Journal(tmp_path / "events.sqlite")
    old_policy = {
        "pause_on_letter_defs": ["ThreatBig"],
        "frontdesk_letter_defs": ["Custom"],
        "frontdesk_choice_letters": False,
        "frontdesk_paused_windows": False,
        "planner_review_ticks": 3000,
    }
    seq = journal._ledger.append("c", "e", 0, "frontdesk", "scheduler.policy", old_policy)
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    policy, authors = journal.wake_policy("c"), journal.wake_policy_authors("c")
    assert policy["pause_on_letter_defs"] == ["ThreatBig"]
    assert policy["frontdesk_choice_letters"] is True and policy["frontdesk_paused_windows"] is True
    assert policy["frontdesk_letter_defs"] != ["Custom"] and "planner_review_ticks" not in policy
    assert authors["pause_on_letter_defs"]["event_seq"] == seq
    assert authors["frontdesk_choice_letters"]["source"] == "plugin"
    journal.ingest_game_event("c", "e", 1, "conn", native(1, 1, "letter", {"def": "Custom", "is_choice_letter": False}))
    assert not any(event["kind"] == "frontdesk.wake" for event in journal.events("c"))
    journal.ingest_game_event(
        "c", "e", 2, "conn", native(2, 2, "letter", {"def": "NeutralEvent", "is_choice_letter": True})
    )
    journal.ingest_game_event(
        "c", "e", 3, "conn", native(3, 3, "pause_changed", {"source": "window", "windows_force_paused": True})
    )
    assert sum(event["kind"] == "frontdesk.wake" for event in journal.events("c")) == 2
    assert journal.event("c", seq)["payload"] == old_policy
    journal.close()


@pytest.mark.asyncio
async def test_idle_query_passes_binding_and_busy_without_changing_ordinary_reads():
    from ..entry import NekoRimworldPlugin
    from ..game.bridge import BridgeBusyError
    from ..game.protocol import SessionDescriptor
    from ..tool_contracts import ToolExecutionError
    from .test_query_path import fixture_response

    class Bridge:
        session = SessionDescriptor("colony", "epoch", 3, 1)

        def __init__(self):
            self.calls = []

        def request(self, operation, arguments, timeout, **kwargs):
            self.calls.append(kwargs)
            if kwargs:
                raise BridgeBusyError("busy")
            return fixture_response()

    plugin = object.__new__(NekoRimworldPlugin)
    plugin._bridge = Bridge()
    await plugin._agent_query_tool({"operation": "query", "arguments": {}})
    with pytest.raises(ToolExecutionError) as caught:
        await plugin._agent_query_tool(
            {"operation": "query", "arguments": {}, "idle_only": True, "binding": ("conn", "colony", "epoch")}
        )
    assert caught.value.code == "bridge_busy"
    assert plugin._bridge.calls == [
        {},
        {"idle_only": True, "expected_connection": "conn", "expected_session": plugin._bridge.session},
    ]
    with pytest.raises(ToolExecutionError):
        await plugin._agent_query_tool(
            {"operation": "query", "arguments": {}, "idle_only": True, "binding": ("conn", "colony", "old")}
        )
    assert len(plugin._bridge.calls) == 2

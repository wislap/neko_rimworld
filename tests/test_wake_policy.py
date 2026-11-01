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


def native(cursor, tick, kind="heartbeat", data=None, epoch="epoch", connection="conn"):
    return {"connection_epoch": connection, "colony_id": "colony", "load_epoch": epoch,
            "game_tick": tick, "snapshot_sequence": cursor, "cursor": cursor,
            "kind": kind, "data": data or {}}




def test_numeric_records_miss_then_hit_and_busy_defers(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    item = journal.register_wake("colony", "epoch", 10, "planner", {"kind": "numeric", "tool": "query", "arguments": {}, "path": ["response", "payload", "data", "food"], "op": "lt", "value": 10, "interval_ticks": 1}, "food")
    assert item["condition"]["interval_ticks"] == 250
    def evaluate(tick, next_tick, obs):
        return journal.evaluate_wake("colony", "epoch", tick, item["id"], obs, expected_next_tick=next_tick)
    assert not evaluate(260, 260, {"error": {"code": "bridge_busy"}})
    assert not evaluate(510, 260, {"response": {"payload": {"status": "ok", "data": {"food": 12}}}})
    assert evaluate(760, 760, {"response": {"payload": {"status": "ok", "data": {"food": 5}}}})
    assert not evaluate(760, 760, {"response": {"payload": {"status": "ok", "data": {"food": 5}}}})
    entries = [e["payload"] for e in journal.events("colony") if e["kind"] == "wake.evaluated"]
    assert [e["value"] for e in entries] == [None, 12, 5]
    assert entries[0]["observation"]["error"]["code"] == "bridge_busy"
    assert [e["matched"] for e in entries] == [False, False, True]
    journal.close()


def test_role_cannot_cancel_other_role_or_forge_scheduler_events(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    item = journal.register_wake("colony", "epoch", 0, "planner", {"kind": "deadline", "tick": 10}, "later")
    with pytest.raises(ValueError):
        journal.cancel_wake("colony", "epoch", 1, "frontdesk", item["id"])
    with pytest.raises(ValueError):
        journal.record("colony", "epoch", 1, "planner", "scheduler.policy", {})
    journal.cancel_wake("colony", "epoch", 1, "planner", item["id"])
    assert not journal.wake_conditions("colony", "epoch")
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
        await plugin._agent_query_tool({"operation": "query", "arguments": {}, "idle_only": True,
                                        "binding": ("conn", "colony", "epoch")})
    assert caught.value.code == "bridge_busy"
    assert plugin._bridge.calls == [{}, {"idle_only": True, "expected_connection": "conn", "expected_session": plugin._bridge.session}]
    with pytest.raises(ToolExecutionError):
        await plugin._agent_query_tool({"operation": "query", "arguments": {}, "idle_only": True,
                                        "binding": ("conn", "colony", "old")})
    assert len(plugin._bridge.calls) == 2




@pytest.mark.parametrize("incomplete", [{"status": "partial"}, {"issues": ["lost"]}, {"truncated": True}, {"coverage": {"complete": False}}, {"status": "unknown"}])
def test_numeric_rejects_incomplete_ancestors(tmp_path, incomplete):
    journal = Journal(tmp_path / "events.sqlite")
    item = journal.register_wake("colony", "epoch", 0, "planner", {"kind": "numeric", "tool": "query", "arguments": {}, "path": ["response", "payload", "data", "value"], "op": "lt", "value": 2, "interval_ticks": 250}, "check")
    observation = {"response": {"payload": {"status": "ok", "data": {**incomplete, "value": 1}}}}
    assert not journal.evaluate_wake("colony", "epoch", 250, item["id"], observation, expected_next_tick=250)
    entry = next(e["payload"] for e in journal.events("colony") if e["kind"] == "wake.evaluated")
    assert entry["error"] and not entry["matched"] and entry["observation"] == observation
    journal.close()






def test_native_work_priority_with_unrelated_coverage_fields(tmp_path):
    import copy
    import json
    from pathlib import Path

    raw = json.loads((Path(__file__).parent / "fixtures" / "wake-native-work.json").read_text(encoding="utf-8"))
    path = ["response", "payload", "data", "objects", 0, "facets", "work", "work_types", 2, "priority"]
    work_type = raw["response"]["payload"]["data"]["objects"][0]["facets"]["work"]["work_types"][2]
    assert work_type["definition"] == "Doctor" and work_type["priority"] == 3
    journal = Journal(tmp_path / "events.sqlite")
    condition = {"kind": "numeric", "tool": "inspect_state", "arguments": {}, "path": path,
                 "op": "eq", "value": 3, "interval_ticks": 250}
    item = journal.register_wake("colony", "epoch", 0, "planner", condition, "Doctor")
    assert journal.evaluate_wake("colony", "epoch", 250, item["id"], raw, expected_next_tick=250)
    assert next(e["payload"] for e in journal.events("colony") if e["kind"] == "wake.evaluated")["observation"] == raw
    incomplete = copy.deepcopy(raw)
    incomplete["response"]["payload"]["data"]["objects"][0]["coverage"]["complete"] = False
    item = journal.register_wake("colony", "epoch", 250, "planner", condition, "Doctor")
    assert not journal.evaluate_wake("colony", "epoch", 500, item["id"], incomplete, expected_next_tick=500)
    journal.close()

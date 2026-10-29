import pytest

from plugin.plugins.neko_rimworld.workspace.journal import Journal


def test_wake_policy_is_rebuilt_and_role_bound(tmp_path):
    path = tmp_path / "events.sqlite"
    journal = Journal(path)
    journal.set_wake_policy("colony", "epoch", 10, "frontdesk", {
        "pause_on_letter_defs": [], "frontdesk_letter_defs": ["Custom"],
        "frontdesk_choice_letters": False,
    })
    journal.set_wake_policy("colony", "epoch", 10, "planner", {"planner_review_ticks": 9000})
    with pytest.raises(ValueError):
        journal.set_wake_policy("colony", "epoch", 10, "planner", {"pause_on_letter_defs": []})
    journal.close()
    journal = Journal(path)
    assert journal.wake_policy("colony") == {
        "pause_on_letter_defs": [], "frontdesk_letter_defs": ["Custom"],
        "frontdesk_choice_letters": False, "frontdesk_paused_windows": True, "planner_review_ticks": 9000,
    }
    assert journal.wake_policy_authors("colony")["planner_review_ticks"]["source"] == "planner"
    assert journal.wake_policy_authors("colony")["pause_on_letter_defs"]["source"] == "frontdesk"
    assert journal.wake_policy_authors("colony")["frontdesk_paused_windows"]["source"] == "plugin"
    journal.close()


def native(cursor, tick, kind="heartbeat", data=None, epoch="epoch", connection="conn"):
    return {"connection_epoch": connection, "colony_id": "colony", "load_epoch": epoch,
            "game_tick": tick, "snapshot_sequence": cursor, "cursor": cursor,
            "kind": kind, "data": data or {}}


def test_wakes_are_atomic_rebuilt_and_use_game_ticks(tmp_path):
    path = tmp_path / "events.sqlite"
    journal = Journal(path)
    journal.ingest_game_event("colony", "epoch", 10, "conn", native(1, 10))
    deadline = journal.register_wake("colony", "epoch", 10, "planner", {"kind": "deadline", "tick": 20}, "due")
    periodic = journal.register_wake("colony", "epoch", 10, "planner", {"kind": "periodic", "interval_ticks": 5}, "repeat")
    event = journal.register_wake("colony", "epoch", 10, "frontdesk", {"kind": "event", "event_kind": "letter", "filters": {"severity": {"op": "ge", "value": 2}}}, "letter")
    journal.ingest_game_event("colony", "epoch", 14, "conn", native(2, 14, "letter", {"severity": 1}))
    assert not any(e["kind"] == "planner.wake" for e in journal.events("colony"))
    journal.ingest_game_event("colony", "epoch", 20, "conn", native(3, 20, "letter", {"severity": 3}))
    assert not journal.ingest_game_event("colony", "epoch", 20, "conn", native(3, 20, "letter", {"severity": 3}))
    events = journal.events("colony")
    assert len([e for e in events if e["kind"] == "planner.wake"]) == 2
    evaluations = [e["payload"] for e in events if e["kind"] == "wake.evaluated"]
    assert [e["matched"] for e in evaluations if e["id"] == event["id"]] == [False, True]
    assert next(e for e in evaluations if e["id"] == deadline["id"])["sample_tick"] == 20
    journal.close()
    journal = Journal(path)
    active = {e["id"]: e for e in journal.wake_conditions("colony", "epoch")}
    assert deadline["id"] not in active and event["id"] not in active
    assert active[periodic["id"]]["next_tick"] == 25
    journal.ingest_game_event("colony", "epoch", 20, "conn", native(4, 20))
    assert len([e for e in journal.events("colony") if e["kind"] == "planner.wake"]) == 2
    journal.close()


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
async def test_scheduler_tools_bind_author_and_use_native_catalog(tmp_path):
    from types import SimpleNamespace

    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..tool_contracts import ToolExecutionError, ToolSetup, ToolSpec
    from ..workspace.runtime import FrontdeskService

    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    scheduler = ColonyScheduler(service, None)
    async def read(args):
        return {}
    native_tool = ToolSpec("query", "read", {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"], "additionalProperties": False}, read)
    scheduler._event_setup = ToolSetup((native_tool,), directory={})
    scheduler._event_identity = lambda: ("conn", "colony", "epoch")
    planner = scheduler.scheduler_tools(game, "planner")[0].handler
    frontdesk = scheduler.scheduler_tools(game, "frontdesk")[0].handler
    condition = {"kind": "numeric", "tool": "query", "arguments": {"count": "bad"}, "path": [], "op": "lt", "value": 4}
    condition["tool"] = "non_native"
    with pytest.raises(ToolExecutionError):
        await planner({"action": "register", "condition": condition, "text": "check"})
    condition["tool"] = "query"
    condition["arguments"] = {"count": 1}
    result = await planner({"action": "register", "condition": condition, "text": "check"})
    with pytest.raises(ToolExecutionError):
        await frontdesk({"action": "cancel", "id": result["id"]})
    with pytest.raises(ToolExecutionError):
        await planner({"action": "policy", "changes": {"pause_on_letter_defs": []}})
    state = await frontdesk({"action": "read"})
    assert state["native_reads"][0]["parameters"] == native_tool.parameters
    assert state["conditions"][0]["role"] == "planner"
    await service.close()


@pytest.mark.asyncio
async def test_numeric_sampling_discards_old_connection_response(tmp_path):
    from types import SimpleNamespace

    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..tool_contracts import ToolSetup, ToolSpec
    from ..workspace.runtime import FrontdeskService

    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    scheduler = ColonyScheduler(service, None)
    identity = ["conn"]
    async def read(operation, args):
        identity[0] = "new"
        return {"response": {"connection_epoch": "conn", "colony_id": "colony", "load_epoch": "epoch", "game_tick": 260,
                             "payload": {"status": "ok", "data": {"value": 1}}}}
    scheduler._event_setup = ToolSetup((ToolSpec("query", "read", {"type": "object"}, None),), directory={}, idle_query=read)
    scheduler._event_identity = lambda: (identity[0], "colony", "epoch")
    await service.call("register_wake", "colony", "epoch", 10, "planner", {"kind": "numeric", "tool": "query", "arguments": {}, "path": ["response", "payload", "data", "value"], "op": "lt", "value": 2, "interval_ticks": 250}, "check")
    await scheduler._sample_numeric(("conn", "colony", "epoch"), 260)
    events = await service.call("events", "colony")
    assert not any(event["kind"] in {"wake.evaluated", "planner.wake"} for event in events)
    await service.close()


def test_policy_changes_rebuild_default_review_and_subscriptions(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.ingest_game_event("colony", "epoch", 10, "conn", native(1, 10))
    journal.set_wake_policy("colony", "epoch", 20, "planner", {"planner_review_ticks": 100})
    journal.set_wake_policy("colony", "epoch", 20, "frontdesk", {"frontdesk_letter_defs": ["Custom"], "frontdesk_choice_letters": False, "frontdesk_paused_windows": False})
    journal.ingest_game_event("colony", "epoch", 30, "conn", native(2, 30, "letter", {"def": "ThreatBig", "is_choice_letter": True}))
    assert not any(e["kind"] == "frontdesk.wake" for e in journal.events("colony"))
    journal.ingest_game_event("colony", "epoch", 40, "conn", native(3, 40, "letter", {"def": "Custom"}))
    assert len([e for e in journal.events("colony") if e["kind"] == "frontdesk.wake"]) == 1
    journal.ingest_game_event("colony", "epoch", 120, "conn", native(4, 120))
    assert len([e for e in journal.events("colony") if e["kind"] == "planner.wake"]) == 1
    journal.ingest_game_event("colony", "epoch", 120, "new", native(1, 120, connection="new"))
    assert len([e for e in journal.events("colony") if e["kind"] == "planner.wake"]) == 1
    journal.close()


@pytest.mark.asyncio
async def test_numeric_sampling_persists_native_observation_and_actual_tick(tmp_path):
    from types import SimpleNamespace

    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..tool_contracts import ToolSetup, ToolSpec
    from ..workspace.runtime import FrontdeskService

    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=260)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    scheduler = ColonyScheduler(service, None)
    observation = {"observation_id": "original", "response": {"connection_epoch": "conn", "colony_id": "colony", "load_epoch": "epoch", "game_tick": 270,
                     "payload": {"status": "ok", "data": {"value": 1}}}}
    async def read(operation, args):
        assert operation == "query"
        assert args == {"foo": "bar"}
        return observation
    scheduler._event_setup = ToolSetup((ToolSpec("query", "read", {"type": "object"}, None),), directory={}, idle_query=read)
    scheduler._event_identity = lambda: ("conn", "colony", "epoch")
    await service.call("register_wake", "colony", "epoch", 10, "planner", {"kind": "numeric", "tool": "query", "arguments": {"foo": "bar"}, "path": ["response", "payload", "data", "value"], "op": "lt", "value": 2, "interval_ticks": 250}, "check")
    await scheduler._sample_numeric(("conn", "colony", "epoch"), 260)
    events = await service.call("events", "colony")
    evaluation = next(event["payload"] for event in events if event["kind"] == "wake.evaluated")
    assert evaluation["observation"] == observation and evaluation["sample_tick"] == 270
    assert evaluation["value"] == 1 and evaluation["matched"] is True
    assert len([event for event in events if event["kind"] == "planner.wake"]) == 1
    await service.close()


@pytest.mark.asyncio
async def test_missing_idle_provider_defers_and_busy_does_not_call_normal_handler(tmp_path):
    from types import SimpleNamespace

    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..tool_contracts import ToolExecutionError, ToolSetup, ToolSpec
    from ..workspace.runtime import FrontdeskService

    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=260)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    scheduler = ColonyScheduler(service, None)
    async def normal(args):
        pytest.fail("Numeric sampling must never enqueue an ordinary read")
    spec = ToolSpec("query", "read", {"type": "object"}, normal)
    scheduler._event_setup = ToolSetup((spec,), directory={})
    scheduler._event_identity = lambda: ("conn", "colony", "epoch")
    item = await service.call("register_wake", "colony", "epoch", 10, "planner", {"kind": "numeric", "tool": "query", "arguments": {}, "path": ["response", "payload", "data"], "op": "lt", "value": 0, "interval_ticks": 250}, "check")
    await scheduler._sample_numeric(("conn", "colony", "epoch"), 260)
    assert (await service.call("wake_conditions", "colony", "epoch"))[0]["next_tick"] == 260
    async def busy(operation, arguments):
        raise ToolExecutionError("bridge_busy", "busy")
    scheduler._event_setup = ToolSetup((spec,), directory={}, idle_query=busy)
    await scheduler._sample_numeric(("conn", "colony", "epoch"), 260)
    state = await service.call("wake_conditions", "colony", "epoch")
    assert state[0]["id"] == item["id"] and state[0]["next_tick"] == 260
    evaluation = next(e["payload"] for e in await service.call("events", "colony") if e["kind"] == "wake.evaluated")
    assert evaluation["observation"]["error"]["code"] == "bridge_busy" and not evaluation["matched"]
    async def available(operation, arguments):
        return {"response": {"connection_epoch": "conn", "colony_id": "colony", "load_epoch": "epoch", "game_tick": 260,
                             "payload": {"status": "ok", "data": 1}}}
    scheduler._event_setup = ToolSetup((spec,), directory={}, idle_query=available)
    await scheduler._sample_numeric(("conn", "colony", "epoch"), 260)
    assert (await service.call("wake_conditions", "colony", "epoch"))[0]["next_tick"] == 510
    await service.close()


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


def test_event_filter_missing_null_and_boolean_are_distinct(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    null = journal.register_wake("colony", "epoch", 1, "planner", {"kind": "event", "event_kind": "letter", "filters": {"value": None}}, "null")
    integer = journal.register_wake("colony", "epoch", 1, "planner", {"kind": "event", "event_kind": "letter", "filters": {"value": 1}}, "one")
    journal.ingest_game_event("colony", "epoch", 2, "conn", native(1, 2, "letter", {}))
    journal.ingest_game_event("colony", "epoch", 3, "conn", native(2, 3, "letter", {"value": True}))
    assert not any(e["kind"] == "planner.wake" for e in journal.events("colony"))
    journal.ingest_game_event("colony", "epoch", 4, "conn", native(3, 4, "letter", {"value": None}))
    journal.ingest_game_event("colony", "epoch", 5, "conn", native(4, 5, "letter", {"value": 1}))
    matched = [e["payload"]["id"] for e in journal.events("colony") if e["kind"] == "wake.evaluated" and e["payload"]["matched"]]
    assert matched == [null["id"], integer["id"]]
    journal.close()


@pytest.mark.parametrize("incomplete", [{"status": "partial"}, {"issues": ["lost"]}, {"truncated": True}, {"coverage": {"complete": False}}, {"status": "unknown"}])
def test_numeric_rejects_incomplete_ancestors(tmp_path, incomplete):
    journal = Journal(tmp_path / "events.sqlite")
    item = journal.register_wake("colony", "epoch", 0, "planner", {"kind": "numeric", "tool": "query", "arguments": {}, "path": ["response", "payload", "data", "value"], "op": "lt", "value": 2, "interval_ticks": 250}, "check")
    observation = {"response": {"payload": {"status": "ok", "data": {**incomplete, "value": 1}}}}
    assert not journal.evaluate_wake("colony", "epoch", 250, item["id"], observation, expected_next_tick=250)
    entry = next(e["payload"] for e in journal.events("colony") if e["kind"] == "wake.evaluated")
    assert entry["error"] and not entry["matched"] and entry["observation"] == observation
    journal.close()


def test_default_review_cancel_stays_cancelled_and_policy_replaces_only_default(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.ingest_game_event("colony", "epoch", 0, "conn", native(1, 0))
    default = journal.wake_conditions("colony", "epoch")[0]
    custom = journal.register_wake("colony", "epoch", 0, "planner", {"kind": "periodic", "interval_ticks": 500}, "custom")
    journal.set_wake_policy("colony", "epoch", 1, "planner", {"planner_review_ticks": 200})
    active = journal.wake_conditions("colony", "epoch")
    assert len(active) == 2 and custom["id"] in [item["id"] for item in active]
    replacement = next(item for item in active if item["id"] != custom["id"])
    assert replacement["id"] != default["id"] and replacement["condition"]["interval_ticks"] == 200
    journal.cancel_wake("colony", "epoch", 2, "planner", replacement["id"])
    journal.ingest_game_event("colony", "epoch", 10, "conn", native(2, 10))
    assert [item["id"] for item in journal.wake_conditions("colony", "epoch")] == [custom["id"]]
    journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response_changes", [{"connection_epoch": "wrong"}, {"game_tick": None}, {"game_tick": 259}])
async def test_numeric_invalid_sample_retains_exact_original(tmp_path, response_changes):
    from types import SimpleNamespace

    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..tool_contracts import ToolSetup, ToolSpec
    from ..workspace.runtime import FrontdeskService

    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=260)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    scheduler = ColonyScheduler(service, None)
    original = {"observation_id": "raw", "response": {"connection_epoch": "conn", "colony_id": "colony",
                "load_epoch": "epoch", "game_tick": 260, "payload": {"status": "ok", "data": 1}}}
    original["response"].update(response_changes)
    async def read(operation, args):
        return original
    scheduler._event_setup = ToolSetup((ToolSpec("query", "read", {"type": "object"}, None),), directory={}, idle_query=read)
    scheduler._event_identity = lambda: ("conn", "colony", "epoch")
    await service.call("register_wake", "colony", "epoch", 10, "planner", {"kind": "numeric", "tool": "query", "arguments": {}, "path": ["response", "payload", "data"], "op": "lt", "value": 2, "interval_ticks": 250}, "check")
    await scheduler._sample_numeric(("conn", "colony", "epoch"), 260)
    events = await service.call("events", "colony")
    evaluation = next(e["payload"] for e in events if e["kind"] == "wake.evaluated")
    assert evaluation["observation"]["observation"] == original
    assert evaluation["observation"]["error"]["code"] == "invalid_observation"
    assert not evaluation["matched"] and not any(e["kind"] == "planner.wake" for e in events)
    await service.close()


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

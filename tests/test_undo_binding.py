import asyncio
from types import SimpleNamespace

import pytest

from .test_plugin import make_plugin


@pytest.mark.asyncio
async def test_undo_rejects_connection_change_while_waiting_for_write_lock():
    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    plugin._bridge = SimpleNamespace(session=game, connection_key="A")
    lock = asyncio.Lock()
    plugin._colony_scheduler = SimpleNamespace(halted=False, write_lock=lock)
    plugin._workspace_service = object()
    await lock.acquire()
    task = asyncio.create_task(plugin.autoplay_undo(seq=7, ui_nonce=plugin._settings_action_nonce))
    await asyncio.sleep(0)
    plugin._bridge.connection_key = "B"
    lock.release()
    result = await asyncio.wait_for(task, 1)
    assert not result.is_ok() and result.error.code == "world_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["event", "setup", "audit", "scheduler", "stable"])
async def test_undo_rejects_binding_changed_before_dispatch(monkeypatch, stage):
    from ..agent.write import service as write_service
    from ..workspace import takeovers

    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    plugin._bridge = SimpleNamespace(session=game, connection_key="A")
    calls = []
    class Work:
        async def call(self, method, *args, **kwargs):
            if method == "event":
                if stage == "event":
                    plugin._bridge.connection_key = "B"
                return {"load_epoch": "e", "kind": "planner.progress", "payload": {"write": {
                    "stage": "result", "mode": "commit", "receipt": {"status": "committed", "undo_token": "token"}}}}
            if method == "record" and stage == "audit":
                plugin._bridge.connection_key = "B"
    class Scheduler:
        halted = False
        async def wake(self, *args, **kwargs):
            pass
        async def wake_frontdesk(self, *args, **kwargs):
            pass
    async def setup():
        if stage == "setup":
            plugin._bridge.connection_key = "B"
        if stage == "scheduler":
            plugin._colony_scheduler = Scheduler()
        return SimpleNamespace(write_catalog={}, write_port=object(),
                               write_identity=lambda: (plugin._bridge.connection_key, "c", "e"),
                               ledger_query=object(), write_fields_query=object())
    class Guard:
        def __init__(self, *args, **kwargs):
            pass
    class Writer:
        def __init__(self, *args, **kwargs):
            pass
        async def undo(self, args):
            calls.append(args)
            return {"status": "undone"}
    monkeypatch.setattr(write_service, "WriteService", Writer)
    monkeypatch.setattr(takeovers, "TakeoverControl", Guard)
    plugin._workspace_service = Work()
    plugin._load_agent_tools = setup
    scheduler = plugin._colony_scheduler = Scheduler()
    result = await plugin._autoplay_undo_locked(7, scheduler)
    if stage == "stable":
        assert result.is_ok() and calls == [{"token": "token"}]
        return
    assert not result.is_ok() and result.error.code == "world_changed"
    assert calls == []


@pytest.mark.asyncio
async def test_undo_unknown_outcome_stops_and_reports_without_retry(monkeypatch):
    from ..agent.write import service as write_service
    from ..tool_contracts import ToolExecutionError
    from ..workspace import takeovers

    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    plugin._bridge = SimpleNamespace(session=game, connection_key="A")
    calls, failures = [], []
    class Work:
        async def call(self, method, *args, **kwargs):
            if method == "event":
                return {"load_epoch": "e", "kind": "planner.progress", "payload": {"write": {
                    "stage": "result", "mode": "commit", "receipt": {"status": "committed", "undo_token": "token"}}}}
    class Scheduler:
        halted = False
    async def setup():
        return SimpleNamespace(write_catalog={}, write_port=object(), write_identity=lambda: ("A", "c", "e"),
                               ledger_query=object(), write_fields_query=object())
    class Guard:
        def __init__(self, *args, **kwargs):
            pass
    class Writer:
        def __init__(self, *args, **kwargs):
            pass
        async def undo(self, args):
            calls.append(args)
            raise ToolExecutionError("undo_outcome_unknown", "ambiguous", recoverable=False)
    async def failure(role, result, bound_game, **kwargs):
        failures.append((role, result["audit"]["error_code"], bound_game.connection_epoch))
    monkeypatch.setattr(write_service, "WriteService", Writer)
    monkeypatch.setattr(takeovers, "TakeoverControl", Guard)
    plugin._workspace_service = Work()
    plugin._load_agent_tools = setup
    plugin._autoplay_failure = failure
    scheduler = plugin._colony_scheduler = Scheduler()
    result = await plugin._autoplay_undo_locked(7, scheduler)
    assert not result.is_ok() and result.error.code == "undo_outcome_unknown"
    assert len(calls) == 1 and failures == [("scheduler", "undo_outcome_unknown", "A")]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["not_sent", "known", "unknown"])
async def test_undo_audit_failure_preserves_outcome_and_reconciliation(monkeypatch, outcome):
    from ..agent.write import service as write_service
    from ..tool_contracts import ToolExecutionError
    from ..workspace import takeovers

    plugin, _ = make_plugin({"neko_rimworld": {}})
    plugin._bridge = SimpleNamespace(session=SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1), connection_key="A")
    failures = []
    class Work:
        async def call(self, method, *args, **kwargs):
            if method == "event":
                return {"load_epoch": "e", "kind": "planner.progress", "payload": {"write": {
                    "stage": "result", "mode": "commit", "receipt": {"status": "committed", "undo_token": "token"}}}}
    class Scheduler:
        halted = False
    async def setup():
        return SimpleNamespace(write_catalog={}, write_port=object(), write_identity=lambda: ("A", "c", "e"),
                               ledger_query=object(), write_fields_query=object())
    class Guard:
        def __init__(self, *args, **kwargs):
            pass
    class Writer:
        def __init__(self, *args, **kwargs):
            pass
        async def undo(self, args):
            raise ToolExecutionError("write_audit_failed", "audit failed", recoverable=False, details={"outcome": outcome})
    async def failure(role, result, bound_game, **kwargs):
        failures.append(result)
    monkeypatch.setattr(write_service, "WriteService", Writer)
    monkeypatch.setattr(takeovers, "TakeoverControl", Guard)
    plugin._workspace_service, plugin._load_agent_tools, plugin._autoplay_failure = Work(), setup, failure
    scheduler = plugin._colony_scheduler = Scheduler()
    result = await plugin._autoplay_undo_locked(7, scheduler)
    assert not result.is_ok() and result.error.details["outcome"] == outcome
    assert failures[0]["write_recording"]["needs_reconciliation"] is (outcome != "not_sent")
    if outcome == "not_sent":
        assert "not sent" in str(result.error)

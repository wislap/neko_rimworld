"""Real scheduler/Journal interleavings with a controlled model response."""
import asyncio
from types import SimpleNamespace

import pytest

from ..agent.runtime.audit import AgentAuditRegistry
from ..agent.runtime.colony_scheduler import ColonyScheduler, _PlannerResult
from ..agent.runtime.loop import AgentRuntime
from ..workspace.runtime import FrontdeskService
from .consultation_setup import semantic_tool_provider
from .test_agent import FakeLogger, settings
from .test_capability_composition import Script, call
from .test_plugin import make_plugin


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_ack", [False, True])
async def test_response_after_reload_saves_old_terminal_and_runs_new_epoch(tmp_path, lost_ack):
    entered, release = asyncio.Event(), asyncio.Event()
    class DelayedScript(Script):
        async def complete(self, *args, **kwargs):
            if not self.requests:
                entered.set()
                await release.wait()
            return await super().complete(*args, **kwargs)
    model = DelayedScript([call("planner_yield", {"handoff":"old result"}),
                           call("planner_yield", {"handoff":"new result"})])
    state = {"game":SimpleNamespace(colony_id="c", load_epoch="old", game_tick=1, connection_epoch="A")}
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _:model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda:state["game"])
    original_call = service.call
    lost = False
    async def commit_without_ack(method, *args, **kwargs):
        nonlocal lost
        result = await original_call(method, *args, **kwargs)
        if lost_ack and method == "finish_planner" and args[1] == "old" and not lost:
            lost = True
            raise OSError("injected lost acknowledgement after commit")
        return result
    service.call = commit_without_ack
    plugin, _ = make_plugin({"neko_rimworld":{}})
    plugin._autoplay_waiters = {}
    plugin._bridge = SimpleNamespace(session=state["game"], connection_key="A")
    async def handler(role, result, origin):
        await plugin._autoplay_failure(role, result, origin, origin_scheduler=scheduler)
    scheduler = ColonyScheduler(service, settings(), failure_handler=None if lost_ack else handler,
                                binding=lambda:(state["game"].connection_epoch, "c", state["game"].load_epoch))
    plugin._colony_scheduler = scheduler
    scheduler.start()
    try:
        await scheduler.wake(state["game"], source="plugin", text="start old")
        await asyncio.wait_for(entered.wait(), 3)
        await service.call("record", "c", "old", 100, "plugin", "clock.observation", {})
        state["game"] = SimpleNamespace(colony_id="c", load_epoch="new", game_tick=5, connection_epoch="A")
        plugin._bridge.session = state["game"]
        await service.call("observe_epoch", "c", "new", 5)
        await scheduler.wake(state["game"], source="plugin", text="start new")
        release.set()
        async with asyncio.timeout(4):
            while True:
                rows = await service.call("events", "c")
                terminals = [e for e in rows if e["kind"] == "planner.finished"]
                if any(e["load_epoch"] == "new" for e in terminals):
                    break
                await asyncio.sleep(.01)
        assert len(terminals) == 2
        old = next(e for e in terminals if e["load_epoch"] == "old")
        assert old["game_tick"] == 100 and old["payload"]["context_tick"] == 1
        assert not scheduler.halted and scheduler.unsaved_result is None
        assert isinstance(scheduler.error, OSError) if lost_ack else scheduler.error is None
        assert not any(e["kind"] == "autoplay.halted" for e in rows)
        if lost_ack:
            diagnostic = next(e for e in rows if e["kind"] == "autoplay.scheduler_exception")
            assert diagnostic["load_epoch"] == "old"
            assert diagnostic["payload"]["phase"] == "planner_result_persistence"
            assert diagnostic["payload"]["start_seq"] == old["payload"]["start_seq"]
    finally:
        release.set()
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
async def test_pending_result_retry_keeps_origin_and_does_not_clear_new_pause_state():
    old = SimpleNamespace(colony_id="c", load_epoch="old", game_tick=10, connection_epoch="old-connection")
    new = SimpleNamespace(colony_id="c", load_epoch="new", game_tick=2, connection_epoch="new-connection")
    calls = []
    class Service:
        def game_session(self): return new
        async def call(self, method, *args):
            calls.append((method,args))
            if len(calls) == 1:
                raise OSError("injected unavailable store")
            return 20
    scheduler = ColonyScheduler(Service(), None)
    pending = _PlannerResult(old, 10, 4, 7, {"terminal":{"kind":"yield", "handoff":"old"}}, {"run_id":"old-run"})
    scheduler.unsaved_result = pending
    scheduler._pause_waiting = True
    scheduler._last_yield = ("new pause marker",)
    with pytest.raises(OSError):
        await scheduler._persist_planner_result()
    assert scheduler.unsaved_result is pending
    await scheduler._persist_planner_result()
    assert calls[0] == calls[1] and calls[1][1][:3] == ("c","old",10)
    assert scheduler.unsaved_result is None
    assert scheduler._pause_waiting is True and scheduler._last_yield == ("new pause marker",)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["workspace_unavailable", "planner_result_persistence_failed"])
async def test_real_persistence_failure_keeps_original_scope_and_stops_shared_runtime(code):
    plugin, _ = make_plugin({"neko_rimworld":{}})
    current = SimpleNamespace(colony_id="c", load_epoch="new", game_tick=1)
    old = SimpleNamespace(colony_id="c", load_epoch="old", game_tick=100, connection_epoch="A")
    plugin._bridge = SimpleNamespace(session=current, connection_key="B")
    recorded = []
    class Service:
        async def call(self, *args, **kwargs): recorded.append(args)
    scheduler = plugin._colony_scheduler = SimpleNamespace(halted=False, budget=SimpleNamespace(blocked=False), service=Service())
    plugin._autoplay_waiters = {}
    for index, binding in enumerate(((scheduler,"A","c","old"), (scheduler,"B","c","new"))):
        future = asyncio.get_running_loop().create_future()
        future._autoplay_binding = binding
        plugin._autoplay_waiters[index] = future
    async def notify(*args, **kwargs): pass
    plugin._audit_failure_notification = notify
    await plugin._autoplay_failure("planner", {"audit":{"error_code":code, "start_seq":4,
                                  "context_tick":100, "run_id":"old-run"}}, old, origin_scheduler=scheduler)
    assert scheduler.halted and scheduler.budget.blocked
    assert plugin._autoplay_fault["origin_load_epoch"] == "old"
    assert plugin._autoplay_fault["origin_connection_epoch"] == "A"
    assert plugin._autoplay_fault["start_seq"] == 4
    assert all(waiter.done() for waiter in plugin._autoplay_waiters.values())
    assert not any(args[4] == "autoplay.stale_failure" for args in recorded)


@pytest.mark.asyncio
async def test_stop_keeps_pending_owner_until_storage_recovers():
    plugin, _ = make_plugin({"neko_rimworld":{}})
    old = SimpleNamespace(colony_id="c", load_epoch="old", game_tick=10, connection_epoch="A")
    current = SimpleNamespace(colony_id="c", load_epoch="new", game_tick=1)
    unavailable = True
    class Service:
        def game_session(self): return current
        async def call(self, method, *args, **kwargs):
            if method == "finish_planner" and unavailable:
                raise OSError("injected unavailable store")
            return 20
    async def failure(role, result, origin):
        await plugin._autoplay_failure(role, result, origin, origin_scheduler=scheduler)
    scheduler = ColonyScheduler(Service(), None, failure_handler=failure)
    scheduler.budget = SimpleNamespace(blocked=False)
    pending = _PlannerResult(old,10,4,7,{"status":"interrupted"},{"run_id":"old-run"})
    scheduler.unsaved_result = pending
    plugin._colony_scheduler = scheduler
    plugin._bridge = SimpleNamespace(session=current, connection_key="B")
    plugin._autoplay_waiters = {}
    async def notify(*args, **kwargs): pass
    plugin._audit_failure_notification = notify
    with pytest.raises(OSError):
        await plugin._stop_autoplay()
    assert plugin._colony_scheduler is scheduler and scheduler.unsaved_result is pending
    assert scheduler.halted and scheduler.budget.blocked
    assert scheduler._worker is None
    unavailable = False
    await plugin._stop_autoplay()
    assert plugin._colony_scheduler is None and scheduler.unsaved_result is None

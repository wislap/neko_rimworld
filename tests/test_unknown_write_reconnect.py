import asyncio
import threading
from types import SimpleNamespace

import pytest

from .test_plugin import make_plugin


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["undo_outcome_unknown", "write_receipt_lost", "write_record_issue"])
async def test_unknown_write_from_old_connection_halts_same_world(code):
    plugin, _ = make_plugin({"neko_rimworld": {}})
    current = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=10)
    old = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=9, connection_epoch="A")
    plugin._bridge = SimpleNamespace(session=current, connection_key="B")
    recorded = []
    class Service:
        async def call(self, *args, **kwargs):
            recorded.append(args)
    scheduler = plugin._colony_scheduler = SimpleNamespace(halted=False, budget=SimpleNamespace(blocked=False), service=Service())
    plugin._autoplay_waiters = {}
    for index, connection in enumerate(("A", "B")):
        future = asyncio.get_running_loop().create_future()
        future._autoplay_binding = (scheduler, connection, "c", "e")
        plugin._autoplay_waiters[index] = future
    async def notification(*args, **kwargs):
        pass
    plugin._audit_failure_notification = notification
    result = {"status": "failed", "audit": {"error_code": code}}
    if code == "write_record_issue":
        result["write_recording"] = {"stage": "result", "mode": "commit", "outcome": "known", "needs_reconciliation": True}
    await plugin._autoplay_failure("planner", result, old,
                                   origin_scheduler=scheduler)
    assert scheduler.halted and scheduler.budget.blocked
    assert all(f.done() and f.result()["audit"]["error_code"] == code for f in plugin._autoplay_waiters.values())
    assert plugin._autoplay_fault["origin_connection_epoch"] == "A"
    assert plugin._autoplay_fault["observed_connection_epoch"] == "B"
    halted = next(args for args in recorded if args[5] == "autoplay.halted")
    assert halted[6]["origin_connection_epoch"] == "A" and halted[6]["observed_connection_epoch"] == "B"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ordinary_failure", "new_epoch", "new_scheduler", "not_sent"])
async def test_other_stale_failures_do_not_halt_current_world(case):
    plugin, _ = make_plugin({"neko_rimworld": {}})
    current = SimpleNamespace(colony_id="c", load_epoch="new" if case == "new_epoch" else "e", game_tick=10)
    old = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=9, connection_epoch="A")
    plugin._bridge = SimpleNamespace(session=current, connection_key="B")
    class Service:
        async def call(self, *args, **kwargs):
            pass
    scheduler = plugin._colony_scheduler = SimpleNamespace(halted=False, budget=SimpleNamespace(blocked=False), service=Service())
    origin = SimpleNamespace(halted=False, budget=SimpleNamespace(blocked=False), service=Service()) if case == "new_scheduler" else scheduler
    plugin._autoplay_waiters = {}
    code = "world_changed" if case == "ordinary_failure" else "write_record_issue" if case == "not_sent" else "undo_outcome_unknown"
    result = {"audit": {"error_code": code}}
    if case == "not_sent":
        result["write_recording"] = {"outcome": "not_sent", "needs_reconciliation": False}
    await plugin._autoplay_failure("planner", result, old, origin_scheduler=origin)
    assert not scheduler.halted and not scheduler.budget.blocked


@pytest.mark.asyncio
async def test_queued_wake_policy_mutation_rechecks_halt_on_worker(tmp_path):
    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..tool_contracts import ToolExecutionError
    from ..agent.runtime.frontdesk_service import FrontdeskService

    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    await service.call("record_autoplay_activation", "c", "e", 1,
                       {"goal": "survive", "constraints": "none", "goal_edited": False, "constraints_edited": False})
    strategy = {"entry_id": "s", "expected_revision": 0, "kind": "strategy", "status": "open", "priority": 1,
                "parent": None, "source": {}, "evidence": [], "review_at_tick": None, "changesets": [], "body": "plan"}
    task_entry = {**strategy, "entry_id": "t", "kind": "task", "parent": "s", "source": {"entry_id": "s"}}
    await service.call("submit", "c", "e", 1, "planner", strategy)
    await service.call("submit", "c", "e", 1, "planner", task_entry)
    scheduler = ColonyScheduler(service, None, binding=lambda: ("B", "c", "e"))
    tool = scheduler.scheduler_tools(game, "planner")[0]
    started, release = threading.Event(), threading.Event()
    def block():
        started.set()
        release.wait(5)
    blocking = asyncio.get_running_loop().run_in_executor(service._executor, block)
    try:
        await asyncio.to_thread(started.wait)
        task = asyncio.create_task(tool.handler({"action": "declare", "level": "focus", "entry_id": "t", "reason": "review"}))
        await asyncio.sleep(0)
        scheduler.halted = True
        release.set()
        await blocking
        with pytest.raises(ToolExecutionError):
            await task
        assert not any(event["kind"] == "cadence.lease_declared" for event in await service.call("events", "c"))
        await service.call("record", "c", "e", 1, "planner", "planner.progress", {"start_seq": 1})
        assert any(e["kind"] == "planner.progress" for e in await service.call("events", "c"))
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_unknown_write_cancels_other_role_not_failure_handler():
    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    plugin._bridge = SimpleNamespace(session=game, connection_key="B")
    other = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    class Service:
        async def call(self, *args, **kwargs):
            pass
    scheduler = plugin._colony_scheduler = SimpleNamespace(
        halted=False, budget=SimpleNamespace(blocked=False), service=Service(),
        _worker=asyncio.current_task(), _frontdesk_worker=other)
    plugin._autoplay_waiters = {}
    async def notification(*args, **kwargs):
        pass
    plugin._audit_failure_notification = notification
    old = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1, connection_epoch="A")
    await plugin._autoplay_failure("planner", {"audit": {"error_code": "write_receipt_lost"}}, old,
                                   origin_scheduler=scheduler)
    assert not asyncio.current_task().cancelled()
    with pytest.raises(asyncio.CancelledError):
        await other


@pytest.mark.asyncio
async def test_unknown_write_halt_stops_new_connection_model_before_tool(tmp_path):
    from ..agent.runtime.audit import AgentAuditRegistry
    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..agent.runtime.loop import AgentRuntime
    from ..agent.runtime.frontdesk_service import FrontdeskService
    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, settings
    from .test_capability_composition import Script, call

    entered, release = asyncio.Event(), asyncio.Event()
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            entered.set()
            await release.wait()
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call("planner_board", {"action": "write", "entry": {
        "entry_id": "after-halt", "expected_revision": 0, "kind": "strategy", "status": "drafting",
        "priority": 1, "parent": None, "source": {}, "evidence": [], "review_at_tick": None,
        "changesets": [], "body": "Must not be written"}}), call("planner_yield", {"handoff": "done"})])
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=10)
    service = FrontdeskService(tmp_path / "events.sqlite", AgentRuntime(
        audit_registry=AgentAuditRegistry(), logger=FakeLogger(), model_factory=lambda _: model,
        tools_provider=semantic_tool_provider()), game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings(), binding=lambda: ("B", "c", "e"))
    plugin, _ = make_plugin({"neko_rimworld": {}})
    plugin._bridge = SimpleNamespace(session=game, connection_key="B")
    plugin._colony_scheduler = scheduler
    plugin._autoplay_waiters = {}
    async def notification(*args, **kwargs):
        pass
    plugin._audit_failure_notification = notification
    scheduler.start()
    try:
        await scheduler.wake(game, source="plugin", text="Plan")
        await asyncio.wait_for(entered.wait(), 3)
        old = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=9, connection_epoch="A")
        await plugin._autoplay_failure("frontdesk", {"audit": {"error_code": "write_receipt_lost"}}, old,
                                       origin_scheduler=scheduler)
        release.set()
        for _ in range(300):
            events = await service.call("events", "c")
            if any(event["kind"] == "planner.finished" for event in events):
                break
            await asyncio.sleep(.01)
        assert scheduler.halted
        assert any(event["kind"] == "planner.finished" for event in events)
        assert await service.call("board", "c") == []
    finally:
        release.set()
        await scheduler.close()
        await service.close()

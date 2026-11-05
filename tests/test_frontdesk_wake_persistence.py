import asyncio
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent.runtime.colony_scheduler import ColonyScheduler
from plugin.plugins.neko_rimworld.agent.runtime.frontdesk_service import FrontdeskService

from .test_plugin import make_plugin


@pytest.mark.asyncio
async def test_concurrent_same_host_request_keeps_one_utterance_and_wake(tmp_path):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    scheduler = ColonyScheduler(service, None)
    scheduler._worker = asyncio.create_task(asyncio.Event().wait())
    original = service.call
    both_read = asyncio.Event()
    count = 0

    async def synchronized_read(method, *args, **kwargs):
        nonlocal count
        if method == "record_frontdesk_wake":
            count += 1
            if count == 2:
                both_read.set()
            await both_read.wait()
        return await original(method, *args, **kwargs)

    service.call = synchronized_read
    try:
        wakes = await asyncio.gather(*(scheduler.wake_frontdesk(game, "Original words", request_id="host-1") for _ in range(2)))
        events = await original("events", "c")
        assert wakes[0] == wakes[1]
        assert len([e for e in events if e["kind"] == "user.utterance"]) == 1
        assert len([e for e in events if e["kind"] == "frontdesk.wake"]) == 1
    finally:
        scheduler._worker.cancel()
        await asyncio.gather(scheduler._worker, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_duplicate_host_call_waiters_share_one_result(cancel_first):
    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    plugin._bridge = SimpleNamespace(session=game, connection_key="connection")
    plugin._agent_settings = SimpleNamespace(configured=True)
    plugin._autoplay_waiters = {}
    class Service:
        async def call(self, *args, **kwargs):
            return None
    class Scheduler:
        halted = False
        _autoplay_colony = "c"
        service = Service()
        async def wake_frontdesk(self, game, text, *, request_id=None):
            return 10
    plugin._colony_scheduler = Scheduler()
    tasks = [asyncio.create_task(plugin._consultation("Question", {"run_id": "host-1"}, game)) for _ in range(2)]
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        if cancel_first:
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
        plugin._autoplay_waiters[10].set_result({"status": "ok", "answer": "Done", "agent_text": "Done"})
        replies = await asyncio.wait_for(asyncio.gather(*(tasks[1:] if cancel_first else tasks)), 1)
        assert all(reply.is_ok() for reply in replies)
        await asyncio.sleep(0)
        assert not plugin._autoplay_waiters
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_duplicate_request_returning_from_wake_after_delivery_does_not_hang():
    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    plugin._bridge = SimpleNamespace(session=game, connection_key="connection")
    plugin._agent_settings = SimpleNamespace(configured=True)
    plugin._autoplay_waiters = {}
    entered, release = asyncio.Event(), asyncio.Event()
    class Service:
        async def call(self, *args, **kwargs):
            return None
    class Scheduler:
        halted = False
        _autoplay_colony = "c"
        service = Service()
        calls = 0
        async def wake_frontdesk(self, game, text, *, request_id=None):
            self.calls += 1
            entered.set()
            await release.wait()
            return 10
    plugin._colony_scheduler = Scheduler()
    tasks = [asyncio.create_task(plugin._consultation("Question", {"run_id": "host-1"}, game)) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0)
        assert plugin._colony_scheduler.calls == 1
        release.set()
        for _ in range(20):
            if 10 in plugin._autoplay_waiters:
                break
            await asyncio.sleep(0)
        plugin._autoplay_waiters[10].set_result({"status": "ok", "answer": "Done", "agent_text": "Done"})
        assert (await asyncio.wait_for(tasks[0], 1)).is_ok()
        assert (await asyncio.wait_for(tasks[1], 1)).is_ok()
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_readonly_duplicate_host_request_runs_once_and_returns_independent_results(tmp_path):
    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    plugin._bridge = SimpleNamespace(session=game, connection_key="connection")
    plugin._agent_settings = SimpleNamespace(configured=True)
    plugin._colony_scheduler = None
    entered, release = asyncio.Event(), asyncio.Event()
    class Runtime:
        calls = 0
        async def execute(self, session, settings):
            self.calls += 1
            entered.set()
            await release.wait()
            return {"status": "ok", "answer": "Done", "agent_text": "Done",
                    "report": {"saved": True, "report_id": "report-1"}}
    runtime = Runtime()
    exports = []
    async def export(report_id, context):
        exports.append(report_id)
        return {"status": "exported"}
    plugin._export_report = export
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    plugin._workspace_service = service
    tasks = [asyncio.create_task(plugin._consultation("Question", {"run_id": "host-1"}, game)) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        release.set()
        first, second = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert first.is_ok() and second.is_ok() and runtime.calls == 1
        assert exports == ["report-1"]
        assert first.value is not second.value
        assert len([event for event in await service.call("events", "c") if event["kind"] == "agent.run"]) == 1
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
async def test_requests_without_host_id_are_not_coalesced():
    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    plugin._bridge = SimpleNamespace(session=game, connection_key="connection")
    plugin._colony_scheduler = None
    calls = []
    async def execute(question, context, bound_game):
        calls.append(question)
        await asyncio.sleep(0)
        return {"status": "ok"}
    plugin._consultation_impl = execute
    await asyncio.gather(plugin._consultation("Question", {}, game), plugin._consultation("Question", {}, game))
    assert calls == ["Question", "Question"]


@pytest.mark.asyncio
async def test_stop_autoplay_cancels_tracked_shared_consultation():
    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    plugin._bridge = SimpleNamespace(session=game, connection_key="connection")
    plugin._autoplay_delivery = None
    plugin._autoplay_waiters = {}
    class Scheduler:
        async def close(self):
            pass
    plugin._colony_scheduler = Scheduler()
    entered = asyncio.Event()
    async def execute(question, context, bound_game):
        entered.set()
        await asyncio.Event().wait()
    plugin._consultation_impl = execute
    task = asyncio.create_task(plugin._consultation("Question", {"run_id": "host-1"}, game))
    await asyncio.wait_for(entered.wait(), 1)
    await plugin._stop_autoplay()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not plugin._consultation_inflight
    assert not plugin._consultation_tasks

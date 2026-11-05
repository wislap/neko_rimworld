import asyncio
import json
from types import SimpleNamespace

import pytest

from ..agent.runtime import frontdesk_dispatch
from ..agent.runtime.colony_scheduler import ColonyScheduler
from ..agent.runtime.dispatch_diagnostics import record_dispatch_error
from ..entry import NekoRimworldPlugin


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["planner", "frontdesk"])
@pytest.mark.parametrize("record_failure", [False, True, "stuck"])
async def test_dispatch_exception_records_bounded_diagnostics_and_still_halts(role, record_failure, monkeypatch):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=4, connection_epoch="conn")
    events = []
    class Service:
        def game_session(self): return game
        async def call(self, method, *args, **kwargs):
            if method == "record":
                if args[4] == "autoplay.scheduler_exception" and record_failure:
                    if record_failure == "stuck":
                        await asyncio.Event().wait()
                    raise OSError("private database message")
                events.append({"colony": args[0], "epoch": args[1], "kind": args[4], "payload": args[5]})
                return len(events)
            raise AssertionError(method)
    service = Service()
    plugin = object.__new__(NekoRimworldPlugin)
    plugin._bridge = SimpleNamespace(session=game, connection_key="conn")
    plugin._autoplay_waiters = {}
    plugin._autoplay_fault = None
    plugin.ctx = SimpleNamespace(push_message=lambda **kwargs: {"submitted": True})
    completed = asyncio.Event()
    async def handler(role, result, bound):
        await plugin._autoplay_failure(role, result, bound, origin_scheduler=scheduler)
        completed.set()
    scheduler = ColonyScheduler(service, None, binding=lambda: ("conn", "c", "e"), failure_handler=handler)
    plugin._colony_scheduler = scheduler
    scheduler._games["c"] = scheduler._bind_game(game)
    async def crash(*_):
        raise ValueError("private failure text must not leak")
    if role == "planner":
        scheduler._run_colony = crash
    else:
        monkeypatch.setattr(frontdesk_dispatch, "frontdesk_colony", crash)
    task = asyncio.create_task(scheduler._run() if role == "planner" else frontdesk_dispatch.run_frontdesk(scheduler))
    (scheduler._signal if role == "planner" else scheduler._frontdesk_signal).set()
    try:
        await asyncio.wait_for(completed.wait(), 3)
        assert scheduler.halted and plugin._autoplay_fault["code"] == "execution_failed"
        assert plugin._autoplay_fault["diagnostic_status"] == ("unconfirmed" if record_failure else "recorded")
        records = [x for x in events if x["kind"] == "autoplay.scheduler_exception"]
        if record_failure:
            assert not records and plugin._autoplay_fault["diagnostic_seq"] is None
        else:
            assert len(records) == 1 and records[0]["epoch"] == "e"
            payload = records[0]["payload"]
            assert payload["exception_type"] == "ValueError" and payload["phase"] == "scheduler_dispatch"
            assert payload["origin_connection_epoch"] == "conn"
            assert 1 <= len(payload["frames"]) <= 8
            assert all(set(frame) == {"file", "function", "line"} for frame in payload["frames"])
            assert "private failure text" not in json.dumps(payload)
            assert plugin._autoplay_fault["diagnostic_seq"] is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_deep_dispatch_trace_keeps_raise_site_and_zero_seq_is_unconfirmed():
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=4, connection_epoch="conn")
    class Service:
        def __init__(self): self.payload = None
        async def call(self, method, *args, **kwargs):
            self.payload = args[5]
            return 0
    service = Service()
    secret = "local-secret-and-message-must-not-leak"
    def deep(level):
        if level == 0:
            raise ValueError(secret)
        return deep(level - 1)
    try:
        deep(12)
    except ValueError as error:
        diagnostic = await record_dispatch_error(service, "planner", error, game, "execution_failed")
    assert diagnostic["diagnostic_seq"] is None and diagnostic["diagnostic_status"] == "unconfirmed"
    assert service.payload["frames_truncated"] is True
    assert len(service.payload["frames"]) == 8 and service.payload["frames"][-1]["function"] == "deep"
    assert secret not in json.dumps(service.payload)
    assert all(set(frame) == {"file", "function", "line"} for frame in service.payload["frames"])

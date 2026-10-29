import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ..agent.runtime.audit import AgentAuditRegistry
from ..agent.runtime.colony_scheduler import ColonyScheduler
from ..agent.runtime.loop import AgentRuntime
from ..workspace.runtime import FrontdeskService
from .consultation_setup import semantic_tool_provider
from .test_agent import FakeLogger, settings, turn
from .test_capability_composition import Script, call


@pytest.mark.asyncio
async def test_scheduler_fresh_runs_and_mid_run_message(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    first_request = asyncio.Event()
    resume = asyncio.Event()
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            if not self.requests:
                first_request.set()
                await resume.wait()
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call("planner_board", {"action": "read"}),
                   call("planner_yield", {"handoff": "Continue with food."}),
                   call("planner_yield", {"handoff": "Done."})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    await scheduler.wake(game, source="plugin", text="first")
    await asyncio.wait_for(first_request.wait(), 5)
    utterance_seq = await service.call("record_utterance", "colony", "epoch", 10, "during run")
    await scheduler.wake(game, source="user", text="during run", utterance_seq=utterance_seq)
    resume.set()
    for _ in range(500):
        events = await service.call("events", "colony")
        if any(e["kind"] == "planner.finished" for e in events):
            break
        await asyncio.sleep(.01)
    assert any(m.get("name") == "planner_inbox" and "during run" in m["content"] for m in model.requests[1]["messages"])
    await scheduler.wake(game, source="plugin", text="again")
    for _ in range(500):
        events = await service.call("events", "colony")
        if sum(e["kind"] == "planner.finished" for e in events) == 2:
            break
        await asyncio.sleep(.01)
    assert sum(e["kind"] == "planner.finished" for e in events) == 2, scheduler.error
    assert "Continue with food." in str(model.requests[2]["messages"])
    assert not any(m.get("role") == "tool" for m in model.requests[2]["messages"])
    await scheduler.close()
    await service.close()


@pytest.mark.asyncio
async def test_finish_failure_is_retained_and_retried(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    model = Script([call("planner_yield", {"handoff": "Next step"})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    original = service.call
    async def fail(method, *args, **kwargs):
        if method == "finish_planner":
            raise OSError("injected")
        return await original(method, *args, **kwargs)
    service.call = fail
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    await scheduler.wake(game, source="plugin", text="start")
    for _ in range(500):
        if scheduler.error:
            break
        await asyncio.sleep(.01)
    assert scheduler.unsaved_result and isinstance(scheduler.error, OSError)
    await scheduler.close()
    service.call = original
    scheduler.start()
    scheduler.resume(game)
    for _ in range(500):
        if scheduler.unsaved_result is None:
            break
        await asyncio.sleep(.01)
    events = await service.call("events", "colony")
    assert sum(e["kind"] == "planner.finished" for e in events) == 1
    assert sum(e["kind"] == "planner.yield" for e in events) == 1
    await scheduler.close()
    await service.close()


@pytest.mark.asyncio
async def test_frontdesk_explicit_update_only_uses_runtime(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    model = Script([call("update_only", {})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    result = await service.consult(game, "User message", settings(), explicit_update_only=True)
    assert result["terminal"] == {"kind": "update_only"}
    assert result["status"] == "ok"
    await service.close()


@pytest.mark.asyncio
async def test_frontdesk_scheduler_delivers_user_inbox_separately(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    entered = asyncio.Event()
    proceed = asyncio.Event()
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            if not self.requests:
                entered.set()
                await proceed.wait()
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call("frontdesk_board", {"action": "read"}), call("update_only", {})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    await scheduler.wake_frontdesk(game, "First words")
    await asyncio.wait_for(entered.wait(), 5)
    await scheduler.wake_frontdesk(game, "Second words")
    proceed.set()
    for _ in range(500):
        events = await service.call("events", "colony")
        if any(e["kind"] == "frontdesk.scheduler_consumed" for e in events):
            break
        await asyncio.sleep(.01)
    assert any(e["kind"] == "frontdesk.scheduler_consumed" for e in events), scheduler.error
    assert any(m.get("name") == "frontdesk_inbox" and "Second words" in m["content"]
               for m in model.requests[1]["messages"])
    assert sum(e["kind"] == "user.utterance" for e in events) == 2
    await scheduler.close()
    await service.close()


@pytest.mark.asyncio
async def test_interrupted_planner_has_no_synthetic_handoff_and_is_replayed(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    model = Script([turn("unfinished"), call("planner_yield", {"handoff": "Finished"})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, replace(settings(), max_model_calls=1))
    scheduler.start()
    await scheduler.wake(game, source="plugin", text="first")
    for _ in range(500):
        events = await service.call("events", "colony")
        if any(e["kind"] == "planner.finished" for e in events):
            break
        await asyncio.sleep(.01)
    assert not any(e["kind"] == "planner.yield" for e in events)
    scheduler.settings = settings()
    await scheduler.wake(game, source="plugin", text="again")
    for _ in range(500):
        events = await service.call("events", "colony")
        if any(e["kind"] == "planner.yield" for e in events):
            break
        await asyncio.sleep(.01)
    assert any(e["kind"] == "planner.yield" for e in events), scheduler.error
    assert "interrupted_runs" in str(model.requests[1]["messages"])
    assert '"start_seq"' in str(model.requests[1]["messages"])
    await scheduler.close()
    await service.close()


@pytest.mark.asyncio
async def test_frontdesk_reply_requires_ack_and_replays_after_restart(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    model = Script([call("submit_response", {"answer": "Hello"})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    seq = await scheduler.wake_frontdesk(game, "Question")
    delivery = await asyncio.wait_for(scheduler.next_frontdesk_result(), 5)
    assert delivery["delivery_id"] == seq
    assert delivery["result"]["terminal"]["kind"] == "reply"
    assert not any(e["kind"] == "frontdesk.scheduler_consumed" for e in await service.call("events", "colony"))
    await scheduler.close()
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    scheduler.resume(game)
    replay = await asyncio.wait_for(scheduler.next_frontdesk_result(), 5)
    assert replay["delivery_id"] == seq and replay["result"]["answer"] == "Hello"
    assert len(model.requests) == 1
    await scheduler.ack_frontdesk_result(seq)
    assert any(e["kind"] == "frontdesk.scheduler_consumed" for e in await service.call("events", "colony"))
    await scheduler.close()
    await service.close()


@pytest.mark.asyncio
async def test_old_epoch_reasons_are_preserved_and_marked_stale(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="new", game_tick=20)
    model = Script([call("planner_yield", {"handoff": "Ready"})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    utterance = await service.call("record_utterance", "colony", "old", 10, "Old words")
    old_front = await service.call("record", "colony", "old", 10, "plugin", "frontdesk.wake",
                                   {"source": "user", "utterance_seq": utterance})
    old_plan = await service.call("record", "colony", "old", 10, "plugin", "planner.wake",
                                  {"source": "game", "text": "Old game event"})
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    scheduler.resume(game)
    await scheduler.wake(game, source="plugin", text="New reason")
    for _ in range(500):
        events = await service.call("events", "colony")
        if any(e["kind"] == "planner.yield" for e in events) and any(
            e["kind"] == "frontdesk.stale_wake" for e in events
        ):
            break
        await asyncio.sleep(.01)
    assert any(e["kind"] == "frontdesk.stale_wake" and e["payload"]["wake_seq"] == old_front for e in events)
    assert any(e["kind"] == "planner.stale_wake" and e["payload"]["wake_seq"] == old_plan for e in events)
    assert "Old game event" not in str(model.requests[0]["messages"])
    await scheduler.close()
    await service.close()


@pytest.mark.asyncio
async def test_planner_board_status_preconditions_through_runtime(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    entry = {"entry_id": "s", "expected_revision": 0, "kind": "strategy", "status": "committed",
             "priority": 1, "parent": None, "source": {}, "evidence": [], "review_at_tick": None,
             "changesets": [], "body": "Plan"}
    valid = {**entry, "status": "drafting"}
    model = Script([call("planner_board", {"action": "write", "entry": entry}),
                    call("planner_board", {"action": "write", "entry": valid}),
                    call("planner_yield", {"handoff": "Review the plan next."})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    await scheduler.wake(game, source="plugin", text="Plan")
    for _ in range(500):
        events = await service.call("events", "colony")
        if any(e["kind"] == "planner.yield" for e in events):
            break
        await asyncio.sleep(.01)
    board = await service.call("board", "colony")
    assert len(board) == 1 and board[0]["revision"] == 1 and board[0]["status"] == "drafting"
    assert "board_invalid" in str(model.requests[1]["messages"])
    await scheduler.close()
    await service.close()

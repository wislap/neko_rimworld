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
    first_input = next(e["payload"]["model_request"] for e in events
                       if e["kind"] == "planner.progress" and e["payload"].get("stage") == "model_request")
    assert first_input["messages"] == model.requests[0]["messages"]
    assert first_input["tools"] == model.requests[0]["tools"]
    assert "manage_wakes" in {tool["function"]["name"] for tool in first_input["tools"]}
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

@pytest.mark.asyncio
async def test_non_user_trigger_survives_history_and_inbox(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    entered, proceed = asyncio.Event(), asyncio.Event()
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            if not self.requests:
                entered.set()
                await proceed.wait()
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call("frontdesk_board", {"action": "read"}), call("update_only", {}),
                   call("update_only", {})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    await scheduler.wake_frontdesk(game, "danger", source="game")
    await asyncio.wait_for(entered.wait(), 5)
    await scheduler.wake_frontdesk(game, "notice", source="plugin")
    proceed.set()
    await asyncio.wait_for(scheduler.next_frontdesk_result(), 5)
    assert {"manage_wakes", "return_takeover"} <= {tool["function"]["name"] for tool in model.requests[0]["tools"]}
    events = await service.call("events", "colony")
    assert not any(e["kind"] == "user.utterance" for e in events)
    assert any(m.get("name") == "frontdesk_trigger" and "danger" in m["content"] for m in model.requests[0]["messages"])
    assert any(m.get("name") == "frontdesk_trigger" and "notice" in m["content"] for m in model.requests[1]["messages"])
    assert any(e["kind"] == "agent.run" and "trigger_seq" in e["payload"] for e in events)
    await scheduler.wake_frontdesk(game, "actual user")
    await asyncio.wait_for(scheduler.next_frontdesk_result(), 5)
    assert any(m.get("name") == "frontdesk_trigger" and "danger" in m["content"] for m in model.requests[2]["messages"])
    await scheduler.close()
    await service.close()

def test_native_events_are_atomic_deduplicated_and_source_preserved(tmp_path):
    from ..workspace.journal import Journal
    from ..workspace.session_view import recall

    journal = Journal(tmp_path / "events.sqlite")
    letter = {"connection_epoch": "conn", "colony_id": "c", "load_epoch": "e", "game_tick": 42,
              "snapshot_sequence": 7, "cursor": 1, "kind": "letter",
              "data": {"cursor": 1, "kind": "letter", "def": "ThreatBig", "is_choice_letter": False}}
    assert journal.ingest_game_event("c", "e", 42, "conn", letter)
    assert not journal.ingest_game_event("c", "e", 42, "conn", letter)
    events = journal.events("c")
    assert [e["kind"] for e in events] == ["game.event", "wake.registered", "scheduler.default_review", "frontdesk.trigger", "frontdesk.wake"]
    assert recall(events, events[0]["seq"])["event"] == letter
    assert journal.game_event_cursor("c", "e", "conn") == 1
    assert not any(e["kind"] == "user.utterance" for e in events)
    heartbeat = {**letter, "game_tick": 43, "cursor": 2, "kind": "heartbeat", "data": {"cursor": 2, "kind": "heartbeat"}}
    journal.ingest_game_event("c", "e", 43, "conn", heartbeat)
    assert len(journal.events("c")) == 6
    journal.close()

@pytest.mark.asyncio
async def test_pause_tool_uses_trusted_role_and_opt_in(monkeypatch):
    from ..agent.runtime import execution_environment as environment
    from ..agent.runtime.audit import AgentAuditLedger
    from ..agent.runtime.session import AgentRunSession
    from ..tool_contracts import ToolExecutionError, ToolSetup

    class Port:
        capabilities = {"colony_pause.v1"}
        def __init__(self):
            self.calls = []
        async def set_paused(self, paused, *, role):
            self.calls.append((paused, role))
            return {"paused": paused}
    port = Port()
    monkeypatch.setattr(environment, "semantic_interaction_factory", lambda *args: object())
    setup = ToolSetup((), write_port=port)
    session = AgentRunSession.create(ledger=AgentAuditLedger("test"), question="test")
    assert not environment.ExecutionEnvironment(setup, session, None).write_tools
    session.explicit_scheduler = True
    tool = environment.ExecutionEnvironment(setup, session, None).write_tools[0]
    assert set(tool.parameters["properties"]) == {"paused"}
    with pytest.raises(ToolExecutionError):
        await tool.handler({"paused": False})
    with pytest.raises(ToolExecutionError):
        await tool.handler({"paused": True, "role": "planner"})
    await tool.handler({"paused": True})
    session.role = "planner"
    planner_tool = environment.ExecutionEnvironment(setup, session, None).write_tools[0]
    await planner_tool.handler({"paused": False})
    assert port.calls == [(True, "frontdesk"), (False, "planner")]

@pytest.mark.asyncio
async def test_native_event_pump_persists_sample_and_installs_and_clears_policy(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=80)
    model = Script([call("update_only", {})])
    native = {"connection_epoch": "connection", "colony_id": "colony", "load_epoch": "epoch",
              "game_tick": 12, "snapshot_sequence": 3, "cursor": 1, "kind": "letter",
              "data": {"cursor": 1, "kind": "letter", "def": "ThreatBig", "is_choice_letter": False}}
    class Port:
        capabilities = {"colony_events.v1", "colony_pause.v1"}
        def __init__(self):
            self.policies = []
        async def events(self, since=0):
            return [native] if since == 0 else []
        async def push_policy(self, policy):
            self.policies.append(policy)
            return {}
    port = Port()
    base = semantic_tool_provider()
    async def provider():
        return replace(await base(), write_port=port,
                       write_identity=lambda: ("connection", "colony", "epoch"))
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=provider)
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings(), event_provider=provider, binding=lambda: "connection")
    scheduler.start()
    await asyncio.wait_for(scheduler.next_frontdesk_result(), 5)
    events = await service.call("events", "colony")
    raw = next(e for e in events if e["kind"] == "game.event")
    assert raw["game_tick"] == 12 and raw["payload"]["event"] == native
    assert not any(e["kind"] == "user.utterance" for e in events)
    await service.call("set_wake_policy", "colony", "epoch", 80, "frontdesk", {"pause_on_letter_defs": ["Custom"]})
    for _ in range(150):
        if len(port.policies) >= 2:
            break
        await asyncio.sleep(.01)
    await scheduler.close()
    assert port.policies == [{"source": "frontdesk", "pause_on_letter_defs": ["ThreatBig", "ThreatSmall", "NegativeEvent"]},
                             {"source": "frontdesk", "pause_on_letter_defs": ["Custom"]},
                             {"source": "frontdesk", "pause_on_letter_defs": []}]
    await service.close()

@pytest.mark.asyncio
async def test_event_gap_is_recorded_and_stops_binding_across_restart(tmp_path):
    from ..tool_contracts import ToolExecutionError, ToolSetup
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=80)
    class Port:
        capabilities = {"colony_events.v1"}
        calls = 0
        async def events(self, since=0):
            self.calls += 1
            raise ToolExecutionError("event_gap", "lost continuity")
    port = Port()
    async def provider():
        return ToolSetup((), write_port=port,
                         write_identity=lambda: ("connection", "colony", "epoch"))
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings(), event_provider=provider, binding=lambda: "connection")
    scheduler.start()
    for _ in range(100):
        events = await service.call("events", "colony")
        if any(e["kind"] == "game.event_gap" for e in events):
            break
        await asyncio.sleep(.01)
    assert [e["kind"] for e in events] == ["game.event_gap"]
    await scheduler.close()
    scheduler = ColonyScheduler(service, settings(), event_provider=provider, binding=lambda: "connection")
    scheduler.start()
    await asyncio.sleep(.1)
    assert port.calls == 1
    await scheduler.close()
    await service.close()

@pytest.mark.asyncio
async def test_agents_send_each_other_source_bound_messages(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    class Model:
        def __init__(self):
            self.frontdesk = 0
            self.planner = 0
        async def complete(self, messages, tools, *, timeout):
            names = {t["function"]["name"] for t in tools}
            if "planner_yield" in names:
                self.planner += 1
                return call("message_frontdesk", {"text": "planner asks"}) if self.planner == 1 else call("planner_yield", {"handoff": "done"})
            self.frontdesk += 1
            return call("message_planner", {"text": "frontdesk reports"}) if self.frontdesk == 1 else call("update_only", {})
    model = Model()
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    await scheduler.wake_frontdesk(game, "game event", source="game")
    for _ in range(500):
        events = await service.call("events", "colony")
        if any(e["kind"] == "frontdesk.trigger" and e["payload"]["source"] == "planner" for e in events):
            break
        await asyncio.sleep(.01)
    assert any(e["kind"] == "planner.wake" and e["payload"]["source"] == "frontdesk" for e in events)
    assert any(e["kind"] == "frontdesk.trigger" and e["payload"]["source"] == "planner" for e in events)
    assert not any(e["kind"] == "user.utterance" for e in events)
    await scheduler.close()
    await service.close()

@pytest.mark.asyncio
async def test_user_inbox_can_anchor_intent_during_game_trigger(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    entered, proceed = asyncio.Event(), asyncio.Event()
    source = {}
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            if not self.requests:
                entered.set()
                await proceed.wait()
                return await super().complete(messages, tools, timeout=timeout)
            if len(self.requests) == 1:
                self.turns[0] = call("frontdesk_board", {"action": "write_intent", "source_event_seq": source["seq"],
                    "entry": {"entry_id": "intent", "expected_revision": 0, "status": "open", "priority": 1,
                              "review_at_tick": None, "body": "Do not sell gold"}})
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call("frontdesk_board", {"action": "read"}), turn("unused"), call("update_only", {})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    await scheduler.wake_frontdesk(game, "danger", source="game")
    await asyncio.wait_for(entered.wait(), 5)
    await scheduler.wake_frontdesk(game, "Do not sell gold")
    events = await service.call("events", "colony")
    source["seq"] = next(e["seq"] for e in events if e["kind"] == "user.utterance")
    proceed.set()
    await asyncio.wait_for(scheduler.next_frontdesk_result(), 5)
    board = await service.call("board", "colony")
    assert board[0]["source"] == {"event_seq": source["seq"]}
    assert any(str(source["seq"]) in m["content"] and "Do not sell gold" in m["content"]
               for m in model.requests[1]["messages"] if m.get("name") == "frontdesk_inbox")
    await scheduler.close()
    await service.close()

@pytest.mark.asyncio
async def test_temporary_empty_setup_retries_without_rebinding_each_poll(tmp_path):
    from ..tool_contracts import ToolSetup
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=1)
    calls = []
    class Port:
        capabilities = {"colony_events.v1"}
        async def events(self, since=0):
            return []
    async def provider():
        calls.append(True)
        if len(calls) == 1:
            return ToolSetup(())
        return ToolSetup((), write_port=Port(), write_identity=lambda: ("conn", "colony", "epoch"))
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings(), event_provider=provider, binding=lambda: "conn")
    scheduler.start()
    await asyncio.sleep(1.1)
    assert len(calls) == 1
    scheduler._retry_at = 0
    await asyncio.sleep(1.1)
    assert len(calls) == 2 and scheduler._event_port is not None
    await scheduler.close()
    await service.close()

@pytest.mark.asyncio
async def test_event_gap_on_old_connection_does_not_block_new_connection(tmp_path):
    from ..tool_contracts import ToolExecutionError, ToolSetup
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=1)
    state = {"connection": "old", "new_calls": 0}
    class Port:
        capabilities = {"colony_events.v1"}
        def __init__(self, connection):
            self.connection = connection
        async def events(self, since=0):
            if self.connection == "old":
                state["connection"] = "new"
                raise ToolExecutionError("event_gap", "old connection gap")
            state["new_calls"] += 1
            return []
    async def provider():
        connection = state["connection"]
        return ToolSetup((), write_port=Port(connection), write_identity=lambda:
                         (connection, "colony", "epoch") if state["connection"] == connection else None)
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings(), event_provider=provider, binding=lambda: state["connection"])
    scheduler.start()
    for _ in range(300):
        if state["new_calls"]:
            break
        await asyncio.sleep(.01)
    events = await service.call("events", "colony")
    assert state["new_calls"] and events[0]["payload"]["connection"] == "old"
    await scheduler.close()
    await service.close()


def test_epoch_change_is_distinguished_from_reconnect_snapshot(tmp_path):
    from ..workspace.journal import Journal
    journal = Journal(tmp_path / "events.sqlite")
    def snapshot(connection, epoch, cursor):
        return {"connection_epoch": connection, "colony_id": "c", "load_epoch": epoch, "game_tick": 10,
                "snapshot_sequence": 1, "cursor": cursor, "kind": "session_snapshot",
                "data": {"cursor": cursor, "kind": "session_snapshot", "origin": "loaded_game", "start_tick": 8}}
    journal.ingest_game_event("c", "e1", 10, "a", snapshot("a", "e1", 1))
    journal.ingest_game_event("c", "e1", 10, "b", snapshot("b", "e1", 1))
    assert [e["kind"] for e in journal.events("c")] == ["game.event", "wake.registered", "scheduler.default_review", "game.event"]
    journal.ingest_game_event("c", "e2", 10, "b", snapshot("b", "e2", 2))
    assert [e["kind"] for e in journal.events("c")][-7:] == ["colony.load", "game.event", "wake.registered", "scheduler.default_review", "frontdesk.trigger", "frontdesk.wake", "planner.wake"]
    journal.close()

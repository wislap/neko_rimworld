import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ..agent.runtime.colony_scheduler import ColonyScheduler
from ..agent.runtime.scheduler_tools import attention_tools
from ..tool_contracts import ToolExecutionError
from ..workspace.journal import Journal
from ..agent.runtime.frontdesk_service import FrontdeskService

CHOICES = {"goal": "survive", "constraints": "none", "goal_edited": False, "constraints_edited": False}


def native(cursor, tick, kind, data):
    return {"connection_epoch": "conn", "colony_id": "c", "load_epoch": "e", "game_tick": tick,
            "snapshot_sequence": cursor, "cursor": cursor, "kind": kind, "data": {"kind": kind, **data}}


def entry(kind, name, parent=None):
    return {"entry_id": name, "expected_revision": 0, "kind": kind, "status": "open", "priority": 1,
            "parent": parent, "source": {"entry_id": parent} if parent else {}, "evidence": [],
            "review_at_tick": None, "changesets": [], "body": "plan"}


def test_native_snapshot_atomic_due_and_deduplicated(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    fixture = Path(__file__).parent / "fixtures/colony-attention-v1/vectors.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))["valid"][0]["payload"]["data"]
    snapshot = native(1, 250, "attention_snapshot", data)
    assert journal.ingest_game_event("c", "e", 250, "conn", snapshot, attention_available=True)
    before = journal.events("c")
    assert not journal.ingest_game_event("c", "e", 250, "conn", snapshot, attention_available=True)
    assert journal.events("c") == before
    assert len([e for e in before if e["kind"] == "attention.signal"]) == 4
    assert any(e["kind"] == "planner.wake" and e["payload"].get("urgency") == "interrupt" for e in before)
    heartbeat = native(2, 60000, "heartbeat", {"cursor": 2})
    journal.ingest_game_event("c", "e", 60000, "conn", heartbeat)
    daily = [e for e in journal.events("c") if e["kind"] == "cadence.review_due" and e["payload"]["review_type"] == "daily"]
    assert len(daily) == 1 and daily[0]["payload"]["due_tick"] == 60000
    assert not any(e["kind"] == "scheduler.default_review" for e in journal.events("c"))
    journal.close()


def test_task_declaration_validation_and_close_expiry(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    journal.submit("c", "e", 1, "planner", entry("strategy", "s"))
    journal.submit("c", "e", 1, "planner", entry("task", "t", "s"))
    seq = journal.attention_declare("c", "e", 2, "planner", "focus", "t", "check")
    assert journal.attention_state("c", "e", 2)["level"] == "focus"
    with pytest.raises(ValueError, match="cooling"):
        journal.attention_declare("c", "e", 3, "planner", "urgent", "t", "check")
    closed = entry("task", "t", "s")
    closed.update(expected_revision=1, status="closed")
    journal.submit("c", "e", 4, "planner", closed)
    assert any(e["kind"] == "cadence.lease_expired" and e["payload"]["lease_id"] == seq for e in journal.events("c"))
    with pytest.raises(ValueError, match="open planner task"):
        journal.attention_declare("c", "e", 3000, "planner", "focus", "t", "check")
    journal.close()


@pytest.mark.asyncio
async def test_frontdesk_request_uses_live_received_source_set(tmp_path):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    seq = await service.call("record_utterance", "c", "e", 1, "watch closely")
    sources = set()
    scheduler = ColonyScheduler(service, None)
    tools = attention_tools(service, game, "frontdesk", binding_check=lambda: scheduler._business_current(game),
                            planner_signal=scheduler._signal, attention_available=scheduler._native_attention_available,
                            intent_sources=sources)
    request = next(tool for tool in tools if tool.name == "attention_request")
    with pytest.raises(ToolExecutionError):
        await request.handler({"action": "declare", "utterance_seq": seq})
    sources.add(seq)
    await request.handler({"action": "declare", "utterance_seq": seq})
    assert (await service.call("attention_state", "c", "e", 1))["level"] == "focus"
    await request.handler({"action": "revoke", "utterance_seq": seq})
    assert (await service.call("attention_state", "c", "e", 1))["level"] == "steady"
    await service.close()


@pytest.mark.asyncio
async def test_busy_planner_delivers_interrupt_without_losing_earlier_daily(tmp_path):
    from ..agent.runtime.audit import AgentAuditRegistry
    from ..agent.runtime.loop import AgentRuntime
    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, settings
    from .test_capability_composition import Script, call

    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    entered, release = asyncio.Event(), asyncio.Event()
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            if not self.requests:
                entered.set()
                await release.wait()
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call("planner_board", {"action": "read"}),
                   call("planner_yield", {"handoff": "continue"}),
                   call("planner_yield", {"handoff": "done"})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    try:
        scheduler.resume(game)
        await asyncio.wait_for(entered.wait(), 5)
        game.game_tick = 60000
        await service.call("ingest_game_event", "c", "e", 60000, "conn", native(1, 60000, "heartbeat", {}))
        await service.call("ingest_game_event", "c", "e", 60000, "conn", native(2, 60000, "letter", {"def": "ThreatBig"}))
        scheduler._signal.set()
        release.set()
        for _ in range(500):
            events = await service.call("events", "c")
            if sum(e["kind"] == "planner.finished" for e in events) >= 2:
                break
            await asyncio.sleep(.01)
        starts = [e for e in events if e["kind"] == "planner.started"]
        assert len(starts) == 2, scheduler.error
        daily = next(e for e in events if e["kind"] == "planner.wake" and e["payload"].get("review_type") == "daily")
        interrupt = next(e for e in events if e["kind"] == "planner.wake" and e["payload"].get("urgency") == "interrupt")
        received = [e for e in events if e["kind"] == "planner.received"]
        assert interrupt["seq"] in received[0]["payload"]["wake_seqs"]
        assert daily["seq"] not in received[0]["payload"]["wake_seqs"]
        assert daily["seq"] in starts[1]["payload"]["wake_seqs"]
    finally:
        release.set()
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
async def test_busy_planner_receives_focus_review_and_consumes_it(tmp_path):
    from ..agent.runtime.audit import AgentAuditRegistry
    from ..agent.runtime.loop import AgentRuntime
    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, settings
    from .test_capability_composition import Script, call

    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    entered, release = asyncio.Event(), asyncio.Event()
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            if not self.requests:
                entered.set()
                await release.wait()
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call("planner_board", {"action": "read"}),
                   call("planner_yield", {"handoff": "continue"}),
                   call("planner_yield", {"handoff": "done"})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    try:
        scheduler.resume(game)
        await asyncio.wait_for(entered.wait(), 5)
        game.game_tick = 1000
        await service.call("ingest_game_event", "c", "e", 1000, "conn", native(1, 1000, "letter", {"def": "NegativeEvent"}))
        game.game_tick = 3500
        await service.call("ingest_game_event", "c", "e", 3500, "conn", native(2, 3500, "heartbeat", {}))
        release.set()
        for _ in range(500):
            events = await service.call("events", "c")
            if any(e["kind"] == "planner.finished" for e in events):
                break
            await asyncio.sleep(.01)
        focus = next(e for e in events if e["kind"] == "planner.wake"
                     and str(e["payload"].get("work_id", "")).startswith("focus:"))
        received = [e for e in events if e["kind"] == "planner.received"]
        assert received and focus["seq"] in received[0]["payload"]["wake_seqs"], scheduler.error
        assert (await service.call("attention_state", "c", "e", 3500))["pending_focus"] is None
    finally:
        release.set()
        await scheduler.close()
        await service.close()


def test_capability_gate_and_failed_normalization_roll_back(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    fixture = Path(__file__).parent / "fixtures/colony-attention-v1/vectors.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))["valid"][0]["payload"]["data"]
    raw = native(1, 250, "attention_snapshot", data)
    journal.ingest_game_event("c", "e", 250, "conn", raw)
    assert not any(e["kind"] == "attention.signal" for e in journal.events("c"))
    invalid = native(2, 500, "attention_snapshot", {"alerts": {"status": "ok", "rows": []}})
    with pytest.raises(KeyError):
        journal.ingest_game_event("c", "e", 500, "conn", invalid, attention_available=True)
    assert journal.game_event_cursor("c", "e", "conn") == 1
    assert not any(e["kind"] == "game.event" and e["payload"]["event"]["cursor"] == 2 for e in journal.events("c"))
    journal.close()


def test_interrupted_run_requeues_explicit_work_and_signals(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    journal.ingest_game_event("c", "e", 60000, "conn", native(1, 60000, "letter", {"def": "ThreatBig"}))
    state = journal.attention_state("c", "e", 60000)
    due = state["pending_daily"]
    signals = state["pending_signal_seqs"]
    start = journal.record("c", "e", 60000, "planner", "planner.started", {"work_ids": due, "signal_seqs": signals, "wake_seqs": []})
    assert not journal.attention_state("c", "e", 60000)["pending_daily"]
    journal.finish_planner("c", "e", 60000, start, 0, {"status": "interrupted"}, {})
    restored = journal.attention_state("c", "e", 60000)
    assert restored["pending_daily"] == due
    assert restored["pending_signal_seqs"] == signals
    journal.close()


@pytest.mark.asyncio
async def test_delayed_snapshot_and_historical_inbox_wake_with_real_scheduler(tmp_path):
    from ..agent.runtime.audit import AgentAuditRegistry
    from ..agent.runtime.loop import AgentRuntime
    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, settings
    from .test_capability_composition import Script, call

    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=900)
    entered, release = asyncio.Event(), asyncio.Event()
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            if not self.requests:
                entered.set()
                await release.wait()
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call("planner_board", {"action": "read"}), call("planner_yield", {"handoff": "first"}),
                   call("planner_board", {"action": "read"}), call("planner_yield", {"handoff": "second"})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    try:
        scheduler.resume(game)
        await asyncio.wait_for(entered.wait(), 5)
        fixture = Path(__file__).parent / "fixtures/colony-attention-v1/vectors.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))["valid"][0]["payload"]["data"]
        data["sample_tick"] = 500
        raw = native(1, 500, "attention_snapshot", data)
        await service.call("ingest_game_event", "c", "e", 500, "conn", raw,
                           attention_available=True, observed_tick=900)
        state = await service.call("attention_state", "c", "e", 950)
        assert state["level"] == "urgent"
        events = await service.call("events", "c")
        native_row = next(e for e in events if e["kind"] == "game.event")
        assert native_row["game_tick"] == 900 and native_row["payload"]["event"] == raw
        assert native_row["payload"]["native_game_tick"] == 500
        assert native_row["payload"]["observed_game_tick"] == 900
        signals = [e for e in events if e["kind"] == "attention.signal"]
        assert all(e["game_tick"] == 900 and e["payload"]["native_game_tick"] == 500 for e in signals)
        release.set()
        for _ in range(500):
            events = await service.call("events", "c")
            if any(e["kind"] == "planner.finished" for e in events):
                break
            await asyncio.sleep(.01)
        prior_received = next(e for e in events if e["kind"] == "planner.received")
        assert prior_received["payload"]["wake_seqs"]
        await scheduler.wake(game, source="plugin", text="second wake")
        for _ in range(500):
            events = await service.call("events", "c")
            if sum(e["kind"] == "planner.finished" for e in events) == 2:
                break
            await asyncio.sleep(.01)
        assert sum(e["kind"] == "planner.finished" for e in events) == 2, scheduler.error
        received = [e for e in events if e["kind"] == "planner.received"]
        assert len(received) == 1
        assert not any(message.get("name") == "planner_inbox" for message in model.requests[3]["messages"])
    finally:
        release.set()
        await scheduler.close()
        await service.close()

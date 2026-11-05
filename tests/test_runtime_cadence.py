import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ..agent.runtime.audit import AgentAuditRegistry
from ..agent.runtime.colony_scheduler import ColonyScheduler
from ..agent.runtime.loop import AgentRuntime
from ..agent.runtime.frontdesk_service import FrontdeskService
from .consultation_setup import semantic_tool_provider
from .scripted_world import ScriptedGameWorld
from .test_agent import FakeLogger, settings
from .test_capability_composition import Script, call

CHOICES = {"goal": "survive", "constraints": "none", "goal_edited": False, "constraints_edited": False}


def test_native_pause_evidence_fixture_reaches_scheduler():
    fixture = Path(__file__).parent / "fixtures/colony-pause-state-v1/vectors.json"
    vectors = json.loads(fixture.read_text(encoding="utf-8"))
    state = vectors["observed"]
    binding = tuple(state[key] for key in ("connection_epoch", "colony_id", "load_epoch"))
    scheduler = ColonyScheduler(SimpleNamespace(), None, binding=lambda: binding)
    scheduler.observe_pause_state(state, binding, evidence_seq=1)
    assert scheduler.pause_state == state
    scheduler.observe_pause_state(vectors["unavailable"], binding, evidence_seq=2)
    assert scheduler.pause_state is None


@pytest.mark.asyncio
async def test_run_type_fixes_hard_budget_and_empty_summary_still_calls_model(tmp_path):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    model = Script([call("planner_yield", {"handoff": "ok"}) for _ in range(4)])
    used = []

    def factory(config):
        used.append((config.max_model_calls, config.run_budget_seconds))
        return model

    runtime = AgentRuntime(
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=factory,
        tools_provider=semantic_tool_provider(),
    )
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    scheduler = ColonyScheduler(service, settings(run_budget_seconds=900))
    scheduler.start()
    try:
        scheduler.resume(game)
        for index, kind in enumerate((None, "daily", "signal", "escalated")):
            if kind:
                await service.call(
                    "record",
                    "c",
                    "e",
                    0,
                    "plugin",
                    "planner.wake",
                    {"source": "game", "text": kind, "work_id": kind, "review_type": kind},
                )
                scheduler.resume(game)
            for _ in range(500):
                events = await service.call("events", "c")
                if sum(e["kind"] == "planner.finished" for e in events) >= index + 1:
                    break
                await asyncio.sleep(0.01)
            assert sum(e["kind"] == "planner.finished" for e in events) == index + 1, scheduler.error
        assert used == [(60, 900), (40, 900), (20, 900), (20, 900)]
        starts = [e for e in events if e["kind"] == "planner.started"]
        assert [e["payload"]["run_type"] for e in starts] == ["startup", "daily", "signal", "escalated"]
        assert starts[0]["payload"]["input"]["changes_since_last_run"] == []
        assert len(model.requests) == 4
    finally:
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
async def test_pause_watchdog_three_yields_then_blind_notification(tmp_path):
    world = ScriptedGameWorld()
    await world.push_policy({"source": "frontdesk", "pause_on_letter_defs": []})
    await world.set_paused(True, role="planner")
    game = SimpleNamespace(colony_id="scripted", load_epoch=world.load_epoch, game_tick=0, connection_epoch="scripted")
    model = Script([call("planner_yield", {"handoff": "still working"}) for _ in range(4)])
    runtime = AgentRuntime(
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        tools_provider=semantic_tool_provider(),
    )
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.call("record_autoplay_activation", game.colony_id, game.load_epoch, 0, CHOICES)
    pause_observation = None
    for native in await world.events():
        if native["cursor"] == 3:
            native["data"]["pause_state"]["monotonic"] = {"counter": 123}
            pause_observation = native["data"]["pause_state"]
        await service.call(
            "ingest_game_event", game.colony_id, game.load_epoch, native["game_tick"], "scripted", native
        )
    notices = []

    async def notify(bound):
        notices.append(bound)

    scheduler = ColonyScheduler(
        service, settings(), binding=lambda: ("scripted", "scripted", world.load_epoch), watchdog_notify=notify
    )
    scheduler._watchdog_interval = 0.02
    evidence = await service.call("game_event_at_cursor", "scripted", world.load_epoch, "scripted", 3)
    scheduler.observe_pause_state(
        pause_observation, ("scripted", "scripted", world.load_epoch), evidence_seq=evidence["seq"]
    )
    scheduler.start()
    try:
        scheduler.resume(game)
        for _ in range(600):
            events = await service.call("events", game.colony_id)
            if notices:
                break
            await asyncio.sleep(0.01)
        wake_rows = [e for e in events if e["kind"] == "pause.watchdog" and e["payload"]["action"] == "wake"]
        assert [e["payload"]["count"] for e in wake_rows] == [1, 2, 3]
        first_finish = next(e for e in events if e["kind"] == "planner.finished")
        assert evidence["seq"] < first_finish["seq"] and evidence["game_tick"] == first_finish["game_tick"]
        assert len(notices) == 1 and world.paused
        assert sum(e["kind"] == "planner.finished" for e in events) == 4
        starts = [e for e in events if e["kind"] == "planner.started"]
        projected = starts[1]["payload"]["input"]
        assert projected["pause_state"]["owner"] == "planner"
        assert projected["pause_state_source_seq"] == evidence["seq"]
        assert "monotonic" not in projected["pause_state"]
        assert '"counter": 123' not in str(model.requests[1]["messages"])
        await asyncio.sleep(0.05)
        assert len(notices) == 1
    finally:
        await scheduler.close()
        await service.close()
    assert scheduler._watchdog_worker is None


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", ["running", "user", "unknown"])
async def test_startup_pause_uses_native_owner_without_overwriting_user_or_unknown(tmp_path, monkeypatch, initial):
    from .test_plugin import make_plugin

    world = ScriptedGameWorld()
    if initial in {"user", "unknown"}:
        world.player_set_paused(True)
    if initial == "unknown":
        world.load(world.save())
    game = SimpleNamespace(colony_id="scripted", load_epoch=world.load_epoch, game_tick=0, connection_epoch="scripted")
    plugin, _ = make_plugin({"neko_rimworld": {}})
    plugin.data_path = lambda name: tmp_path / name
    plugin._agent_settings = settings()
    plugin._bridge = SimpleNamespace(session=game, connection_key="scripted")
    plugin._autoplay.budget = None
    plugin._autoplay.scheduler = None
    plugin._autoplay.waiters = {}
    notifications = []
    plugin.ctx = SimpleNamespace(push_message=lambda **kwargs: notifications.append(kwargs) or {"submitted": True})
    world.capabilities = world.capabilities | {"colony_write.ledger_snapshot.v1"}
    setup = SimpleNamespace(
        write_port=world,
        write_identity=lambda: ("scripted", "scripted", world.load_epoch),
        write_catalog={},
        ledger_query=object(),
        write_fields_query=object(),
        idle_query=world.idle_query,
    )

    async def provider():
        return setup

    plugin._load_agent_tools = provider
    monkeypatch.setattr(ColonyScheduler, "start", lambda self: None)
    monkeypatch.setattr(ColonyScheduler, "resume", lambda self, bound: None)
    scheduler = await plugin._autoplay.start(CHOICES)
    try:
        expected = "planner" if initial == "running" else initial
        assert world.paused and world.pause_state()["owner"] == expected
        assert scheduler.pause_state["owner"] == expected
        if initial == "running":
            await scheduler._watchdog_notify(game)
            assert notifications[0]["ai_behavior"] == "blind"
        events = await plugin._workspace_service.call("events", "scripted")
        result = next(e for e in events if e["kind"] == "autoplay.startup_pause" and e["payload"]["stage"] == "result")
        assert result["payload"]["pause_state"]["owner"] == expected
        initial_attention = result["payload"]["initial_attention"]
        assert initial_attention["scope"]["load_epoch"] == world.load_epoch
        assert initial_attention["attention_snapshot"]["kind"] == "attention_snapshot"
        assert initial_attention["sample"]["finished_tick"] == 0
        assert not any(event["kind"] == "game.event" for event in events)
    finally:
        await plugin._autoplay.stop()
        await plugin._workspace_service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner,force,window",
    [
        ("user", False, False),
        ("unknown", False, False),
        ("policy", False, False),
        ("planner", True, False),
        ("planner", False, True),
    ],
)
async def test_watchdog_ignores_user_unknown_policy_and_forced_pauses(tmp_path, owner, force, window):
    world = ScriptedGameWorld()
    await world.push_policy({"source": "frontdesk", "pause_on_letter_defs": []})
    native = (await world.events())[0]
    state = {
        **world.pause_state(),
        "owner": owner,
        "paused": True,
        "speed_paused": True,
        "force_paused": force,
        "windows_force_paused": window,
    }
    native["data"]["pause_state"] = state
    game = SimpleNamespace(colony_id="scripted", load_epoch=world.load_epoch, game_tick=0, connection_epoch="scripted")
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    await service.call("ingest_game_event", "scripted", world.load_epoch, 0, "scripted", native)
    notices = []

    async def notify(bound):
        notices.append(bound)

    scheduler = ColonyScheduler(
        service, settings(), binding=lambda: ("scripted", "scripted", world.load_epoch), watchdog_notify=notify
    )
    scheduler._last_yield = ("scripted", world.load_epoch, 0)
    scheduler.observe_pause_state(state, ("scripted", "scripted", world.load_epoch))
    scheduler.start()
    try:
        await asyncio.sleep(0.05)
        events = await service.call("events", "scripted")
        assert not notices and not any(e["kind"] == "pause.watchdog" for e in events)
    finally:
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
async def test_message_frontdesk_requires_declared_purpose(tmp_path):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    model = Script(
        [
            call("message_frontdesk", {"text": "routine"}),
            call("message_frontdesk", {"text": "choose", "purpose": "user_decision"}),
            call("planner_yield", {"handoff": "done"}),
        ]
    )
    runtime = AgentRuntime(
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        tools_provider=semantic_tool_provider(),
    )
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    sent = []

    async def deliver(game, text, *, source, purpose):
        sent.append((text, source, purpose))
        return 100

    scheduler.wake_frontdesk = deliver
    scheduler.start()
    try:
        await scheduler.wake(game, source="plugin", text="start")
        for _ in range(500):
            events = await service.call("events", "c")
            if any(e["kind"] == "planner.finished" for e in events):
                break
            await asyncio.sleep(0.01)
        assert sent == [("choose", "planner", "user_decision")]
        schema = next(t for t in model.requests[0]["tools"] if t["function"]["name"] == "message_frontdesk")
        assert set(schema["function"]["parameters"]["required"]) == {"text", "purpose"}
    finally:
        await scheduler.close()
        await service.close()


def test_autoplay_frontdesk_only_choice_and_force_window_are_delivered(tmp_path):
    from ..workspace.journal import Journal

    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)

    def event(cursor, tick, kind, data):
        return {
            "connection_epoch": "x",
            "colony_id": "c",
            "load_epoch": "e",
            "game_tick": tick,
            "snapshot_sequence": cursor,
            "cursor": cursor,
            "kind": kind,
            "data": {"kind": kind, **data},
        }

    journal.ingest_game_event("c", "e", 1, "x", event(1, 1, "letter", {"def": "ThreatBig", "is_choice_letter": False}))
    assert not any(e["kind"] == "frontdesk.wake" for e in journal.events("c"))
    journal.ingest_game_event(
        "c", "e", 2, "x", event(2, 2, "letter", {"def": "NeutralEvent", "is_choice_letter": True})
    )
    journal.ingest_game_event(
        "c", "e", 3, "x", event(3, 3, "pause_changed", {"source": "window", "windows_force_paused": True})
    )
    assert sum(e["kind"] == "frontdesk.wake" for e in journal.events("c")) == 2
    journal.close()


@pytest.mark.asyncio
async def test_run_budget_respects_lower_user_limits(tmp_path):
    from dataclasses import replace

    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    model = Script([call("planner_yield", {"handoff": "ok"})])
    used = []

    def factory(config):
        used.append((config.max_model_calls, config.run_budget_seconds))
        return model

    runtime = AgentRuntime(
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=factory,
        tools_provider=semantic_tool_provider(),
    )
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    scheduler = ColonyScheduler(service, replace(settings(run_budget_seconds=30), max_model_calls=5))
    scheduler.start()
    try:
        scheduler.resume(game)
        for _ in range(500):
            if used:
                break
            await asyncio.sleep(0.01)
        assert used == [(5, 30)]
    finally:
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
async def test_startup_unknown_pause_read_fails_before_writing(tmp_path, monkeypatch):
    from .test_plugin import make_plugin

    world = ScriptedGameWorld()
    world.capabilities = world.capabilities | {"colony_write.ledger_snapshot.v1"}
    game = SimpleNamespace(colony_id="scripted", load_epoch=world.load_epoch, game_tick=0, connection_epoch="scripted")
    plugin, _ = make_plugin({"neko_rimworld": {}})
    plugin.data_path = lambda name: tmp_path / name
    plugin._agent_settings = settings()
    plugin._bridge = SimpleNamespace(session=game, connection_key="scripted")
    plugin._autoplay.budget = None
    plugin._autoplay.scheduler = None
    calls = []

    async def unreadable(operation, arguments):
        raise RuntimeError("unavailable")

    async def pause(paused, *, role):
        calls.append((paused, role))
        return {"paused": paused, "role": role}

    monkeypatch.setattr(world, "set_paused", pause)
    setup = SimpleNamespace(
        write_port=world,
        write_identity=lambda: ("scripted", "scripted", world.load_epoch),
        write_catalog={},
        ledger_query=object(),
        write_fields_query=object(),
        idle_query=unreadable,
    )

    async def provider():
        return setup

    plugin._load_agent_tools = provider
    with pytest.raises(ValueError, match="Startup pause state unavailable"):
        await plugin._autoplay.start(CHOICES)
    try:
        events = await plugin._workspace_service.call("events", "scripted")
        assert calls == [] and not any(e["kind"] == "autoplay.activation" for e in events)
        assert any(
            e["payload"].get("reason") == "native_read_RuntimeError"
            for e in events
            if e["kind"] == "autoplay.startup_pause"
        )
    finally:
        await plugin._workspace_service.close()


@pytest.mark.asyncio
async def test_startup_preserves_forced_native_pause_without_sending_write(tmp_path, monkeypatch):
    from .test_plugin import make_plugin

    world = ScriptedGameWorld()
    world.capabilities = world.capabilities | {"colony_write.ledger_snapshot.v1"}
    game = SimpleNamespace(colony_id="scripted", load_epoch=world.load_epoch, game_tick=0, connection_epoch="scripted")
    plugin, _ = make_plugin({"neko_rimworld": {}})
    plugin.data_path = lambda name: tmp_path / name
    plugin._agent_settings = settings()
    plugin._bridge = SimpleNamespace(session=game, connection_key="scripted")
    plugin._autoplay.budget = None
    plugin._autoplay.scheduler = None
    plugin._autoplay.waiters = {}
    calls = []
    state = {
        **world.pause_state(),
        "paused": True,
        "speed_paused": False,
        "force_paused": True,
        "windows_force_paused": True,
        "owner": "unknown",
    }

    async def pause(paused, *, role):
        calls.append((paused, role))
        return {"paused": paused, "role": role}

    async def query(operation, arguments):
        return {
            "response": {
                "connection_epoch": "scripted",
                "colony_id": "scripted",
                "load_epoch": world.load_epoch,
                "payload": {"data": {"clock": {"pause_state": state}}},
            }
        }

    monkeypatch.setattr(world, "set_paused", pause)
    setup = SimpleNamespace(
        write_port=world,
        write_identity=lambda: ("scripted", "scripted", world.load_epoch),
        write_catalog={},
        ledger_query=object(),
        write_fields_query=object(),
        idle_query=query,
    )

    async def provider():
        return setup

    plugin._load_agent_tools = provider
    monkeypatch.setattr(ColonyScheduler, "start", lambda self: None)
    monkeypatch.setattr(ColonyScheduler, "resume", lambda self, bound: None)
    scheduler = await plugin._autoplay.start(CHOICES)
    try:
        assert calls == [] and scheduler.pause_state == state
    finally:
        await plugin._autoplay.stop()
        await plugin._workspace_service.close()


@pytest.mark.asyncio
async def test_daily_wins_mixed_due_work_and_keeps_all_reasons(tmp_path):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    model = Script([call("planner_yield", {"handoff": "first"}), call("planner_yield", {"handoff": "second"})])
    used = []

    def factory(config):
        used.append(config.max_model_calls)
        return model

    runtime = AgentRuntime(
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=factory,
        tools_provider=semantic_tool_provider(),
    )
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    scheduler = ColonyScheduler(service, settings(run_budget_seconds=900))
    scheduler.start()
    try:
        scheduler.resume(game)
        for _ in range(500):
            events = await service.call("events", "c")
            if any(e["kind"] == "planner.finished" for e in events):
                break
            await asyncio.sleep(0.01)
        async with scheduler.write_lock:
            daily = await service.call(
                "record",
                "c",
                "e",
                0,
                "plugin",
                "planner.wake",
                {"source": "game", "text": "daily", "work_id": "daily-test", "review_type": "daily"},
            )
            signal = await service.call(
                "record",
                "c",
                "e",
                0,
                "plugin",
                "planner.wake",
                {
                    "source": "game",
                    "text": "urgent",
                    "work_id": "signal-test",
                    "review_type": "signal",
                    "urgency": "interrupt",
                },
            )
            scheduler.resume(game)
        for _ in range(500):
            events = await service.call("events", "c")
            if sum(e["kind"] == "planner.finished" for e in events) == 2:
                break
            await asyncio.sleep(0.01)
        start = [e for e in events if e["kind"] == "planner.started"][-1]["payload"]
        assert start["run_type"] == "daily" and set(start["wake_seqs"]) == {daily, signal}
        assert {e["seq"] for e in start["input"]["wake_reasons"]} == {daily, signal}
        assert used == [60, 40]
    finally:
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
async def test_startup_model_input_contains_recorded_fresh_attention(tmp_path):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    model = Script([call("planner_yield", {"handoff": "ready"})])
    runtime = AgentRuntime(
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        tools_provider=semantic_tool_provider(),
    )
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    evidence = {
        "scope": {"connection_epoch": "x", "colony_id": "c", "load_epoch": "e"},
        "sample": {"finished_tick": 0},
        "attention_snapshot": {"kind": "attention_snapshot", "alerts": {"rows": [{"id": "native-alert"}]}},
    }
    await service.call(
        "record",
        "c",
        "e",
        0,
        "planner",
        "autoplay.startup_pause",
        {
            "stage": "result",
            "pause_state": {"status": "unavailable"},
            "attention_available": True,
            "initial_attention": evidence,
        },
    )
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    try:
        scheduler.resume(game)
        for _ in range(500):
            events = await service.call("events", "c")
            if any(e["kind"] == "planner.finished" for e in events):
                break
            await asyncio.sleep(0.01)
        start = next(e for e in events if e["kind"] == "planner.started")
        assert start["payload"]["input"]["initial_attention"] == evidence
        assert start["payload"]["input"]["attention_available"] is True
        assert start["payload"]["input"]["pause_state"] == {"status": "unavailable"}
        assert start["payload"]["input"]["pause_state_source_seq"] is None
        assert "native-alert" in str(model.requests[0]["messages"])
        assert not any(e["kind"] == "game.event" for e in events)
    finally:
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "reconnect", "later_sample"])
async def test_startup_pause_projection_uses_bound_source_across_await(tmp_path, change):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0, connection_epoch="x")
    identity = ["x", "c", "e"]
    model = Script([call("planner_yield", {"handoff": "ready"})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    state = {"status": "observed", "connection_epoch": "x", "colony_id": "c", "load_epoch": "e",
             "sampled_tick": 0, "pause_revision": 1, "pause_transition_revision": 1,
             "paused": True, "speed_paused": True, "force_paused": False, "windows_force_paused": False,
             "owner": "planner", "monotonic": {"counter": 123}}
    source = await service.call("record", "c", "e", 0, "planner", "autoplay.startup_pause",
                                {"stage": "result", "pause_state": state})
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    scheduler = ColonyScheduler(service, settings(), binding=lambda: tuple(identity))
    scheduler.observe_pause_state(state, tuple(identity), evidence_seq=source)
    original = service.call

    async def call_with_reconnect(method, *args, **kwargs):
        result = await original(method, *args, **kwargs)
        if method == "event" and args == ("c", source):
            if change == "reconnect":
                identity[0] = "new-connection"
            elif change == "later_sample":
                later = {**state, "sampled_tick": 1}
                scheduler.observe_pause_state(later, tuple(identity), evidence_seq=source + 2)
        return result

    service.call = call_with_reconnect
    scheduler.start()
    try:
        scheduler.resume(game)
        for _ in range(500):
            events = await original("events", "c")
            if any(e["kind"] == "planner.finished" for e in events) or scheduler.error:
                break
            await asyncio.sleep(0.01)
        starts = [e for e in events if e["kind"] == "planner.started"]
        if change == "reconnect":
            assert not starts and not model.requests
        else:
            projection = starts[0]["payload"]["input"]
            assert projection["pause_state"]["owner"] == "planner"
            assert projection["pause_state_source_seq"] == source
            assert "monotonic" not in projection["pause_state"]
    finally:
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["stale_scope", "unrecorded_sample"])
async def test_planner_input_rejects_pause_without_matching_scope_and_source(tmp_path, case):
    world = ScriptedGameWorld()
    await world.push_policy({"source": "frontdesk", "pause_on_letter_defs": []})
    await world.set_paused(True, role="planner")
    game = SimpleNamespace(colony_id="scripted", load_epoch=world.load_epoch, game_tick=0,
                           connection_epoch="scripted")
    model = Script([call("planner_yield", {"handoff": "ready"})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.call("record_autoplay_activation", game.colony_id, game.load_epoch, 0, CHOICES)
    for native in await world.events():
        await service.call("ingest_game_event", game.colony_id, game.load_epoch, native["game_tick"], "scripted", native)
    evidence = await service.call("game_event_at_cursor", "scripted", world.load_epoch, "scripted", 3)
    scheduler = ColonyScheduler(service, settings(), binding=lambda: ("scripted", "scripted", world.load_epoch))
    state = world.pause_state()
    if case == "unrecorded_sample":
        state["sampled_tick"] += 1
    scheduler.observe_pause_state(state, ("scripted", "scripted", world.load_epoch), evidence_seq=evidence["seq"])
    if case == "stale_scope":
        scheduler.pause_state["connection_epoch"] = "old"
    scheduler.start()
    try:
        scheduler.resume(game)
        for _ in range(500):
            events = await service.call("events", game.colony_id)
            if any(e["kind"] == "planner.finished" for e in events):
                break
            await asyncio.sleep(0.01)
        start = next(e for e in events if e["kind"] == "planner.started")
        assert start["payload"]["input"]["pause_state"] == {"status": "unavailable"}
        assert start["payload"]["input"]["pause_state_source_seq"] is None
    finally:
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
async def test_watchdog_discards_old_scope_on_same_epoch_reconnect(tmp_path):
    world = ScriptedGameWorld()
    await world.push_policy({"source": "frontdesk", "pause_on_letter_defs": []})
    await world.set_paused(True, role="planner")
    game = SimpleNamespace(colony_id="scripted", load_epoch=world.load_epoch, game_tick=0, connection_epoch="scripted")
    binding = {"connection": "scripted"}

    def identity():
        return (binding["connection"], "scripted", world.load_epoch)

    class Port:
        capabilities = {"colony_events.v1", "colony_pause.v1"}

        async def events(self, since=0):
            return []

        async def push_policy(self, policy):
            return policy

    async def provider():
        return SimpleNamespace(write_port=Port(), write_identity=identity)

    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    for native in await world.events():
        await service.call("ingest_game_event", game.colony_id, game.load_epoch, 0, "scripted", native)
    notices = []

    async def notify(bound):
        notices.append(bound)

    scheduler = ColonyScheduler(service, settings(), event_provider=provider, binding=identity, watchdog_notify=notify)
    evidence = await service.call("game_event_at_cursor", "scripted", world.load_epoch, "scripted", 3)
    scheduler._last_yield = ("scripted", game.colony_id, game.load_epoch, evidence["seq"] + 1)
    scheduler.observe_pause_state(world.pause_state(), identity(), evidence_seq=evidence["seq"])
    scheduler._pause_restored = True
    scheduler._pause_count = 1
    scheduler._pause_last_wake = asyncio.get_running_loop().time()
    scheduler._watchdog_interval = 0.05
    scheduler.start()
    try:
        await asyncio.sleep(0.01)
        binding["connection"] = "new-connection"
        game.connection_epoch = "new-connection"
        await asyncio.sleep(1.1)
        rows = await service.call("events", "scripted")
        assert scheduler.pause_state is None
        assert not notices and not any(e["kind"] == "pause.watchdog" for e in rows)
    finally:
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
async def test_frontdesk_model_tools_limit_planner_messages_to_user_intent_and_risk(tmp_path):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    model = Script([call("update_only", {})])
    runtime = AgentRuntime(
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        tools_provider=semantic_tool_provider(),
    )
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    try:
        await scheduler.wake_frontdesk(game, "鐢ㄦ埛瑕佹眰淇濈暀椋熺墿", source="user")
        await asyncio.wait_for(scheduler.next_frontdesk_result(), 5)
        request = model.requests[0]
        tool = next(item["function"] for item in request["tools"] if item["function"]["name"] == "message_planner")
        assert "intent, constraint or decision, or an urgent risk" in tool["description"]
        assert "material new information" not in tool["description"]
        assert "鐢ㄦ埛瑕佹眰淇濈暀椋熺墿" in str(request["messages"])
    finally:
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
async def test_same_tick_pause_after_yield_waits_for_wall_clock_then_wakes(tmp_path):
    world = ScriptedGameWorld()
    await world.push_policy({"source": "frontdesk", "pause_on_letter_defs": []})
    game = SimpleNamespace(colony_id="scripted", load_epoch=world.load_epoch, game_tick=0, connection_epoch="scripted")
    model = Script(
        [
            call("planner_yield", {"handoff": "first"}),
            call("planner_yield", {"handoff": "second"}),
            call("planner_yield", {"handoff": "watchdog"}),
        ]
    )
    runtime = AgentRuntime(
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        tools_provider=semantic_tool_provider(),
    )
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.call("record_autoplay_activation", "scripted", world.load_epoch, 0, CHOICES)
    for native in await world.events():
        await service.call("ingest_game_event", "scripted", world.load_epoch, 0, "scripted", native)
    notices = []

    async def notify(bound):
        notices.append(bound)
        return {"submitted": True}

    scheduler = ColonyScheduler(
        service, settings(), binding=lambda: ("scripted", "scripted", world.load_epoch), watchdog_notify=notify
    )
    scheduler._watchdog_interval = 0.2
    scheduler.start()
    try:
        scheduler.resume(game)
        for _ in range(500):
            events = await service.call("events", "scripted")
            if any(e["kind"] == "planner.finished" for e in events):
                break
            await asyncio.sleep(0.01)
        first_finish = next(e for e in events if e["kind"] == "planner.finished")
        await world.set_paused(True, role="frontdesk")
        pause_event = (await world.events())[-1]
        await service.call("ingest_game_event", "scripted", world.load_epoch, 0, "scripted", pause_event)
        evidence = await service.call(
            "game_event_at_cursor", "scripted", world.load_epoch, "scripted", pause_event["cursor"]
        )
        assert evidence["game_tick"] == first_finish["game_tick"] and evidence["seq"] > first_finish["seq"]
        scheduler.observe_pause_state(
            pause_event["data"]["pause_state"], ("scripted", "scripted", world.load_epoch), evidence_seq=evidence["seq"]
        )
        await asyncio.sleep(0.08)
        assert not any(e["kind"] == "pause.watchdog" for e in await service.call("events", "scripted"))
        for _ in range(500):
            events = await service.call("events", "scripted")
            if (
                any(e["kind"] == "pause.watchdog" and e["payload"]["action"] == "wake" for e in events)
                and sum(e["kind"] == "planner.finished" for e in events) >= 2
            ):
                break
            await asyncio.sleep(0.01)
        wake = next(e for e in events if e["kind"] == "pause.watchdog" and e["payload"]["action"] == "wake")
        assert wake["payload"]["count"] == 1
        assert sum(e["kind"] == "planner.finished" for e in events) >= 2
    finally:
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delivery,expected",
    [("submitted", "submitted"), ("rejected", "rejected"), ("error", "unknown"), ("cancel", "unknown")],
)
async def test_watchdog_notification_records_submission_outcome_once(tmp_path, delivery, expected):
    world = ScriptedGameWorld()
    await world.push_policy({"source": "frontdesk", "pause_on_letter_defs": []})
    await world.set_paused(True, role="planner")
    game = SimpleNamespace(colony_id="scripted", load_epoch=world.load_epoch, game_tick=0, connection_epoch="scripted")
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    for native in await world.events():
        await service.call("ingest_game_event", "scripted", world.load_epoch, 0, "scripted", native)
    evidence = await service.call("game_event_at_cursor", "scripted", world.load_epoch, "scripted", 3)
    calls = []

    async def notify(bound):
        calls.append(bound)
        if delivery == "error":
            raise RuntimeError("transport")
        if delivery == "cancel":
            raise asyncio.CancelledError()
        return {"submitted": delivery == "submitted"}

    scheduler = ColonyScheduler(
        service, settings(), binding=lambda: ("scripted", "scripted", world.load_epoch), watchdog_notify=notify
    )
    scheduler._last_yield = ("scripted", "scripted", world.load_epoch, evidence["seq"] + 1)
    scheduler.observe_pause_state(
        world.pause_state(), ("scripted", "scripted", world.load_epoch), evidence_seq=evidence["seq"]
    )
    scheduler._pause_restored = True
    scheduler._pause_count = 3
    scheduler.start()
    try:
        for _ in range(500):
            events = await service.call("events", "scripted")
            if any(e["kind"] == "pause.watchdog" and e["payload"]["action"] == "notify_result" for e in events):
                break
            await asyncio.sleep(0.01)
        rows = [e["payload"] for e in events if e["kind"] == "pause.watchdog"]
        assert [row["action"] for row in rows] == ["notify_attempt", "notify_result"]
        assert rows[-1]["outcome"] == expected
        await asyncio.sleep(0.05)
        assert len(calls) == 1
    finally:
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
async def test_pause_after_yield_gets_wall_clock_watchdog_when_still_idle(tmp_path):
    world = ScriptedGameWorld()
    await world.push_policy({"source": "frontdesk", "pause_on_letter_defs": []})
    game = SimpleNamespace(colony_id="scripted", load_epoch=world.load_epoch, game_tick=0, connection_epoch="scripted")
    model = Script([call("planner_yield", {"handoff": "observed pause"})])
    runtime = AgentRuntime(
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        tools_provider=semantic_tool_provider(),
    )
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    start = await service.call(
        "record", "scripted", world.load_epoch, 0, "planner", "planner.started", {"wake_seqs": []}
    )
    finish = await service.call(
        "finish_planner",
        "scripted",
        world.load_epoch,
        0,
        start,
        0,
        {"status": "ok", "terminal": {"kind": "yield", "handoff": "done"}},
        {},
    )
    await world.set_paused(True, role="frontdesk")
    for native in await world.events():
        await service.call("ingest_game_event", "scripted", world.load_epoch, 0, "scripted", native)
    evidence = await service.call("game_event_at_cursor", "scripted", world.load_epoch, "scripted", 3)
    assert finish < evidence["seq"]
    notices = []

    async def notify(bound):
        notices.append(bound)
        return {"submitted": True}

    scheduler = ColonyScheduler(
        service, settings(), binding=lambda: ("scripted", "scripted", world.load_epoch), watchdog_notify=notify
    )
    scheduler._last_yield = ("scripted", "scripted", world.load_epoch, finish)
    scheduler.observe_pause_state(
        world.pause_state(), ("scripted", "scripted", world.load_epoch), evidence_seq=evidence["seq"]
    )
    scheduler._watchdog_interval = 0.08
    scheduler.start()
    try:
        await asyncio.sleep(0.02)
        assert not any(e["kind"] == "pause.watchdog" for e in await service.call("events", "scripted"))
        for _ in range(100):
            events = await service.call("events", "scripted")
            if any(e["kind"] == "pause.watchdog" and e["payload"]["action"] == "wake" for e in events):
                break
            await asyncio.sleep(0.01)
        assert any(e["kind"] == "pause.watchdog" and e["payload"]["action"] == "wake" for e in events)
        assert not notices
    finally:
        await scheduler.close()
        await service.close()


def test_journal_rejects_old_pause_episode_after_new_connection_snapshot(tmp_path):
    from ..workspace.journal import Journal

    journal = Journal(tmp_path / "events.sqlite")

    def snapshot(connection, revision):
        state = {
            "status": "observed",
            "connection_epoch": connection,
            "colony_id": "c",
            "load_epoch": "e",
            "sampled_tick": 10,
            "owner": "planner",
            "pause_revision": revision,
            "pause_transition_revision": revision,
            "paused": True,
            "speed_paused": True,
            "force_paused": False,
            "windows_force_paused": False,
        }
        return {
            "connection_epoch": connection,
            "colony_id": "c",
            "load_epoch": "e",
            "game_tick": 10,
            "snapshot_sequence": 1,
            "cursor": 1,
            "kind": "session_snapshot",
            "data": {"kind": "session_snapshot", "pause_state": state},
        }

    journal.ingest_game_event("c", "e", 10, "old", snapshot("old", 2))
    old_episode = ("old", "c", "e", 2, 2)
    journal.ingest_game_event("c", "e", 10, "new", snapshot("new", 3))
    assert journal.pause_watchdog_wake("c", "e", 10, old_episode, 1) is None
    assert journal.pause_watchdog_notify("c", "e", 10, old_episode) is False
    assert not any(event["kind"] in {"planner.wake", "pause.watchdog"} for event in journal.events("c"))
    journal.close()


@pytest.mark.asyncio
async def test_daily_input_covers_due_day_even_after_intermediate_signal_run(tmp_path):
    from .test_attention_journal import native

    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    model = Script(
        [
            call("planner_yield", {"handoff": "startup"}),
            call("planner_yield", {"handoff": "signal"}),
            call("planner_yield", {"handoff": "daily"}),
        ]
    )
    runtime = AgentRuntime(
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        tools_provider=semantic_tool_provider(),
    )
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    try:
        scheduler.resume(game)
        for _ in range(500):
            events = await service.call("events", "c")
            if sum(e["kind"] == "planner.finished" for e in events) == 1:
                break
            await asyncio.sleep(0.01)
        game.game_tick = 10
        await service.call("ingest_game_event", "c", "e", 10, "conn", native(1, 10, "letter", {"def": "NeutralEvent"}))
        game.game_tick = 100
        changed = {
            "category": "alerts",
            "source_id": "alert-1",
            "edge": "appeared",
            "map_id": "map",
            "current": {
                "id": "alert-1",
                "runtime_type": "RimWorld.Alert_ColonistNeedsRescuing",
                "priority": "Critical",
                "targets_complete": True,
                "targets": [],
            },
            "previous": None,
        }
        await service.call(
            "ingest_game_event",
            "c",
            "e",
            100,
            "conn",
            native(2, 100, "attention_changed", changed),
            attention_available=True,
        )
        events = await service.call("events", "c")
        early_signal = next(
            e for e in events if e["kind"] == "attention.signal" and e["payload"].get("def") == "NeutralEvent"
        )
        early_change = next(e for e in events if e["kind"] == "cadence.changed" and e["game_tick"] == 100)
        scheduler.resume(game)
        for _ in range(500):
            events = await service.call("events", "c")
            if sum(e["kind"] == "planner.finished" for e in events) == 2:
                break
            await asyncio.sleep(0.01)
        signal_start = [e for e in events if e["kind"] == "planner.started"][-1]
        assert signal_start["payload"]["run_type"] == "signal"
        assert early_signal["seq"] in signal_start["payload"]["signal_seqs"]
        game.game_tick = 60000
        await service.call("ingest_game_event", "c", "e", 60000, "conn", native(3, 60000, "heartbeat", {}))
        scheduler.resume(game)
        for _ in range(500):
            events = await service.call("events", "c")
            if sum(e["kind"] == "planner.finished" for e in events) == 3:
                break
            await asyncio.sleep(0.01)
        daily = [e for e in events if e["kind"] == "planner.started"][-1]["payload"]
        assert daily["run_type"] == "daily"
        assert early_signal["seq"] in {e["seq"] for e in daily["input"]["daily_signals"]}
        assert early_change["seq"] in {e["seq"] for e in daily["input"]["cadence_history"]}
        assert early_signal["seq"] not in {e["seq"] for e in daily["input"]["changes_since_last_run"]}
        assert "NeutralEvent" in str(model.requests[-1]["messages"])
    finally:
        await scheduler.close()
        await service.close()


@pytest.mark.asyncio
async def test_startup_and_later_signal_keep_recent_closed_and_load_review_marks(tmp_path):
    from .test_attention_journal import entry

    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    model = Script([call("planner_yield", {"handoff": "startup"}), call("planner_yield", {"handoff": "signal"})])
    runtime = AgentRuntime(
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        tools_provider=semantic_tool_provider(),
    )
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    closed = entry("strategy", "old-plan")
    closed.update(status="closed", body="Do not repeat this abandoned plan")
    await service.call("submit", "c", "e", 0, "planner", closed)
    await service.call("record_load", "c", "e", 0)
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    try:
        scheduler.resume(game)
        for _ in range(500):
            events = await service.call("events", "c")
            if sum(event["kind"] == "planner.finished" for event in events) == 1:
                break
            await asyncio.sleep(0.01)
        await scheduler.wake(game, source="plugin", text="new signal")
        for _ in range(500):
            events = await service.call("events", "c")
            if sum(event["kind"] == "planner.finished" for event in events) == 2:
                break
            await asyncio.sleep(0.01)
        starts = [event["payload"]["input"] for event in events if event["kind"] == "planner.started"]
        assert [context["run_type"] for context in starts] == ["startup", "signal"]
        for context in starts:
            assert [entry["entry_id"] for entry in context["recent_closed"]] == ["old-plan"]
            assert context["review_marks"] == {"old-plan": ["load"]}
            assert (
                context["activation_meaning"]
                == "Latest user-selected autoplay goal and constraints; persistent across loads, not a current world observation."
            )
        assert not any(event["kind"] == "board.revision" for event in starts[1]["changes_since_last_run"])
        assert all("Do not repeat this abandoned plan" in str(request["messages"]) for request in model.requests)
        assert all("review_marks" in str(request["messages"]) for request in model.requests)
    finally:
        await scheduler.close()
        await service.close()

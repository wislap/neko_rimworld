import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ..entry import NekoRimworldPlugin
from ..game.protocol import ValidationError, decode_envelope
from ..game.scripted_world import ScriptedGameWorld
from ..tool_contracts import ToolExecutionError


@pytest.mark.asyncio
async def test_policy_pause_is_true_only_and_preserves_pause():
    world = ScriptedGameWorld()
    assert "colony_pause.policy.v1" in world.capabilities
    assert await world.set_paused(True, role="policy") == {"paused": True, "role": "policy"}
    with pytest.raises(ToolExecutionError):
        await world.set_paused(False, role="policy")
    assert world.paused


def test_native_policy_vectors_parse():
    vectors = json.loads((Path(__file__).parent / "fixtures/colony-pause-policy-v1/vectors.json").read_text(encoding="utf8"))
    for item in vectors["valid"]:
        decode_envelope(json.dumps(item).encode())
    for item in vectors["invalid"]:
        with pytest.raises(ValidationError):
            decode_envelope(json.dumps(item).encode())


@pytest.mark.asyncio
async def test_host_submission_is_acknowledged_once_without_claiming_consumption():
    class Service:
        def __init__(self):
            self.events = []
        async def call(self, method, *args):
            self.events.append((method, args))
            if method == "events_by_kind":
                return []
    class Scheduler:
        def __init__(self):
            self.service = Service()
            self._pending_frontdesk = {"colony_id": "c", "load_epoch": "e", "game_tick": 1}
            self.queue = asyncio.Queue()
            self.acks = []
        async def next_frontdesk_result(self):
            return await self.queue.get()
        async def ack_frontdesk_result(self, ident):
            self.acks.append(ident)
    class Context:
        def __init__(self):
            self.messages = []
        def push_message(self, **kwargs):
            self.messages.append(kwargs)
            return {"submitted": True}
    plugin = object.__new__(NekoRimworldPlugin)
    plugin.ctx = Context()
    plugin._autoplay_waiters = {}
    scheduler = Scheduler()
    task = asyncio.create_task(plugin._deliver_autoplay(scheduler))
    await scheduler.queue.put({"delivery_id": 1, "result": {"status": "ok", "answer": "hello", "workspace": {"turn_id": "4"}, "terminal": {"kind": "reply"}}})
    for _ in range(20):
        if scheduler.acks:
            break
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert scheduler.acks == [1]
    assert plugin.ctx.messages[0]["ai_behavior"] == "blind"
    submission = next(args[-1] for method, args in scheduler.service.events if method == "record" and args[4] == "autoplay.host_submission_result")
    assert submission["outcome"] == "submitted"
    assert "host may drop" in submission["meaning"]

@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected_pause", [("model_transport", True), ("agent_cumulative_budget", False), ("workspace_unavailable", False)])
async def test_failure_pauses_only_endpoint_and_uses_blind_notification(code, expected_pause):
    class Service:
        def __init__(self): self.events = []
        async def call(self, method, *args): self.events.append(args)
    class Port:
        def __init__(self): self.calls = []
        async def set_paused(self, value, *, role): self.calls.append((value, role))
    class Context:
        def __init__(self): self.messages = []
        def push_message(self, **kwargs):
            self.messages.append(kwargs)
            return {"submitted": True}
    plugin = object.__new__(NekoRimworldPlugin)
    plugin.ctx = Context()
    plugin._autoplay_waiters = {}
    service, port = Service(), Port()
    plugin._colony_scheduler = SimpleNamespace(halted=False, service=service, budget=None)
    plugin._bridge = SimpleNamespace(session=SimpleNamespace(colony_id="c", load_epoch="e", game_tick=3))
    async def setup(): return SimpleNamespace(write_port=port, write_identity=lambda: ("connection", "c", "e"))
    plugin._load_agent_tools = setup
    await plugin._autoplay_failure("planner", {"status": "failed", "audit": {"error_code": code}})
    assert plugin._colony_scheduler.halted
    assert bool(port.calls) is expected_pause
    if expected_pause:
        assert port.calls == [(True, "policy")]
        assert plugin.ctx.messages[0]["ai_behavior"] == "blind"
    else:
        assert plugin.ctx.messages[0]["ai_behavior"] == "blind"
        assert "Game pause state is unknown" in plugin.ctx.messages[0]["parts"][0]["text"]
        assert "endpoint" not in plugin.ctx.messages[0]["parts"][0]["text"]

@pytest.mark.asyncio
async def test_frontdesk_reuses_same_inflight_host_request(tmp_path):
    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..workspace.runtime import FrontdeskService

    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    scheduler = ColonyScheduler(service, None)
    scheduler._worker = asyncio.create_task(asyncio.Event().wait())
    first = await scheduler.wake_frontdesk(game, "original words", request_id="host-id")
    again = await scheduler.wake_frontdesk(game, "original words", request_id="host-id")
    different = await scheduler.wake_frontdesk(game, "different words", request_id="host-id")
    events = await service.call("events", "colony")
    assert first == again and different != first
    assert len([e for e in events if e["kind"] == "user.utterance"]) == 2
    scheduler._worker.cancel()
    await asyncio.gather(scheduler._worker, return_exceptions=True)
    await service.close()

@pytest.mark.asyncio
async def test_recorded_host_attempt_is_not_replayed_after_restart():
    class Service:
        async def call(self, method, *args):
            if method == "events_by_kind":
                return [{"kind": "autoplay.host_submission", "payload": {"delivery_id": 1}}]
    class Scheduler:
        def __init__(self):
            self.service = Service()
            self._pending_frontdesk = {"colony_id": "c", "load_epoch": "e", "game_tick": 1}
            self.queue = asyncio.Queue()
            self.acks = []
        async def next_frontdesk_result(self): return await self.queue.get()
        async def ack_frontdesk_result(self, ident): self.acks.append(ident)
    class Context:
        def push_message(self, **kwargs): raise AssertionError("already attempted")
    plugin = object.__new__(NekoRimworldPlugin)
    plugin.ctx = Context()
    plugin._autoplay_waiters = {}
    scheduler = Scheduler()
    task = asyncio.create_task(plugin._delivery_loop(scheduler))
    await scheduler.queue.put({"delivery_id": 1, "result": {"status": "ok", "answer": "hello", "workspace": {"turn_id": "4"}, "terminal": {"kind": "reply"}}})
    for _ in range(20):
        if scheduler.acks:
            break
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert scheduler.acks == [1]

@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["reply", "update_only"])
async def test_user_in_background_inbox_receives_same_result(terminal):
    class Service:
        async def call(self, method, *args): return []
    class Scheduler:
        def __init__(self):
            self.service = Service()
            self._pending_frontdesk = None if terminal == "update_only" else {"colony_id": "c", "load_epoch": "e", "game_tick": 1}
            self.queue = asyncio.Queue()
            self.acks = []
        async def next_frontdesk_result(self): return await self.queue.get()
        async def ack_frontdesk_result(self, ident): self.acks.append(ident)
    plugin = object.__new__(NekoRimworldPlugin)
    plugin._autoplay_waiters = {8: asyncio.get_running_loop().create_future()}
    scheduler = Scheduler()
    task = asyncio.create_task(plugin._delivery_loop(scheduler))
    result = {"status": "ok", "answer": "hello", "workspace": {"turn_id": "4"}, "terminal": {"kind": terminal}}
    await scheduler.queue.put({"delivery_id": 5, "through_seq": 8, "result": result})
    assert await asyncio.wait_for(plugin._autoplay_waiters[8], 1) == result
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

@pytest.mark.asyncio
async def test_budget_save_preserves_existing_values():
    from .test_plugin import make_plugin

    plugin, _ = make_plugin({"neko_rimworld": {"autoplay": {"max_requests": 9, "input_price": 2.5}}})
    result = await plugin._save_autoplay_settings(ui_nonce=plugin._settings_action_nonce, budgets={"output_price": 3.0})
    assert result.is_ok()
    assert plugin.config.value["neko_rimworld"]["autoplay"] == {"max_requests": 9, "input_price": 2.5, "output_price": 3.0}

@pytest.mark.asyncio
async def test_activation_wakes_planner_and_real_strategy_is_written(tmp_path):
    from ..agent.runtime.audit import AgentAuditRegistry
    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..agent.runtime.loop import AgentRuntime
    from ..workspace.runtime import FrontdeskService
    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, settings
    from .test_capability_composition import Script, call

    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    entry = {"entry_id": "strategy", "expected_revision": 0, "kind": "strategy", "status": "drafting",
             "priority": 1, "parent": None, "source": {}, "evidence": [], "review_at_tick": None,
             "changesets": [], "body": "Prioritize survival and stability; no cannibalism."}
    revised = {**entry, "expected_revision": 1, "body": "Build a peaceful colony; no live organ harvesting."}
    model = Script([call("planner_board", {"action": "write", "entry": entry}),
                    call("planner_yield", {"handoff": "Review next."}),
                    call("planner_board", {"action": "write", "entry": revised}),
                    call("planner_yield", {"handoff": "Review the changed goal."}),
                    call("planner_yield", {"handoff": "Reconsider after reload."}),
                    call("planner_yield", {"handoff": "Continue after restart."})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    choices = {"goal": "Prioritize survival and stability.", "constraints": "No cannibalism or live organ harvesting.",
               "goal_edited": False, "constraints_edited": False}
    await service.call("record_autoplay_activation", "colony", "epoch", 10, choices)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    scheduler.resume(game)
    for _ in range(500):
        events = await service.call("events", "colony")
        if any(event["kind"] == "planner.yield" for event in events):
            break
        await asyncio.sleep(.01)
    board = await service.call("board", "colony")
    assert board and board[0]["kind"] == "strategy" and board[0]["status"] == "drafting"
    assert "No cannibalism or live organ harvesting." in str(model.requests[0]["messages"])
    assert not any(event["kind"] == "user.utterance" for event in events)
    assert any(event["kind"] == "autoplay.activation" and event["payload"] == choices for event in events)
    changed = {**choices, "goal": "Build a peaceful colony.", "goal_edited": True}
    await service.call("record_autoplay_activation", "colony", "epoch", 10, changed)
    scheduler.resume(game)
    for _ in range(500):
        events = await service.call("events", "colony")
        if sum(event["kind"] == "planner.yield" for event in events) == 2:
            break
        await asyncio.sleep(.01)
    updated = await service.call("board", "colony")
    assert updated[0]["revision"] == 2 and updated[0]["body"] == revised["body"]
    second_input = str(model.requests[2]["messages"])
    assert "Build a peaceful colony." in second_input
    assert entry["body"] in second_input
    # The activation wake has been consumed. A new epoch and a later restart
    # must still carry the original selected choices as a commitment.
    game.load_epoch = "epoch-reloaded"
    await scheduler.wake(game, source="plugin", text="Review after load")
    for _ in range(500):
        events = await service.call("events", "colony")
        if sum(event["kind"] == "planner.yield" for event in events) == 3:
            break
        await asyncio.sleep(.01)
    third = next(event for event in reversed(events) if event["kind"] == "planner.started")
    assert third["payload"]["input"]["activation_choices"]["payload"] == changed
    assert third["payload"]["input"]["activation_choices"]["load_epoch"] == "epoch"
    assert third["payload"]["input"]["activation_choices"]["source"] == "user"
    assert "Build a peaceful colony." in str(model.requests[4]["messages"])
    await scheduler.close()
    scheduler.start()
    await scheduler.wake(game, source="plugin", text="Review after restart")
    for _ in range(500):
        events = await service.call("events", "colony")
        if sum(event["kind"] == "planner.yield" for event in events) == 4:
            break
        await asyncio.sleep(.01)
    fourth = next(event for event in reversed(events) if event["kind"] == "planner.started")
    assert fourth["payload"]["input"]["activation_choices"]["payload"] == changed
    assert "Build a peaceful colony." in str(model.requests[5]["messages"])
    await scheduler.close()
    await service.close()

@pytest.mark.asyncio
async def test_optional_budget_clear_roundtrips_through_host_toml_codec(tmp_path):
    import tomllib

    from plugin.server.infrastructure.config_merge import deep_merge
    from plugin.server.infrastructure.config_updates import dump_toml_bytes

    from .test_plugin import make_plugin

    plugin, _ = make_plugin({"neko_rimworld": {"autoplay": {"max_requests": 9, "input_price": 2.5, "max_cost": 10.0}}})
    original = plugin.config.value
    async def update(patch, *, timeout=5.0):
        merged = deep_merge(original, patch)
        path = tmp_path / "settings.toml"
        path.write_bytes(dump_toml_bytes(merged))
        with path.open("rb") as stream:
            plugin.config.value = tomllib.load(stream)
        return plugin.config.value
    plugin.config.update = update
    result = await plugin._save_autoplay_settings(ui_nonce=plugin._settings_action_nonce, budgets={"max_cost": None})
    assert result.is_ok()
    saved = plugin.config.value["neko_rimworld"]["autoplay"]
    assert saved == {"max_requests": 9, "input_price": 2.5, "max_cost": ""}
    from ..workspace.budget import BudgetSettings
    normalized = {name: None if name in {"max_cost", "input_price", "output_price", "cache_read_price"} and value == "" else value for name, value in saved.items()}
    assert BudgetSettings(**normalized).max_cost is None

@pytest.mark.asyncio
async def test_panel_enable_records_activation_and_starts_one_scheduler(monkeypatch):
    from ..agent.runtime import colony_scheduler
    from .test_plugin import make_plugin

    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    plugin._bridge = SimpleNamespace(session=game, connection_key="connection")
    plugin._agent_settings = SimpleNamespace(configured=True)
    plugin._autoplay_budget = None
    plugin._colony_scheduler = None
    plugin._autoplay_waiters = {}
    class Journal:
        closed = False
        def __init__(self): self.calls = []
        async def call(self, *args, **kwargs):
            self.calls.append(args)
            return 1
    work = plugin._workspace_service = Journal()
    required = {"colony_write.transactions.v1", "colony_write.ledger_snapshot.v1", "colony_events.v1", "colony_pause.v1", "colony_pause.policy.v1"}
    class Port:
        capabilities = required
        paused = False
        async def set_paused(self, paused, *, role):
            self.paused = paused
            return {"paused": paused, "role": role}
    port = Port()
    async def idle_query(operation, arguments):
        state = {"status": "observed", "connection_epoch": "connection", "colony_id": "colony", "load_epoch": "epoch",
                 "sampled_tick": 10, "owner": "planner" if port.paused else "unknown", "pause_revision": int(port.paused),
                 "pause_transition_revision": int(port.paused), "paused": port.paused, "speed_paused": port.paused,
                 "force_paused": False, "windows_force_paused": False}
        return {"response": {"connection_epoch": "connection", "colony_id": "colony", "load_epoch": "epoch",
                             "payload": {"data": {"clock": {"pause_state": state}}}}}
    async def setup():
        return SimpleNamespace(write_port=port, write_identity=lambda: ("connection", "colony", "epoch"), write_catalog={}, ledger_query=object(), write_fields_query=object(), idle_query=idle_query)
    plugin._load_agent_tools = setup
    class Scheduler:
        def __init__(self, *args, **kwargs):
            self.resumed = None
            self.pause_state = None
        def observe_pause_state(self, state, binding, *, evidence_seq=None): self.pause_state = state
        def start(self): pass
        def resume(self, value): self.resumed = value
        async def close(self): pass
        async def next_frontdesk_result(self): await asyncio.Event().wait()
    monkeypatch.setattr(colony_scheduler, "ColonyScheduler", Scheduler)
    result = await plugin.autoplay_mode(enabled=True, ui_nonce=plugin._settings_action_nonce,
        goal="Keep the colony peaceful", constraints="No cannibalism or live organ harvesting.",
        goal_edited=True, constraints_edited=False)
    assert result.is_ok()
    assert [call[0] for call in work.calls] == ["observe_epoch", "record", "record", "record_autoplay_activation"]
    assert work.calls[-1] == ("record_autoplay_activation", "colony", "epoch", 10,
        {"goal": "Keep the colony peaceful", "constraints": "No cannibalism or live organ harvesting.",
         "goal_edited": True, "constraints_edited": False})
    assert plugin._colony_scheduler.resumed is game
    await plugin._stop_autoplay()

@pytest.mark.asyncio
async def test_autoplay_binding_rejects_a_different_colony():
    from ..agent.runtime.colony_scheduler import ColonyScheduler
    budget = SimpleNamespace(game=SimpleNamespace(colony_id="A"))
    scheduler = ColonyScheduler(None, None, budget=budget)
    with pytest.raises(ToolExecutionError, match="explicitly"):
        await scheduler._check_autoplay_binding(SimpleNamespace(colony_id="B", load_epoch="e", game_tick=1))


@pytest.mark.asyncio
async def test_late_endpoint_failure_never_pauses_new_connection():
    class Context:
        def push_message(self, **kwargs): return {"submitted": True}
    class Service:
        async def call(self, *args): return None
    class Port:
        def __init__(self): self.calls = []
        async def set_paused(self, value, *, role): self.calls.append((value, role))
    port = Port()
    plugin = object.__new__(NekoRimworldPlugin)
    plugin.ctx = Context()
    plugin._autoplay_waiters = {}
    plugin._bridge = SimpleNamespace(session=SimpleNamespace(colony_id="A", load_epoch="epoch", game_tick=1))
    plugin._colony_scheduler = SimpleNamespace(halted=False, service=Service(), budget=None)
    async def setup(): return SimpleNamespace(write_port=port, write_identity=lambda: ("new-connection", "A", "epoch"))
    plugin._load_agent_tools = setup
    await plugin._autoplay_failure("planner", {"audit": {"error_code": "model_transport"}},
        SimpleNamespace(colony_id="A", load_epoch="epoch", connection_epoch="old-connection"))
    assert not port.calls and not plugin._colony_scheduler.halted


@pytest.mark.asyncio
@pytest.mark.parametrize("same_colony_new_connection", [False, True])
async def test_old_colony_delivery_is_acknowledged_without_host_push(same_colony_new_connection):
    class Service:
        def __init__(self): self.calls = []
        async def call(self, *args): self.calls.append(args)
    class Scheduler:
        def __init__(self):
            self.service = Service()
            self.queue = asyncio.Queue()
            self.acks = []
            self._pending_frontdesk = {"colony_id": "A", "load_epoch": "old", "game_tick": 1}
        async def next_frontdesk_result(self): return await self.queue.get()
        async def ack_frontdesk_result(self, ident): self.acks.append(ident)
    class Context:
        def push_message(self, **kwargs): raise AssertionError("stale response was pushed")
    plugin = object.__new__(NekoRimworldPlugin)
    plugin.ctx = Context()
    plugin._bridge = SimpleNamespace(session=SimpleNamespace(colony_id="A" if same_colony_new_connection else "B", load_epoch="old" if same_colony_new_connection else "new"), connection_key="new-connection")
    waiter = asyncio.get_running_loop().create_future()
    plugin._autoplay_waiters = {3: waiter}
    scheduler = Scheduler()
    task = asyncio.create_task(plugin._delivery_loop(scheduler))
    await scheduler.queue.put({"delivery_id": 3, "through_seq": 3, "colony_id": "A", "load_epoch": "old", "connection_epoch": "old-connection", "require_connection": True,
                               "result": {"status": "ok", "terminal": {"kind": "reply"}}})
    assert (await asyncio.wait_for(waiter, 1))["audit"]["error_code"] == "world_changed"
    assert scheduler.acks == [3]
    assert any(call[5] == "autoplay.stale_delivery" for call in scheduler.service.calls)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

@pytest.mark.asyncio
async def test_readonly_view_restores_latest_choices_across_epoch(tmp_path):
    from ..workspace.runtime import FrontdeskService
    from ..workspace.views import WorkspaceViews

    path = tmp_path / "events.sqlite"
    assert WorkspaceViews(path).latest_activation("A") is None
    active = {"game": SimpleNamespace(colony_id="A", load_epoch="old")}
    service = FrontdeskService(path, None, game_session=lambda: active["game"])
    first = {"goal": "Initial goal", "constraints": "Initial constraint", "goal_edited": True, "constraints_edited": True}
    latest = {**first, "goal": "Revised goal"}
    await service.call("record_autoplay_activation", "A", "old", 1, first)
    active["game"] = SimpleNamespace(colony_id="B", load_epoch="other")
    await service.call("record_autoplay_activation", "B", "other", 1, first)
    active["game"] = SimpleNamespace(colony_id="A", load_epoch="older")
    await service.call("record_autoplay_activation", "A", "older", 2, latest)
    await service.close()
    row = WorkspaceViews(path).latest_activation("A")
    assert row["payload"] == latest and row["source"] == "user" and row["load_epoch"] == "older"

@pytest.mark.asyncio
async def test_inbox_host_retry_rejects_reconnected_scope_without_new_dispatch(tmp_path):
    from ..agent.runtime.audit import AgentAuditRegistry
    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..agent.runtime.loop import AgentRuntime
    from ..workspace.runtime import FrontdeskService
    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, settings
    from .test_capability_composition import Script, call
    from .test_plugin import make_plugin

    entered, proceed = asyncio.Event(), asyncio.Event()
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            if not self.requests:
                entered.set()
                await proceed.wait()
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call("frontdesk_board", {"action": "read"}),
                   call("submit_response", {"answer": "Prior response"})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    scheduler._autoplay_colony = "colony"
    async def binding(_):
        return SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10, connection_epoch="old")
    scheduler._check_autoplay_binding = binding
    plugin, _ = make_plugin({})
    plugin._agent_settings = settings()
    plugin._bridge = SimpleNamespace(session=game, connection_key="old")
    plugin._colony_scheduler = scheduler
    plugin._autoplay_waiters = {}
    pushes = []
    plugin.ctx = SimpleNamespace(push_message=lambda **kwargs: pushes.append(kwargs) or {"submitted": True})
    scheduler.start()
    consumer = asyncio.create_task(plugin._deliver_autoplay(scheduler))
    try:
        await scheduler.wake_frontdesk(game, "game trigger", source="game")
        await asyncio.wait_for(entered.wait(), 5)
        request = asyncio.create_task(plugin._consultation("User question", {"run_id": "host-id"}, game))
        for _ in range(500):
            events = await service.call("events", "colony")
            if any(e["kind"] == "user.utterance" for e in events):
                break
            await asyncio.sleep(.01)
        proceed.set()
        answer = await asyncio.wait_for(request, 5)
        assert answer.is_ok()
        events = await service.call("events", "colony")
        assert any(e["kind"] == "frontdesk.inbox" for e in events)
        assert any(e["kind"] == "frontdesk.run_binding" and e["payload"]["connection"] == "old" for e in events)
        before = len(model.requests)
        same = await asyncio.wait_for(plugin._consultation("User question", {"run_id": "host-id"}, game), 1)
        assert same.is_ok()
        plugin._bridge.connection_key = "new"
        retry = await asyncio.wait_for(plugin._consultation("User question", {"run_id": "host-id"}, game), 1)
        assert not retry.is_ok() and retry.error.code == "world_changed"
        plugin._bridge.connection_key = "old"
        initial_wake = next(e["payload"]["wake_seq"] for e in events if e["kind"] == "frontdesk.run_binding")
        await service.call("record", "colony", "epoch", 10, "plugin", "autoplay.stale_delivery",
                           {"delivery_id": initial_wake, "reason": "world_changed"})
        expired = await asyncio.wait_for(plugin._consultation("User question", {"run_id": "host-id"}, game), 1)
        assert not expired.is_ok() and expired.error.code == "world_changed"
        assert len(model.requests) == before and not pushes
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await scheduler.close()
        await service.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("inbox", [False, True])
async def test_readonly_retry_rejects_completed_autoplay_run_without_new_model(tmp_path, inbox):
    from ..agent.runtime.audit import AgentAuditRegistry
    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..agent.runtime.loop import AgentRuntime
    from ..workspace.runtime import FrontdeskService
    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, settings
    from .test_capability_composition import Script, call
    from .test_plugin import make_plugin

    entered, proceed = asyncio.Event(), asyncio.Event()
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            if not self.requests:
                entered.set()
                await proceed.wait()
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call("frontdesk_board", {"action": "read"}),
                   call("submit_response", {"answer": "Autoplay response"})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10)
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    scheduler._autoplay_colony = "colony"
    async def binding(_):
        return SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=10, connection_epoch="old")
    scheduler._check_autoplay_binding = binding
    plugin, _ = make_plugin({})
    plugin._agent_settings = settings()
    plugin._bridge = SimpleNamespace(session=game, connection_key="old")
    plugin._colony_scheduler = scheduler
    plugin._workspace_service = service
    plugin._autoplay_waiters = {}
    pushes = []
    plugin.ctx = SimpleNamespace(push_message=lambda **kwargs: pushes.append(kwargs) or {"submitted": True})
    scheduler.start()
    consumer = asyncio.create_task(plugin._deliver_autoplay(scheduler))
    try:
        if inbox:
            await scheduler.wake_frontdesk(game, "game trigger", source="game")
            await asyncio.wait_for(entered.wait(), 5)
        request = asyncio.create_task(plugin._consultation("User question", {"run_id": "host-id"}, game))
        if not inbox:
            await asyncio.wait_for(entered.wait(), 5)
        for _ in range(500):
            events = await service.call("events", "colony")
            if any(e["kind"] == "user.utterance" for e in events):
                break
            await asyncio.sleep(.01)
        proceed.set()
        assert (await asyncio.wait_for(request, 5)).is_ok()
        before = len(model.requests)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await scheduler.close()
        plugin._colony_scheduler = None
        result = await asyncio.wait_for(plugin._consultation("User question", {"run_id": "host-id"}, game), 1)
        assert not result.is_ok() and result.error.code == "world_changed"
        assert len(model.requests) == before and not pushes
    finally:
        if not consumer.done():
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        if scheduler._worker is not None:
            await scheduler.close()
        await service.close()

@pytest.mark.asyncio
async def test_readonly_retry_rejects_degraded_scheduled_run_using_binding(tmp_path):
    from ..workspace.runtime import FrontdeskService
    from .test_agent import settings
    from .test_plugin import make_plugin

    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    wake = await service.call("record_frontdesk_wake", "c", "e", 1, "user", "question", request_id="same-id")
    events = await service.call("events", "c")
    utterance = next(e["seq"] for e in events if e["kind"] == "user.utterance")
    await service.call("record", "c", "e", 1, "plugin", "frontdesk.run_binding", {"wake_seq": wake, "connection": "connection"})
    await service.call("record_run", "c", "e", 1, "frontdesk", utterance, [], result={"status": "degraded"})
    plugin, _ = make_plugin({})
    plugin._agent_settings = settings()
    plugin._bridge = SimpleNamespace(session=game)
    plugin._workspace_service = service
    plugin._colony_scheduler = None
    try:
        result = await asyncio.wait_for(plugin._consultation("question", {"run_id": "same-id"}, game), 1)
        assert not result.is_ok() and result.error.code == "world_changed"
    finally:
        await service.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("timeout,phase,useful,code,expected", [
    (60, "first_response", None, "model_request_deadline", True),
    (12, "first_response", None, "model_request_deadline", False),
    (60, "stream_read", 25, "model_request_deadline", False),
    (60, None, None, "model_request_deadline", False),
    (60, "first_response", None, "agent_run_budget", False),
    (60, "first_response", None, "agent_cumulative_budget", False),
])
async def test_deadline_pause_requires_silent_endpoint_and_full_request_limit(timeout, phase, useful, code, expected):
    class Registry:
        def get_details(self, run_id, **kwargs):
            assert run_id == "run"
            return {"events": [
                {"event": "model_request_started", "data": {"timeout_seconds": timeout}},
                {"event": "model_request_failed", "data": {"error_code": "model_request_deadline",
                 "diagnostics": {"phase": phase, "first_useful_delta_ms": useful}}},
            ]}
    class Service:
        async def call(self, *args): return None
    class Port:
        def __init__(self): self.calls = []
        async def set_paused(self, value, *, role): self.calls.append((value, role))
    port = Port()
    plugin = object.__new__(NekoRimworldPlugin)
    plugin.ctx = SimpleNamespace(push_message=lambda **kwargs: {"submitted": True})
    plugin._agent_audit = Registry()
    plugin._autoplay_waiters = {}
    plugin._bridge = SimpleNamespace(session=SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1))
    plugin._colony_scheduler = SimpleNamespace(halted=False, budget=None, service=Service(),
                                                settings=SimpleNamespace(request_timeout_seconds=60))
    async def setup(): return SimpleNamespace(write_port=port, write_identity=lambda: ("connection", "c", "e"))
    plugin._load_agent_tools = setup
    await plugin._autoplay_failure("planner", {"audit": {"run_id": "run", "error_code": code}})
    assert bool(port.calls) is expected
    assert plugin._autoplay_fault["kind"] == ("endpoint" if expected else "budget" if code == "agent_cumulative_budget" else "execution")

@pytest.mark.asyncio
async def test_request_deadline_classification_uses_actual_runtime_audit():
    from ..agent.model.model_client import ModelClientError
    from ..agent.runtime.audit import AgentAuditRegistry
    from ..agent.runtime.loop import AgentRuntime
    from ..entry import _unresponsive_request_deadline
    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, settings

    class Model:
        async def complete(self, messages, tools, *, timeout):
            raise ModelClientError("model_request_deadline", "Request expired", diagnostics={"phase": "first_response"})
    registry = AgentAuditRegistry()
    runtime = AgentRuntime(audit_registry=registry, logger=FakeLogger(),
                           model_factory=lambda _: Model(), tools_provider=semantic_tool_provider())
    config = settings()
    result = await runtime.run("Test input", config)
    assert result["audit"]["error_code"] == "model_request_deadline"
    assert _unresponsive_request_deadline(registry, result["audit"]["run_id"], config.request_timeout_seconds)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,outcome", [("no_client", "unknown"), ("reject", "rejected"), ("exception", "unknown")])
async def test_failure_notification_persists_attempt_and_result(tmp_path, mode, outcome):
    from ..workspace.runtime import FrontdeskService
    game = SimpleNamespace(colony_id="old", load_epoch="old-epoch", game_tick=3, connection_epoch="old-connection")
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    plugin = object.__new__(NekoRimworldPlugin)
    plugin._autoplay_fault = {}
    def push(**kwargs):
        if mode == "exception":
            raise RuntimeError("host failure")
        return {"submitted": False}
    plugin.ctx = SimpleNamespace() if mode == "no_client" else SimpleNamespace(push_message=push)
    await plugin._audit_failure_notification(SimpleNamespace(service=service), game, "planner", "failure", "endpoint", "pause unconfirmed")
    events = [e for e in await service.call("events", "old") if e["kind"] == "autoplay.failure_notification"]
    assert [e["payload"]["phase"] for e in events] == ["attempting", "result"]
    assert events[-1]["payload"]["outcome"] == outcome
    assert all(e["load_epoch"] == "old-epoch" for e in events)
    assert not await service.call("events", "new")
    await service.close()


@pytest.mark.asyncio
async def test_failure_notification_journal_failure_still_attempts_delivery():
    class Service:
        async def call(self, *args):
            raise RuntimeError("journal failed")
    plugin = object.__new__(NekoRimworldPlugin)
    plugin._autoplay_fault = {}
    sent = []
    plugin.ctx = SimpleNamespace(push_message=lambda **kwargs: sent.append(kwargs) or {"submitted": True})
    game = SimpleNamespace(colony_id="old", load_epoch="epoch", game_tick=3)
    await plugin._audit_failure_notification(SimpleNamespace(service=Service()), game, "planner", "failure", "endpoint", "pause unconfirmed")
    assert len(sent) == 1
    assert plugin._autoplay_fault["notification_record"] == "unknown"
    assert plugin._autoplay_fault["notification"] == "submitted"


@pytest.mark.asyncio
async def test_failure_notification_uses_old_binding_after_world_change(tmp_path):
    from ..workspace.runtime import FrontdeskService
    old = SimpleNamespace(colony_id="old", load_epoch="epoch-old", game_tick=5, connection_epoch="connection-old")
    new = SimpleNamespace(colony_id="new", load_epoch="epoch-new", game_tick=9)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: new)
    plugin = object.__new__(NekoRimworldPlugin)
    plugin.ctx = SimpleNamespace(push_message=lambda **kwargs: {"submitted": True})
    plugin._bridge = SimpleNamespace(session=new)
    plugin._autoplay_waiters = {}
    plugin._colony_scheduler = SimpleNamespace(halted=False, budget=None, service=service)
    await plugin._autoplay_failure("planner", {"audit": {"error_code": "model_transport"}}, bound_game=old)
    events = await service.call("events", "old")
    assert [e["kind"] for e in events] == ["autoplay.stale_failure"]
    assert events[0]["load_epoch"] == "epoch-old"
    assert events[0]["payload"]["connection_epoch"] == "connection-old"
    assert not plugin._colony_scheduler.halted
    assert not await service.call("events", "new")
    await service.close()


@pytest.mark.asyncio
async def test_stale_failure_does_not_halt_current_scheduler():
    plugin = object.__new__(NekoRimworldPlugin)
    old = SimpleNamespace(colony_id="c", load_epoch="old", game_tick=1, connection_epoch="old-conn")
    current = SimpleNamespace(colony_id="c", load_epoch="new", game_tick=1)
    current_scheduler = SimpleNamespace(halted=False, budget=SimpleNamespace(blocked=False))
    plugin._bridge = SimpleNamespace(session=current, connection_key="new-conn")
    plugin._colony_scheduler = current_scheduler
    plugin._autoplay_waiters = {}
    plugin._autoplay_fault = None
    await plugin._autoplay_failure("planner", {"audit": {"error_code": "execution_failed"}}, old)
    assert not current_scheduler.halted and not current_scheduler.budget.blocked
    assert plugin._autoplay_fault is None


@pytest.mark.asyncio
async def test_old_scheduler_failure_cannot_halt_new_scheduler_or_waiter():
    plugin = object.__new__(NekoRimworldPlugin)
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1, connection_epoch="conn")
    old = SimpleNamespace()
    current = SimpleNamespace(halted=False, budget=SimpleNamespace(blocked=False))
    plugin._bridge = SimpleNamespace(session=game, connection_key="conn")
    plugin._colony_scheduler = current
    waiter = asyncio.get_running_loop().create_future()
    waiter._autoplay_binding = (current, "conn", "c", "e")
    plugin._autoplay_waiters = {1: waiter}
    plugin._autoplay_fault = None
    await plugin._autoplay_failure("planner", {"audit": {"error_code": "execution_failed"}}, game, origin_scheduler=old)
    assert not current.halted and not current.budget.blocked and not waiter.done()
    waiter.cancel()


@pytest.mark.asyncio
async def test_consult_reports_world_changed_during_enqueue():
    from ..tool_contracts import ToolExecutionError
    from .test_plugin import make_plugin
    plugin, _ = make_plugin({"neko_rimworld": {}})
    plugin._bridge = SimpleNamespace(session=SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1))
    async def stale(*args):
        raise ToolExecutionError("world_changed", "private fixture", recoverable=False)
    plugin._consultation = stale
    result = await plugin.rimworld_consult("hello")
    assert not result.is_ok() and result.error.code == "world_changed"


@pytest.mark.asyncio
async def test_consult_enqueue_changed_epoch_reports_world_changed(tmp_path):
    import threading

    from ..workspace.runtime import FrontdeskService
    from .test_plugin import make_plugin
    plugin, _ = make_plugin({"neko_rimworld": {}})
    old = SimpleNamespace(colony_id="c", load_epoch="old", game_tick=1)
    plugin._bridge = SimpleNamespace(session=old, connection_key="connection")
    plugin._agent_settings = SimpleNamespace(configured=True)
    plugin._autoplay_waiters = {}
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: plugin._bridge.session)
    entered, release = threading.Event(), threading.Event()
    def block_worker():
        entered.set()
        release.wait(5)
    blocked = service._executor.submit(block_worker)
    assert await asyncio.to_thread(entered.wait, 2)
    queued = asyncio.Event()
    class Scheduler:
        halted = False
        _autoplay_colony = "c"
        async def wake_frontdesk(self, game, question, request_id=None):
            queued.set()
            return await service.call("record_frontdesk_wake", game.colony_id, game.load_epoch, game.game_tick, "user", question)
    plugin._colony_scheduler = Scheduler()
    task = asyncio.create_task(plugin.rimworld_consult("hello"))
    try:
        await asyncio.wait_for(queued.wait(), 2)
        plugin._bridge.session = SimpleNamespace(colony_id="c", load_epoch="new", game_tick=1)
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert not result.is_ok() and result.error.code == "world_changed"
        assert not [e for e in await service.call("events", "c") if e["kind"] == "user.utterance"]
    finally:
        release.set()
        await asyncio.to_thread(blocked.result, 2)
        await service.close()


@pytest.mark.asyncio
async def test_consult_enqueue_connection_change_returns_world_changed(tmp_path):
    import threading

    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..workspace.runtime import FrontdeskService
    from .test_plugin import make_plugin

    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    plugin._bridge = SimpleNamespace(session=game, connection_key="A")
    plugin._agent_settings = SimpleNamespace(configured=True)
    plugin._autoplay_waiters = {}
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: plugin._bridge.session)
    scheduler = ColonyScheduler(service, None, binding=lambda: (plugin._bridge.connection_key, "c", "e"))
    scheduler._autoplay_colony = "c"
    scheduler._worker = asyncio.create_task(asyncio.Event().wait())
    plugin._colony_scheduler = scheduler
    entered, release = threading.Event(), threading.Event()
    def block():
        entered.set()
        release.wait(5)
    blocked = service._executor.submit(block)
    assert await asyncio.to_thread(entered.wait, 2)
    request = asyncio.create_task(plugin._consultation("hello", {}, game))
    await asyncio.sleep(0)
    plugin._bridge.connection_key = "B"
    release.set()
    try:
        result = await asyncio.wait_for(request, 2)
        assert not result.is_ok() and result.error.code == "world_changed"
        assert not plugin._autoplay_waiters
        assert not [e for e in await service.call("events", "c") if e["kind"] == "user.utterance"]
    finally:
        release.set()
        await asyncio.to_thread(blocked.result, 2)
        scheduler._worker.cancel()
        await asyncio.gather(scheduler._worker, return_exceptions=True)
        await service.close()

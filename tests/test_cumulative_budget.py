from plugin.plugins.neko_rimworld.agent.model.model_client import parse_usage
from plugin.plugins.neko_rimworld.agent.runtime.prompt import build_system_prompt
from plugin.plugins.neko_rimworld.workspace.budget import BudgetSettings
from plugin.plugins.neko_rimworld.workspace.journal import Journal


def test_deepseek_usage_and_missing_cache():
    assert parse_usage({"prompt_tokens": 20, "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 15}) == {
        "prompt_tokens": 20, "cached_tokens": 5, "cache_miss_tokens": 15}
    assert "cached_tokens" not in parse_usage({"prompt_tokens": 20})
    assert "cached_tokens" not in parse_usage({"prompt_tokens_details": {"cached_tokens": 2},
                                                "prompt_cache_hit_tokens": 3})


def test_autoplay_prompts_are_independent():
    front = build_system_prompt(persistent_work=True, autoplay=True)
    planner = build_system_prompt(role="planner", planner_loop=True)
    assert "private read-only" not in front and "cannot resume" in front
    assert "planner_yield" in planner and "No long-term memory" not in planner
    assert "private read-only" in build_system_prompt(persistent_work=True)


def test_cumulative_reservation_persists_and_retains_unknown(tmp_path):
    path = tmp_path / "events.sqlite"
    journal = Journal(path)
    prices = {"input_price": 2, "output_price": 4, "cache_read_price": 1}
    def payload(ident):
        return {"id": ident, "role": "planner", "input_estimate": 100, "output_limit": 20,
                "reserved_cost": 0.00028, "prices": prices}
    assert journal.budget_reserve("c", "e", 1, payload("a"), 2, 200, 1)
    journal.budget_settle("c", "e", 1, {"id": "a", "usage": {}})
    assert journal.budget_reserve("c", "e", 1, payload("b"), 2, 200, 1)
    assert not journal.budget_reserve("c", "e", 1, payload("c"), 2, 200, 1)
    journal.close()
    journal = Journal(path)
    state = journal.budget_status("c")
    assert state["requests"] == 2 and state["input_tokens"] == 200
    assert state["unknown_usage"] == state["unknown_cost"] == 2
    journal.close()


def test_cap_requires_prices(tmp_path):
    j = Journal(tmp_path / "events.sqlite")
    payload = {"id": "a", "role": "frontdesk", "input_estimate": 10, "output_limit": 1,
               "reserved_cost": None, "prices": {"input_price": None, "output_price": None, "cache_read_price": None}}
    assert not j.budget_reserve("c", "e", 1, payload, 10, 100, 10)
    assert j.budget_status("c")["requests"] == 0
    j.close()


def test_settings_reject_invalid():
    import pytest
    with pytest.raises(ValueError):
        BudgetSettings(max_cost=float("nan"))


def test_concurrent_admission_is_atomic(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "events.sqlite"
    journal = Journal(path)
    journal.close()
    gate = threading.Barrier(2)
    def attempt(ident):
        j = Journal(path)
        payload = {"id": ident, "role": "planner", "input_estimate": 10, "output_limit": 1,
                   "reserved_cost": None, "prices": {"input_price": None, "output_price": None, "cache_read_price": None}}
        gate.wait()
        result = j.budget_reserve("c", "e", 1, payload, 1, 100, None)
        j.close()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, ("a", "b"))) == [False, True]


async def test_receipt_survives_storage_failure_and_flushes_before_admission(tmp_path):
    from types import SimpleNamespace

    import pytest

    from plugin.plugins.neko_rimworld.workspace.budget import ColonyBudget

    class Service:
        def __init__(self):
            self.journal = Journal(tmp_path / "events.sqlite")
            self.fail = False
        async def call(self, name, *args):
            if self.fail and name == "budget_settle":
                raise OSError("storage unavailable")
            return getattr(self.journal, name)(*args)

    service = Service()
    budget = ColonyBudget(service, SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1), BudgetSettings())
    first = await budget.reserve("frontdesk", 100, 10)
    service.fail = True
    with pytest.raises(OSError):
        await budget.settle(first, {"prompt_tokens": 30, "completion_tokens": 2})
    assert await budget.reserve("planner", 100, 10) is None
    with pytest.raises(OSError):
        await budget.reenable()
    assert budget.pending[first][1]["usage"]["prompt_tokens"] == 30
    assert (await budget.status())["requests"] == 1
    service.fail = False
    await budget.reenable()
    assert await budget.reserve("planner", 100, 10)
    assert not budget.pending
    assert (await budget.status())["known_prompt_tokens"] == 30
    service.journal.close()


async def test_each_model_retry_reserves_and_settles(monkeypatch):
    from types import SimpleNamespace

    from plugin.plugins.neko_rimworld.agent.model.model_client import ModelClientError, build_agent_model_settings
    from plugin.plugins.neko_rimworld.agent.model.model_requests import request_model
    from plugin.plugins.neko_rimworld.agent.runtime.audit import AgentAuditLedger
    from plugin.plugins.neko_rimworld.agent.runtime.session import AgentRunSession

    class Budget:
        def __init__(self):
            self.reserves = []
            self.settles = []
        async def status(self, scope):
            return {"requests": len(self.reserves), "unknown_usage": 0}
        async def reserve(self, role, estimate, output, **kwargs):
            self.reserves.append((role, estimate, output))
            return str(len(self.reserves))
        async def begin_dispatch(self, ident):
            return True
        async def settle(self, ident, usage, **kwargs):
            self.settles.append((ident, usage))

    class Model:
        async def complete(self, *args, **kwargs):
            if len(budget.reserves) == 1:
                raise ModelClientError("model_transport", "transient", usage={})
            from plugin.plugins.neko_rimworld.message_contracts import AssistantTurn
            return AssistantTurn("id", "model", "stop", "done", usage={"prompt_tokens": 10, "completion_tokens": 2})

    async def no_sleep(delay):
        pass
    monkeypatch.setattr("plugin.plugins.neko_rimworld.agent.model.model_requests.asyncio.sleep", no_sleep)
    ledger = AgentAuditLedger("question")
    session = AgentRunSession.create(ledger=ledger, question="question")
    budget = session.budget = Budget()
    settings = build_agent_model_settings(base_url="http://localhost:11434/v1", model="test")
    result = await request_model(Model(), settings, ledger, session, session.messages, [], finalizing=True,
        reserve=0, research_deadline=900, cycle=1, request_sha256="x",
        logger=SimpleNamespace(warning=lambda *args: None, error=lambda *args: None))
    assert result.error is None and len(budget.reserves) == 2
    assert budget.settles == [("1", {}), ("2", {"prompt_tokens": 10, "completion_tokens": 2})]


def test_partial_known_usage_exceeding_estimate_keeps_upper_cost(tmp_path):
    j = Journal(tmp_path / "events.sqlite")
    payload = {"id": "a", "role": "planner", "input_estimate": 100, "output_limit": 20,
               "reserved_cost": 0.00028, "prices": {"input_price": 2, "output_price": 4, "cache_read_price": 1}}
    assert j.budget_reserve("c", "e", 1, payload, 10, 10000, 1)
    j.budget_settle("c", "e", 1, {"id": "a", "usage": {"prompt_tokens": 1000, "completion_tokens": 200}})
    state = j.budget_status("c")
    assert state["cost"] == 0.0028 and state["unknown_cost"] == 1
    assert not j.budget_reserve("c", "e", 1, {**payload, "id": "b"}, 10, 10000, 0.003)
    j.close()


async def test_settlement_keeps_original_scope_and_full_response_on_failure(tmp_path):
    from types import SimpleNamespace

    import pytest

    from plugin.plugins.neko_rimworld.workspace.budget import ColonyBudget

    class Service:
        def __init__(self):
            self.j = Journal(tmp_path / "events.sqlite")
            self.fail = False
        async def call(self, name, *args):
            if self.fail and name == "budget_settle":
                raise OSError("failure")
            return getattr(self.j, name)(*args)
    service = Service()
    budget = ColonyBudget(service, SimpleNamespace(colony_id="c", load_epoch="old", game_tick=1), BudgetSettings())
    ident = await budget.reserve("planner", 100, 20, scope=("c", "old", 1), attempt_id="attempt",
                                 metadata={"run_id": "run", "cycle": 2, "attempt": 1, "request_sha256": "hash"})
    budget.game = SimpleNamespace(colony_id="other", load_epoch="new", game_tick=20)
    response = {"audit": {"content": "complete response", "usage": {}}}
    service.fail = True
    with pytest.raises(OSError):
        await budget.settle(ident, {"prompt_tokens": 500}, response=response)
    assert budget.pending[ident][1]["response"] == response
    service.fail = False
    await budget.reenable()
    events = service.j.events("c")
    assert [(e["kind"], e["load_epoch"]) for e in events] == [("budget.reserve", "old"), ("budget.settle", "old")]
    assert events[0]["payload"]["request_sha256"] == "hash"
    assert events[1]["payload"]["response"] == response
    assert service.j.events("other") == []
    service.j.close()


async def test_cancelled_admission_records_not_dispatched(tmp_path):
    import asyncio
    from types import SimpleNamespace

    import pytest

    from plugin.plugins.neko_rimworld.workspace.budget import ColonyBudget

    class Service:
        def __init__(self):
            self.j = Journal(tmp_path / "events.sqlite")
            self.started = asyncio.Event()
            self.release = asyncio.Event()
        async def call(self, name, *args):
            if name == "budget_reserve":
                self.started.set()
                await self.release.wait()
            return getattr(self.j, name)(*args)
    service = Service()
    budget = ColonyBudget(service, SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1), BudgetSettings())
    task = asyncio.create_task(budget.reserve("planner", 100, 20, attempt_id="a"))
    await service.started.wait()
    task.cancel()
    service.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = service.j.budget_status("c")
    assert state["reservations"] == state["not_dispatched"] == 1
    assert state["requests"] == state["attempted"] == 0
    assert budget.blocked
    service.j.close()


def test_autoplay_finalization_matches_terminal_tools():
    from plugin.plugins.neko_rimworld.agent.runtime.prompt import build_runtime_advisory
    kwargs = dict(cycle=1, elapsed_seconds=9, remaining_seconds=1, model_calls=1, tool_calls=1, finalizing=True)
    planner = build_runtime_advisory(**kwargs, autoplay_role="planner")
    frontdesk = build_runtime_advisory(**kwargs, autoplay_role="frontdesk")
    assert "planner_yield" in planner and "submit_response" not in planner and "answer" not in planner
    assert "submit_response" in frontdesk and "update_only" in frontdesk
    for role in ("frontdesk", "planner"):
        prompt = build_system_prompt(role=role, autoplay=True)
        assert "human meat" in prompt and "organs from living people" in prompt
        assert "user" in prompt and "change" in prompt


async def test_failed_settlement_preserves_full_model_turn_and_input(tmp_path):
    from types import SimpleNamespace

    import pytest

    from plugin.plugins.neko_rimworld.agent.model.model_client import build_agent_model_settings
    from plugin.plugins.neko_rimworld.agent.model.model_requests import _canonical_hash, request_model
    from plugin.plugins.neko_rimworld.agent.runtime.audit import AgentAuditLedger
    from plugin.plugins.neko_rimworld.agent.runtime.loop import AgentRuntime
    from plugin.plugins.neko_rimworld.agent.runtime.session import AgentRunSession
    from plugin.plugins.neko_rimworld.message_contracts import AssistantTurn, ToolCall
    from plugin.plugins.neko_rimworld.workspace.budget import ColonyBudget

    class Service:
        def __init__(self):
            self.j = Journal(tmp_path / "events.sqlite")
        async def call(self, name, *args):
            if name == "budget_settle":
                raise OSError("disk failed")
            return getattr(self.j, name)(*args)
    class Model:
        async def complete(self, messages, tools, **kwargs):
            self.messages, self.tools = messages, tools
            return AssistantTurn("provider-id", "model", "tool_calls", "response",
                                 (ToolCall("call", "look", "{}"),), {"prompt_tokens": 20},
                                 reasoning_content="continuation")
    service = Service()
    ledger = AgentAuditLedger("question")
    session = AgentRunSession.create(ledger=ledger, question="question", role="planner", planner_loop=True)
    session.budget = ColonyBudget(service, SimpleNamespace(colony_id="c", load_epoch="e", game_tick=5), BudgetSettings())
    session.budget_scope = ("c", "e", 5)
    captured = []
    async def persist(event):
        captured.append(event)
    session.persist_progress = persist
    model = Model()
    settings = build_agent_model_settings(base_url="http://localhost:11434/v1", model="test")
    tools = [{"name": "planner_yield"}]
    with pytest.raises(OSError):
        await request_model(model, settings, ledger, session, session.messages, tools, finalizing=True,
            reserve=0, research_deadline=900, cycle=3, request_sha256="unused",
            logger=SimpleNamespace(warning=lambda *args: None, error=lambda *args: None))
    prepared, admitted = captured
    request = prepared["model_request"]
    assert request["dispatch_state"] == "prepared_not_dispatched"
    assert admitted["attempt_id"] == request["attempt_id"]
    assert request["messages"] == model.messages and request["tools"] == tools
    assert request["request_sha256"] == _canonical_hash({"messages": model.messages, "tools": tools})
    assert "Cumulative model budget" in model.messages[-1]["content"]
    assert model.messages[0]["content"] == session.messages[0]["content"]
    pending = session.budget.pending[request["attempt_id"]][1]
    assert "reasoning_content" not in str(pending)
    assert session.received_model_turn.reasoning_content is not None
    assert session.received_model_turn.content == "response" and session.model_receipt_unconfirmed
    result = AgentRuntime._attach_handoff({"status": "degraded"}, session)
    assert result["received_model_response"]["tool_calls"][0]["id"] == "call"
    reservation = service.j.events("c")[0]["payload"]
    assert (reservation["run_id"], reservation["cycle"], reservation["attempt"], reservation["request_sha256"]) == (
        ledger.run_id, 3, 1, request["request_sha256"])
    service.j.close()


async def test_summary_and_frontdesk_requests_share_budget_and_record_exact_inputs(tmp_path):
    from types import SimpleNamespace

    from plugin.plugins.neko_rimworld.agent.runtime.audit import AgentAuditLedger, AgentAuditRegistry
    from plugin.plugins.neko_rimworld.agent.runtime.loop import AgentRuntime
    from plugin.plugins.neko_rimworld.agent.runtime.session import AgentRunSession
    from plugin.plugins.neko_rimworld.workspace.budget import ColonyBudget

    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, settings, turn
    from .test_capability_composition import Script, call

    class Service:
        def __init__(self):
            self.j = Journal(tmp_path / "events.sqlite")
        async def call(self, name, *args):
            return getattr(self.j, name)(*args)
    service = Service()
    model = Script([turn("narrative summary"), call("submit_response", {"answer": "Ready"})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    session = AgentRunSession.create(ledger=AgentAuditLedger("question"), question="question", autoplay=True)
    session.explicit_scheduler = True
    session.budget = ColonyBudget(service, SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1), BudgetSettings())
    session.budget_scope = ("c", "e", 1)
    session.narrative_request = [{"role": "system", "content": "summarize narrative"},
                                 {"role": "user", "content": "original narrative"}]
    summaries = []
    async def accept(value):
        summaries.append(value)
    session.accept_narrative_summary = accept
    progress = []
    async def persist(value):
        progress.append(value)
    session.persist_progress = persist
    result = await runtime.execute(session, settings())
    assert result["status"] == "ok" and summaries == ["narrative summary"]
    assert len(model.requests) == 2
    requests = [e["model_request"] for e in progress if "model_request" in e]
    assert len(requests) == 2
    for recorded, dispatched in zip(requests, model.requests):
        assert recorded["messages"] == dispatched["messages"] and recorded["tools"] == dispatched["tools"]
        assert "Cumulative model budget" in dispatched["messages"][-1]["content"]
    assert model.requests[0]["tools"] == []
    assert "human meat" in model.requests[1]["messages"][0]["content"]
    state = service.j.budget_status("c")
    assert state["requests"] == state["attempted"] == 2
    service.j.close()


async def test_cancel_during_settlement_lock_keeps_receipt_and_private_data_in_session(tmp_path):
    import asyncio
    from types import SimpleNamespace

    import pytest

    from plugin.plugins.neko_rimworld.agent.model.model_client import build_agent_model_settings
    from plugin.plugins.neko_rimworld.agent.model.model_requests import request_model
    from plugin.plugins.neko_rimworld.agent.runtime.audit import AgentAuditLedger
    from plugin.plugins.neko_rimworld.agent.runtime.session import AgentRunSession
    from plugin.plugins.neko_rimworld.message_contracts import AssistantTurn
    from plugin.plugins.neko_rimworld.workspace.budget import ColonyBudget

    class Service:
        def __init__(self):
            self.j = Journal(tmp_path / "events.sqlite")
            self.reserve_started, self.release = asyncio.Event(), asyncio.Event()
        async def call(self, name, *args):
            if name == "budget_reserve" and args[3]["role"] == "frontdesk":
                self.reserve_started.set()
                await self.release.wait()
            return getattr(self.j, name)(*args)
    class Model:
        def __init__(self):
            self.started, self.release = asyncio.Event(), asyncio.Event()
            self.calls = 0
        async def complete(self, *args, **kwargs):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return AssistantTurn("id", "model", "stop", "received", usage={"prompt_tokens": 25},
                                 reasoning_content="provider-private-continuation")
    service, model = Service(), Model()
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    budget = ColonyBudget(service, game, BudgetSettings())
    ledger = AgentAuditLedger("question")
    session = AgentRunSession.create(ledger=ledger, question="question", role="planner", planner_loop=True)
    session.budget, session.budget_scope = budget, ("c", "e", 1)
    settings = build_agent_model_settings(base_url="http://localhost:11434/v1", model="test")
    request = asyncio.create_task(request_model(model, settings, ledger, session, session.messages, [], finalizing=True,
        reserve=0, research_deadline=900, cycle=1, request_sha256="", logger=SimpleNamespace()))
    await model.started.wait()
    other_model = Model()
    other_ledger = AgentAuditLedger("frontdesk")
    other_session = AgentRunSession.create(ledger=other_ledger, question="frontdesk", autoplay=True)
    other_session.budget, other_session.budget_scope = budget, ("c", "e", 1)
    other = asyncio.create_task(request_model(other_model, settings, other_ledger, other_session,
        other_session.messages, [], finalizing=True, reserve=0, research_deadline=900, cycle=1,
        request_sha256="", logger=SimpleNamespace()))
    await service.reserve_started.wait()
    model.release.set()
    for _ in range(100):
        if budget.pending:
            break
        await asyncio.sleep(0)
    assert budget.pending and session.model_receipt_unconfirmed
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert budget.blocked and len(budget.pending) == 1
    assert session.received_model_turn.reasoning_content is not None
    service.release.set()
    denied = await other
    assert denied.error.code == "agent_cumulative_budget" and other_model.calls == 0
    assert model.calls == 1 and other_session.model_calls == 0
    await budget.reenable()
    events = service.j.events("c")
    settlement = next(e for e in events if e["kind"] == "budget.settle" and e["payload"]["outcome"] == "attempted")
    assert settlement["payload"]["usage"]["prompt_tokens"] == 25
    state = service.j.budget_status("c")
    assert state["reservations"] == 2 and state["attempted"] == 1 and state["not_dispatched"] == 1
    assert "reasoning_content" not in str(settlement)
    assert "provider-private-continuation" not in str(events)
    service.j.close()


async def test_activation_seeds_planner_authored_strategy(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from plugin.plugins.neko_rimworld.agent.runtime.audit import AgentAuditRegistry
    from plugin.plugins.neko_rimworld.agent.runtime.colony_scheduler import ColonyScheduler
    from plugin.plugins.neko_rimworld.agent.runtime.loop import AgentRuntime
    from plugin.plugins.neko_rimworld.agent.runtime.frontdesk_service import FrontdeskService

    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, settings
    from .test_capability_composition import Script, call

    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    choices = {"goal": "Preserve colonists", "constraints": "Avoid human meat", "goal_edited": True,
               "constraints_edited": True}
    strategy = {"entry_id": "strategy-1", "expected_revision": 0, "kind": "strategy", "status": "drafting",
                "priority": 1, "parent": None, "source": {}, "evidence": [], "review_at_tick": None,
                "changesets": [], "body": "Preserve colonists; avoid human meat."}
    model = Script([call("planner_board", {"action": "write", "entry": strategy}),
                    call("planner_yield", {"handoff": "Develop tasks next."})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    try:
        wake = await service.call("record_autoplay_activation", "c", "e", 1, choices)
        scheduler.start()
        scheduler.resume(game)
        for _ in range(500):
            events = await service.call("events", "c")
            if any(e["kind"] == "planner.finished" for e in events):
                break
            await asyncio.sleep(.01)
        activation = next(e for e in events if e["kind"] == "autoplay.activation")
        assert activation["source"] == "user" and activation["payload"] == choices
        assert not any(e["kind"] == "user.utterance" for e in events)
        wake_event = next(e for e in events if e["seq"] == wake)
        assert wake_event["payload"]["activation_seq"] == activation["seq"]
        first = model.requests[0]["messages"]
        assert "first create a draft or open strategy" in first[0]["content"]
        assert "Preserve colonists" in str(first) and "Avoid human meat" in str(first)
        board = await service.call("board", "c")
        assert len(board) == 1 and board[0]["author"] == "planner" and board[0]["body"] == strategy["body"]
    finally:
        await scheduler.close()
        await service.close()


def test_activation_validation_and_atomic_wake(tmp_path, monkeypatch):
    import pytest

    j = Journal(tmp_path / "events.sqlite")
    choices = {"goal": " goal ", "constraints": "constraints", "goal_edited": False, "constraints_edited": True}
    for invalid in ({**choices, "goal": " "}, {**choices, "goal_edited": 1}, {**choices, "constraints": "x" * 2001}):
        with pytest.raises(ValueError):
            j.record_autoplay_activation("c", "e", 1, invalid)
    original = j._ledger.append
    def fail_wake(*args):
        if args[4] == "planner.wake":
            raise OSError("injected")
        return original(*args)
    monkeypatch.setattr(j._ledger, "append", fail_wake)
    with pytest.raises(OSError):
        j.record_autoplay_activation("c", "e", 1, choices)
    assert j.events("c") == []
    j.close()


def test_retained_inbox_response_uses_run_anchor_and_exact_identity(tmp_path):
    import json

    j = Journal(tmp_path / "events.sqlite")
    trigger = j.record("c", "e", 1, "plugin", "frontdesk.trigger", {"source": "game", "text": "danger"})
    wake = j.record_frontdesk_wake("c", "e", 2, "user", "help", request_id="host-id")
    j.record("c", "e", 2, "frontdesk", "frontdesk.inbox", {"trigger_seq": trigger, "wake_seq": wake})
    j.record_run("c", "e", 3, "frontdesk", trigger, [], result={"status": "ok", "answer": "answer"})
    j.record("c", "e", 3, "frontdesk", "agent.delivery", {"utterance_seq": trigger, "handoff": {"answer": "answer"}})
    retained = j.retained_response("c", "e", "help", "host-id")
    assert retained["payload"]["trigger_seq"] == trigger
    assert json.loads(retained["delivery_payload"])["handoff"]["answer"] == "answer"
    assert j.retained_response("c", "other", "help", "host-id") is None
    assert j.retained_response("c", "e", "other", "host-id") is None
    assert j.retained_response("c", "e", "help", "other") is None
    assert j.retained_response("other", "e", "help", "host-id") is None
    j.close()


async def test_frontdesk_projects_latest_activation_across_loads(tmp_path):
    from types import SimpleNamespace

    from plugin.plugins.neko_rimworld.agent.runtime.frontdesk_service import FrontdeskService

    class Runtime:
        async def execute(self, session, settings):
            self.session = session
            return {"status": "ok", "answer": "ok"}
    game = SimpleNamespace(colony_id="c", load_epoch="old", game_tick=1)
    runtime = Runtime()
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    choices = {"goal": "Survive", "constraints": "No human meat", "goal_edited": True, "constraints_edited": False}
    try:
        await service.call("record_autoplay_activation", "c", "old", 1, choices)
        game.load_epoch = "new"
        await service.consult(game, "What next?", object())
        messages = [m for m in runtime.session.messages if m.get("name") == "autoplay_activation"]
        assert len(messages) == 1
        text = messages[0]["content"]
        assert '"load_epoch":"old"' in text and '"goal_edited":true' in text
        assert '"source":"user"' in text and "not a current world observation" in text
    finally:
        await service.close()


async def test_block_after_admission_and_before_dispatch_stops_model(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from plugin.plugins.neko_rimworld.agent.model.model_client import build_agent_model_settings
    from plugin.plugins.neko_rimworld.agent.model.model_requests import request_model
    from plugin.plugins.neko_rimworld.agent.runtime.audit import AgentAuditLedger
    from plugin.plugins.neko_rimworld.agent.runtime.session import AgentRunSession
    from plugin.plugins.neko_rimworld.workspace.budget import ColonyBudget

    class Service:
        def __init__(self):
            self.j = Journal(tmp_path / "events.sqlite")
        async def call(self, name, *args):
            return getattr(self.j, name)(*args)
    class Model:
        calls = 0
        async def complete(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("request should have been blocked")
    service, model = Service(), Model()
    budget = ColonyBudget(service, SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1), BudgetSettings())
    ledger = AgentAuditLedger("q")
    session = AgentRunSession.create(ledger=ledger, question="q", autoplay=True)
    session.budget, session.budget_scope = budget, ("c", "e", 1)
    entered, release = asyncio.Event(), asyncio.Event()
    async def persist(event):
        if event["stage"] == "model_request_admitted":
            entered.set()
            await release.wait()
    session.persist_progress = persist
    settings = build_agent_model_settings(base_url="http://localhost:11434/v1", model="test")
    request = asyncio.create_task(request_model(model, settings, ledger, session, session.messages, [],
        finalizing=True, reserve=0, research_deadline=900, cycle=1, request_sha256="", logger=SimpleNamespace()))
    await entered.wait()
    budget.blocked = True
    release.set()
    result = await request
    assert result.error.code == "agent_cumulative_budget" and model.calls == session.model_calls == 0
    state = service.j.budget_status("c")
    assert state["reservations"] == state["not_dispatched"] == 1 and state["requests"] == 0
    service.j.close()

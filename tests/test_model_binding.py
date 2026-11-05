import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from ..agent.model.model_client import build_agent_model_settings
from ..agent.model.model_requests import request_model
from ..agent.runtime.audit import AgentAuditLedger
from ..agent.runtime.session import AgentRunSession
from .test_agent import FakeLogger


@pytest.mark.asyncio
async def test_scope_change_after_reservation_before_dispatch_settles_not_dispatched():
    entered, proceed = asyncio.Event(), asyncio.Event()
    active = {"valid": True}
    class Budget:
        blocked = False
        def __init__(self): self.receipts = []
        async def status(self, scope): return {}
        async def reserve(self, *args, **kwargs): return "reservation"
        async def begin_dispatch(self, ident): return True
        async def settle(self, ident, usage, **kwargs): self.receipts.append((ident, usage, kwargs))
    class Model:
        calls = 0
        last_usage = {}
        @asynccontextmanager
        async def request_scope(self):
            entered.set()
            await proceed.wait()
            yield
        async def complete(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("stale request dispatched")
    model, budget = Model(), Budget()
    ledger = AgentAuditLedger("q")
    session = AgentRunSession.create(ledger=ledger, question="q", autoplay=True)
    session.budget, session.budget_scope = budget, ("c", "e", 1)
    session.binding_check = lambda: active["valid"]
    settings = build_agent_model_settings(base_url="http://localhost:11434/v1", model="test")
    task = asyncio.create_task(request_model(model, settings, ledger, session, session.messages, [],
        finalizing=True, reserve=0, research_deadline=900, cycle=1, logger=FakeLogger()))
    await entered.wait()
    active["valid"] = False
    proceed.set()
    result = await task
    assert result.error.code == "world_changed"
    assert model.calls == session.model_calls == 0
    assert budget.receipts[0][2]["outcome"] == "not_dispatched"

@pytest.mark.asyncio
@pytest.mark.parametrize('failure_point', ['scope', 'dispatch'])
async def test_unexpected_request_failure_preserves_dispatch_accounting(failure_point):
    class Budget:
        blocked = False
        def __init__(self): self.receipts = []
        async def status(self, scope): return {}
        async def reserve(self, *args, **kwargs): return 'reservation'
        async def begin_dispatch(self, ident): return True
        async def settle(self, ident, usage, **kwargs): self.receipts.append((ident, usage, kwargs))
    class Model:
        @asynccontextmanager
        async def request_scope(self):
            if failure_point == 'scope': raise RuntimeError('scope failed')
            yield
        async def complete(self, *args, **kwargs): raise RuntimeError('transport unknown')
    budget = Budget()
    ledger = AgentAuditLedger('q')
    session = AgentRunSession.create(ledger=ledger, question='q', autoplay=True)
    session.budget, session.budget_scope = budget, ('c', 'e', 1)
    settings = build_agent_model_settings(base_url='http://localhost:11434/v1', model='test')
    with pytest.raises(RuntimeError):
        await request_model(Model(), settings, ledger, session, session.messages, [], finalizing=True,
                            reserve=0, research_deadline=900, cycle=1, logger=FakeLogger())
    if failure_point == 'scope':
        assert budget.receipts[0][2]['outcome'] == 'not_dispatched'
    else:
        assert budget.receipts == []  # Dispatched outcome remains conservatively unresolved.

@pytest.mark.asyncio
async def test_scope_failure_settlement_survives_cancellation():
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Budget:
        blocked = False
        async def status(self, scope): return {}
        async def reserve(self, *args, **kwargs): return 'reservation'
        async def begin_dispatch(self, ident): return True
        async def settle(self, ident, usage, **kwargs):
            assert kwargs['outcome'] == 'not_dispatched'
            entered.set()
            await release.wait()
            finished.set()
    class Model:
        @asynccontextmanager
        async def request_scope(self):
            raise RuntimeError('scope failed')
            yield
    ledger = AgentAuditLedger('q')
    session = AgentRunSession.create(ledger=ledger, question='q', autoplay=True)
    session.budget, session.budget_scope = Budget(), ('c', 'e', 1)
    settings = build_agent_model_settings(base_url='http://localhost:11434/v1', model='test')
    task = asyncio.create_task(request_model(Model(), settings, ledger, session, session.messages, [],
        finalizing=True, reserve=0, research_deadline=900, cycle=1, logger=FakeLogger()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    release.set()
    await asyncio.wait_for(finished.wait(), 1)

@pytest.mark.asyncio
async def test_pending_scope_cannot_dispatch_after_other_settlement_blocks_budget(tmp_path):
    from ..workspace.budget import BudgetSettings, ColonyBudget
    from ..workspace.journal import Journal
    from ..message_contracts import AssistantTurn
    class Service:
        def __init__(self):
            self.journal = Journal(tmp_path / 'events.sqlite')
            self.other_id = None
        async def call(self, method, *args, **kwargs):
            if method == 'budget_reserve' and args[3]['role'] == 'other':
                self.other_id = args[3]['id']
            if method == 'budget_settle' and args[3]['id'] == self.other_id:
                raise OSError('simulated journal failure')
            return getattr(self.journal, method)(*args, **kwargs)
    service = Service()
    budget = ColonyBudget(service, SimpleNamespace(colony_id='c', load_epoch='e', game_tick=1), BudgetSettings())
    entered, proceed = asyncio.Event(), asyncio.Event()
    class Model:
        calls = 0
        @asynccontextmanager
        async def request_scope(self):
            entered.set()
            await proceed.wait()
            yield
        async def complete(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError('blocked budget dispatched')
    model = Model()
    ledger = AgentAuditLedger('q')
    session = AgentRunSession.create(ledger=ledger, question='q', autoplay=True)
    session.budget, session.budget_scope = budget, ('c', 'e', 1)
    settings = build_agent_model_settings(base_url='http://localhost:11434/v1', model='test')
    task = asyncio.create_task(request_model(model, settings, ledger, session, session.messages, [],
        finalizing=True, reserve=0, research_deadline=900, cycle=1, logger=FakeLogger()))
    await entered.wait()
    class OtherModel:
        async def complete(self, *args, **kwargs):
            return AssistantTurn('id', 'test', 'stop', 'done', usage={'prompt_tokens': 5, 'completion_tokens': 1})
    other_ledger = AgentAuditLedger('other')
    other_session = AgentRunSession.create(ledger=other_ledger, question='other', autoplay=True)
    other_session.budget, other_session.budget_scope, other_session.role = budget, ('c', 'e', 1), 'other'
    with pytest.raises(OSError):
        await request_model(OtherModel(), settings, other_ledger, other_session, other_session.messages, [],
                            finalizing=True, reserve=0, research_deadline=900, cycle=1, logger=FakeLogger())
    proceed.set()
    result = await task
    assert result.error.code == 'agent_cumulative_budget'
    assert model.calls == 0
    status = service.journal.budget_status('c')
    assert status['not_dispatched'] == 1 and status['dispatch_unknown'] == 1
    service.journal.close()

@pytest.mark.asyncio
async def test_rejected_admission_scope_exit_failure_does_not_settle_twice():
    class Budget:
        blocked = False
        def __init__(self): self.settlements = 0
        async def status(self, scope): return {}
        async def reserve(self, *args, **kwargs): return 'reservation'
        async def begin_dispatch(self, ident):
            self.settlements += 1
            return False
        async def settle(self, *args, **kwargs): self.settlements += 1
    class Model:
        @asynccontextmanager
        async def request_scope(self):
            try: yield
            finally: raise RuntimeError('scope exit failed')
        async def complete(self, *args, **kwargs): raise AssertionError('must not dispatch')
    budget = Budget()
    ledger = AgentAuditLedger('q')
    session = AgentRunSession.create(ledger=ledger, question='q', autoplay=True)
    session.budget, session.budget_scope = budget, ('c', 'e', 1)
    settings = build_agent_model_settings(base_url='http://localhost:11434/v1', model='test')
    with pytest.raises(RuntimeError, match='scope exit failed'):
        await request_model(Model(), settings, ledger, session, session.messages, [], finalizing=True,
                            reserve=0, research_deadline=900, cycle=1, logger=FakeLogger())
    assert budget.settlements == 1

@pytest.mark.asyncio
async def test_admission_failure_does_not_repeat_its_pending_settlement():
    class Budget:
        blocked = False
        def __init__(self): self.settlements = 0
        async def status(self, scope): return {}
        async def reserve(self, *args, **kwargs): return 'reservation'
        async def begin_dispatch(self, ident):
            self.settlements += 1
            raise OSError('settlement failed inside admission')
        async def settle(self, *args, **kwargs): self.settlements += 1
    budget = Budget()
    ledger = AgentAuditLedger('q')
    session = AgentRunSession.create(ledger=ledger, question='q', autoplay=True)
    session.budget, session.budget_scope = budget, ('c', 'e', 1)
    settings = build_agent_model_settings(base_url='http://localhost:11434/v1', model='test')
    with pytest.raises(OSError, match='settlement failed'):
        await request_model(SimpleNamespace(), settings, ledger, session, session.messages, [], finalizing=True,
                            reserve=0, research_deadline=900, cycle=1, logger=FakeLogger())
    assert budget.settlements == 1

@pytest.mark.asyncio
@pytest.mark.parametrize('exit_kind', ['error', 'cancel'])
async def test_unbudgeted_scope_exit_retains_received_response(exit_kind):
    from ..message_contracts import AssistantTurn
    class Model:
        @asynccontextmanager
        async def request_scope(self):
            yield
            if exit_kind == 'cancel': raise asyncio.CancelledError()
            raise RuntimeError('scope exit failed')
        async def complete(self, *args, **kwargs):
            return AssistantTurn('provider-id', 'test', 'stop', 'retained answer', usage={'prompt_tokens': 7, 'completion_tokens': 3})
    ledger = AgentAuditLedger('q')
    session = AgentRunSession.create(ledger=ledger, question='q')
    settings = build_agent_model_settings(base_url='http://localhost:11434/v1', model='test')
    with pytest.raises(asyncio.CancelledError if exit_kind == 'cancel' else RuntimeError):
        await request_model(Model(), settings, ledger, session, session.messages, [], finalizing=True,
                            reserve=0, research_deadline=900, cycle=1, logger=FakeLogger())
    assert session.received_model_turn.content == 'retained answer'
    assert session.model_receipt_unconfirmed is True
    from ..agent.runtime.loop import AgentRuntime
    retained = AgentRuntime._attach_handoff({'status': 'degraded'}, session)
    assert retained['received_model_response']['content'] == 'retained answer'
    assert retained['model_response_recording'] == 'unconfirmed'

@pytest.mark.asyncio
async def test_unbudgeted_success_clears_temporary_response_marker():
    from ..message_contracts import AssistantTurn
    class Model:
        async def complete(self, *args, **kwargs):
            return AssistantTurn('id', 'test', 'stop', 'success', usage={'prompt_tokens': 7, 'completion_tokens': 3})
    ledger = AgentAuditLedger('q')
    session = AgentRunSession.create(ledger=ledger, question='q')
    settings = build_agent_model_settings(base_url='http://localhost:11434/v1', model='test')
    result = await request_model(Model(), settings, ledger, session, session.messages, [], finalizing=True,
                                 reserve=0, research_deadline=900, cycle=1, logger=FakeLogger())
    assert result.turn.content == 'success'
    assert session.model_receipt_unconfirmed is False

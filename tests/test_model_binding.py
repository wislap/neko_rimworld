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
        finalizing=True, reserve=0, research_deadline=900, cycle=1, request_sha256="", logger=FakeLogger()))
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
                            reserve=0, research_deadline=900, cycle=1, request_sha256='', logger=FakeLogger())
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
        finalizing=True, reserve=0, research_deadline=900, cycle=1, request_sha256='', logger=FakeLogger()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    release.set()
    await asyncio.wait_for(finished.wait(), 1)

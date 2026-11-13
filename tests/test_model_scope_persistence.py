import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from ..agent.runtime.audit import AgentAuditLedger, AgentAuditRegistry
from ..agent.runtime.frontdesk_service import FrontdeskService
from ..agent.runtime.loop import AgentRuntime
from ..message_contracts import AssistantTurn, ToolCall
from .consultation_setup import semantic_tool_provider
from .test_agent import FakeLogger, settings


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_kind', ['error', 'cancel'])
async def test_frontdesk_persists_unbudgeted_scope_exit_response(tmp_path, exit_kind):
    class Model:
        calls = 0
        @asynccontextmanager
        async def request_scope(self):
            yield
            if exit_kind == 'cancel':
                raise asyncio.CancelledError()
            raise RuntimeError('scope exit failed')
        async def complete(self, messages, tools, **kwargs):
            self.calls += 1
            return AssistantTurn('provider-id', 'test', 'tool_calls', 'retained answer',
                                 (ToolCall('call-1', 'look', '{}'),),
                                 {'prompt_tokens': 7, 'completion_tokens': 3})
    model = Model()
    runtime = AgentRuntime(tools_provider=semantic_tool_provider(), audit_registry=AgentAuditRegistry(),
                           logger=FakeLogger(), model_factory=lambda _: model)
    game = SimpleNamespace(colony_id='c', load_epoch='e', game_tick=1)
    service = FrontdeskService(tmp_path / 'events.sqlite', runtime, game_session=lambda: game)
    try:
        if exit_kind == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await service.consult(game, 'question', settings())
        else:
            result = await service.consult(game, 'question', settings())
            assert result['status'] != 'ok'
        events = await service.call('events_by_kind', 'c', ('agent.run', 'agent.progress'))
        runs = [event for event in events if event['kind'] == 'agent.run']
        assert len(runs) == 1 and model.calls == 1
        run = runs[0]['payload']
        assert run['result']['status'] != 'ok'
        retained = run['result']['received_model_response']
        assert retained['content'] == 'retained answer'
        assert retained['usage']['prompt_tokens'] == 7
        assert retained['usage']['completion_tokens'] == 3
        assert retained['tool_calls'][0]['id'] == 'call-1'
        assert not any((event['payload'].get('stage') == 'tool') for event in events)
        pending = set()
        for message in run['messages']:
            if message['role'] == 'assistant':
                pending.update(call['id'] for call in message.get('tool_calls', []))
            elif message['role'] == 'tool':
                assert message['tool_call_id'] in pending
                pending.remove(message['tool_call_id'])
        assert not pending
    finally:
        await service.close()

@pytest.mark.asyncio
async def test_capacity_after_response_retains_unbudgeted_turn(tmp_path, monkeypatch):
    from ..agent.runtime import audit
    registry = AgentAuditRegistry()
    class Model:
        async def complete(self, messages, tools, **kwargs):
            ledger = next(iter(registry._active.values()))
            monkeypatch.setattr(audit, 'MAX_EVENTS_PER_RUN', len(ledger._events) + 1)
            return AssistantTurn('provider-id', 'test', 'tool_calls', 'retained at capacity',
                                 (ToolCall('call-1', 'look', '{}'),),
                                 {'prompt_tokens': 7, 'completion_tokens': 3})
    runtime = AgentRuntime(tools_provider=semantic_tool_provider(), audit_registry=registry,
                           logger=FakeLogger(), model_factory=lambda _: Model())
    game = SimpleNamespace(colony_id='c', load_epoch='e', game_tick=1)
    service = FrontdeskService(tmp_path / 'events.sqlite', runtime, game_session=lambda: game)
    try:
        result = await service.consult(game, 'question', settings())
        assert result['status'] != 'ok' and result['audit']['error_code'] == 'audit_capacity_reached'
        assert result['received_model_response']['content'] == 'retained at capacity'
        assert result['received_model_response']['usage']['prompt_tokens'] == 7
        runs = await service.call('events_by_kind', 'c', ('agent.run',))
        assert len(runs) == 1 and runs[0]['payload']['result']['received_model_response']['content'] == 'retained at capacity'
    finally:
        await service.close()

@pytest.mark.asyncio
async def test_capacity_after_settled_budget_does_not_change_receipt(tmp_path, monkeypatch):
    from ..agent.runtime import audit
    from ..agent.runtime.session import AgentRunSession
    from ..workspace.budget import BudgetSettings, ColonyBudget
    from ..workspace.journal import Journal
    class Service:
        def __init__(self): self.journal = Journal(tmp_path / 'events.sqlite')
        async def call(self, method, *args, **kwargs): return getattr(self.journal, method)(*args, **kwargs)
    service = Service()
    registry = AgentAuditRegistry()
    class Model:
        async def complete(self, messages, tools, **kwargs):
            ledger = next(iter(registry._active.values()))
            monkeypatch.setattr(audit, 'MAX_EVENTS_PER_RUN', len(ledger._events) + 1)
            return AssistantTurn('provider-id', 'test', 'stop', 'retained at capacity', usage={'prompt_tokens': 7, 'completion_tokens': 3})
    runtime = AgentRuntime(tools_provider=semantic_tool_provider(), audit_registry=registry,
                           logger=FakeLogger(), model_factory=lambda _: Model())
    ledger = AgentAuditLedger('q')
    session = AgentRunSession.create(ledger=ledger, question='q')
    session.budget = ColonyBudget(service, SimpleNamespace(colony_id='c', load_epoch='e', game_tick=1), BudgetSettings())
    session.budget_scope = ('c', 'e', 1)
    try:
        result = await runtime.execute(session, settings())
        status = service.journal.budget_status('c')
        assert result['received_model_response']['content'] == 'retained at capacity'
        assert result['model_response_audit'] == 'capacity_unrecorded'
        assert status['attempted'] == 1 and status['unknown_usage'] == 0
        assert status['input_tokens'] == 7 and session.model_receipt_unconfirmed is False
    finally:
        service.journal.close()

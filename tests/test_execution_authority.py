import asyncio

import pytest

from ..agent.runtime import execution_environment as environment
from ..agent.runtime.audit import AgentAuditLedger
from ..agent.runtime.session import AgentRunSession
from ..tool_contracts import ToolExecutionError, ToolSetup


@pytest.mark.asyncio
@pytest.mark.parametrize(('role', 'paused'), [('frontdesk', True), ('planner', False)])
async def test_halt_during_pause_audit_prevents_native_send(monkeypatch, role, paused):
    entered, release = asyncio.Event(), asyncio.Event()
    active = {'value': True}
    class Port:
        capabilities = {'colony_pause.v1', 'colony_pause.resume_guard.v1'}
        calls = []
        async def set_paused(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {'paused': paused}
    async def persist(payload):
        entered.set()
        await release.wait()
    monkeypatch.setattr(environment, 'semantic_interaction_factory', lambda *args: object())
    port = Port()
    session = AgentRunSession.create(ledger=AgentAuditLedger('q'), question='q')
    session.role, session.explicit_scheduler = role, True
    session.binding_check = lambda: active['value']
    session.persist_progress = persist
    tool = environment.ExecutionEnvironment(ToolSetup((), write_port=port), session, None).write_tools[0]
    args = {'paused': paused, **({'expected_owner': 'planner'} if not paused else {})}
    task = asyncio.create_task(tool.handler(args))
    await entered.wait()
    active['value'] = False
    release.set()
    with pytest.raises(ToolExecutionError) as failure:
        await task
    assert failure.value.code == 'world_changed'
    assert not port.calls and session.write_record_issue is None

@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['commit', 'undo'])
async def test_halt_during_prepared_audit_prevents_write_send(monkeypatch, mode):
    from .test_write_service import IDENTITY, Port, catalog, observation, op
    entered, release = asyncio.Event(), asyncio.Event()
    active = {'value': True}
    async def persist(payload):
        if payload.get('write', {}).get('stage') == 'prepared':
            entered.set()
            await release.wait()
    monkeypatch.setattr(environment, 'semantic_interaction_factory', lambda *args: object())
    port = Port()
    session = AgentRunSession.create(ledger=AgentAuditLedger('q'), question='q')
    session.role = 'planner'
    session.binding_check = lambda: active['value']
    session.observations = [observation()]
    session.persist_progress = persist
    setup = ToolSetup((), write_catalog=catalog(), write_port=port, write_identity=lambda: IDENTITY)
    tools = environment.ExecutionEnvironment(setup, session, None).write_tools
    tool = next(item for item in tools if item.name == mode)
    args = {'token': 'token'} if mode == 'undo' else {'entry_id': 'task', 'ops': [op()]}
    task = asyncio.create_task(tool.handler(args))
    await asyncio.wait_for(entered.wait(), 1)
    active['value'] = False
    release.set()
    with pytest.raises(ToolExecutionError) as failure:
        await task
    assert failure.value.code == 'world_changed'
    assert not port.calls and session.write_record_issue is None

@pytest.mark.asyncio
async def test_halt_during_takeover_guard_prevents_commit_send(monkeypatch):
    from ..workspace import takeovers
    from .test_write_service import IDENTITY, Port, catalog, observation, op
    entered, release = asyncio.Event(), asyncio.Event()
    active = {'value': True}
    class Guard:
        def __init__(self, service, ledger_query, identity, fields_query): self.identity = identity
        async def guard_changeset(self, changeset):
            assert self.identity() == IDENTITY
            entered.set()
            await release.wait()
    monkeypatch.setattr(takeovers, 'TakeoverControl', Guard)
    monkeypatch.setattr(environment, 'semantic_interaction_factory', lambda *args: object())
    port = Port()
    session = AgentRunSession.create(ledger=AgentAuditLedger('q'), question='q')
    session.role, session.explicit_scheduler = 'planner', True
    session.binding_check = lambda: active['value']
    session.takeover_service = object()
    session.observations = [observation()]
    setup = ToolSetup((), write_catalog=catalog(), write_port=port, write_identity=lambda: IDENTITY)
    tool = next(item for item in environment.ExecutionEnvironment(setup, session, None).write_tools if item.name == 'commit')
    task = asyncio.create_task(tool.handler({'entry_id': 'task', 'ops': [op()]}))
    await asyncio.wait_for(entered.wait(), 1)
    active['value'] = False
    release.set()
    with pytest.raises(ToolExecutionError) as failure:
        await task
    assert failure.value.code == 'world_changed' and not port.calls

@pytest.mark.asyncio
async def test_pause_result_recording_failure_preserves_known_receipt(monkeypatch):
    from ..agent.runtime.loop import AgentRuntime
    class Port:
        capabilities = {'colony_pause.v1'}
        async def set_paused(self, *args, **kwargs): return {'paused': True}
    async def persist(payload):
        if 'pause_result' in payload: raise OSError('record failure')
    monkeypatch.setattr(environment, 'semantic_interaction_factory', lambda *args: object())
    session = AgentRunSession.create(ledger=AgentAuditLedger('q'), question='q')
    session.role, session.explicit_scheduler = 'frontdesk', True
    session.persist_progress = persist
    tool = environment.ExecutionEnvironment(ToolSetup((), write_port=Port()), session, None).write_tools[0]
    with pytest.raises(OSError): await tool.handler({'paused': True})
    result = AgentRuntime._attach_handoff({'status': 'degraded'}, session)
    assert result['write_recording'] == {'stage': 'result', 'mode': 'set_paused', 'outcome': 'known', 'needs_reconciliation': True}
    assert result['write_outcomes'][-1]['receipt'] == {'paused': True}

@pytest.mark.asyncio
@pytest.mark.parametrize('failure_kind', ['not_dispatched', 'timeout'])
async def test_pause_transport_preserves_dispatch_boundary(monkeypatch, failure_kind):
    from ..game.bridge import BridgeNotDispatched, BridgeQueryTimeout
    class Port:
        capabilities = {'colony_pause.v1'}
        async def set_paused(self, *args, **kwargs):
            raise BridgeNotDispatched('not queued') if failure_kind == 'not_dispatched' else BridgeQueryTimeout('unknown')
    records = []
    async def persist(payload): records.append(payload)
    monkeypatch.setattr(environment, 'semantic_interaction_factory', lambda *args: object())
    session = AgentRunSession.create(ledger=AgentAuditLedger('q'), question='q')
    session.role, session.explicit_scheduler = 'frontdesk', True
    session.persist_progress = persist
    tool = environment.ExecutionEnvironment(ToolSetup((), write_port=Port()), session, None).write_tools[0]
    if failure_kind == 'not_dispatched':
        with pytest.raises(ToolExecutionError) as error: await tool.handler({'paused': True})
        assert error.value.code == 'write_not_dispatched'
        assert error.value.details['outcome'] == 'not_sent'
        assert session.write_record_issue is None
        assert session.write_outcomes[-1]['outcome'] == 'not_sent'
    else:
        with pytest.raises(BridgeQueryTimeout): await tool.handler({'paused': True})
        assert session.write_record_issue == 'unknown'
        assert session.write_record_context['outcome'] == 'unknown'
    assert records == [{'pause_request': {'paused': True, 'role': 'frontdesk'}}]

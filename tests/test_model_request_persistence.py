import copy
from types import SimpleNamespace

from ..agent.model.model_client import ModelClientError
from ..agent.runtime.audit import AgentAuditLedger, AgentAuditRegistry
from ..agent.runtime.loop import AgentRuntime
from ..agent.runtime.session import AgentRunSession
from ..workspace.runtime import FrontdeskService
from .consultation_setup import semantic_tool_provider
from .test_agent import FakeLogger, settings
from .test_capability_composition import Script, call


async def test_actual_request_is_durable_before_model_dispatch(tmp_path):
    game = SimpleNamespace(colony_id='colony', load_epoch='epoch', game_tick=10)
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            events = await service.call('events', game.colony_id)
            requests = [e['payload']['model_request'] for e in events
                        if e['kind'] == 'agent.progress' and 'model_request' in e['payload']]
            assert len(requests) == 1
            assert requests[0]['messages'] == messages
            assert requests[0]['tools'] == tools
            assert requests[0]['attempt'] == 1
            assert 'base_url' not in requests[0]
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call('submit_response', {'answer': 'Ready'})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / 'events.sqlite', runtime, game_session=lambda: game)
    try:
        result = await service.consult(game, 'Original user words', settings())
        assert result['status'] == 'ok'
        assert len(model.requests) == 1
    finally:
        await service.close()


async def test_unrecordable_request_is_never_dispatched():
    model = Script([call('submit_response', {'answer': 'Should not run'})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    session = AgentRunSession.create(ledger=AgentAuditLedger('Question'), question='Question')
    progress = []
    async def persist(event):
        progress.append(copy.deepcopy(event))
        if 'model_request' in event:
            raise OSError('injected request journal failure')
    session.persist_progress = persist
    result = await runtime.execute(session, settings())
    assert result['status'] != 'ok'
    assert not model.requests
    assert session.model_calls == 0
    assert any('model_request' in event for event in progress)


async def test_retry_attempt_is_persisted_before_its_own_dispatch():
    attempts = []
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            request = attempts[-1]
            assert request['messages'] == messages and request['tools'] == tools
            if request['attempt'] == 1:
                raise ModelClientError('model_transport', 'injected retry')
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call('submit_response', {'answer': 'Retried'})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    session = AgentRunSession.create(ledger=AgentAuditLedger('Question'), question='Question')
    async def persist(event):
        if 'model_request' in event:
            attempts.append(copy.deepcopy(event['model_request']))
    session.persist_progress = persist
    result = await runtime.execute(session, settings())
    assert result['status'] == 'ok'
    assert [event['attempt'] for event in attempts] == [1, 2]
    assert attempts[0]['request_sha256'] == attempts[1]['request_sha256']
    assert session.model_calls == 2

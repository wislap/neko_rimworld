import json

import pytest

from ..agent.runtime.audit import AgentAuditLedger, AgentAuditRegistry
from ..agent.runtime.loop import AgentRuntime
from ..agent.runtime.session import AgentRunSession
from ..agent.write.service import WriteService
from ..game.bridge import BridgeQueryTimeout
from ..message_contracts import AssistantTurn, ToolCall
from ..tool_contracts import ReceiptLost, ToolSpec
from .consultation_setup import semantic_tool_provider
from .test_agent import FakeLogger, settings
from .test_write_service import IDENTITY, Port, catalog, observation, op


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['undo_outcome_unknown', 'write_receipt_lost'])
async def test_unknown_write_stops_model_and_preserves_tool_result(code):
    calls = []
    progress = []
    writes = []
    class LostPort(Port):
        async def commit(self, data):
            writes.append(('commit', data))
            raise ReceiptLost(data['idempotency_key'])
        async def undo(self, token):
            writes.append(('undo', token))
            raise BridgeQueryTimeout('timeout')
    async def record(event): progress.append({'stage': 'write_audit', **event})
    service = WriteService(catalog(), [observation()], LostPort(), lambda: IDENTITY, record=record)
    class Model:
        async def complete(self, messages, tools, **kwargs):
            calls.append('model')
            if calls.count('model') != 1:
                raise AssertionError('model continued after unknown write')
            return AssistantTurn('provider', 'test', 'tool_calls', '',
                (ToolCall('write-1', 'write_test', '{}'),), {'prompt_tokens': 7, 'completion_tokens': 3})
    async def write(args):
        calls.append('write')
        return (await service.undo({'token': 'token-1'}) if code == 'undo_outcome_unknown'
                else await service.commit({'entry_id': 'task', 'ops': [op()]}))
    async def persist(payload): progress.append(payload)
    spec = ToolSpec('write_test', 'fake write', {'type': 'object', 'properties': {}}, write, exclusive=True)
    runtime = AgentRuntime(tools_provider=semantic_tool_provider(), audit_registry=AgentAuditRegistry(),
                           logger=FakeLogger(), model_factory=lambda _: Model())
    ledger = AgentAuditLedger('q')
    session = AgentRunSession.create(ledger=ledger, question='q', capabilities=(spec,))
    session.persist_progress = persist
    result = await runtime.execute(session, settings())
    assert calls == ['model', 'write']
    assert result['status'] != 'ok' and result['audit']['error_code'] == code
    tool = next(item for item in progress if item.get('stage') == 'tool')
    assert tool['tool_result']['error']['code'] == code
    assert tool['tool_result']['error']['details']
    assert len(writes) == (1 if code == 'undo_outcome_unknown' else 2)
    if len(writes) == 2:
        assert writes[0] == writes[1]
    messages = session.run_messages()
    paired = [m for m in messages if m['role'] == 'tool' and m['tool_call_id'] == 'write-1']
    assert len(paired) == 1 and json.loads(paired[0]['content'])['error']['code'] == code

@pytest.mark.parametrize(('stage','mode','event','expected'), [
    ('prepared', 'commit', {}, ('not_sent', False)),
    ('queued', 'undo', {'outcome': 'not_sent'}, ('not_sent', False)),
    ('result', 'commit', {'outcome': 'not_sent'}, ('not_sent', False)),
    ('result', 'commit', {'outcome': 'known', 'receipt': {'status': 'committed'}}, ('known', True)),
    ('result', 'undo', {'outcome': 'unknown'}, ('unknown', True)),
    ('unknown', 'set_paused', {}, ('unknown', True)),
    ('result', 'dry_run', {'outcome': 'unknown'}, ('unknown', False)),
    ('queued', 'commit', {'attempt': 2}, ('unknown', True)),
    ('result', 'commit', {'attempt': 2, 'outcome': 'not_sent'}, ('unknown', True)),
])
def test_recording_issue_has_structured_effect(stage, mode, event, expected):
    from ..agent.runtime.audit import AgentAuditLedger
    from ..agent.runtime.loop import AgentRuntime
    from ..agent.runtime.session import AgentRunSession
    session = AgentRunSession.create(ledger=AgentAuditLedger('q'), question='q')
    session.write_record_issue = stage
    session.write_record_context = {'stage': stage, 'mode': mode, **event}
    result = AgentRuntime._attach_handoff({'status': 'degraded'}, session)
    assert result['write_recording'] == {'stage': stage, 'mode': mode,
        'outcome': expected[0], 'needs_reconciliation': expected[1]}
    if expected[0] == 'not_sent':
        assert 'may have been applied' not in result['answer']

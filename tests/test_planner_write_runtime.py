import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from plugin.plugins.neko_rimworld.agent.runtime.audit import (
    AgentAuditCapacityError,
    AgentAuditLedger,
    AgentAuditRegistry,
)
from plugin.plugins.neko_rimworld.agent.runtime.loop import AgentRuntime
from plugin.plugins.neko_rimworld.agent.runtime.session import AgentRunSession
from plugin.plugins.neko_rimworld.game.observation import Observation
from plugin.plugins.neko_rimworld.game.query_tools import make_game_tools

from .test_agent import FakeLogger, settings
from .test_capability_composition import Script, call
from .test_semantic_tools import public_source


@pytest.mark.asyncio
@pytest.mark.parametrize("request_hint, persist_mode", [(False, "ok"), (True, "ok"), (False, "fail"), (False, "none"), (False, "result_fail"), (False, "capacity"), (False, "cancel"), (False, "retry_fail")])
async def test_planner_runtime_reads_writes_and_delivers_receipt(request_hint, persist_mode):
    source = public_source()
    source['colony_state'] = {'version': 'colony_state.v1', 'operations': [
        {'operation': 'inspect_storage', 'parameters': {'type': 'object', 'properties': {'items': {'type': 'array'}}}}]}
    reads = []
    async def query(request):
        reads.append(copy.deepcopy(request))
        return Observation({'observation_id': 'e1', 'received_at': 'now', 'request': request, 'response': {
            'connection_epoch': 'c', 'colony_id': 'colony', 'load_epoch': 'epoch', 'game_tick': 1,
            'snapshot_sequence': 1, 'payload': {'status': 'ok', 'issues': [], 'data': {'objects': [
                {'id': 's', 'map_id': 'm', 'priority': 'Normal', 'status': 'observed'}]}}}})
    setup = make_game_tools(source, query)
    catalog = json.loads((Path(__file__).parent / 'fixtures/colony-write-v1/catalog.json').read_text(encoding='utf-8'))
    class Port:
        async def commit(self, changeset):
            self.changeset = changeset
            if persist_mode == 'retry_fail':
                from plugin.plugins.neko_rimworld.tool_contracts import ReceiptLost
                raise ReceiptLost(changeset['idempotency_key'])
            return {'status': 'committed', 'readback': 'Critical'}
    port = Port()
    setup = replace(setup, write_catalog=catalog, write_port=port, write_identity=lambda: ('c', 'colony', 'epoch'))
    async def provide():
        return setup
    progress = []
    async def persist(event):
        progress.append(copy.deepcopy(event))
        if persist_mode == "fail" and event.get("write", {}).get("stage") == "prepared":
            raise RuntimeError("storage unavailable")
        if persist_mode == "retry_fail" and event.get("write", {}).get("stage") == "retry":
            raise RuntimeError("storage unavailable")
        if event.get("write", {}).get("stage") == "result":
            if persist_mode == "result_fail":
                raise RuntimeError("storage unavailable")
            if persist_mode == "cancel":
                import asyncio
                raise asyncio.CancelledError
    write = {'entry_id': 'task', 'ops': [{'op': 'set_storage_priority', 'storage': 's', 'map': 'm', 'value': 'Critical'}]}
    class HintScript(Script):
        async def complete(self, messages, tools, *, timeout):
            if request_hint and len(self.requests) == 1:
                result = json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))
                assert result['error']['code'] == 'evidence_required'
                hint = result['error']['details']['reads'][0]['read']
                self.turns[0] = call(hint['tool'], hint['arguments'])
            return await super().complete(messages, tools, timeout=timeout)
    turns = [call('look', {'object': {'kind': 'storage', 'id': 's', 'map': 'm'}}),
             call('commit', write), call('submit_response', {'answer': 'Storage priority is Critical.'})]
    if request_hint:
        turns.insert(0, call('commit', write))
    model = HintScript(turns)
    session = AgentRunSession.create(ledger=AgentAuditLedger('plan'), question='plan', colony_id='colony', role='planner')
    session.persist_progress = persist if persist_mode != "none" else None
    if persist_mode == 'capacity':
        original_record = session.ledger.record
        def record(event, data=None):
            if event == 'write_result':
                raise AgentAuditCapacityError('full')
            return original_record(event, data)
        session.ledger.record = record
    runtime = AgentRuntime(tools_provider=provide, audit_registry=AgentAuditRegistry(), logger=FakeLogger(), model_factory=lambda _: model)
    if persist_mode == 'cancel':
        import asyncio
        with pytest.raises(asyncio.CancelledError):
            await runtime.execute(session, settings())
        result = session.ledger.snapshot()['result']
    else:
        result = await runtime.execute(session, settings())
    assert result['status'] == ('cancelled' if persist_mode == 'cancel' else 'degraded' if persist_mode in ('fail', 'result_fail', 'capacity', 'retry_fail') else 'ok'), (result, progress)
    assert reads == [{'operation': 'inspect_storage', 'arguments': {'ids': ['s'], 'map': 'm'}}]
    events = session.ledger.snapshot()['events']
    prepared = next(event['data'] for event in events if event['event'] == 'write_prepared')
    changeset = prepared['changeset']
    assert changeset['ops'][0]['expect'] == 'Normal' and changeset['ops'][0]['evidence'] == 'e1'
    assert changeset['load_epoch'] == 'epoch' and changeset['idempotency_key']
    assert prepared['catalog_id'] == catalog['catalog_id']
    assert changeset['idempotency_key'] not in str(model.requests)
    assert len(model.requests) == (2 if persist_mode in ('fail', 'result_fail', 'capacity', 'cancel', 'retry_fail') else 4 if request_hint else 3)
    if persist_mode == 'fail':
        assert not hasattr(port, 'changeset')
        assert not any(event['event'] == 'write_result' for event in events)
    elif persist_mode == 'retry_fail':
        assert port.changeset == changeset
        assert 'may have been applied' in result['answer']
        assert not result.get('write_outcomes')
    elif persist_mode in ('result_fail', 'capacity', 'cancel'):
        assert port.changeset == changeset
        assert result['write_outcomes'][0]['receipt']['readback'] == 'Critical'
        assert 'receipt' in result['answer']
    else:
        assert port.changeset == changeset
        assert any(event['event'] == 'write_result' and event['data']['receipt']['readback'] == 'Critical' for event in events)
        assert 'Critical' in str(model.requests[3 if request_hint else 2]['messages'])
        if persist_mode == 'ok':
            assert any(event.get('write') == prepared for event in progress)
            assert any('Critical' in str(event.get('tool_result')) for event in progress)
    assert 'commit' in {t['function']['name'] for t in model.requests[0]['tools']}
    from plugin.plugins.neko_rimworld.agent.runtime.prompt import build_system_prompt
    prompt = model.requests[0]['messages'][0]['content']
    assert prompt == build_system_prompt(role='planner')
    assert all(part in prompt for part in ('Carry out the bounded planning task', 'evidence_required',
               'need not ask the user to confirm each commit', 'receipt and readback',
               'not proof that a job ran', 'Finish through submit_response', 'Ordinary JSON'))
    assert all(part not in prompt for part in ('read-only RimWorld domain agent',
               'Do not investigate unasked prerequisites', 'recommendation conditional',
               'No long-term memory, game mutation or autonomous action.'))

@pytest.mark.asyncio
async def test_frontdesk_never_registers_write_tools():
    source = public_source()
    async def query(_):
        raise AssertionError('no reads')
    catalog = json.loads((Path(__file__).parent / 'fixtures/colony-write-v1/catalog.json').read_text(encoding='utf-8'))
    setup = replace(make_game_tools(source, query), write_catalog=catalog, write_port=object(),
                    write_identity=lambda: ('c', 'colony', 'epoch'))
    async def provide():
        return setup
    model = Script([call('submit_response', {'answer': 'Done.'})])
    runtime = AgentRuntime(tools_provider=provide, audit_registry=AgentAuditRegistry(), logger=FakeLogger(), model_factory=lambda _: model)
    result = await runtime.execute(AgentRunSession.create(ledger=AgentAuditLedger('ask'), question='ask'), settings())
    assert result['status'] == 'ok', result
    assert not {'dry_run', 'commit', 'undo'} & {t['function']['name'] for t in model.requests[0]['tools']}

@pytest.mark.asyncio
async def test_all_catalog_hints_dispatch_through_real_look():
    from plugin.plugins.neko_rimworld.agent.runtime.execution_environment import ExecutionEnvironment
    from plugin.plugins.neko_rimworld.agent.write.service import WriteService
    from plugin.plugins.neko_rimworld.tool_contracts import ToolExecutionError

    directory = json.loads((Path(__file__).parent / 'fixtures/colony-write-v1/catalog.json').read_text(encoding='utf-8'))
    source = public_source()
    source['colony_state'] = {'version': 'colony_state.v1', 'operations': [
        {'operation': name, 'parameters': {'type': 'object', 'properties': {'items': {'type': 'array'}}}}
        for name in ('inspect_storage', 'inspect_research')]}
    reads = []
    async def query(request):
        reads.append(copy.deepcopy(request))
        return Observation({'observation_id': 'e', 'received_at': 'now', 'request': request, 'response': {
            'connection_epoch': 'c', 'colony_id': 'colony', 'load_epoch': 'epoch', 'game_tick': 1,
            'snapshot_sequence': 1, 'payload': {'status': 'ok', 'issues': [], 'data': {'objects': []}}}})
    setup = make_game_tools(source, query)
    for descriptor in directory['operations']:
        schema = descriptor['schema']
        values = {'string': 'x', 'integer': 1, 'boolean': True, 'object': {'suspended': True}}
        op = {key: prop.get('const', values.get(prop.get('type') if isinstance(prop.get('type'), str) else 'string', 'x'))
              for key, prop in schema['properties'].items() if key in schema['required']}
        if descriptor['op'] == 'set_allowed_area':
            op['value'] = {'map_id': 'x', 'area_id': None}
        session = AgentRunSession.create(ledger=AgentAuditLedger('plan'), question='plan', role='planner')
        service = WriteService(directory, session.observations, object(), lambda: ('c', 'colony', 'epoch'))
        reads.clear()
        with pytest.raises(ToolExecutionError) as missing:
            service._prepare({'entry_id': 'task', 'ops': [op]})
        assert not reads
        hint = missing.value.details['reads'][0]['read']
        ExecutionEnvironment(setup, session, lambda *_: None)
        look = next(tool for tool in session.interaction.tools() if tool.name == hint['tool'])
        reads.clear()
        try:
            await look.handler(hint['arguments'])
        except ToolExecutionError:
            pass
        assert reads and reads[0]['operation'] == descriptor['observation']['read']['native_operation'], descriptor['op']



def test_frontdesk_prompt_matches_baseline_bytes():
    from plugin.plugins.neko_rimworld.agent.runtime.prompt import build_system_prompt
    assert hashlib.sha256(build_system_prompt().encode()).hexdigest() == '7e59b28cc9d455607915604e90cef3252388c0b00d5b69091c76fa2cb3a33a78'
    assert hashlib.sha256(build_system_prompt(persistent_work=True).encode()).hexdigest() == '452e644fdc11234a1fc17b2cf00a275492f9090b233ef13ef44230bf2ed53a32'

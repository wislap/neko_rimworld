import copy

import pytest

from ..agent.write.service import WriteService
from ..game.write_catalog import catalog_digest
from ..tool_contracts import ToolExecutionError

IDENTITY = ('connection', 'colony', 'a' * 32)


def catalog():
    directory = {'capability': 'colony_write.transactions.v1', 'version': 'colony_write.v1', 'modes': ['dry_run', 'commit', 'undo'], 'operations': [{
        'op': 'set_care',
        'schema': {'type': 'object', 'additionalProperties': False, 'properties': {
            'op': {'const': 'set_care'}, 'pawn': {'type': 'string', 'minLength': 1}, 'value': {'type': 'string'}},
            'required': ['op', 'pawn', 'value']},
        'observation': {'read': {'tool': 'look', 'native_operation': 'inspect_state', 'arguments': {'targets': [{'id': {'arg': 'pawn'}}]}},
                        'extract': {'kind': 'field', 'objects_path': ['objects'], 'identity_path': ['target', 'id'],
                                    'status_path': ['facets', 'work', 'status'], 'object_id': {'arg': 'pawn'},
                                    'path': ['facets', 'work', 'medical_care'], 'allow_null': False}}}]}
    directory['catalog_id'] = catalog_digest(directory)
    return directory


def observation():
    return {'observation_id': 'original', 'request': {'operation': 'inspect_state'},
            'response': {'connection_epoch': IDENTITY[0], 'colony_id': IDENTITY[1], 'load_epoch': IDENTITY[2],
                         'payload': {'status': 'partial', 'data': {'objects': [{'target': {'id': 'Pawn1'},
                             'facets': {'work': {'status': 'observed', 'medical_care': 'old'}}}]}}}}


class Port:
    def __init__(self):
        self.calls = []

    async def commit(self, data):
        self.calls.append(copy.deepcopy(data))
        return {'status': 'conflict', 'observed': 'new'}

    async def dry_run(self, data):
        self.calls.append(copy.deepcopy(data))
        return {'status': 'ok', 'preview': []}

    async def undo(self, token):
        self.calls.append(token)
        return {'status': 'undone', 'readback': []}


def op():
    return {'op': 'set_care', 'pawn': 'Pawn1', 'value': 'new'}


async def test_raw_evidence_and_receipts():
    port = Port()
    service = WriteService(catalog(), [observation()], port, lambda: IDENTITY)
    assert [s['function']['name'] for s in service.schemas()] == ['dry_run', 'commit', 'undo']
    assert (await service.commit({'entry_id': 'task', 'ops': [op()]}))['status'] == 'conflict'
    assert port.calls[0]['ops'][0] == {**op(), 'expect': 'old', 'evidence': 'original'}
    assert port.calls[0]['author'] == 'planner'
    assert (await service.undo({'token': 'receipt'}))['status'] == 'undone'


async def test_atomic_missing_forgery_and_identity():
    port = Port()
    identity = list(IDENTITY)
    service = WriteService(catalog(), [observation()], port, lambda: identity)
    with pytest.raises(ToolExecutionError) as error:
        await service.commit({'entry_id': 'task', 'ops': [op(), {**op(), 'pawn': 'missing'}]})
    assert error.value.code == 'evidence_required'
    assert error.value.details['reads'][0]['read']['arguments']['targets'][0]['id'] == 'missing'
    with pytest.raises(ToolExecutionError):
        await service.commit({'entry_id': 'task', 'ops': [{**op(), 'expect': 'fake'}]})
    identity[0] = 'new connection'
    with pytest.raises(ToolExecutionError):
        await service.commit({'entry_id': 'task', 'ops': [op()]})
    assert not port.calls


async def test_missing_null_and_old_epoch_are_unknown():
    for mutation in ('null', 'missing', 'epoch'):
        obs = observation()
        work = obs['response']['payload']['data']['objects'][0]['facets']['work']
        if mutation == 'null':
            work['medical_care'] = None
        elif mutation == 'missing':
            del work['medical_care']
        else:
            obs['response']['load_epoch'] = 'b' * 32
        service = WriteService(catalog(), [obs], Port(), lambda: IDENTITY)
        with pytest.raises(ToolExecutionError) as error:
            await service.dry_run({'entry_id': 'task', 'ops': [op()]})
        assert error.value.code == 'evidence_required'


def test_advertised_operations_generate_valid_raw_evidence():
    import json
    from pathlib import Path

    directory = json.loads((Path(__file__).parent / 'fixtures/colony-write-v1/catalog.json').read_text(encoding='utf-8'))
    for descriptor in directory['operations']:
        schema = descriptor['schema']
        values = {'string': 'x', 'integer': 1, 'boolean': True, 'object': {'suspended': True}}
        op = {key: prop.get('const', values.get(prop.get('type') if isinstance(prop.get('type'), str) else 'string', 'x')) for key, prop in schema['properties'].items() if key in schema['required']}
        if descriptor['op'] == 'set_allowed_area':
            op['value'] = {'map_id': 'x', 'area_id': None}
        elif descriptor['op'] == 'add_production_bill':
            op['value'] = {'recipe': 'Meal'}
        extract = descriptor['observation']['extract']
        def resolve(item):
            return op[item['arg']] if isinstance(item, dict) and 'arg' in item else item
        def put(root, path, value):
            for segment in path[:-1]:
                root = root.setdefault(segment, {})
            root[path[-1]] = value
        row = {}
        if extract['identity_path']:
            put(row, extract['identity_path'], resolve(extract['object_id']))
        if extract['status_path']:
            put(row, extract['status_path'], 'observed')
        for match in extract.get('match', []):
            put(row, match['path'], resolve(match['value']))
        expected = op['value']
        if extract['kind'] == 'keyed_row':
            expected = [{extract['key_field']: resolve(extract['key']), extract['value_field']: op['value']}]
        elif extract['kind'] == 'object_fields':
            expected = [{extract['key_field']: resolve(extract['key']), **extract.get('row_require', {}), **op['value']}]
        elif extract['kind'] == 'ordered_ids':
            expected = [{extract['id_field']: 'Bill2'}, {extract['id_field']: 'Bill1'}]
            put(row, extract['complete_path'], True)
            for rule in extract['require']:
                put(row, rule['path'], rule['value'])
            available = extract['available']
            put(row, available['path'], [{available['key_field']: 'Meal', available['value_field']: True}])
        put(row, extract['path'], expected)
        data = {'objects': [row]} if extract['objects_path'] else row
        obs = observation()
        obs['request']['operation'] = descriptor['observation']['read']['native_operation']
        obs['response']['payload'] = {'status': 'ok', 'data': data, 'issues': []}
        service = WriteService(directory, [obs], Port(), lambda: IDENTITY)
        result = service._prepare({'entry_id': 'task', 'ops': [op]})
        wanted = {extract['expect_field']: ['Bill2', 'Bill1']} if extract['kind'] == 'ordered_ids' else op['value']
        assert result['ops'][0]['expect'] == wanted, descriptor['op']
        broken = copy.deepcopy(obs)
        broken['response']['payload']['data'] = {}
        service = WriteService(directory, [obs, broken], Port(), lambda: IDENTITY)
        with pytest.raises(ToolExecutionError) as error:
            service._prepare({'entry_id': 'task', 'ops': [op]})
        assert error.value.code == 'evidence_required', descriptor['op']




async def test_newest_unknown_does_not_fall_back_and_unrelated_target_does():
    older = observation()
    newer = copy.deepcopy(older)
    newer['observation_id'] = 'new'
    del newer['response']['payload']['data']['objects'][0]['facets']['work']['medical_care']
    service = WriteService(catalog(), [older, newer], Port(), lambda: IDENTITY)
    with pytest.raises(ToolExecutionError) as error:
        await service.commit({'entry_id': 'task', 'ops': [op()]})
    assert error.value.code == 'evidence_required'
    newer['request']['arguments'] = {'targets': [{'id': 'another'}]}
    result = service._prepare({'entry_id': 'task', 'ops': [op()]})
    assert result['ops'][0]['evidence'] == 'original'


@pytest.mark.parametrize('issue_path', ['row', 'facets', 'work'])
async def test_issues_on_field_ancestors_reject(issue_path):
    obs = observation()
    row = obs['response']['payload']['data']['objects'][0]
    node = row if issue_path == 'row' else row['facets'] if issue_path == 'facets' else row['facets']['work']
    node['issues'] = [{'code': 'unavailable'}]
    service = WriteService(catalog(), [obs], Port(), lambda: IDENTITY)
    with pytest.raises(ToolExecutionError):
        await service.commit({'entry_id': 'task', 'ops': [op()]})


@pytest.mark.parametrize(('old', 'new'), [('ResearchA', None), (None, 'ResearchB')])
def test_nullable_research_old_and_new_types(old, new):
    import json
    from pathlib import Path
    directory = json.loads((Path(__file__).parent / 'fixtures/colony-write-v1/catalog.json').read_text(encoding='utf-8'))
    obs = observation()
    obs['request']['operation'] = 'inspect_research'
    obs['response']['payload'] = {'status': 'ok', 'data': {'current_regular_project': old}, 'issues': []}
    service = WriteService(directory, [obs], Port(), lambda: IDENTITY)
    result = service._prepare({'entry_id': 'task', 'ops': [{'op': 'set_research', 'research': 'current', 'value': new}]})
    assert result['ops'][0]['expect'] == old



def test_all_advertised_read_hints_validate_against_actual_look_schema():
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from ..agent.world.look_tools import look_spec
    from .test_semantic_tools import public_source

    fixture = Path(__file__).parent / 'fixtures'
    directory = json.loads((fixture / 'colony-write-v1/catalog.json').read_text(encoding='utf-8'))
    surface = public_source()
    surface['colony_state'] = json.loads((fixture / 'colony_state_catalog.json').read_text(encoding='utf-8'))
    surface['colony_state']['operations'].append({'operation': 'inspect_storage', 'parameters': {'properties': {'items': {}}}})
    schema = look_spec(surface)['parameters']
    for descriptor in directory['operations']:
        properties = descriptor['schema']['properties']
        arguments = {name: 'x' for name in properties}
        hint = descriptor['observation']['read']['arguments']
        from ..agent.write.service import _substitute
        errors = list(Draft202012Validator(schema).iter_errors(_substitute(hint, arguments)))
        assert not errors, (descriptor['op'], [e.message for e in errors])



def test_catalog_digest_and_invalid_contracts_fail_closed():
    import json
    from pathlib import Path

    from ..game.write_catalog import validate_write_catalog

    path = Path(__file__).parent / 'fixtures/colony-write-v1/catalog.json'
    directory = json.loads(path.read_text(encoding='utf-8'))
    validate_write_catalog(directory)
    assert catalog_digest(directory) == directory['catalog_id']
    for mutation in ('digest', 'capability', 'version', 'modes', 'schema'):
        altered = copy.deepcopy(directory)
        if mutation == 'digest':
            altered['operations'][0]['op'] = 'changed'
        elif mutation == 'capability':
            altered['capability'] = 'unknown'
        elif mutation == 'version':
            altered['version'] = 'unknown'
        elif mutation == 'modes':
            altered['modes'] = ['commit']
        else:
            altered['operations'][0]['schema']['additionalProperties'] = True
        if mutation != 'digest':
            altered['catalog_id'] = catalog_digest(altered)
        with pytest.raises(ValueError):
            validate_write_catalog(altered)


@pytest.mark.parametrize('marker', [{'truncated': True}, {'coverage': {'complete': False}}, {'page': {'next_offset': 10}}])
async def test_incomplete_field_ancestors_reject(marker):
    obs = observation()
    obs['response']['payload']['data']['objects'][0]['facets']['work'].update(marker)
    service = WriteService(catalog(), [obs], Port(), lambda: IDENTITY)
    with pytest.raises(ToolExecutionError) as error:
        await service.dry_run({'entry_id': 'task', 'ops': [op()]})
    assert error.value.code == 'evidence_required'


async def test_lost_receipt_retries_identical_changeset_once_and_audits():
    from ..tool_contracts import ReceiptLost

    class DeduplicatingPort(Port):
        effects = 0
        seen = set()

        async def commit(self, data):
            self.calls.append(copy.deepcopy(data))
            key = data['idempotency_key']
            if key not in self.seen:
                self.seen.add(key)
                self.effects += 1
                raise ReceiptLost(key)
            return {'status': 'committed', 'undo_token': 'original'}

    events = []
    async def record(event):
        events.append(event)
    port = DeduplicatingPort()
    service = WriteService(catalog(), [observation()], port, lambda: IDENTITY, record=record)
    assert (await service.commit({'entry_id': 'task', 'ops': [op()]}))['undo_token'] == 'original'
    assert len(port.calls) == 2 and port.calls[0] == port.calls[1] and port.effects == 1
    assert [event['stage'] for event in events] == ['prepared', 'retry', 'result']
    assert events[0]['changeset']['ops'][0]['evidence'] == 'original'
    assert events[0]['changeset'] == port.calls[0]


async def test_two_lost_receipts_report_same_key_and_unknown_outcome():
    from ..tool_contracts import ReceiptLost

    class LostPort(Port):
        async def commit(self, data):
            self.calls.append(copy.deepcopy(data))
            raise ReceiptLost(data['idempotency_key'])
    port = LostPort()
    service = WriteService(catalog(), [observation()], port, lambda: IDENTITY)
    with pytest.raises(ToolExecutionError) as error:
        await service.commit({'entry_id': 'task', 'ops': [op()]})
    assert error.value.code == 'write_receipt_lost'
    assert port.calls[0] == port.calls[1]
    assert error.value.details == {key: port.calls[0][key] for key in ('changeset_id', 'idempotency_key')}


async def test_conflict_no_retry_and_audit_failure_prevents_send():
    port = Port()
    service = WriteService(catalog(), [observation()], port, lambda: IDENTITY)
    assert (await service.commit({'entry_id': 'task', 'ops': [op()]}))['status'] == 'conflict'
    assert len(port.calls) == 1
    async def fail(_event):
        raise RuntimeError('audit failed')
    port = Port()
    service = WriteService(catalog(), [observation()], port, lambda: IDENTITY, record=fail)
    with pytest.raises(RuntimeError, match='audit failed'):
        await service.commit({'entry_id': 'task', 'ops': [op()]})
    assert not port.calls


async def test_cancel_and_changed_epoch_never_retry():
    import asyncio

    from ..tool_contracts import ReceiptLost

    identity = list(IDENTITY)
    class FailingPort(Port):
        async def commit(self, data):
            self.calls.append(copy.deepcopy(data))
            if len(self.calls) == 1 and identity[2] == IDENTITY[2]:
                identity[2] = 'b' * 32
                raise ReceiptLost(data['idempotency_key'])
            raise AssertionError('must not retry')
    port = FailingPort()
    service = WriteService(catalog(), [observation()], port, lambda: identity)
    with pytest.raises(ToolExecutionError) as error:
        await service.commit({'entry_id': 'task', 'ops': [op()]})
    assert error.value.code == 'world_changed' and len(port.calls) == 1

    class CancelPort(Port):
        async def commit(self, data):
            self.calls.append(data)
            raise asyncio.CancelledError()
    port = CancelPort()
    service = WriteService(catalog(), [observation()], port, lambda: IDENTITY)
    with pytest.raises(asyncio.CancelledError):
        await service.commit({'entry_id': 'task', 'ops': [op()]})
    assert len(port.calls) == 1


@pytest.mark.parametrize('mode', ['commit', 'dry_run', 'undo'])
async def test_result_audit_failure_carries_known_receipt(mode):
    async def record(event):
        if event['stage'] == 'result':
            raise OSError('audit unavailable')
    port = Port()
    service = WriteService(catalog(), [observation()], port, lambda: IDENTITY, record=record)
    arguments = {'token': 'original'} if mode == 'undo' else {'entry_id': 'task', 'ops': [op()]}
    with pytest.raises(ToolExecutionError) as error:
        await getattr(service, mode)(arguments)
    assert error.value.code == 'write_audit_failed' and not error.value.recoverable
    assert error.value.details['outcome'] == 'known'
    assert error.value.details['receipt']['status'] == {'commit': 'conflict', 'dry_run': 'ok', 'undo': 'undone'}[mode]
    assert len(port.calls) == 1


@pytest.mark.parametrize('failed_stage', ['retry', 'result'])
async def test_lost_receipt_audit_failure_carries_unknown_identity(failed_stage):
    from ..tool_contracts import ReceiptLost

    class LostPort(Port):
        async def commit(self, data):
            self.calls.append(copy.deepcopy(data))
            raise ReceiptLost(data['idempotency_key'])
    async def record(event):
        if event['stage'] == failed_stage:
            raise OSError('audit unavailable')
    port = LostPort()
    service = WriteService(catalog(), [observation()], port, lambda: IDENTITY, record=record)
    with pytest.raises(ToolExecutionError) as error:
        await service.commit({'entry_id': 'task', 'ops': [op()]})
    assert error.value.code == 'write_audit_failed' and not error.value.recoverable
    assert error.value.details == {'outcome': 'unknown', **{k: port.calls[0][k] for k in ('changeset_id', 'idempotency_key')}}
    assert len(port.calls) == (1 if failed_stage == 'retry' else 2)


async def test_capacity_error_propagates_unchanged_after_receipt():
    from ..agent.runtime.audit import AgentAuditCapacityError

    sentinel = AgentAuditCapacityError('full')
    async def record(event):
        if event['stage'] == 'result':
            raise sentinel
    service = WriteService(catalog(), [observation()], Port(), lambda: IDENTITY, record=record)
    with pytest.raises(AgentAuditCapacityError) as error:
        await service.commit({'entry_id': 'task', 'ops': [op()]})
    assert error.value is sentinel


async def test_native_failure_and_audit_failure_keep_failure_details():
    class FailedPort(Port):
        async def commit(self, data):
            raise ToolExecutionError('busy', 'try later', details={'reason': 'queue'})
    async def record(event):
        if event['stage'] == 'result':
            raise OSError('audit unavailable')
    service = WriteService(catalog(), [observation()], FailedPort(), lambda: IDENTITY, record=record)
    with pytest.raises(ToolExecutionError) as error:
        await service.commit({'entry_id': 'task', 'ops': [op()]})
    assert error.value.details == {'outcome': 'known_failure', 'error': 'busy', 'message': 'try later', 'details': {'reason': 'queue'}}
    assert not error.value.recoverable

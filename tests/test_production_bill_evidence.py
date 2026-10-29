"""Structural writes require complete, ordered originals and native availability."""
import copy

import pytest

from ..agent.write.service import WriteService
from ..game.write_catalog import catalog_digest, validate_write_catalog
from ..game.write_contract import validate_request
from ..tool_contracts import ToolExecutionError
from .test_write_service import IDENTITY, Port, catalog, observation


def directory():
    result = catalog()
    result['operations'] = [{
        'op': 'append_recipe',
        'schema': {'type': 'object', 'additionalProperties': False, 'properties': {
            'op': {'const': 'append_recipe'}, 'worktable': {'type': 'string', 'minLength': 1},
            'value': {'type': 'object', 'additionalProperties': False,
                      'properties': {'recipe': {'type': 'string', 'minLength': 1}}, 'required': ['recipe']}},
            'required': ['op', 'worktable', 'value']},
        'expect_schema': {'type': 'object', 'additionalProperties': False,
                          'properties': {'bill_ids': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}}},
                          'required': ['bill_ids']},
        'observation': {'read': {'tool': 'look', 'native_operation': 'inspect_state', 'arguments': {
            'object': {'kind': 'thing', 'id': {'arg': 'worktable'}}, 'aspects': ['state'],
            'focus': {'state': ['production']}, 'detail': 100}},
            'extract': {'kind': 'ordered_ids', 'object_id': {'arg': 'worktable'},
                        'objects_path': ['objects'], 'identity_path': ['target', 'id'],
                        'status_path': ['facets', 'production', 'status'], 'status_value': 'observed',
                        'path': ['facets', 'production', 'bills'], 'id_field': 'bill_id',
                        'expect_field': 'bill_ids', 'complete_path': ['facets', 'production', 'complete'],
                        'complete_value': True,
                        'require': [{'path': ['facets', 'production', 'bill_owner', 'role'], 'value': 'worktable'}],
                        'available': {'path': ['facets', 'production', 'available_recipes'], 'key_field': 'recipe',
                                      'key': {'value_path': ['recipe']}, 'value_field': 'available_now', 'value': True}}}}]
    result['catalog_id'] = catalog_digest(result)
    return result


def original():
    obs = observation()
    obs['request']['arguments'] = {'targets': [{'id': 'Table1'}]}
    obs['response']['payload']['data'] = {'objects': [{'target': {'id': 'Table1'}, 'facets': {'production': {
        'status': 'observed', 'complete': True, 'bill_owner': {'role': 'worktable'},
        'bills': [{'bill_id': 'Bill2'}, {'bill_id': 'Bill1'}],
        'available_recipes': [{'recipe': 'Meal', 'available_now': True}]}}}]}
    return obs


def arguments():
    return {'entry_id': 'task', 'ops': [{'op': 'append_recipe', 'worktable': 'Table1', 'value': {'recipe': 'Meal'}}]}


def production(obs):
    return obs['response']['payload']['data']['objects'][0]['facets']['production']


async def test_ordered_creation_expect_is_independent_of_intent_and_receipt_is_unmodified():
    port = Port()
    service = WriteService(directory(), [original()], port, lambda: IDENTITY)
    result = await service.commit(arguments())
    assert result == {'status': 'conflict', 'observed': 'new'}
    sent = port.calls[0]['ops'][0]
    assert sent['expect'] == {'bill_ids': ['Bill2', 'Bill1']}
    assert sent['value'] == {'recipe': 'Meal'}
    assert sent['evidence'] == 'original'
    assert 'expect' not in service.schemas()[1]['function']['parameters']['properties']['ops']['items']['oneOf'][0]['properties']
    assert original()['response']['payload']['data']['objects'][0]['facets']['production']['bills'][0]['bill_id'] == 'Bill2'


@pytest.mark.parametrize('fault', [
    'incomplete', 'no_complete', 'numeric_complete', 'unavailable', 'duplicate_recipe', 'recipe_issue',
    'missing_recipe', 'missing_availability', 'numeric_available', 'role', 'owner_issue', 'missing_id',
    'blank_id', 'duplicate_id', 'bill_issue', 'bill_not_object', 'facet_issue', 'ancestor_issue',
    'truncated', 'coverage', 'row_issue', 'old_epoch', 'untrusted_projection',
])
async def test_creation_unknown_never_sends_or_falls_back(fault):
    older, newer = original(), original()
    facet = production(newer)
    if fault == 'incomplete':
        facet['complete'] = False
    elif fault == 'no_complete':
        del facet['complete']
    elif fault == 'numeric_complete':
        facet['complete'] = 1
    elif fault == 'unavailable':
        facet['available_recipes'][0]['available_now'] = False
    elif fault == 'duplicate_recipe':
        facet['available_recipes'] *= 2
    elif fault == 'recipe_issue':
        facet['available_recipes'][0]['issues'] = ['unknown']
    elif fault == 'missing_recipe':
        facet['available_recipes'] = []
    elif fault == 'missing_availability':
        del facet['available_recipes'][0]['available_now']
    elif fault == 'numeric_available':
        facet['available_recipes'][0]['available_now'] = 1
    elif fault == 'role':
        facet['bill_owner']['role'] = 'medical'
    elif fault == 'owner_issue':
        facet['bill_owner']['issues'] = ['unknown']
    elif fault == 'missing_id':
        facet['bills'][0] = {}
    elif fault == 'blank_id':
        facet['bills'][0]['bill_id'] = ' '
    elif fault == 'duplicate_id':
        facet['bills'] *= 2
    elif fault == 'bill_issue':
        facet['bills'][0]['issues'] = ['unknown']
    elif fault == 'bill_not_object':
        facet['bills'][0] = None
    elif fault == 'facet_issue':
        facet['issues'] = ['unknown']
    elif fault == 'ancestor_issue':
        newer['response']['payload']['data']['objects'][0]['facets']['issues'] = ['unknown']
    elif fault == 'truncated':
        facet['truncated'] = True
    elif fault == 'coverage':
        facet['coverage'] = {'complete': False}
    elif fault == 'row_issue':
        newer['response']['payload']['data']['objects'][0]['issues'] = ['unknown']
    elif fault == 'old_epoch':
        older['response']['load_epoch'] = newer['response']['load_epoch'] = 'b' * 32
    else:
        newer['response']['payload']['data'] = {'summary': 'all bills read'}
    port = Port()
    service = WriteService(directory(), [older, newer], port, lambda: IDENTITY)
    with pytest.raises(ToolExecutionError) as failure:
        await service.commit(arguments())
    assert failure.value.code == 'evidence_required'
    assert failure.value.details['reads'][0]['read']['arguments']['object']['id'] == 'Table1'
    assert not port.calls


def test_empty_complete_list_is_valid_and_missing_op_value_is_rejected():
    obs = original()
    production(obs)['bills'] = []
    service = WriteService(directory(), [obs], Port(), lambda: IDENTITY)
    assert service._prepare(arguments())['ops'][0]['expect'] == {'bill_ids': []}
    forged = arguments()
    forged['ops'][0]['expect'] = {'bill_ids': []}
    with pytest.raises(ToolExecutionError):
        service._prepare(forged)


@pytest.mark.parametrize('field', ['complete_path', 'complete_value', 'id_field', 'expect_field', 'require', 'available'])
def test_invalid_structural_descriptors_are_rejected(field):
    data = directory()
    del data['operations'][0]['observation']['extract'][field]
    data['catalog_id'] = catalog_digest(data)
    with pytest.raises(ValueError):
        validate_write_catalog(data)


def test_structural_wire_is_explicit_and_does_not_relax_other_operations():
    data = {'changeset_id': 'c', 'author': 'planner', 'entry_id': 't', 'load_epoch': 'a' * 32,
            'idempotency_key': 'k', 'ops': [{'op': 'add_production_bill', 'worktable': 't',
                                          'value': {'recipe': 'Meal'}, 'expect': {'bill_ids': []}, 'evidence': 'e'}]}
    validate_request('commit', data)
    for mutation in ('other_op', 'extra_target', 'duplicate', 'null_ids', 'blank_recipe', 'extra_value', 'long_table'):
        broken = copy.deepcopy(data)
        op = broken['ops'][0]
        if mutation == 'other_op':
            op['op'] = 'set_bill'
        elif mutation == 'extra_target':
            op['bill'] = 'b'
        elif mutation == 'duplicate':
            op['expect']['bill_ids'] = ['b', 'b']
        elif mutation == 'null_ids':
            op['expect']['bill_ids'] = None
        elif mutation == 'blank_recipe':
            op['value']['recipe'] = ' '
        elif mutation == 'long_table':
            op['worktable'] = 'x' * 257
        else:
            op['value']['suspended'] = True
        with pytest.raises(ValueError):
            validate_request('commit', broken)


@pytest.mark.parametrize('expression', [{'arg': []}, {'arg': None}, {'arg': ''},
                                        {'value_path': 'recipe'}, {'value_path': [1]}, {'value_path': []}])
def test_malformed_structural_expressions_reject_at_negotiation(expression):
    data = directory()
    data['operations'][0]['observation']['extract']['require'][0]['value'] = expression
    data['catalog_id'] = catalog_digest(data)
    with pytest.raises(ValueError):
        validate_write_catalog(data)

"""Verify committed values against original catalog-scoped observations."""
import json

from .service import WriteService, _path, _safe_path, _unsafe


def _equal(a, b):
    return json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(b, ensure_ascii=False, sort_keys=True)


def verify_op(directory, op, observation, connection, colony, epoch, commit_tick, receipt, fields):
    """Return True only for a catalog-extracted current value matching the commit."""
    descriptor = next((x for x in directory['operations'] if x['op'] == op.get('op')), None)
    if not descriptor:
        return False
    response = observation.get('response') or {}
    request = observation.get('request') or {}
    payload = response.get('payload') or {}
    if ((response.get('connection_epoch'), response.get('colony_id'), response.get('load_epoch')) != (connection, colony, epoch)
            or response.get('type') != 'query_result' or not response.get('request_id')
            or type(response.get('game_tick')) is not int or response['game_tick'] <= commit_tick
            or request.get('operation') != descriptor['observation']['read']['native_operation']
            or payload.get('status') not in ('ok', 'partial') or payload.get('issues')):
        return False
    service = WriteService(directory, [observation], None, lambda: (connection, colony, epoch))
    extracted = service._evidence(op, descriptor)
    if extracted is None:
        return False
    value, evidence_id = extracted
    if evidence_id != observation.get('observation_id'):
        return False
    if op.get('op') != 'add_production_bill':
        if 'value' not in op or not _equal(value, op['value']):
            return False
        readback = receipt.get('readback') or []
        if op.get('op') == 'set_bill' and {f['field'].removeprefix('bill:') for f in fields} != set(op['value']):
            return False
        for field in fields:
            matches = [row for row in readback if isinstance(row, dict) and row.get('object') == field['object'] and row.get('field') == field['field'] and 'value' in row]
            if len(matches) != 1:
                return False
            expected = op['value']
            if op.get('op') == 'set_bill':
                name = field['field'].removeprefix('bill:')
                if not field['field'].startswith('bill:') or name not in expected:
                    return False
                expected = expected[name]
            if not _equal(matches[0]['value'], expected):
                return False
        return True
    # The catalog's ordered_ids extractor proves the complete worktable stack;
    # the receipt names the newly allocated bill and its recipe. Confirm both
    # the ordered stack and that bill's current native production row.
    if not isinstance(value, dict) or not isinstance(value.get('bill_ids'), list):
        return False
    readback = receipt.get('readback') or []
    candidates = [x.get('value') for x in readback if isinstance(x, dict) and
                  x.get('object') == op.get('worktable') and any(x.get('object') == f.get('object') and x.get('field') == f.get('field') for f in fields) and isinstance(x.get('value'), dict)]
    if len(candidates) != 1:
        return False
    committed = candidates[0]
    created = committed.get('created') or {}
    bill_id = created.get('bill_id')
    if (not isinstance(bill_id, str) or not bill_id or created.get('recipe') != (op.get('value') or {}).get('recipe')
            or not _equal(committed.get('bill_ids'), value['bill_ids']) or bill_id not in value['bill_ids']):
        return False
    spec = descriptor['observation']['extract']
    data = payload.get('data') or {}
    objects = _path(data, spec['objects_path'])
    if not isinstance(objects, list):
        return False
    rows = [row for row in objects if isinstance(row, dict) and _path(row, spec['identity_path']) == op.get('worktable')]
    if len(rows) != 1:
        return False
    bills = _safe_path(rows[0], spec['path'])
    if not isinstance(bills, list):
        return False
    matches = [bill for bill in bills if isinstance(bill, dict) and bill.get('bill_id') == bill_id and not _unsafe(bill)]
    return len(matches) == 1 and matches[0].get('bill_kind') == 'production' and matches[0].get('recipe') == created['recipe']

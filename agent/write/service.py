"""Prepare planner changes from authenticated, unprojected observations."""
from __future__ import annotations

import copy
import uuid

from ...game.write_catalog import validate_write_catalog
from ...tool_contracts import ReceiptLost, ToolExecutionError
from ..runtime.audit import AgentAuditCapacityError

_MISSING = object()


def _path(value, path):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _unsafe(node):
    if not isinstance(node, dict):
        return False
    if (bool(node.get('issues')) or node.get('status') in {'unavailable', 'partial', 'failed'}
            or node.get('truncated') is True or node.get('complete') is False
            or node.get('has_more') is True or node.get('next_offset') is not None):
        return True
    return any(_unsafe(node.get(key)) for key in ('coverage', 'scope', 'page')
               if isinstance(node.get(key), dict))


def _safe_path(node, path):
    for key in path:
        if _unsafe(node) or not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return _MISSING if _unsafe(node) else node


def _schema_check(value, schema):
    if not isinstance(schema, dict):
        return False
    if 'const' in schema and value != schema['const']:
        return False
    if 'enum' in schema and value not in schema['enum']:
        return False
    kind = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'number': (int, float), 'boolean': bool, 'null': type(None)}
    if isinstance(kind, list):
        return any(_schema_check(value, {**schema, 'type': t}) for t in kind)
    if kind in types and (not isinstance(value, types[kind]) or kind in ('integer', 'number') and isinstance(value, bool)):
        return False
    if isinstance(value, dict):
        props = schema.get('properties', {})
        if len(value) < schema.get('minProperties', 0):
            return False
        if not set(schema.get('required', ())) <= value.keys():
            return False
        if schema.get('additionalProperties') is False and not value.keys() <= props.keys():
            return False
        return all(_schema_check(v, props[k]) for k, v in value.items() if k in props)
    if isinstance(value, list):
        return len(value) >= schema.get('minItems', 0) and len(value) <= schema.get('maxItems', 100000) and all(_schema_check(v, schema.get('items', {})) for v in value)
    if isinstance(value, str) and len(value) < schema.get('minLength', 0):
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get('minimum', float('-inf')) or value > schema.get('maximum', float('inf')):
            return False
    return True


def _substitute(value, op):
    if isinstance(value, dict):
        if set(value) == {'arg'}:
            return op.get(value['arg'])
        if set(value) == {'value_path'}:
            return _path(op.get('value'), value['value_path'])
        return {k: _substitute(v, op) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, op) for v in value]
    return value


class WriteService:
    """A run-scoped service. identity returns connection, colony and load epoch."""

    def __init__(self, catalog, observations, port, identity, *, source_kind='native', record=None, guard=None):
        if source_kind not in {'native', 'scripted_game_world'}:
            raise ToolExecutionError('untrusted_observation_source', 'Writes require native or scripted world observations.')
        directory = catalog.get('colony_write', catalog)
        try:
            validate_write_catalog(directory)
        except ValueError as exc:
            raise ToolExecutionError('invalid_catalog', str(exc), recoverable=False) from exc
        self.directory = copy.deepcopy(directory)
        self.operations = {item['op']: item for item in directory['operations']}
        if len(self.operations) != len(directory['operations']):
            raise ToolExecutionError('invalid_catalog', 'Duplicate write operation.')
        self.observations, self.port, self.identity = observations, port, identity
        self.record = record
        self.source_kind = source_kind
        self.guard = guard
        current = identity()
        self.bound = tuple(current) if current is not None else ()
        if len(self.bound) != 3 or not all(self.bound):
            raise ToolExecutionError('invalid_session', 'A current connection and loaded colony are required.')

    def schemas(self):
        operation_schemas = [copy.deepcopy(item['schema']) for item in self.operations.values()]
        params = {'type': 'object', 'additionalProperties': False,
                  'properties': {'entry_id': {'type': 'string', 'minLength': 1},
                                 'ops': {'type': 'array', 'minItems': 1, 'maxItems': 64,
                                         'items': {'oneOf': operation_schemas}}},
                  'required': ['entry_id', 'ops']}
        undo = {'type': 'object', 'additionalProperties': False, 'properties': {'token': {'type': 'string', 'minLength': 1}}, 'required': ['token']}
        modes = self.directory.get('modes', [])
        return [{'type': 'function', 'function': {'name': name, 'description': description, 'parameters': copy.deepcopy(schema)}}
                for name, description, schema in [('dry_run', 'Validate a planner changeset in the game.', params),
                    ('commit', 'Commit a planner changeset using observed old values.', params),
                    ('undo', 'Undo a committed changeset using its receipt token.', undo)] if name in modes]

    def _current(self):
        current = self.identity()
        if current is None or tuple(current) != self.bound:
            raise ToolExecutionError('world_changed', 'Connection or loaded world changed.', recoverable=False)

    def _evidence(self, op, descriptor):
        spec = descriptor['observation']
        extract = spec['extract']
        target = _substitute(extract.get('object_id'), op)
        for observation in reversed(self.observations):
            raw = observation.to_dict() if hasattr(observation, 'to_dict') else observation
            if not isinstance(raw, dict):
                continue
            response = raw.get('response', {})
            if (response.get('connection_epoch'), response.get('colony_id'), response.get('load_epoch')) != self.bound:
                continue
            if raw.get('request', {}).get('operation') != spec['read'].get('native_operation'):
                continue
            request = raw.get('request', {}).get('arguments', {})
            selectors = request.get('targets', request.get('ids')) if isinstance(request, dict) else None
            if target and isinstance(selectors, list):
                ids = [item.get('id') if isinstance(item, dict) else item for item in selectors]
                if target not in ids:
                    continue
            payload = response.get('payload', {})
            if payload.get('status') not in ('ok', 'partial') or payload.get('issues'):
                return None
            data = payload.get('data', {})
            if _unsafe(data):
                return None
            objects_path = extract.get('objects_path', [])
            objects = _path(data, objects_path)
            if not objects_path and isinstance(objects, dict):
                objects = [objects]
            if not isinstance(objects, list):
                return None
            identity_path = extract.get('identity_path', [])
            rows = [row for row in objects if isinstance(row, dict) and
                    (not identity_path or _path(row, identity_path) == target) and
                    all(_path(row, rule['path']) == _substitute(rule['value'], op)
                        for rule in extract.get('match', []))]
            if len(rows) != 1:
                return None
            row = rows[0]
            path = extract['path']
            status_path = extract.get('status_path', [])
            if status_path:
                if _safe_path(row, status_path) != 'observed':
                    return None
                facet = _path(row, status_path[:-1])
            else:
                if payload.get('status') != 'ok' or payload.get('issues'):
                    return None
                facet = row
            # A path-specific issue invalidates the row; absence is never null.
            if row.get('issues') or (isinstance(facet, dict) and facet.get('issues')) or row.get('status') == 'unavailable':
                return None
            value = _safe_path(row, path)
            if value is _MISSING:
                return None
            kind = extract['kind']
            if kind in ('keyed_row', 'object_fields'):
                if not isinstance(value, list):
                    return None
                matches = [v for v in value if isinstance(v, dict) and v.get(extract['key_field']) == _substitute(extract['key'], op)]
                if len(matches) != 1 or _unsafe(matches[0]):
                    return None
                matched = matches[0]
                if any(matched.get(k) != v for k, v in extract.get('row_require', {}).items()):
                    return None
                if kind == 'keyed_row':
                    value = matched.get(extract['value_field'], _MISSING)
                else:
                    value = {k: matched.get(k, _MISSING) for k in op['value']}
                    if _MISSING in value.values():
                        return None
            elif kind == 'membership':
                if not isinstance(value, list):
                    return None
                present = op.get(extract['item_target']) in value
                if not present and _path(row, extract['complete_path']) is not True:
                    return None
                value = present
            elif kind == 'ordered_ids':
                # Structural creation compares the complete ordered collection,
                # not the creation intent. All prerequisites come from this same
                # original observation, including native recipe availability.
                if (not isinstance(value, list) or
                        _safe_path(row, extract['complete_path']) is not extract['complete_value'] or
                        any(_safe_path(row, rule['path']) != _substitute(rule['value'], op)
                            for rule in extract['require'])):
                    return None
                identifiers = [item.get(extract['id_field']) if isinstance(item, dict) and not _unsafe(item)
                               else None for item in value]
                if (any(not isinstance(item, str) or not item.strip() for item in identifiers) or
                        len(set(identifiers)) != len(identifiers)):
                    return None
                availability = extract['available']
                recipes = _safe_path(row, availability['path'])
                if not isinstance(recipes, list):
                    return None
                matching = [item for item in recipes if isinstance(item, dict) and
                            item.get(availability['key_field']) == _substitute(availability['key'], op)]
                if (len(matching) != 1 or _unsafe(matching[0]) or
                        matching[0].get(availability['value_field']) is not availability['value']):
                    return None
                value = {extract['expect_field']: identifiers}
            elif kind != 'field':
                return None
            value_schema = descriptor.get('expect_schema', descriptor['schema']['properties']['value'])
            if (value is _MISSING or (value is None and not extract.get('allow_null', False))
                    or not _schema_check(value, value_schema)):
                return None
            evidence = raw.get('observation_id')
            if isinstance(evidence, str) and evidence:
                return copy.deepcopy(value), evidence
            return None
        return None

    def _prepare(self, args):
        self._current()
        if not isinstance(args, dict) or set(args) != {'entry_id', 'ops'} or not isinstance(args['entry_id'], str) or not args['entry_id'].strip() or not isinstance(args['ops'], list) or not 1 <= len(args['ops']) <= 64:
            raise ToolExecutionError('invalid_arguments', 'Expected entry_id and one to 64 operations.')
        prepared, missing = [], []
        for op in args['ops']:
            descriptor = self.operations.get(op.get('op')) if isinstance(op, dict) else None
            if (descriptor is None or set(op) & {'expect', 'evidence', 'author', 'changeset_id', 'idempotency_key'}
                    or not _schema_check(op, descriptor['schema'])):
                raise ToolExecutionError('invalid_arguments', 'Operation does not match the advertised directory.')
            evidence = self._evidence(op, descriptor)
            if evidence is None:
                missing.append({'op': op['op'], 'read': _substitute(descriptor['observation']['read'], op)})
                continue
            expected, evidence_id = evidence
            prepared.append({**copy.deepcopy(op), 'expect': expected, 'evidence': evidence_id})
        if missing:
            raise ToolExecutionError('evidence_required', 'Read the current field before writing.', details={'reads': missing})
        return {'changeset_id': uuid.uuid4().hex, 'author': 'planner', 'entry_id': args['entry_id'], 'load_epoch': self.bound[2], 'idempotency_key': uuid.uuid4().hex, 'ops': prepared}

    async def _record(self, event):
        if self.record is not None:
            await self.record(copy.deepcopy(event))

    async def _record_after(self, event, outcome):
        try:
            await self._record(event)
        except AgentAuditCapacityError:
            raise
        except Exception as exc:
            raise ToolExecutionError('write_audit_failed', 'Write audit failed after dispatch.',
                                     recoverable=False, details=copy.deepcopy(outcome)) from exc

    def _scope(self):
        return dict(connection_epoch=self.bound[0], colony_id=self.bound[1], load_epoch=self.bound[2])

    async def _invoke(self, mode, argument, event, attempt):
        from ...game.bridge import BridgeNotDispatched
        from ...game.write_port import BridgeGameWritePort
        context = {**self._scope(), 'origin': self.source_kind, 'mode': mode, 'attempt': attempt}
        if mode == 'undo':
            context['undo_token'] = argument
        else:
            context.update(changeset_id=argument['changeset_id'], idempotency_key=argument['idempotency_key'])
        envelope = None
        async def audit(stage, request_id, response):
            nonlocal envelope
            context['request_id'] = request_id
            if stage == 'queued':
                try:
                    await self._record({'stage': 'queued', **context})
                except Exception as exc:
                    raise ToolExecutionError('write_audit_failed', 'Write audit failed before dispatch.', recoverable=False) from exc
            else:
                envelope = response
        try:
            method = getattr(self.port, mode)
            result = await method(copy.deepcopy(argument), audit=audit) if isinstance(self.port, BridgeGameWritePort) else await method(copy.deepcopy(argument))
        except BridgeNotDispatched as exc:
            await self._record({'stage': 'result', **event, **context, 'error': exc.code,
                                'outcome': 'not_sent', 'transport_state': 'queued_not_sent'})
            raise
        except ReceiptLost:
            if isinstance(self.port, BridgeGameWritePort) or attempt == 2:
                await self._record_after({'stage': 'result', **event, **context, 'error': 'write_receipt_lost', 'outcome': 'unknown', 'transport_state': 'attempted_write'},
                                         {'outcome': 'unknown', **({k: context[k] for k in ('changeset_id', 'idempotency_key')} if mode != 'undo' else {})})
            raise
        except ToolExecutionError as exc:
            if exc.code == 'write_audit_failed':
                raise
            outcome = {'outcome': 'known_failure' if envelope is not None or not isinstance(self.port, BridgeGameWritePort) else 'unknown', 'error': exc.code, 'message': str(exc), 'details': exc.details}
            await self._record_after({'stage': 'result', **event, **context, 'envelope': envelope, 'error': exc.code,
                                      'message': str(exc), 'details': exc.details, 'outcome': outcome['outcome'], 'transport_state': 'confirmed_receipt' if envelope is not None else 'unknown'}, outcome)
            raise
        await self._record_after({'stage': 'result', **event, **context, 'envelope': envelope, 'receipt': result, 'outcome': 'known', 'transport_state': 'confirmed_receipt' if envelope is not None else 'scripted'},
                                 {'outcome': 'known', 'receipt': result})
        return copy.deepcopy(result)

    async def _changeset_call(self, mode, args):
        changeset = self._prepare(args)
        if self.guard is not None:
            await self.guard.guard_changeset(changeset)
        event = {'mode': mode, 'changeset': changeset, 'catalog_id': self.directory['catalog_id'], **self._scope(), 'origin': self.source_kind}
        await self._record({'stage': 'prepared', **event})
        unknown = {'outcome': 'unknown', 'changeset_id': changeset['changeset_id'], 'idempotency_key': changeset['idempotency_key']}
        for attempt in range(1, 3 if mode == 'commit' else 2):
            self._current()
            try:
                return await self._invoke(mode, changeset, event, attempt)
            except ReceiptLost as exc:
                if mode != 'commit' or exc.idempotency_key != changeset['idempotency_key']:
                    raise
                if attempt == 2:
                    raise ToolExecutionError('write_receipt_lost', 'Write outcome is unknown after receipt loss.', details={k: unknown[k] for k in ('changeset_id', 'idempotency_key')}) from exc
                self._current()
                await self._record_after({'stage': 'retry', **event}, unknown)

    async def dry_run(self, args):
        return await self._changeset_call('dry_run', args)

    async def commit(self, args):
        return await self._changeset_call('commit', args)

    async def undo(self, args):
        self._current()
        if not _schema_check(args, {'type': 'object', 'additionalProperties': False, 'properties': {'token': {'type': 'string', 'minLength': 1}}, 'required': ['token']}):
            raise ToolExecutionError('invalid_arguments', 'Expected an undo token.')
        if self.guard is not None:
            await self.guard.guard_undo(args['token'])
        event = {'mode': 'undo', 'token': args['token'], 'catalog_id': self.directory['catalog_id'], **self._scope(), 'origin': self.source_kind}
        await self._record({'stage': 'prepared', **event})
        self._current()
        return await self._invoke('undo', args['token'], event, 1)

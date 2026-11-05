"""Validate the Mod-owned write catalog and its content identity."""
from __future__ import annotations

import hashlib
import json
import re


def catalog_digest(directory):
    body = {key: value for key, value in directory.items() if key != 'catalog_id'}
    encoded = json.dumps(body, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _schema(schema):
    if not isinstance(schema, dict):
        return False
    allowed = {'type', 'const', 'enum', 'properties', 'required', 'additionalProperties', 'items',
               'minLength', 'maxLength', 'minimum', 'maximum', 'minItems', 'maxItems', 'minProperties', 'uniqueItems', 'description'}
    if set(schema) - allowed:
        return False
    if 'description' in schema and not isinstance(schema['description'], str):
        return False
    kind = schema.get('type')
    valid_types = {'object', 'array', 'string', 'integer', 'number', 'boolean', 'null'}
    if kind is not None and (not isinstance(kind, (str, list)) or
                             (isinstance(kind, str) and kind not in valid_types) or
                             (isinstance(kind, list) and (not kind or any(t not in valid_types for t in kind)))):
        return False
    props = schema.get('properties', {})
    if not isinstance(props, dict) or any(not isinstance(k, str) or not _schema(v) for k, v in props.items()):
        return False
    required = schema.get('required', [])
    if not isinstance(required, list) or any(not isinstance(k, str) or k not in props for k in required):
        return False
    if 'additionalProperties' in schema and not isinstance(schema['additionalProperties'], bool):
        return False
    if 'items' in schema and not _schema(schema['items']):
        return False
    return True


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _path(value):
    return isinstance(value, list) and bool(value) and all(_text(v) for v in value)


def _expression(value):
    if isinstance(value, dict):
        if set(value) == {'arg'}:
            return _text(value['arg'])
        if set(value) == {'value_path'}:
            return _path(value['value_path'])
        return all(isinstance(key, str) and _expression(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_expression(item) for item in value)
    return value is None or isinstance(value, (str, int, float, bool))


def validate_write_catalog(directory):
    """Raise ValueError unless the full, original ordered directory is valid."""
    try:
        if not isinstance(directory, dict) or directory.get('capability') != 'colony_write.transactions.v1' or directory.get('version') != 'colony_write.v1':
            raise ValueError('Unsupported write catalog.')
        if directory.get('modes') != ['dry_run', 'commit', 'undo']:
            raise ValueError('Unsupported write modes.')
        digest = directory.get('catalog_id')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest) or digest != catalog_digest(directory):
            raise ValueError('Write catalog identity mismatch.')
        operations = directory.get('operations')
        if not isinstance(operations, list) or not operations:
            raise ValueError('Missing write operations.')
        names = set()
        for item in operations:
            name, schema, observation = item['op'], item['schema'], item['observation']
            if not isinstance(name, str) or not name or name in names or not _schema(schema):
                raise ValueError('Invalid write operation schema.')
            names.add(name)
            props = schema.get('properties', {})
            if (schema.get('type') != 'object' or schema.get('additionalProperties') is not False or
                    props.get('op', {}).get('const') != name or 'value' not in props or
                    not {'op', 'value'} <= set(schema.get('required', [])) or
                    set(props) & {'expect', 'evidence', 'author', 'changeset_id', 'idempotency_key'}):
                raise ValueError('Unsafe write operation schema.')
            read, extract = observation['read'], observation['extract']
            direct_reads = {'inspect_production', 'inspect_food_policies', 'preview_construction', 'inspect_areas', 'inspect_construction_intents'}
            verification = item.get('verification_read')
            if verification is not None and (not isinstance(verification, dict)
                    or set(verification) != {'tool', 'native_operation', 'arguments'}
                    or verification.get('tool') not in direct_reads
                    or verification.get('native_operation') != verification.get('tool')
                    or not isinstance(verification.get('arguments'), dict)
                    or not _expression(verification['arguments'])):
                raise ValueError('Invalid post-commit observation descriptor.')
            if ((read.get('tool') != 'look' and not (read.get('tool') in direct_reads and read.get('tool') == read.get('native_operation'))) or not isinstance(read.get('native_operation'), str) or
                    not isinstance(read.get('arguments'), dict) or extract.get('kind') not in {'field', 'keyed_row', 'object_fields', 'ordered_ids'}):
                raise ValueError('Invalid evidence descriptor.')
            for key in ('path', 'objects_path', 'identity_path', 'status_path'):
                if not isinstance(extract.get(key), list) or any(not isinstance(v, str) or not v for v in extract[key]):
                    raise ValueError('Invalid evidence path.')
            if not extract['path']:
                raise ValueError('Empty evidence path.')
            if 'require' in extract and (not isinstance(extract['require'], list) or not extract['require']
                    or any(not isinstance(rule, dict) or not _path(rule.get('path')) or 'value' not in rule
                           or not _expression(rule['value']) for rule in extract['require'])):
                raise ValueError('Invalid evidence prerequisite.')
            if extract['kind'] in {'keyed_row', 'object_fields'} and not all(k in extract for k in ('key', 'key_field')):
                raise ValueError('Invalid evidence selector.')
            if extract['kind'] == 'keyed_row' and not isinstance(extract.get('value_field'), str):
                raise ValueError('Missing evidence value field.')
            if 'expect_schema' in item and not _schema(item['expect_schema']):
                raise ValueError('Invalid expected-value schema.')
            if extract['kind'] == 'ordered_ids':
                required = extract.get('require')
                available = extract.get('available')
                if (not _path(extract.get('complete_path')) or extract.get('complete_value') is not True or
                        not _text(extract.get('id_field')) or not _text(extract.get('expect_field')) or
                        (required is not None and (not isinstance(required, list) or not required)) or
                        any(not isinstance(rule, dict) or not _path(rule.get('path')) or 'value' not in rule or
                            not _expression(rule['value'])
                            for rule in (required or [])) or
                        not _expression(extract.get('object_id')) or not _expression(read['arguments']) or
                        'expect_schema' not in item):
                    raise ValueError('Invalid ordered collection evidence descriptor.')
                if available is not None and (not isinstance(available, dict) or not _path(available.get('path')) or
                        not _text(available.get('key_field')) or not _text(available.get('value_field')) or
                        available.get('value') is not True or
                        not isinstance(available.get('key'), dict) or
                        set(available['key']) != {'value_path'} or not _path(available['key']['value_path'])):
                    raise ValueError('Invalid recipe availability descriptor.')
                if name != 'reorder_production_bills' and (not required or available is None):
                    raise ValueError('Bill creation requires native recipe eligibility.')
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError('Invalid write catalog.') from exc

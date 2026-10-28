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
               'minLength', 'minimum', 'maximum', 'minItems', 'maxItems', 'minProperties'}
    if set(schema) - allowed:
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
            if (read.get('tool') != 'look' or not isinstance(read.get('native_operation'), str) or
                    not isinstance(read.get('arguments'), dict) or extract.get('kind') not in {'field', 'keyed_row', 'object_fields'}):
                raise ValueError('Invalid evidence descriptor.')
            for key in ('path', 'objects_path', 'identity_path', 'status_path'):
                if not isinstance(extract.get(key), list) or any(not isinstance(v, str) or not v for v in extract[key]):
                    raise ValueError('Invalid evidence path.')
            if not extract['path']:
                raise ValueError('Empty evidence path.')
            if extract['kind'] in {'keyed_row', 'object_fields'} and not all(k in extract for k in ('key', 'key_field')):
                raise ValueError('Invalid evidence selector.')
            if extract['kind'] == 'keyed_row' and not isinstance(extract.get('value_field'), str):
                raise ValueError('Missing evidence value field.')
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError('Invalid write catalog.') from exc

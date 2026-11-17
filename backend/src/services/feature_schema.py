"""Feature schema registry: versioned field aliases, units and conversions.

Every record is stored in the native unit of its own ``schema_version``.
Conversions always derive from the preserved original payload
(``Record.raw_values``) so replaying a batch can never apply a unit factor
twice.  The canonical base unit for the centroid is Hz; a version declares
how many base units one native unit represents (``base_factor``).
"""
import math
from ..config.settings import CONFIG

_SCHEMAS = CONFIG['feature_schemas']
_VERSIONS = _SCHEMAS['versions']
_NUMBER_EPS = 1e-12


class SchemaError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__('；'.join(e['detail'] for e in errors))


def baseline_version():
    return _SCHEMAS['baseline']


def target_version():
    return _SCHEMAS['target']


def known_versions():
    return list(_VERSIONS.keys())


def version_label(version):
    return _VERSIONS.get(version, {}).get('label', version)


def version_note(version):
    return _VERSIONS.get(version, {}).get('note', '')


def fields_for(version):
    return _VERSIONS[version]['fields']


def field_spec(version, key):
    return _VERSIONS[version]['fields'][key]


def _spec(version):
    if version not in _VERSIONS:
        raise ValueError('不支持的口径版本：'+str(version))
    return _VERSIONS[version]


def schema_document():
    """Public registry used by the UI and by API consumers."""
    return {
        'baseline': baseline_version(),
        'target': target_version(),
        'versions': {
            name: {
                'label': spec['label'],
                'note': spec.get('note', ''),
                'fields': {
                    key: {
                        'label': field['label'],
                        'kind': field['kind'],
                        'required': bool(field.get('required', True)),
                        'unit': field.get('unit', ''),
                        'base_factor': float(field.get('base_factor', 1.0)),
                        'aliases': dict(field.get('aliases') or {key: 1.0}),
                    }
                    for key, field in spec['fields'].items()
                },
            }
            for name, spec in _VERSIONS.items()
        },
    }


def _number(value, field):
    if isinstance(value, bool):
        raise ValueError(field['label']+'必须为数值')
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(field['label']+'必须为数值')
        try:
            number = float(text)
        except ValueError:
            raise ValueError(field['label']+'必须为数值')
    else:
        raise ValueError(field['label']+'必须为数值')
    if not math.isfinite(number):
        raise ValueError(field['label']+'必须是有限数值')
    return number


def _text(value, field):
    limit = int(field.get('max_length', 600))
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(field['label']+'不能为空且最多'+str(limit)+'字符')
    return value.strip()


def _normalize(payload, version, key, field):
    """Normalize one field from a submitted payload into version-native units."""
    if field['kind'] == 'number':
        number = _number(payload, field)
        if field.get('min') is not None and number < field['min']:
            raise ValueError(field['label']+'不可为负' if field['min'] == 0 else field['label']+'低于允许下限')
        return number
    return _text(payload, field)


def resolve_payload(payload, version):
    """Resolve aliases/units of a raw payload for ``version``.

    Returns ``(native_values, raw_values, errors)``.  ``raw_values`` keeps the
    submitted values under their submitted aliases (the original observation).
    Multiple aliases for the same field are accepted only when they normalize
    to the same value; otherwise the sample is ambiguous and must be
    quarantined instead of guessed.
    """
    if not isinstance(payload, dict):
        return {}, {}, [{'field': '*', 'code': 'invalid_payload', 'detail': '样本值必须为对象'}]
    spec = _spec(version)
    # Keep the exact submitted payload as the preserved original, including
    # unknown keys and their submitted units.  Conversion only ever reads the
    # declared aliases, so extra provenance cannot affect the result.
    raw = dict(payload)
    native, errors = {}, []
    for key, field in spec['fields'].items():
        aliases = field.get('aliases') or {key: 1.0}
        values = []
        field_errors = []
        for alias, factor in aliases.items():
            if alias not in payload:
                continue
            try:
                value = _normalize(payload[alias], version, key, field)
            except ValueError as exc:
                field_errors.append({'field': key, 'code': 'invalid_value', 'detail': str(exc)})
                continue
            if field['kind'] == 'number':
                value = value * float(factor)
            values.append(value)
        if field_errors:
            errors.extend(field_errors)
            continue
        if not values:
            if field.get('required', True):
                unit = ('（'+field['unit']+'）') if field.get('unit') else ''
                errors.append({'field': key, 'code': 'missing_field', 'detail': '缺少必填字段：'+field['label']+unit})
            continue
        first = values[0]
        same = all(
            math.isclose(first, other, rel_tol=1e-9, abs_tol=_NUMBER_EPS)
            if field['kind'] == 'number' else first == other
            for other in values[1:]
        )
        if not same:
            errors.append({
                'field': key,
                'code': 'ambiguous_value',
                'detail': '字段口径歧义：'+field['label']+'的多个别名指向不同数值（'+', '.join(aliases.keys())+'）',
            })
            continue
        native[key] = first
    return native, raw, errors


def raw_payload_for_storage(payload, version):
    """Keep only recognized aliases; unknown keys are never persisted."""
    _, raw, _ = resolve_payload(payload, version)
    return raw


def validate_payload(payload, version):
    """Strict ingestion validation.  Raises ValueError on any issue."""
    native, raw, errors = resolve_payload(payload, version)
    if errors:
        raise ValueError('；'.join(error['detail'] for error in errors))
    return native, raw


def convert_native_values(values, from_version, to_version):
    """Convert stored version-native values into ``to_version`` native units."""
    source = _spec(from_version)
    target = _spec(to_version)
    converted, errors = {}, []
    for key, field in target['fields'].items():
        value = values.get(key) if isinstance(values, dict) else None
        if value is None:
            if field.get('required', True):
                errors.append({'field': key, 'code': 'missing_field', 'detail': '缺少必填字段：'+field['label']})
            continue
        try:
            if field['kind'] == 'number':
                number = _number(value, field)
                from_factor = float(source['fields'][key].get('base_factor', 1.0))
                to_factor = float(field.get('base_factor', 1.0))
                number = number * from_factor / to_factor
                if not math.isfinite(number):
                    raise ValueError(field['label']+'换算后不是有限数值')
                if field.get('min') is not None and number < field['min']:
                    raise ValueError(field['label']+'换算后低于允许下限')
                converted[key] = number
            else:
                converted[key] = _text(value, field)
        except ValueError as exc:
            errors.append({'field': key, 'code': 'invalid_value', 'detail': str(exc)})
    return converted, errors


def convert_record_payload(raw_values, values, from_version, to_version):
    """Convert a stored record, preferring its preserved original payload.

    ``raw_values`` may be absent for rows written before versioning existed;
    in that case the stored values are already in ``from_version`` native
    units.  Returns ``(target_values, errors)``.
    """
    if isinstance(raw_values, dict) and raw_values:
        native, _, errors = resolve_payload(raw_values, from_version)
        if errors:
            return {}, errors
    else:
        native = values if isinstance(values, dict) else {}
    return convert_native_values(native, from_version, to_version)

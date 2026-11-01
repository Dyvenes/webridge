"""Валидация tool calls по JSON Schema из tools (раздел 9.6 п.5)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import jsonschema

from webbridge.errors import ProtocolError

from .parse import ParsedToolCall


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _to_plain(obj: Any) -> Any:
    """Рекурсивно превращает Pydantic-модели/Mapping в обычные dict/list."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return _to_plain(dump(exclude_none=True))
        except TypeError:
            return _to_plain(dump())
    as_dict = getattr(obj, "dict", None)
    if callable(as_dict) and not isinstance(obj, type):
        try:
            return _to_plain(as_dict())
        except TypeError:
            pass
    return obj


def _tool_function(tool: Any) -> Any:
    return _get(tool, "function", tool)


def allowed_tool_names(tools: list[Any] | None) -> list[str]:
    """Имена доступных инструментов в порядке объявления."""
    names: list[str] = []
    for tool in tools or []:
        name = _get(_tool_function(tool), "name")
        if name and name not in names:
            names.append(str(name))
    return names


def _coerce(name: str, value: Any, schema: dict[str, Any]) -> Any:
    """Приводит значение к типу из JSON Schema.

    XML-протокол передаёт все аргументы строками. Объекты и массивы
    (например, ``todos`` у opencode) должны быть разобраны из JSON, иначе
    валидация схемы отклонит корректный вызов.
    """
    typ = schema.get("type")
    if typ is None:
        if "properties" in schema:
            typ = "object"
        elif "items" in schema:
            typ = "array"
    types = typ if isinstance(typ, list) else [typ]

    # anyOf/oneOf: пробуем варианты по очереди. Сначала обрабатываем null,
    # иначе строка "null" успешно пройдёт валидацию как string-вариант.
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if not isinstance(variants, list):
            continue
        has_null = any(
            isinstance(variant, Mapping) and variant.get("type") == "null" for variant in variants
        )
        if has_null and isinstance(value, str):
            if value.strip().lower() in ("null", "none", ""):
                return None
        for variant in variants:
            if not isinstance(variant, Mapping):
                continue
            candidate = _coerce(name, value, dict(variant))
            try:
                jsonschema.validate(candidate, variant)
            except Exception:
                continue
            return candidate

    if isinstance(value, str):
        stripped = value.strip()
        if "null" in types and stripped.lower() in ("null", "none", ""):
            return None
        if "integer" in types:
            try:
                return int(stripped)
            except ValueError:
                pass
        if "number" in types:
            try:
                return float(stripped)
            except ValueError:
                pass
        if "boolean" in types:
            low = stripped.lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
        if "object" in types or "array" in types or "any" in types:
            if stripped[:1] in ("{", "["):
                try:
                    parsed = json.loads(stripped)
                except (TypeError, ValueError):
                    parsed = None
                if parsed is not None:
                    if "object" in types and isinstance(parsed, dict):
                        value = parsed
                    elif "array" in types and isinstance(parsed, list):
                        value = parsed
                    elif "any" in types:
                        value = parsed

    if "object" in types and isinstance(value, Mapping):
        props = schema.get("properties") or {}
        if isinstance(props, Mapping):
            return {str(k): _coerce(str(k), v, dict(props.get(k) or {})) for k, v in value.items()}
        return dict(value)

    if "array" in types and isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            return [_coerce(name, item, dict(items)) for item in value]
        return list(value)

    if "string" in types and not isinstance(value, str):
        if isinstance(value, (Mapping, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)
    return value


def validate_tool_call(call: ParsedToolCall, tools: list[Any]) -> tuple[dict[str, Any], str]:
    """Проверяет вызов и возвращает (arguments_dict, raw_json_string)."""
    tool = None
    for candidate in tools or []:
        fn = _tool_function(candidate)
        if _get(fn, "name") == call.name:
            tool = fn
            break
    if tool is None:
        raise ProtocolError(f"unknown tool: {call.name}")

    if not isinstance(call.arguments, Mapping):
        raise ProtocolError(f"arguments for tool {call.name} must be an object")

    schema = _to_plain(_get(tool, "parameters") or {})
    if not isinstance(schema, Mapping):
        schema = {}
    schema = dict(schema)
    props = schema.get("properties") or {}
    if not isinstance(props, Mapping):
        props = {}
    required = schema.get("required") or []

    coerced: dict[str, Any] = {}
    for key, val in call.arguments.items():
        prop_schema = props.get(key)
        if not isinstance(prop_schema, Mapping):
            prop_schema = {}
        coerced[key] = _coerce(key, val, dict(prop_schema))
    for req in required:
        if req not in coerced:
            raise ProtocolError(f"missing required arg: {req}")
    try:
        jsonschema.validate(coerced, schema)
    except jsonschema.ValidationError as exc:
        raise ProtocolError(f"schema validation failed: {exc.message}") from exc
    except jsonschema.SchemaError as exc:
        raise ProtocolError(f"invalid tool schema: {exc.message}") from exc
    return coerced, json.dumps(coerced, ensure_ascii=False)


def validate_tool_calls(
    calls: list[ParsedToolCall], tools: list[Any]
) -> list[tuple[ParsedToolCall, dict[str, Any], str]]:
    """Валидирует вызовы по порядку, сохраняя порядок в результате."""
    validated: list[tuple[ParsedToolCall, dict[str, Any], str]] = []
    for call in calls or []:
        args, raw = validate_tool_call(call, tools)
        validated.append((call, args, raw))
    return validated

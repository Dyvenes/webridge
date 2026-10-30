"""Валидация tool calls по JSON Schema из tools (раздел 9.6 п.5)."""
from __future__ import annotations

import json
from typing import Any, Mapping

import jsonschema

from webbridge.errors import ProtocolError

from .parse import ParsedToolCall


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _coerce(name: str, value: Any, schema: dict[str, Any]) -> Any:
    """Приводит базовые скаляры к нужному типу по схеме."""
    typ = schema.get("type")
    if typ == "integer" and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    if typ == "number" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    if typ == "boolean" and isinstance(value, str):
        low = value.lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    if typ == "string" and not isinstance(value, str):
        return str(value)
    return value


def validate_tool_call(call: ParsedToolCall, tools: list[Any]) -> tuple[dict[str, Any], str]:
    """Проверяет вызов и возвращает (arguments_dict, raw_json_string)."""
    tool = None
    for t in tools or []:
        fn = _get(t, "function", t)
        if _get(fn, "name") == call.name:
            tool = fn
            break
    if tool is None:
        raise ProtocolError(f"unknown tool: {call.name}")
    schema = _get(tool, "parameters") or {}
    props = _get(schema, "properties") or {}
    required = _get(schema, "required") or []
    coerced: dict[str, Any] = {}
    for key, val in call.arguments.items():
        coerced[key] = _coerce(key, val, props.get(key) or {})
    for req in required:
        if req not in coerced:
            raise ProtocolError(f"missing required arg: {req}")
    try:
        jsonschema.validate(coerced, schema)
    except jsonschema.ValidationError as e:
        raise ProtocolError(f"schema validation failed: {e.message}")
    return coerced, json.dumps(coerced, ensure_ascii=False)
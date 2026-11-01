"""Unit-тесты валидации tool calls по JSON Schema."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from webbridge.errors import ProtocolError
from webbridge.protocol.parse import ParsedToolCall
from webbridge.protocol.schema_validate import (
    allowed_tool_names,
    validate_tool_call,
    validate_tool_calls,
)


def _tool(name, props=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "",
            "parameters": {
                "type": "object",
                "properties": props or {},
                "required": required or [],
            },
        },
    }


def test_allowed_tool_names():
    tools = [
        _tool("read_file"),
        {"type": "function", "function": {"name": "write_file"}},
    ]
    assert allowed_tool_names(tools) == ["read_file", "write_file"]
    assert allowed_tool_names(None) == []


def test_validate_tool_call_coerces_scalars():
    tools = [
        _tool(
            "run",
            props={
                "count": {"type": "integer"},
                "flag": {"type": "boolean"},
                "name": {"type": "string"},
            },
            required=["count"],
        )
    ]
    args, raw = validate_tool_call(
        ParsedToolCall("run", {"count": "5", "flag": "true", "name": 123}),
        tools,
    )
    assert args == {"count": 5, "flag": True, "name": "123"}
    assert json.loads(raw) == args


def test_validate_tool_calls_preserves_order():
    tools = [_tool("a"), _tool("b")]
    calls = [ParsedToolCall("b", {"x": "1"}), ParsedToolCall("a", {})]
    result = validate_tool_calls(calls, tools)
    assert [call.name for call, _args, _raw in result] == ["b", "a"]
    assert result[0][1] == {"x": "1"}
    assert result[1][1] == {}


def test_validate_tool_call_unknown_tool():
    with pytest.raises(ProtocolError, match="unknown tool: nope"):
        validate_tool_call(ParsedToolCall("nope", {}), [_tool("read_file")])


def test_validate_tool_call_missing_required():
    tools = [_tool("read_file", props={"path": {"type": "string"}}, required=["path"])]
    with pytest.raises(ProtocolError, match="missing required arg: path"):
        validate_tool_call(ParsedToolCall("read_file", {}), tools)


def test_validate_tool_call_schema_failure():
    tools = [_tool("run", props={"count": {"type": "integer"}}, required=["count"])]
    with pytest.raises(ProtocolError, match="schema validation failed"):
        validate_tool_call(ParsedToolCall("run", {"count": "not-a-number"}), tools)


def test_validate_tool_calls_stops_on_failure():
    tools = [_tool("a")]
    calls = [ParsedToolCall("a", {}), ParsedToolCall("missing", {})]
    with pytest.raises(ProtocolError, match="unknown tool: missing"):
        validate_tool_calls(calls, tools)


def test_validate_tool_call_accepts_pydantic_tools():
    class Params(BaseModel):
        type: str = "object"
        properties: dict = {"path": {"type": "string"}}
        required: list[str] = ["path"]

    class Function(BaseModel):
        name: str
        description: str = ""
        parameters: Params

    class Tool(BaseModel):
        type: str = "function"
        function: Function

    tools = [Tool(function=Function(name="read_file", parameters=Params()))]
    args, raw = validate_tool_call(ParsedToolCall("read_file", {"path": "a.txt"}), tools)
    assert args == {"path": "a.txt"}
    assert json.loads(raw) == args


def test_validate_tool_call_coerces_object_and_array_from_json():
    tools = [
        _tool(
            "todos",
            props={
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "done": {"type": "boolean"},
                        },
                        "required": ["content"],
                    },
                },
                "meta": {"type": "object", "properties": {"count": {"type": "integer"}}},
            },
            required=["items"],
        )
    ]
    raw_items = '[{"content": "a", "done": "true"}, {"content": "b"}]'
    args, raw = validate_tool_call(
        ParsedToolCall("todos", {"items": raw_items, "meta": '{"count": "2"}'}),
        tools,
    )
    assert args["items"] == [
        {"content": "a", "done": True},
        {"content": "b"},
    ]
    assert args["meta"] == {"count": 2}
    assert json.loads(raw) == args


def test_validate_tool_call_coerces_nullable():
    tools = [
        _tool(
            "run",
            props={"note": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        )
    ]
    args, _ = validate_tool_call(ParsedToolCall("run", {"note": "null"}), tools)
    assert args["note"] is None

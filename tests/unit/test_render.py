"""Unit-тесты рендеринга сообщений и инструментов."""
from __future__ import annotations

from webbridge.protocol.render import render_delta, render_initial, render_tools


def _tool(name, desc='', required=None, props=None):
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': desc,
            'parameters': {
                'type': 'object',
                'properties': props or {},
                'required': required or [],
            },
        },
    }


def test_render_tools_empty():
    assert '(no tools available)' in render_tools(None)
    assert '(no tools available)' in render_tools([])


def test_render_tools_basic():
    tools = [
        _tool(
            'read_file',
            desc='Read a file',
            props={'path': {'type': 'string'}},
            required=['path'],
        )
    ]
    out = render_tools(tools)
    assert 'read_file' in out
    assert 'Read a file' in out
    assert 'path: string (required)' in out


def test_render_initial_with_system_and_user():
    messages = [
        {'role': 'system', 'content': 'You are a helpful agent.'},
        {'role': 'user', 'content': 'fix the bug'},
    ]
    out = render_initial(messages, tools=None)
    assert 'You are operating as a coding agent' in out
    assert 'You are a helpful agent.' in out
    assert 'fix the bug' in out


def test_render_delta_user():
    out = render_delta([{'role': 'user', 'content': 'hello'}])
    assert 'hello' in out


def test_render_delta_tool():
    out = render_delta(
        [
            {
                'role': 'tool',
                'name': 'read_file',
                'tool_call_id': 'call_1',
                'content': 'file contents',
            }
        ]
    )
    assert 'file contents' in out
    assert 'read_file' in out
    assert 'call_1' in out
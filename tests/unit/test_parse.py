"""Unit-тесты парсера tool-блоков."""
from __future__ import annotations

from webbridge.protocol.parse import (
    find_tool_blocks,
    parse_json_body,
    parse_tool_calls,
    parse_xml_body,
)


def test_find_tool_blocks_simple():
    text = 'explanation\n```tool\n<call name="x"><arg name="a">1</arg></call>\n```'
    blocks = find_tool_blocks(text)
    assert len(blocks) == 1
    assert '<call name="x">' in blocks[0]


def test_find_tool_blocks_variable_fence():
    blocks = find_tool_blocks('````tool\nbody\n````')
    assert blocks == ['\nbody\n']


def test_find_tool_blocks_none():
    assert find_tool_blocks('just plain text') == []
    assert find_tool_blocks('```python\ncode\n```') == []


def test_parse_xml_body():
    body = '<call name="read_file"><arg name="path">a.txt</arg></call>'
    call = parse_xml_body(body)
    assert call.name == 'read_file'
    assert call.arguments == {'path': 'a.txt'}


def test_parse_xml_body_multiline_value():
    body = (
        '<call name="edit_file">'
        '<arg name="path">f.py</arg>'
        '<arg name="old_str">\nline1\nline2\n</arg>'
        '<arg name="new_str">\nline3\n</arg>'
        '</call>'
    )
    call = parse_xml_body(body)
    assert call.name == 'edit_file'
    assert call.arguments['path'] == 'f.py'
    assert 'line1\nline2' in call.arguments['old_str']
    assert 'line3' in call.arguments['new_str']


def test_parse_json_body():
    body = '{"name": "read_file", "arguments": {"path": "a.txt"}}'
    call = parse_json_body(body)
    assert call.name == 'read_file'
    assert call.arguments == {'path': 'a.txt'}


def test_parse_json_body_soft_recovery():
    # trailing comma
    call = parse_json_body('{"name": "x", "arguments": {"a": 1,}}')
    assert call.name == 'x'
    assert call.arguments == {'a': 1}

    # single quotes
    call2 = parse_json_body("{'name': 'y', 'arguments': {'b': 2}}")
    assert call2.name == 'y'
    assert call2.arguments == {'b': 2}


def test_parse_tool_calls_xml():
    text = (
        'Some explanation\n'
        '```tool\n'
        '<call name="read_file"><arg name="path">a.txt</arg></call>\n'
        '```\n'
    )
    calls = parse_tool_calls(text, fmt='xml')
    assert len(calls) == 1
    assert calls[0].name == 'read_file'
    assert calls[0].arguments == {'path': 'a.txt'}


def test_parse_tool_calls_no_blocks():
    assert parse_tool_calls('just text', fmt='xml') == []
    assert parse_tool_calls('', fmt='json') == []
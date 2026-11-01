"""Unit-тесты парсера tool-блоков."""

from __future__ import annotations

import pytest

from webbridge.protocol.parse import (
    find_tool_blocks,
    has_tool_block,
    parse_json_body,
    parse_tool_calls,
    parse_xml_body,
    strip_tool_blocks,
)


def test_find_tool_blocks_simple():
    text = 'explanation\n```tool\n<call name="x"><arg name="a">1</arg></call>\n```'
    blocks = find_tool_blocks(text)
    assert len(blocks) == 1
    assert '<call name="x">' in blocks[0]


def test_find_tool_blocks_variable_fence():
    blocks = find_tool_blocks("````tool\nbody\n````")
    assert blocks == ["\nbody\n"]


def test_find_tool_blocks_none():
    assert find_tool_blocks("just plain text") == []
    assert find_tool_blocks("```python\ncode\n```") == []


def test_has_tool_block():
    assert has_tool_block('```tool\n<call name="x"></call>\n```')
    assert has_tool_block('```tool\n<call name="x"></call>')
    assert not has_tool_block("```python\nprint(1)\n```")
    assert not has_tool_block("plain text")


def test_strip_tool_blocks():
    text = 'Before\n```tool\n<call name="read_file"><arg name="path">a.txt</arg></call>\n```\nAfter'
    out = strip_tool_blocks(text)
    assert "```tool" not in out
    assert "<call" not in out
    assert "Before" in out
    assert "After" in out


def test_strip_tool_blocks_variable_fence():
    text = 'Before\n````tool\n<call name="x"></call>\n````\nAfter'
    out = strip_tool_blocks(text)
    assert out == "Before\n\nAfter"


def test_strip_tool_blocks_no_blocks():
    assert strip_tool_blocks("  plain text  ") == "plain text"
    assert strip_tool_blocks("") == ""


def test_parse_xml_body():
    body = '<call name="read_file"><arg name="path">a.txt</arg></call>'
    call = parse_xml_body(body)
    assert call.name == "read_file"
    assert call.arguments == {"path": "a.txt"}


def test_parse_xml_body_multiline_value():
    body = (
        '<call name="edit_file">'
        '<arg name="path">f.py</arg>'
        '<arg name="old_str">\nline1\nline2\n</arg>'
        '<arg name="new_str">\nline3\n</arg>'
        "</call>"
    )
    call = parse_xml_body(body)
    assert call.name == "edit_file"
    assert call.arguments["path"] == "f.py"
    assert "line1\nline2" in call.arguments["old_str"]
    assert "line3" in call.arguments["new_str"]


def test_parse_json_body():
    body = '{"name": "read_file", "arguments": {"path": "a.txt"}}'
    call = parse_json_body(body)
    assert call.name == "read_file"
    assert call.arguments == {"path": "a.txt"}


def test_parse_json_body_soft_recovery():
    # trailing comma
    call = parse_json_body('{"name": "x", "arguments": {"a": 1,}}')
    assert call.name == "x"
    assert call.arguments == {"a": 1}

    # single quotes
    call2 = parse_json_body("{'name': 'y', 'arguments': {'b': 2}}")
    assert call2.name == "y"
    assert call2.arguments == {"b": 2}


def test_parse_tool_calls_xml():
    text = (
        "Some explanation\n"
        "```tool\n"
        '<call name="read_file"><arg name="path">a.txt</arg></call>\n'
        "```\n"
    )
    calls = parse_tool_calls(text, fmt="xml")
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "a.txt"}


def test_parse_tool_calls_json():
    text = '```tool\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```'
    calls = parse_tool_calls(text, fmt="json")
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "a.txt"}


def test_parse_tool_calls_multiple_preserves_order():
    text = '```tool\n<call name="a"></call>\n```\n```tool\n<call name="b"></call>\n```'
    calls = parse_tool_calls(text, fmt="xml")
    assert [c.name for c in calls] == ["a", "b"]


def test_parse_tool_calls_no_blocks():
    assert parse_tool_calls("just text", fmt="xml") == []
    assert parse_tool_calls("", fmt="json") == []


def test_parse_tool_calls_malformed_raises():
    text = "```tool\nnot a valid call\n```"
    with pytest.raises(ValueError, match="malformed tool block"):
        parse_tool_calls(text, fmt="xml")


def test_parse_tool_calls_unterminated_raises():
    text = '```tool\n<call name="x"></call>'
    with pytest.raises(ValueError, match="missing closing"):
        parse_tool_calls(text, fmt="xml")


def test_parse_tool_calls_malformed_json_raises():
    text = '```tool\n{"not": "a tool call"}\n```'
    with pytest.raises(ValueError, match="malformed tool block"):
        parse_tool_calls(text, fmt="json")


def test_parse_xml_multiple_calls_in_one_block():
    text = (
        "```tool\n"
        '<call name="read_file"><arg name="path">a.txt</arg></call>\n'
        '<call name="read_file"><arg name="path">b.txt</arg></call>\n'
        "```"
    )
    calls = parse_tool_calls(text, fmt="xml")
    assert [c.name for c in calls] == ["read_file", "read_file"]
    assert [c.arguments["path"] for c in calls] == ["a.txt", "b.txt"]


def test_parse_xml_single_quotes_and_self_closing():
    text = "```tool\n<call name='list_tools'><arg name='depth'/></call>\n```"
    calls = parse_tool_calls(text, fmt="xml")
    assert calls[0].name == "list_tools"
    assert calls[0].arguments == {"depth": ""}


def test_parse_xml_falls_back_to_json_body():
    text = '```tool\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```'
    calls = parse_tool_calls(text, fmt="xml")
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "a.txt"}


def test_fence_inside_text_is_not_a_tool_block():
    text = "inline ```tool is not a block"
    assert find_tool_blocks(text) == []

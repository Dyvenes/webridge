"""Парсер блоков ```tool из текста модели (раздел 9.6)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


_FENCE_RE = re.compile(r"(`{3,})tool\b")


def find_tool_blocks(text: str) -> list[str]:
    """Возвращает тела блоков ```tool, поддерживая ограждения переменной длины."""
    blocks: list[str] = []
    i, n = 0, len(text)
    while i < n:
        m = _FENCE_RE.search(text, i)
        if not m:
            break
        flen = len(m.group(1))
        start = m.end()
        close_re = re.compile(r"`{%d,}" % flen)
        cm = close_re.search(text, start)
        if not cm:
            break
        blocks.append(text[start:cm.start()])
        i = cm.end()
    return blocks


def parse_xml_body(body: str) -> ParsedToolCall:
    call_m = re.search(r'<call\s+name="([^"]+)"\s*>', body)
    if not call_m:
        raise ValueError("missing <call name=...>")
    name = call_m.group(1)
    args: dict[str, Any] = {}
    for am in re.finditer(
        r'<arg\s+name="([^"]+)"\s*>(.*?)</arg>', body, re.DOTALL
    ):
        args[am.group(1)] = am.group(2)
    return ParsedToolCall(name=name, arguments=args)


def _soft_json_loads(raw: str) -> Any:
    """JSON с мягким восстановлением: trailing comma, одинарные кавычки."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return json.loads(cleaned.replace("'", '"'))


def parse_json_body(body: str) -> ParsedToolCall:
    data = _soft_json_loads(body)
    if not isinstance(data, dict) or "name" not in data:
        raise ValueError("missing 'name' in tool call")
    args = data.get("arguments") or {}
    if isinstance(args, str):
        args = _soft_json_loads(args)
    return ParsedToolCall(name=data["name"], arguments=args)


def parse_tool_calls(text: str, fmt: str = "xml") -> list[ParsedToolCall]:
    """Возвращает список разобранных вызовов. Пустой список — нет блоков."""
    bodies = find_tool_blocks(text)
    if not bodies:
        return []
    parser = parse_xml_body if fmt == "xml" else parse_json_body
    parsed: list[ParsedToolCall] = []
    for b in bodies:
        parsed.append(parser(b))
    return parsed
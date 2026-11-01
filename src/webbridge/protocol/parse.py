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


# Открывающий фенс должен стоять отдельной строкой. Иначе ```tool внутри
# обычного текста или кода ложно считается вызовом инструмента.
_FENCE_RE = re.compile(r"(?m)^[ \t]*(`{3,})tool[ \t]*\r?$")


def _scan_tool_blocks(
    text: str,
) -> tuple[list[tuple[int, int, int, int]], int | None]:
    """Возвращает span'ы блоков и начало незакрытого блока (если есть).

    Span: (start фенса, начало тела, конец тела, конец закрывающего фенса).
    """
    spans: list[tuple[int, int, int, int]] = []
    unterminated_start: int | None = None
    i, n = 0, len(text)
    while i < n:
        m = _FENCE_RE.search(text, i)
        if not m:
            break
        flen = len(m.group(1))
        start = m.end()
        # Закрывающий фенс — тоже отдельная строка, не короче открывающего.
        close_re = re.compile(rf"(?m)^[ \t]*`{{{flen},}}[ \t]*\r?$")
        cm = close_re.search(text, start)
        if not cm:
            unterminated_start = m.start()
            break
        spans.append((m.start(), start, cm.start(), cm.end()))
        i = cm.end()
    return spans, unterminated_start


def find_tool_blocks(text: str) -> list[str]:
    """Возвращает тела блоков ```tool, поддерживая ограждения переменной длины."""
    spans, _ = _scan_tool_blocks(text)
    return [text[start:end] for _, start, end, _ in spans]


def strip_tool_blocks(text: str) -> str:
    """Удаляет фенсы ```tool вместе с содержимым и обрезает пробелы."""
    spans, unterminated_start = _scan_tool_blocks(text)
    if not spans and unterminated_start is None:
        return text.strip()

    parts: list[str] = []
    last = 0
    for start, _body_start, _body_end, end in spans:
        parts.append(text[last:start])
        last = end
    if unterminated_start is not None:
        parts.append(text[last:unterminated_start])
        last = len(text)
    parts.append(text[last:])
    cleaned = "".join(parts)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def has_tool_block(text: str) -> bool:
    """True, если в тексте есть открывающий фенс ```tool."""
    return _FENCE_RE.search(text) is not None


_NAME_ATTR_RE = re.compile(r"""\bname\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_CALL_BLOCK_RE = re.compile(r"<call\b([^>]*)>(.*?)</call\s*>", re.DOTALL)
_CALL_SELF_RE = re.compile(r"<call\b([^>]*)/\s*>", re.DOTALL)
_ARG_BLOCK_RE = re.compile(r"<arg\b([^>]*)>(.*?)</arg\s*>", re.DOTALL)
_ARG_SELF_RE = re.compile(r"<arg\b([^>]*)/\s*>", re.DOTALL)


def _attr_name(attrs: str) -> str | None:
    match = _NAME_ATTR_RE.search(attrs or "")
    if not match:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def _parse_args(body: str) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for match in _ARG_BLOCK_RE.finditer(body):
        name = _attr_name(match.group(1))
        if name:
            args[name] = match.group(2)
    for match in _ARG_SELF_RE.finditer(body):
        name = _attr_name(match.group(1))
        if name and name not in args:
            args[name] = ""
    return args


def parse_xml_calls(body: str) -> list[ParsedToolCall]:
    """Разбирает один или несколько ``<call>`` в теле tool-блока."""
    calls: list[ParsedToolCall] = []
    for match in _CALL_BLOCK_RE.finditer(body):
        name = _attr_name(match.group(1))
        if not name:
            raise ValueError("missing <call name=...>")
        calls.append(ParsedToolCall(name=name, arguments=_parse_args(match.group(2))))
    if calls:
        return calls

    for match in _CALL_SELF_RE.finditer(body):
        name = _attr_name(match.group(1))
        if not name:
            raise ValueError("missing <call name=...>")
        calls.append(ParsedToolCall(name=name, arguments={}))
    if calls:
        return calls

    raise ValueError("missing <call name=...>")


def parse_xml_body(body: str) -> ParsedToolCall:
    """Совместимость: первый вызов из тела блока."""
    calls = parse_xml_calls(body)
    return calls[0]


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
    """Возвращает список разобранных вызовов. Пустой список — нет блоков.

    Если блок найден, но разобрать его не удалось, поднимает ValueError
    с понятным сообщением (вызывающий код превращает его в ProtocolError).
    """
    spans, unterminated_start = _scan_tool_blocks(text)
    if unterminated_start is not None:
        raise ValueError("malformed tool block: missing closing backtick fence")
    if not spans:
        return []
    bodies = [text[start:end] for _, start, end, _ in spans]
    parsed: list[ParsedToolCall] = []
    for index, body in enumerate(bodies, start=1):
        if fmt == "xml":
            try:
                parsed.extend(parse_xml_calls(body))
                continue
            except Exception as xml_exc:
                # Модель могла проигнорировать формат и выдать JSON.
                try:
                    parsed.append(parse_json_body(body))
                    continue
                except Exception:
                    raise ValueError(f"malformed tool block #{index}: {xml_exc}") from xml_exc
        try:
            parsed.append(parse_json_body(body))
        except Exception as exc:
            raise ValueError(f"malformed tool block #{index}: {exc}") from exc
    return parsed

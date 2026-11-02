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

# ---------------------------------------------------------------------------
# DSML-формат DeepSeek:
#   <｜｜DSML｜｜ calls>
#   <｜｜DSML｜｜ invoke name="write">
#   <｜｜DSML｜｜ parameter name="path" string="true">...</｜｜DSML｜｜ parameter>
#   </｜｜DSML｜｜ invoke>
#   </｜｜DSML｜｜ calls>
# ---------------------------------------------------------------------------
_DSML_OPEN = r"<[｜|]+\s*DSML\s*[｜|]+\s*"
_DSML_CLOSE = r"</[｜|]+\s*DSML\s*[｜|]+\s*"
_DSML_FLAGS = re.DOTALL | re.IGNORECASE

_DSML_CALLS_OPEN_RE = re.compile(_DSML_OPEN + r"(?:function_)?calls\s*>", re.IGNORECASE)
_DSML_CALLS_RE = re.compile(
    _DSML_OPEN + r"(?:function_)?calls\s*>(.*?)" + _DSML_CLOSE + r"(?:function_)?calls\s*>",
    _DSML_FLAGS,
)
_DSML_INVOKE_RE = re.compile(
    _DSML_OPEN + r"invoke\b([^>]*)>(.*?)" + _DSML_CLOSE + r"invoke\s*>",
    _DSML_FLAGS,
)
_DSML_PARAM_RE = re.compile(
    _DSML_OPEN + r"parameter\b([^>]*)>(.*?)" + _DSML_CLOSE + r"parameter\s*>",
    _DSML_FLAGS,
)
_STRING_ATTR_RE = re.compile(r"""\bstring\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
# markdownify экранирует _ и * (например, new\_file.txt).
_MD_ESCAPE_RE = re.compile(r"\\([_*])")


def has_dsml_block(text: str) -> bool:
    return _DSML_CALLS_OPEN_RE.search(text) is not None


def strip_dsml_blocks(text: str) -> str:
    cleaned = _DSML_CALLS_RE.sub("", text)
    m = _DSML_CALLS_OPEN_RE.search(cleaned)  # незакрытый блок отбрасываем до конца
    if m:
        cleaned = cleaned[: m.start()]
    return cleaned


def _parse_dsml_params(body: str) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for m in _DSML_PARAM_RE.finditer(body):
        attrs = m.group(1)
        pname = _attr_name(attrs)
        if not pname:
            continue
        value = _MD_ESCAPE_RE.sub(r"\1", m.group(2))
        sm = _STRING_ATTR_RE.search(attrs)
        flag = ""
        if sm:
            flag = (sm.group(1) if sm.group(1) is not None else sm.group(2)).lower()
        if flag == "false":
            # Не строка: число, булево, список или объект в JSON.
            try:
                args[pname] = _soft_json_loads(value.strip())
                continue
            except ValueError:
                pass
        args[pname] = value
    return args


def parse_dsml_calls(text: str) -> list[ParsedToolCall]:
    """Разбирает DSML-блоки. Пустой список — блоков нет."""
    blocks = list(_DSML_CALLS_RE.finditer(text))
    if not blocks:
        if has_dsml_block(text):
            raise ValueError("malformed DSML block: missing closing tag")
        return []
    calls: list[ParsedToolCall] = []
    for block in blocks:
        invokes = list(_DSML_INVOKE_RE.finditer(block.group(1)))
        if not invokes:
            raise ValueError("DSML block without invoke")
        for inv in invokes:
            name = _attr_name(inv.group(1))
            if not name:
                raise ValueError("missing DSML invoke name=...")
            calls.append(
                ParsedToolCall(name=name, arguments=_parse_dsml_params(inv.group(2)))
            )
    return calls


# Сырые аргументы: <arg name="x" raw="TOKEN">\n ...текст... \nTOKEN
_RAW_OPEN_RE = re.compile(
    r"""<arg\b[^>\n]*?\braw\s*=\s*(?:"([^"\n]+)"|'([^'\n]+)')[^>\n]*>[ \t]*\r?\n"""
)
_ARG_CLOSE_RE = re.compile(r"\s*</arg\s*>")
_RAW_PLACEHOLDER = "\x00RAW{}\x00"


def _raw_regions(text: str, start: int = 0) -> list[tuple[int, int, int, int, str]]:
    """(начало тега, начало содержимого, конец содержимого, конец токена, токен)."""
    regions: list[tuple[int, int, int, int, str]] = []
    pos = start
    while True:
        m = _RAW_OPEN_RE.search(text, pos)
        if not m:
            break
        token = m.group(1) or m.group(2)
        term = re.compile(rf"(?m)^[ \t]*{re.escape(token)}[ \t]*\r?$")
        tm = term.search(text, m.end())
        if not tm:
            break  # незакрытый raw-блок: не трогаем
        regions.append((m.start(), m.end(), tm.start(), tm.end(), token))
        pos = tm.end()
    return regions


def _find_close_fence(text: str, start: int, flen: int):
    """Закрывающий фенс, пропуская всё, что внутри raw-аргументов."""
    close_re = re.compile(rf"(?m)^[ \t]*`{{{flen},}}[ \t]*\r?$")
    regions = _raw_regions(text, start)
    pos = start
    while True:
        cm = close_re.search(text, pos)
        if cm is None:
            return None
        inside = next((r for r in regions if r[1] <= cm.start() < r[3]), None)
        if inside is None:
            return cm
        pos = inside[3]


def _extract_raw_args(body: str) -> tuple[str, dict[str, str]]:
    """Заменяет raw-аргументы заглушками, чтобы регулярки их не видели."""
    raws: dict[str, str] = {}
    out: list[str] = []
    last = 0
    for i, (tag_start, content_start, content_end, end, _token) in enumerate(_raw_regions(body)):
        name = _attr_name(body[tag_start:content_start])
        if not name:
            continue
        content = body[content_start:content_end]
        # Убираем только один перевод строки перед токеном.
        content = content[:-2] if content.endswith("\r\n") else content[:-1] if content.endswith("\n") else content
        key = _RAW_PLACEHOLDER.format(i)
        raws[key] = content
        out.append(body[last:tag_start])
        out.append(f'<arg name="{name}">{key}</arg>')
        close = _ARG_CLOSE_RE.match(body, end)
        last = close.end() if close else end
    out.append(body[last:])
    return "".join(out), raws


# Открывающий фенс должен стоять отдельной строкой. Иначе ```tool внутри
# обычного текста или кода ложно считается вызовом инструмента.
_FENCE_RE = re.compile(r"(?m)^[ \t]*(`{3,})tool[ \t]*\r?$")


def _scan_labelled_blocks(
    text: str,
) -> tuple[list[tuple[int, int, int, int]], int | None]:
    """Блоки с меткой ```tool: span'ы и начало незакрытого блока (если есть).

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
        cm = _find_close_fence(text, start, flen)
        if not cm:
            unterminated_start = m.start()
            break
        spans.append((m.start(), start, cm.start(), cm.end()))
        i = cm.end()
    return spans, unterminated_start


# ChatGPT при копировании ответа («Copy») теряет метку языка у блока кода:
# в интерфейсе подпись ``tool`` есть, а в скопированном тексте фенс голый (```)
# и строки разделены \r\n. Поэтому, если размеченных блоков нет, принимаем
# фенс без метки (или с нейтральной меткой), чьё тело целиком является вызовом.
_ANY_FENCE_RE = re.compile(r"(?m)^[ \t]*(`{3,})([A-Za-z0-9_+.\-]*)[ \t]*\r?$")
_NEUTRAL_LANGS = frozenset({"", "xml", "json", "text", "txt", "plain", "plaintext"})
_TOOLISH_XML_RE = re.compile(
    r"""^\s*<call\s+name\s*=\s*(["'])[^"']+\1\s*(?:/>|>.*</call\s*>)\s*$""", re.DOTALL
)
_TOOL_START_RE = re.compile(r"\s*<call\s+name\s*=")


def _looks_like_tool_body(body: str) -> bool:
    """Тело блока само по себе — законченный вызов (XML <call> или JSON name+arguments)."""
    s = body.strip()
    if not s:
        return False
    if _TOOLISH_XML_RE.match(s):
        return True
    if s.startswith("{") and s.endswith("}"):
        try:
            data = _soft_json_loads(s)
        except ValueError:
            return False
        return isinstance(data, dict) and isinstance(data.get("name"), str) and "arguments" in data
    return False


def _scan_untagged_blocks(
    text: str,
) -> tuple[list[tuple[int, int, int, int]], int | None]:
    """Блоки без метки ``tool``, по содержимому похожие на вызов. Формат как у _scan_labelled_blocks."""
    spans: list[tuple[int, int, int, int]] = []
    unterminated_start: int | None = None
    i, n = 0, len(text)
    while i < n:
        m = _ANY_FENCE_RE.search(text, i)
        if not m:
            break
        flen = len(m.group(1))
        start = m.end()
        neutral = m.group(2).lower() in _NEUTRAL_LANGS
        cm = _find_close_fence(text, start, flen)
        if not cm:
            if neutral and _TOOL_START_RE.match(text, start):
                unterminated_start = m.start()  # ответ оборвался внутри вызова
            break
        if neutral and _looks_like_tool_body(text[start : cm.start()]):
            spans.append((m.start(), start, cm.start(), cm.end()))
        i = cm.end()  # перепрыгиваем весь блок, чтобы не принять закрывающий фенс за открывающий
    return spans, unterminated_start


def _scan_tool_blocks(
    text: str,
) -> tuple[list[tuple[int, int, int, int]], int | None]:
    """Размеченные блоки ```tool; если их нет — блоки без метки, похожие на вызов."""
    spans, unterminated_start = _scan_labelled_blocks(text)
    if spans or unterminated_start is not None:
        return spans, unterminated_start
    return _scan_untagged_blocks(text)


def find_tool_blocks(text: str) -> list[str]:
    """Возвращает тела блоков ```tool, поддерживая ограждения переменной длины."""
    spans, _ = _scan_tool_blocks(text)
    return [text[start:end] for _, start, end, _ in spans]


def strip_tool_blocks(text: str) -> str:
    """Удаляет фенсы ```tool вместе с содержимым и обрезает пробелы."""
    text = strip_dsml_blocks(text)
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
    """True, если в тексте есть блок вызова: ```tool, фенс без метки с вызовом внутри или DSML."""
    spans, unterminated_start = _scan_tool_blocks(text)
    return bool(spans) or unterminated_start is not None or has_dsml_block(text)


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
    body, raws = _extract_raw_args(body)
    for match in _CALL_BLOCK_RE.finditer(body):
        name = _attr_name(match.group(1))
        if not name:
            raise ValueError("missing <call name=...>")
        calls.append(ParsedToolCall(name=name, arguments=_parse_args(match.group(2))))
    if calls:
        for call in calls:
            for key, value in call.arguments.items():
                if isinstance(value, str) and value in raws:
                    call.arguments[key] = raws[value]
        return calls

    for match in _CALL_SELF_RE.finditer(body):
        name = _attr_name(match.group(1))
        if not name:
            raise ValueError("missing <call name=...>")
        calls.append(ParsedToolCall(name=name, arguments={}))
    if calls:
        for call in calls:
            for key, value in call.arguments.items():
                if isinstance(value, str) and value in raws:
                    call.arguments[key] = raws[value]
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
    dsml_calls = parse_dsml_calls(text)
    spans, unterminated_start = _scan_tool_blocks(text)
    if unterminated_start is not None:
        raise ValueError("malformed tool block: missing closing backtick fence")
    if not spans:
        return dsml_calls
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
    return dsml_calls + parsed

_BROKEN_CALL_RE = re.compile(
    r"(?im)^[ \t]*tool[ \t]*$|<call\s+name\s*=|<arg\s+name\s*=|</call\s*>|DSML|<invoke\b"
)


def looks_like_broken_tool_call(text: str) -> bool:
    """Нет распознанного блока, но текст похож на неудавшуюся попытку вызова."""
    return not has_tool_block(text) and _BROKEN_CALL_RE.search(text) is not None


_CALL_OPEN_ANY_RE = re.compile(r"<call\b[^>]*?(?<!/)>")
_CALL_CLOSE_ANY_RE = re.compile(r"</call\s*>")


def _mask_raw(text: str) -> str:
    """Вырезает содержимое закрытых raw-аргументов (там могут быть любые теги)."""
    out: list[str] = []
    last = 0
    for _tag_start, content_start, content_end, _end, _token in _raw_regions(text):
        out.append(text[last:content_start])
        last = content_end
    out.append(text[last:])
    return "".join(out)


def tool_call_truncated(text: str) -> bool:
    """True, если вызов начат, но не закрыт: ответ, видимо, оборвался по лимиту."""
    if not (has_tool_block(text) or has_dsml_block(text)):
        return False
    if len(_RAW_OPEN_RE.findall(text)) > len(_raw_regions(text)):
        return True  # raw-аргумент без завершающего токена
    masked = _mask_raw(text)
    return len(_CALL_OPEN_ANY_RE.findall(masked)) > len(_CALL_CLOSE_ANY_RE.findall(masked))
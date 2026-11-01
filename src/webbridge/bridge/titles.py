"""Локальная генерация заголовков сессий.

opencode периодически просит модель придумать короткий заголовок разговора.
Такие запросы не должны попадать в веб-чат: они расходуют бюджет, создают
лишние вкладки и могут перемешать служебную инструкцию с историей диалога.
Заголовок строится детерминированно из первого осмысленного сообщения
пользователя.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_TITLE_MARKERS = (
    "generate a title",
    "generate title",
    "create a title",
    "conversation title",
    "title for this conversation",
    "title for the conversation",
    "short title",
    "summarize this conversation in a title",
    "summarise this conversation in a title",
    "you are a title",
    "title generator",
)

_MAX_TITLE = 60

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")
_LINE_PREFIX_RE = re.compile(r"^\s*(?:[#>*+-]+|\d+[.)])\s*", re.MULTILINE)
_SPACE_RE = re.compile(r"\s+")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def message_text(message: Any) -> str:
    """Достаёт текст сообщения, поддерживая строку и content-parts."""
    content = _get(message, "content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            text = _get(part, "text")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)
    return str(content)


def looks_like_title_prompt(text: str) -> bool:
    """True, если текст похож на служебную просьбу сгенерировать заголовок."""
    low = (text or "").lower()
    if len(low) > 8000:
        return False
    return any(marker in low for marker in _TITLE_MARKERS)


def clean_title_text(text: str) -> str:
    """Убирает Markdown-разметку и лишние пробелы из кандидата в заголовок."""
    value = text or ""
    value = _FENCE_RE.sub(" ", value)
    value = _INLINE_CODE_RE.sub(r"\1", value)
    value = _LINK_RE.sub(r"\1", value)
    value = _URL_RE.sub(" ", value)
    value = _LINE_PREFIX_RE.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _SPACE_RE.sub(" ", value).strip()
    # Служебные префиксы, которые иногда добавляет клиент.
    value = re.sub(r"^(?:user|conversation|request)\s*:\s*", "", value, flags=re.I)
    return value.strip(" \t\n\"'`«»“”")


def _shorten(value: str, limit: int = _MAX_TITLE) -> str:
    if len(value) <= limit:
        return value
    cut = value[:limit].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:!?-—")


def make_title(messages: list[Any]) -> str:
    """Строит короткий заголовок без обращения к веб-модели.

    Предпочитается первое сообщение пользователя, которое не является
    инструкцией «сгенерируй заголовок». Если такого нет — используется
    последнее сообщение; если и оно служебное — нейтральный заголовок.
    """
    candidates: list[str] = []
    fallback = ""
    for message in messages or []:
        if str(_get(message, "role", "")).lower() != "user":
            continue
        raw = message_text(message)
        cleaned = clean_title_text(raw)
        if not cleaned:
            continue
        fallback = cleaned
        if looks_like_title_prompt(raw) or looks_like_title_prompt(cleaned):
            continue
        if len(cleaned) < 2:
            continue
        candidates.append(cleaned)

    value = candidates[0] if candidates else fallback
    if not value:
        return "New conversation"

    # Первая строка обычно содержит суть запроса; код и длинные вставки
    # не должны превращаться в заголовок.
    value = value.split("\n", 1)[0]
    value = _shorten(value)
    if not value:
        return "New conversation"
    return value[0].upper() + value[1:]

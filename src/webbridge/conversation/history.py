"""Канонизация истории и вычисление дельты.

opencode — источник истины по истории. WebBridge хранит последнюю
зафиксированную историю сессии и отправляет в веб-чат только новые
сообщения. Если клиент переписал историю (compaction, смена модели,
редактирование), удалённый чат пересоздаётся и засевается заново.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def normalize_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, Mapping):
                if part.get("type") in (None, "text", "input_text", "output_text"):
                    text = part.get("text")
                    if text is not None:
                        parts.append(str(text))
                elif part.get("type") == "image_url":
                    parts.append("[image]")
                else:
                    text = part.get("text")
                    if text is not None:
                        parts.append(str(text))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


def _normalize_arguments(arguments: Any) -> str:
    if arguments is None:
        return "{}"
    if isinstance(arguments, str):
        raw = arguments
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        return json.dumps(parsed, sort_keys=True, ensure_ascii=False)
    try:
        return json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(arguments)


def normalize_tool_calls(tool_calls: Any) -> list[dict[str, Any]] | None:
    if not tool_calls:
        return None
    result: list[dict[str, Any]] = []
    for tc in tool_calls:
        fn = _get(tc, "function", {}) or {}
        result.append(
            {
                "id": str(_get(tc, "id", "") or ""),
                "type": "function",
                "function": {
                    "name": str(_get(fn, "name", "") or ""),
                    "arguments": _normalize_arguments(_get(fn, "arguments")),
                },
            }
        )
    return result


def canonical_message(msg: Any) -> dict[str, Any]:
    role = str(_get(msg, "role", "") or "")
    if role == "developer":
        role = "system"
    return {
        "role": role,
        "content": normalize_content(_get(msg, "content")),
        "name": _get(msg, "name"),
        "tool_calls": normalize_tool_calls(_get(msg, "tool_calls")),
        "tool_call_id": _get(msg, "tool_call_id"),
    }


def canonical_messages(messages: list[Any]) -> list[dict[str, Any]]:
    return [canonical_message(m) for m in messages]


def canonical_json(msg: dict[str, Any]) -> str:
    return json.dumps(msg, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def history_hash(messages: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for msg in messages:
        h.update(canonical_json(msg).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def char_count(messages: list[dict[str, Any]]) -> int:
    return sum(len(canonical_json(m)) for m in messages)


def conversation_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("role") != "system"]


def system_text(messages: list[dict[str, Any]]) -> str:
    return "\n\n".join(m.get("content", "") for m in messages if m.get("role") == "system")


def tools_hash(tools: list[Any] | None) -> str:
    if not tools:
        return ""
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        fn = _get(tool, "function", tool) or {}
        normalized.append(
            {
                "type": "function",
                "function": {
                    "name": _get(fn, "name"),
                    "description": _get(fn, "description"),
                    "parameters": _get(fn, "parameters") or {},
                },
            }
        )
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def estimate_tokens(text: str | None) -> int:
    return max(1, int(len(text or "") / 3.5))


@dataclass
class HistoryDelta:
    mode: str  # new | continue | idempotent | reset
    delta: list[dict[str, Any]]
    system_changed: bool = False
    current_system: str = ""
    reason: str = ""


def split_history(stored: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> HistoryDelta:
    """Сравнивает сохранённую и входящую историю.

    Системные сообщения не участвуют в сравнении префикса: opencode
    подмешивает динамический контекст (cwd, дата, файлы), и из-за этого
    нельзя пересоздавать удалённый чат на каждом запросе.
    """
    current_system = system_text(incoming)
    if not stored:
        return HistoryDelta("new", incoming, False, current_system, "no stored history")

    stored_conv = conversation_messages(stored)
    incoming_conv = conversation_messages(incoming)
    system_changed = system_text(stored) != current_system

    if not stored_conv:
        return HistoryDelta(
            "new", incoming, system_changed, current_system, "empty stored conversation"
        )

    if len(incoming_conv) >= len(stored_conv) and incoming_conv[: len(stored_conv)] == stored_conv:
        if len(incoming_conv) == len(stored_conv):
            if system_changed:
                return HistoryDelta("continue", [], True, current_system, "system prompt changed")
            return HistoryDelta(
                "idempotent", [], False, current_system, "history already committed"
            )
        return HistoryDelta(
            "continue",
            incoming_conv[len(stored_conv) :],
            system_changed,
            current_system,
            "prefix matched",
        )

    return HistoryDelta("reset", incoming, system_changed, current_system, "history diverged")


def truncate_history(messages: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """Аварийное усечение для seed нового чата.

    opencode сам делает compaction; это лишь страховка, чтобы seed
    гарантированно поместился в лимит веб-чата.
    """
    if char_count(messages) <= max_chars:
        return messages

    systems = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    note = {
        "role": "system",
        "content": "[WebBridge: earlier part of the conversation was truncated because it exceeded the web chat message limit.]",
        "name": None,
        "tool_calls": None,
        "tool_call_id": None,
    }

    kept: list[dict[str, Any]] = []
    total = char_count(systems + [note])
    for msg in reversed(rest):
        size = len(canonical_json(msg))
        if total + size > max_chars and kept:
            break
        kept.append(msg)
        total += size
    kept.reverse()
    return systems + [note] + kept

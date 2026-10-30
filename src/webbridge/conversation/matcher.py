"""Сопоставление чатов по префиксу сообщений (раздел 10.2)."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .store import Store


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _normalize_content(content: Any) -> Any:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # OpenAI multimodal content: list of {"type": "text", "text": ...}
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text", ""))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


def _normalize_tool_calls(tcs: Any) -> Any:
    if not tcs:
        return None
    out = []
    for tc in tcs:
        fn = _get(tc, "function", {}) or {}
        out.append({
            "id": _get(tc, "id"),
            "name": _get(fn, "name"),
            "arguments": _get(fn, "arguments"),
        })
    return out


def canonical_message(msg: Any) -> str:
    payload = {
        "role": _get(msg, "role"),
        "content": _normalize_content(_get(msg, "content")),
        "name": _get(msg, "name"),
        "tool_calls": _normalize_tool_calls(_get(msg, "tool_calls")),
        "tool_call_id": _get(msg, "tool_call_id"),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def fingerprint(messages: list[Any]) -> str:
    h = hashlib.sha256()
    for m in messages:
        h.update(canonical_message(m).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def char_count(messages: list[Any]) -> int:
    return sum(len(canonical_message(m)) for m in messages)


async def match(
    store: Store, service: str, messages: list[Any]
) -> tuple[str, dict[str, Any] | None, list[Any]]:
    """Возвращает ('continue', chat, delta) или ('new', None, messages)."""
    canonical = [canonical_message(m) for m in messages]
    chats = await store.list_active_chats(service)
    for chat in chats:
        stored = await store.get_chat_messages(chat["id"])
        n = len(stored)
        if n == 0:
            continue
        if n <= len(canonical) and canonical[:n] == stored:
            return "continue", chat, messages[n:]
    return "new", None, messages
"""Заготовка сжатия истории и ротации чата (раздел 10.4)."""
from __future__ import annotations

from typing import Any, Mapping


COMPACTION_SYSTEM = (
    "Summarise the current coding session. Include: the goal, decisions made, "
    "files changed, and unfinished steps. Be compact. This summary will seed "
    "a fresh chat."
)


def needs_rotation(char_count: int, threshold: int) -> bool:
    return char_count >= threshold


def build_summary_prompt(messages: list[Any]) -> str:
    lines = [COMPACTION_SYSTEM, "", "--- session transcript ---"]
    for m in messages:
        role = m.get("role") if isinstance(m, Mapping) else getattr(m, "role", "?")
        content = m.get("content") if isinstance(m, Mapping) else getattr(m, "content", "")
        if content:
            lines.append(f"[{role}] {content}")
    return "\n".join(lines)
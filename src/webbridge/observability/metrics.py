"""Простые счётчики метрик (без внешних зависимостей)."""
from __future__ import annotations

from collections import defaultdict


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}

    def inc(self, name: str, value: float = 1.0) -> None:
        self._counters[name] += value

    def set(self, name: str, value: float) -> None:
        self._gauges[name] = value

    def render_prometheus(self) -> str:
        lines: list[str] = []
        for k in sorted(self._counters):
            lines.append(f"# TYPE {k} counter")
            lines.append(f"{k} {self._counters[k]}")
        for k in sorted(self._gauges):
            lines.append(f"# TYPE {k} gauge")
            lines.append(f"{k} {self._gauges[k]}")
        return "\n".join(lines) + "\n"
"""Заготовка tier 2 (быстрый путь через прямые HTTP-запросы).

Реализуется на M7 после HAR-разведки. Здесь — заглушка, чтобы явно
обозначить точку расширения и правило отката.
"""
from __future__ import annotations

from webbridge.errors import Tier2Unavailable


class HttpChatAdapter:
    def __init__(self, name: str, *args, **kwargs):
        self.name = name

    async def ensure_session(self) -> str:
        raise Tier2Unavailable(f"tier2 not implemented for {self.name}")

    async def new_chat(self, model=None):
        raise Tier2Unavailable("tier2 not implemented")

    async def open_chat(self, chat_url):
        raise Tier2Unavailable("tier2 not implemented")

    async def send(self, chat, text, files=()):
        raise Tier2Unavailable("tier2 not implemented")

    async def wait_done(self, chat, timeout_s):
        raise Tier2Unavailable("tier2 not implemented")

    async def close(self):
        return None
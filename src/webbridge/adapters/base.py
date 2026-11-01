"""Контракты адаптеров веб-чатов."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ChatHandle:
    service: str
    chat_url: str | None = None
    model: str | None = None
    page_key: str = "default"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Reply:
    text: str
    finished: bool = True
    meta: dict[str, Any] = field(default_factory=dict)


class ChatAdapter(Protocol):
    name: str

    async def ensure_session(self) -> str: ...

    async def new_chat(self, model: str | None = None, page_key: str = "default") -> ChatHandle: ...

    async def open_chat(self, chat_url: str, page_key: str = "default") -> ChatHandle: ...

    async def send(self, chat: ChatHandle, text: str, files: Sequence[Path] = ()) -> None: ...

    async def wait_done(
        self, chat: ChatHandle, timeout_s: float = 300.0, stable_s: float = 1.5
    ) -> Reply: ...

    def stream(self, chat: ChatHandle) -> AsyncIterator[str]: ...

    async def cancel(self, chat: ChatHandle) -> None: ...

    async def close_page(self, page_key: str) -> None: ...

    async def close(self) -> None: ...

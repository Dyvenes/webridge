"""Интерфейс ChatAdapter (раздел 8.1)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Protocol, Sequence


@dataclass
class ChatHandle:
    service: str
    chat_url: str | None = None
    model: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Reply:
    text: str
    finished: bool
    meta: dict[str, Any] = field(default_factory=dict)


class ChatAdapter(Protocol):
    name: str

    async def ensure_session(self) -> str: ...
    async def new_chat(self, model: str | None = None) -> ChatHandle: ...
    async def open_chat(self, chat_url: str) -> ChatHandle: ...
    async def send(self, chat: ChatHandle, text: str,
                   files: Sequence[Path] = ()) -> None: ...
    def stream(self, chat: ChatHandle) -> AsyncIterator[str]: ...
    async def wait_done(self, chat: ChatHandle, timeout_s: float) -> Reply: ...
    async def close(self) -> None: ...
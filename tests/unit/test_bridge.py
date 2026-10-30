"""Smoke-тест моста M2 на фейковом адаптере."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator, Sequence

import pytest
from httpx import ASGITransport, AsyncClient

from webbridge.adapters.base import ChatHandle, Reply
from webbridge.bridge import app as bridge_app
from webbridge.bridge.auth import check_auth
from webbridge.config import AppConfig, RoutingConfig, ServiceConfig
from webbridge.bridge.app import create_app


class FakeAdapter:
    name = "fake"

    def __init__(self) -> None:
        self._chats: dict[str, list[str]] = {}
        self._counter = 0
        self.calls: list[str] = []

    async def ensure_session(self) -> str:
        return "OK"

    async def new_chat(self, model: str | None = None) -> ChatHandle:
        self._counter += 1
        cid = f"chat-{self._counter}"
        self._chats[cid] = []
        return ChatHandle(service="svc", chat_url=f"fake://{cid}", model=model)

    async def open_chat(self, chat_url: str) -> ChatHandle:
        cid = chat_url.replace("fake://", "")
        return ChatHandle(service="svc", chat_url=chat_url)

    async def send(self, chat: ChatHandle, text: str, files: Sequence[Path] = ()) -> None:
        self.calls.append(text)

    async def wait_done(self, chat: ChatHandle, timeout_s: float = 300.0) -> Reply:
        return Reply(text=f"echo {len(self.calls)}", finished=True)

    async def stream(self, chat: ChatHandle) -> AsyncIterator[str]:
        yield "echo"

    async def close(self) -> None:
        pass


@pytest.fixture
async def client(tmp_path: Path, monkeypatch):
    cfg = AppConfig(
        services={
            "svc": ServiceConfig(
                base_url="http://fake",
                profile_dir=tmp_path / "prof",
                selectors_file=tmp_path / "sel.yaml",
                models={"m1": "Model1"},
            )
        },
        routing=RoutingConfig(default_model="m1", fallbacks={}),
    )
    app = create_app(cfg, store_path=tmp_path / "state.db")
    fake = FakeAdapter()
    app.state.router._adapters["svc"] = fake

    async def _no_auth():
        return None

    app.dependency_overrides[check_auth] = _no_auth
    monkeypatch.setenv("WEBBRIDGE_TEST", "1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        async with app.router.lifespan_context(app):
            yield c, fake


@pytest.mark.asyncio
async def test_models(client):
    c, _ = client
    r = await c.get("/v1/models")
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == ["m1"]


@pytest.mark.asyncio
async def test_chat_completion_non_stream(client):
    c, fake = client
    body = {
        "model": "m1",
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await c.post("/v1/chat/completions", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["choices"][0]["message"]["content"].startswith("echo")
    assert fake.calls and "hi" in fake.calls[0]


@pytest.mark.asyncio
async def test_chat_completion_stream(client):
    c, _ = client
    body = {
        "model": "m1",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    async with c.stream("POST", "/v1/chat/completions", json=body) as r:
        assert r.status_code == 200
        chunks = []
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                chunks.append(line)
    assert any("[DONE]" in c for c in chunks)


@pytest.mark.asyncio
async def test_error_unknown_model(client):
    c, _ = client
    r = await c.post(
        "/v1/chat/completions",
        json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 500
    assert r.json()["error"]["type"] == "server_error"
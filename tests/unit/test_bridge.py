"""Интеграционные тесты API на фейковом адаптере.

Проверяем контракт opencode: модели, стриминг, tool_calls, дельту истории,
идемпотентность и ошибки.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from webbridge.adapters.base import ChatHandle, Reply
from webbridge.bridge.app import create_app
from webbridge.bridge.auth import check_auth
from webbridge.bridge.schemas import ChatCompletionRequest
from webbridge.config import AppConfig, RoutingConfig, ServiceConfig
from webbridge.errors import ChatLost


class FakeAdapter:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.replies: list[str] = []
        self.stream_parts: list[list[str]] = []
        self.new_chat_calls = 0
        self.open_chat_calls = 0
        self.wait_done_calls = 0
        self.cancel_calls = 0
        self._counter = 0

    async def ensure_session(self) -> str:
        return "OK"

    async def new_chat(self, model: str | None = None, page_key: str = "default") -> ChatHandle:
        self._counter += 1
        self.new_chat_calls += 1
        return ChatHandle(
            service="svc",
            chat_url=f"fake://chat-{self._counter}",
            model=model,
            page_key=page_key,
        )

    async def open_chat(self, chat_url: str, page_key: str = "default") -> ChatHandle:
        if not chat_url:
            raise ChatLost("fake: no chat url")
        self.open_chat_calls += 1
        return ChatHandle(service="svc", chat_url=chat_url, page_key=page_key)

    async def send(self, chat: ChatHandle, text: str, files: Sequence[Path] = ()) -> None:
        self.calls.append(text)

    async def wait_done(
        self, chat: ChatHandle, timeout_s: float = 300.0, stable_s: float = 1.5
    ) -> Reply:
        self.wait_done_calls += 1
        if not self.replies:
            return Reply(text="ok", finished=True)
        return Reply(text=self.replies.pop(0), finished=True)

    async def stream(self, chat: ChatHandle) -> AsyncIterator[str]:
        parts = self.stream_parts.pop(0) if self.stream_parts else ["ok"]
        for part in parts:
            yield part

    async def cancel(self, chat: ChatHandle) -> None:
        self.cancel_calls += 1

    async def close_page(self, page_key: str) -> None:
        pass

    async def close(self) -> None:
        pass


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        services={
            "svc": ServiceConfig(
                type="generic",
                base_url="http://fake",
                profile_dir=tmp_path / "prof",
                selectors_file=tmp_path / "sel.yaml",
                models={"m1": "Model1"},
            )
        },
        routing=RoutingConfig(default_model="m1", fallbacks={}),
    )


@pytest.fixture
async def client(tmp_path: Path):
    fake = FakeAdapter()
    app = create_app(
        _config(tmp_path),
        adapters={"svc": fake},
        store_path=tmp_path / "state.db",
        api_token="test-token",
    )

    async def _no_auth():
        return None

    app.dependency_overrides[check_auth] = _no_auth

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            yield c, fake


@pytest.mark.asyncio
async def test_models(client):
    c, _ = client
    r = await c.get("/v1/models")
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == ["m1"]


@pytest.mark.asyncio
async def test_auth_required(tmp_path: Path):
    app = create_app(
        _config(tmp_path),
        adapters={"svc": FakeAdapter()},
        store_path=tmp_path / "auth.db",
        api_token="secret",
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            r = await c.get("/v1/models")
            assert r.status_code == 401
            assert r.json()["error"]["type"] == "authentication_error"

            r = await c.get(
                "/v1/models",
                headers={"Authorization": "Bearer secret"},
            )
            assert r.status_code == 200


@pytest.mark.asyncio
async def test_non_stream_and_initial_seed(client):
    c, fake = client
    fake.replies.append("hello")
    body = {
        "model": "m1",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ],
    }
    r = await c.post("/v1/chat/completions", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["choices"][0]["message"]["content"] == "hello"
    assert data["choices"][0]["finish_reason"] == "stop"

    assert len(fake.calls) == 1
    seed = fake.calls[0]
    assert "[User]\nhi" in seed
    assert "be brief" in seed
    assert "You can call tools" in seed


@pytest.mark.asyncio
async def test_delta_only_on_second_turn(client):
    c, fake = client
    fake.replies.extend(["one", "two"])

    first = {
        "model": "m1",
        "messages": [{"role": "user", "content": "first question"}],
    }
    r = await c.post("/v1/chat/completions", json=first)
    assert r.status_code == 200, r.text

    second = {
        "model": "m1",
        "messages": [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "one"},
            {"role": "user", "content": "second question"},
        ],
    }
    r = await c.post("/v1/chat/completions", json=second)
    assert r.status_code == 200, r.text

    assert len(fake.calls) == 2
    assert "first question" in fake.calls[0]
    assert "second question" in fake.calls[1]
    assert "first question" not in fake.calls[1]
    assert fake.new_chat_calls == 1
    assert fake.open_chat_calls == 1


@pytest.mark.asyncio
async def test_idempotent_retry_does_not_resend(client):
    c, fake = client
    fake.replies.append("once")
    body = {
        "model": "m1",
        "messages": [{"role": "user", "content": "same"}],
    }
    r1 = await c.post("/v1/chat/completions", json=body)
    r2 = await c.post("/v1/chat/completions", json=body)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["choices"][0]["message"]["content"] == "once"
    assert r2.json()["choices"][0]["message"]["content"] == "once"
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_tool_call_response_and_tool_result_delta(client):
    c, fake = client
    fake.replies.append(
        'reading\n\n```tool\n<call name="read_file"><arg name="path">a.txt</arg></call>\n```'
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]
    body = {
        "model": "m1",
        "messages": [{"role": "user", "content": "read a.txt"}],
        "tools": tools,
        "tool_choice": "auto",
    }
    r = await c.post("/v1/chat/completions", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    choice = data["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "reading"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "a.txt"}

    fake.replies.append("done")
    second = {
        "model": "m1",
        "messages": [
            {"role": "user", "content": "read a.txt"},
            choice["message"],
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": "file body",
            },
        ],
        "tools": tools,
    }
    r2 = await c.post("/v1/chat/completions", json=second)
    assert r2.status_code == 200, r2.text
    assert r2.json()["choices"][0]["message"]["content"] == "done"

    delta = fake.calls[1]
    assert "<tool_result" in delta
    assert 'name="read_file"' in delta
    assert f'call_id="{call["id"]}"' in delta
    assert "file body" in delta
    assert "read a.txt" not in delta


@pytest.mark.asyncio
async def test_tool_choice_required_without_tool_block_is_error(client):
    c, fake = client
    fake.replies.append("no tools here")
    body = {
        "model": "m1",
        "messages": [{"role": "user", "content": "use a tool"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": "required",
    }
    r = await c.post("/v1/chat/completions", json=body)
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "bad_model_output"


@pytest.mark.asyncio
async def test_stream_text(client):
    c, fake = client
    fake.stream_parts.append(["echo", " stream"])
    body = {
        "model": "m1",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    async with c.stream("POST", "/v1/chat/completions", json=body) as r:
        assert r.status_code == 200
        chunks = [line async for line in r.aiter_lines() if line.startswith("data: ")]
    assert chunks[-1] == "data: [DONE]"
    content = [json.loads(x[6:]) for x in chunks if x != "data: [DONE]"]
    deltas = [ch["choices"][0]["delta"].get("content") for ch in content if ch.get("choices")]
    # Текст буферизуется до конца генерации: DOM веб-чата может
    # перерисовывать markdown, поэтому промежуточные дельты ненадёжны.
    assert deltas == ["", "echo stream", None]
    assert content[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_stream_tool_calls_are_buffered(client):
    c, fake = client
    fake.stream_parts.append(
        ["```tool\n", '<call name="read_file">', '<arg name="path">a</arg>', "</call>\n```"]
    )
    body = {
        "model": "m1",
        "messages": [{"role": "user", "content": "read"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ],
        "stream": True,
    }
    async with c.stream("POST", "/v1/chat/completions", json=body) as r:
        assert r.status_code == 200
        chunks = [line async for line in r.aiter_lines() if line.startswith("data: ")]
    payloads = [json.loads(x[6:]) for x in chunks if x != "data: [DONE]"]
    tool_deltas = [
        p["choices"][0]["delta"]["tool_calls"]
        for p in payloads
        if p.get("choices") and "tool_calls" in p["choices"][0]["delta"]
    ]
    assert tool_deltas
    assert tool_deltas[0][0]["function"]["name"] == "read_file"
    assert payloads[-1]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_unknown_model_is_404(client):
    c, _ = client
    r = await c.post(
        "/v1/chat/completions",
        json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "model_not_found"


@pytest.mark.asyncio
async def test_history_divergence_starts_new_chat(client):
    c, fake = client
    fake.replies.extend(["one", "two"])
    first = {
        "model": "m1",
        "messages": [{"role": "user", "content": "original"}],
    }
    assert (await c.post("/v1/chat/completions", json=first)).status_code == 200

    # Клиент переписал историю (compaction): префикс не совпадает.
    second = {
        "model": "m1",
        "messages": [
            {"role": "system", "content": "summary"},
            {"role": "user", "content": "after compaction"},
        ],
    }
    r = await c.post("/v1/chat/completions", json=second)
    assert r.status_code == 200, r.text
    assert fake.new_chat_calls == 2
    assert "after compaction" in fake.calls[1]


@pytest.mark.asyncio
async def test_title_request_is_answered_locally(client):
    c, fake = client
    body = {
        "model": "m1",
        "messages": [
            {"role": "user", "content": "Generate a title for this conversation"},
            {"role": "user", "content": "Fix the login bug in auth.py"},
        ],
    }
    r = await c.post("/v1/chat/completions", json=body)
    assert r.status_code == 200, r.text
    content = r.json()["choices"][0]["message"]["content"]
    assert "Fix the login bug" in content
    assert len(content) <= 60
    # Заголовок не должен доходить до браузера.
    assert fake.calls == []
    assert fake.new_chat_calls == 0
    assert fake.open_chat_calls == 0


@pytest.mark.asyncio
async def test_title_by_header_is_local_even_without_marker(client):
    c, fake = client
    body = {
        "model": "m1",
        "messages": [{"role": "user", "content": "read src/app.py"}],
    }
    r = await c.post(
        "/v1/chat/completions",
        json=body,
        headers={"X-WebBridge-Kind": "title"},
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"]
    assert fake.calls == []


@pytest.mark.asyncio
async def test_image_content_is_rejected(client):
    c, fake = client
    body = {
        "model": "m1",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/a.png"},
                    },
                ],
            }
        ],
    }
    r = await c.post("/v1/chat/completions", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"
    assert "unsupported content" in r.json()["error"]["message"]
    assert fake.calls == []


@pytest.mark.asyncio
async def test_abandoned_turn_cancels_generation(client):
    c, fake = client
    app = c._transport.app  # type: ignore[attr-defined]
    req = ChatCompletionRequest(
        model="m1",
        messages=[{"role": "user", "content": "long task"}],
    )
    turn = await app.state.service.begin(req, {})
    await turn.send()
    await turn.aclose()
    assert fake.cancel_calls == 1


@pytest.mark.asyncio
async def test_multiple_tool_calls_are_returned_in_order(client):
    c, fake = client
    fake.replies.append(
        "```tool\n"
        '<call name="read_file"><arg name="path">a.txt</arg></call>\n'
        '<call name="read_file"><arg name="path">b.txt</arg></call>\n'
        "```"
    )
    body = {
        "model": "m1",
        "messages": [{"role": "user", "content": "read both"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ],
    }
    r = await c.post("/v1/chat/completions", json=body)
    assert r.status_code == 200, r.text
    calls = r.json()["choices"][0]["message"]["tool_calls"]
    assert [call["function"]["name"] for call in calls] == [
        "read_file",
        "read_file",
    ]
    assert [json.loads(call["function"]["arguments"])["path"] for call in calls] == [
        "a.txt",
        "b.txt",
    ]

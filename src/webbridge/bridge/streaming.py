"""SSE-обёртки для /v1/chat/completions (раздел 11.3)."""
from __future__ import annotations

import json
import time
import uuid
from typing import AsyncIterator


def _chunk(
    response_id: str,
    model: str,
    *,
    delta: dict,
    finish: str | None = None,
    index: int = 0,
) -> bytes:
    payload = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": index, "delta": delta, "finish_reason": finish}
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def stream_text(response_id: str, model: str, text: str) -> AsyncIterator[bytes]:
    yield _chunk(response_id, model, delta={"role": "assistant"})
    if text:
        yield _chunk(response_id, model, delta={"content": text})
    yield _chunk(response_id, model, delta={}, finish="stop")
    yield b"data: [DONE]\n\n"


async def stream_tool_calls(
    response_id: str, model: str, tool_calls: list[dict]
) -> AsyncIterator[bytes]:
    yield _chunk(response_id, model, delta={"role": "assistant"})
    for i, tc in enumerate(tool_calls):
        delta_tc = {
            "index": i,
            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {
                "name": tc["function"]["name"],
                "arguments": tc["function"]["arguments"],
            },
        }
        yield _chunk(response_id, model, delta={"tool_calls": [delta_tc]})
    yield _chunk(response_id, model, delta={}, finish="tool_calls")
    yield b"data: [DONE]\n\n"


def chunk_bytes(response_id: str, model: str, delta: dict, finish: str | None = None) -> bytes:
    """Публичный helper для app.py (keep-alive/ошибочные чанки)."""
    return _chunk(response_id, model, delta=delta, finish=finish)
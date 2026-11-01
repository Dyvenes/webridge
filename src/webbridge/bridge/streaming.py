"""SSE-обёртки для /v1/chat/completions."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any


def _payload(
    response_id: str,
    model: str,
    *,
    delta: dict[str, Any] | None = None,
    finish: str | None = None,
    usage: dict[str, Any] | None = None,
    system_fingerprint: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
    }
    if system_fingerprint:
        payload["system_fingerprint"] = system_fingerprint
    if delta is not None:
        payload["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
    if usage is not None:
        payload["usage"] = usage
    return payload


def sse_data(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def chunk_bytes(
    response_id: str,
    model: str,
    delta: dict[str, Any],
    finish: str | None = None,
    *,
    usage: dict[str, Any] | None = None,
    system_fingerprint: str | None = None,
) -> bytes:
    return sse_data(
        _payload(
            response_id,
            model,
            delta=delta,
            finish=finish,
            usage=usage,
            system_fingerprint=system_fingerprint,
        )
    )


def usage_chunk(
    response_id: str,
    model: str,
    usage: dict[str, Any],
    system_fingerprint: str | None = None,
) -> bytes:
    return sse_data(
        _payload(
            response_id,
            model,
            delta=None,
            usage=usage,
            system_fingerprint=system_fingerprint,
        )
    )


def error_chunk(
    response_id: str,
    model: str,
    error: dict[str, Any],
    system_fingerprint: str | None = None,
) -> bytes:
    payload = _payload(
        response_id,
        model,
        delta=None,
        system_fingerprint=system_fingerprint,
    )
    payload["error"] = error
    return sse_data(payload)


def done_chunk() -> bytes:
    return b"data: [DONE]\n\n"


async def stream_text(
    response_id: str,
    model: str,
    text: str,
    *,
    include_usage: bool = False,
    usage: dict[str, Any] | None = None,
    system_fingerprint: str | None = None,
) -> AsyncIterator[bytes]:
    yield chunk_bytes(
        response_id,
        model,
        {"role": "assistant", "content": ""},
        system_fingerprint=system_fingerprint,
    )
    if text:
        yield chunk_bytes(
            response_id,
            model,
            {"content": text},
            system_fingerprint=system_fingerprint,
        )
    yield chunk_bytes(
        response_id,
        model,
        {},
        finish="stop",
        system_fingerprint=system_fingerprint,
    )
    if include_usage and usage is not None:
        yield usage_chunk(response_id, model, usage, system_fingerprint)
    yield done_chunk()


async def stream_tool_calls(
    response_id: str,
    model: str,
    tool_calls: list[dict[str, Any]],
    *,
    content: str | None = None,
    include_usage: bool = False,
    usage: dict[str, Any] | None = None,
    system_fingerprint: str | None = None,
) -> AsyncIterator[bytes]:
    first_delta: dict[str, Any] = {"role": "assistant"}
    if content:
        first_delta["content"] = content
    yield chunk_bytes(response_id, model, first_delta, system_fingerprint=system_fingerprint)

    for i, tc in enumerate(tool_calls):
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        delta_tc = {
            "index": i,
            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", "{}"),
            },
        }
        yield chunk_bytes(
            response_id,
            model,
            {"tool_calls": [delta_tc]},
            system_fingerprint=system_fingerprint,
        )

    yield chunk_bytes(
        response_id,
        model,
        {},
        finish="tool_calls",
        system_fingerprint=system_fingerprint,
    )
    if include_usage and usage is not None:
        yield usage_chunk(response_id, model, usage, system_fingerprint)
    yield done_chunk()

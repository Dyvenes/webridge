"""FastAPI-приложение моста (раздел 11)."""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from webbridge.adapters.base import ChatHandle
from webbridge.adapters.browser_base import BrowserChatAdapter
from webbridge.config import AppConfig
from webbridge.conversation.matcher import canonical_message, char_count, fingerprint, match
from webbridge.conversation.store import Store
from webbridge.errors import (
    CaptchaRequired,
    GenerationTimeout,
    MessageTooLong,
    NeedsLogin,
    ProtocolError,
    RateLimited,
    SelectorBroken,
    WebBridgeError,
)
from webbridge.limits.budget import BudgetManager
from webbridge.observability.logging import configure as configure_logging, get_logger
from webbridge.observability.metrics import Metrics
from webbridge.protocol.render import render_delta, render_initial
from webbridge.session.manager import SessionManager

from .auth import check_auth
from .router import Router
from .schemas import (
    AssistantMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ErrorBody,
    ErrorResponse,
    Model,
    ModelList,
    Usage,
)
from .streaming import chunk_bytes, stream_text

log = get_logger("bridge")
metrics = Metrics()

_DEFAULT_STORE = Path.home() / ".webbridge" / "state.db"


def _err(status: int, etype: str, msg: str, headers: dict | None = None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(type=etype, message=msg)).model_dump()
    return JSONResponse(content=body, status_code=status, headers=headers)


def _map_error(e: Exception) -> JSONResponse:
    if isinstance(e, (NeedsLogin, CaptchaRequired)):
        return _err(503, "session_unavailable", str(e) or type(e).__name__)
    if isinstance(e, RateLimited):
        headers: dict[str, str] = {}
        reset = getattr(e, "reset_at", None)
        if isinstance(reset, datetime):
            delta = (reset - datetime.now(timezone.utc)).total_seconds()
            headers["Retry-After"] = str(max(1, int(delta)))
        return _err(429, "rate_limit_exceeded", str(e), headers)
    if isinstance(e, GenerationTimeout):
        return _err(504, "timeout", str(e) or "generation timeout")
    if isinstance(e, ProtocolError):
        return _err(502, "bad_model_output", str(e))
    if isinstance(e, SelectorBroken):
        return _err(503, "adapter_broken", str(e))
    if isinstance(e, MessageTooLong):
        return _err(400, "invalid_request_error", str(e))
    if isinstance(e, WebBridgeError):
        return _err(500, "server_error", str(e))
    return _err(500, "server_error", str(e) or type(e).__name__)


def _estimate_tokens(text: str | None) -> int:
    return max(1, int(len(text or "") / 3.5))


async def _persist_reply(
    store: Store,
    chat_id: str,
    incoming_messages: list,
    reply_text: str,
    handle: ChatHandle,
) -> None:
    reply_canon = canonical_message({"role": "assistant", "content": reply_text})
    stored = await store.get_chat_messages(chat_id)
    new_all_canon = stored + [reply_canon]
    new_fp = fingerprint([{"role": "assistant", "content": reply_text}])  # overwritten below
    # Build a deterministic fingerprint over ALL canonical strings
    import hashlib
    h = hashlib.sha256()
    for c in new_all_canon:
        h.update(c.encode("utf-8"))
        h.update(b"\n")
    new_fp = h.hexdigest()
    new_cc = sum(len(c) for c in new_all_canon)
    await store.append_chat_messages(chat_id, [reply_canon], new_fp, new_cc)
    if handle.chat_url:
        await store.update_chat_url(chat_id, handle.chat_url)


def create_app(config: AppConfig, store_path: Path | None = None) -> FastAPI:
    store = Store(store_path or _DEFAULT_STORE)
    session_mgr = SessionManager()
    adapters: dict[str, BrowserChatAdapter] = {}
    for svc_name, svc in config.services.items():
        if not svc.enabled:
            continue
        session = session_mgr.get(svc_name, svc)
        adapters[svc_name] = BrowserChatAdapter(svc_name, svc, session, config.humanize)
    router = Router(config, adapters)
    budget = BudgetManager(store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await store.connect()
        log.info("bridge_started", services=list(adapters.keys()))
        try:
            yield
        finally:
            await session_mgr.close_all()
            await store.close()
            log.info("bridge_stopped")

    app = FastAPI(title="WebBridge", lifespan=lifespan)
    app.state.config = config
    app.state.store = store
    app.state.router = router
    app.state.budget = budget

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "services": list(adapters.keys())}

    @app.get("/metrics")
    async def prom_metrics():
        return Response(metrics.render_prometheus(), media_type="text/plain; version=0.0.4")

    @app.get("/v1/models", dependencies=[Depends(check_auth)])
    async def list_models():
        return ModelList(data=[Model(id=m) for m in router.list_models()])

    @app.get("/admin/sessions", dependencies=[Depends(check_auth)])
    async def admin_sessions():
        result = {}
        for name in adapters:
            svc = config.services[name]
            result[name] = await budget.status(name, svc.limits)
        return {"services": result}

    @app.post("/admin/chats/{chat_id}/reset", dependencies=[Depends(check_auth)])
    async def admin_reset(chat_id: str):
        await store.close_chat(chat_id)
        return {"ok": True, "chat_id": chat_id}

    # ------------------------------------------------------------------
    async def _prepare(req: ChatCompletionRequest):
        adapter, service_name, service_model = router.route(req.model)
        svc_cfg = config.services[service_name]
        await budget.check(service_name, svc_cfg.limits)

        status = await adapter.ensure_session()
        if status != "OK":
            if status == "NEEDS_LOGIN":
                raise NeedsLogin(service_name)
            if status == "CAPTCHA":
                raise CaptchaRequired(service_name)
            if status == "RATE_LIMITED":
                raise RateLimited(service_name)
            raise SelectorBroken("logged_in_indicator", service_name)

        kind, chat_row, delta = await match(store, service_name, req.messages)

        if kind == "new":
            handle = await adapter.new_chat(service_model)
            text = render_initial(req.messages, req.tools)
            init_canon = [canonical_message(m) for m in req.messages]
            import hashlib
            h = hashlib.sha256()
            for c in init_canon:
                h.update(c.encode("utf-8"))
                h.update(b"\n")
            init_fp = h.hexdigest()
            init_cc = sum(len(c) for c in init_canon)
            chat_id = await store.create_chat(
                service_name, handle.chat_url, service_model,
                init_fp, len(init_canon), init_cc,
            )
            await store.store_chat_messages(chat_id, init_canon, init_fp, init_cc)
        else:
            assert chat_row is not None
            chat_id = chat_row["id"]
            if chat_row.get("chat_url"):
                handle = await adapter.open_chat(chat_row["chat_url"])
            else:
                handle = ChatHandle(service=service_name, model=service_model)
            text = render_delta(delta)

        if len(text) > svc_cfg.limits.max_message_chars:
            raise MessageTooLong(svc_cfg.limits.max_message_chars, len(text))

        return adapter, service_name, service_model, svc_cfg, chat_id, handle, text

    async def _run_once(
        adapter, svc_cfg, handle: ChatHandle, text: str, chat_id: str, req: ChatCompletionRequest
    ):
        await adapter.send(handle, text)
        reply = await adapter.wait_done(handle, svc_cfg.limits.max_generation_s if hasattr(svc_cfg.limits, "max_generation_s") else 300.0)
        await _persist_reply(store, chat_id, req.messages, reply.text, handle)
        return reply

    @app.post("/v1/chat/completions", dependencies=[Depends(check_auth)])
    async def chat_completions(req: ChatCompletionRequest):
        started = time.monotonic()
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        try:
            adapter, service_name, service_model, svc_cfg, chat_id, handle, text = await _prepare(req)

            if req.stream:
                return StreamingResponse(
                    _stream_generator(
                        req, adapter, svc_cfg, handle, text, chat_id, service_name, request_id
                    ),
                    media_type="text/event-stream",
                )

            await budget.consume(service_name)
            reply = await _run_once(adapter, svc_cfg, handle, text, chat_id, req)
            duration_ms = int((time.monotonic() - started) * 1000)
            await store.log_request(
                service_name, chat_id, 1,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                duration_ms, "ok" if reply.finished else "timeout",
            )
            metrics.inc("requests_total")
            metrics.inc(f"requests_{service_name}_total")

            prompt_tokens = _estimate_tokens(text)
            completion_tokens = _estimate_tokens(reply.text)
            return ChatCompletionResponse(
                model=req.model,
                choices=[
                    Choice(
                        message=AssistantMessage(content=reply.text),
                        finish_reason="stop",
                    )
                ],
                usage=Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                ),
            )
        except Exception as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            log.error("chat_completions_failed", request_id=request_id,
                      error=str(e), error_type=type(e).__name__)
            metrics.inc("errors_total")
            metrics.inc(f"errors_{type(e).__name__}_total")
            return _map_error(e)

    async def _stream_generator(
        req, adapter, svc_cfg, handle, text, chat_id, service_name, request_id
    ) -> AsyncIterator[bytes]:
        response_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        started = time.monotonic()
        try:
            await budget.consume(service_name)
            await adapter.send(handle, text)
            timeout = getattr(svc_cfg.limits, "max_generation_s", 300.0) or 300.0
            wait_task = asyncio.create_task(adapter.wait_done(handle, timeout))

            yield chunk_bytes(response_id, req.model, {"role": "assistant"})
            while True:
                done, _ = await asyncio.wait({wait_task}, timeout=10)
                if wait_task in done:
                    break
                yield b": keep-alive\n\n"

            reply = wait_task.result()
            await _persist_reply(store, chat_id, req.messages, reply.text, handle)
            if reply.text:
                yield chunk_bytes(response_id, req.model, {"content": reply.text})
            yield chunk_bytes(response_id, req.model, {}, finish="stop")

            duration_ms = int((time.monotonic() - started) * 1000)
            await store.log_request(
                service_name, chat_id, 1,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                duration_ms, "ok" if reply.finished else "timeout",
            )
            metrics.inc("requests_total")
            metrics.inc(f"requests_{service_name}_total")
        except Exception as e:
            log.error("stream_failed", request_id=request_id,
                      error=str(e), error_type=type(e).__name__)
            metrics.inc("errors_total")
            yield chunk_bytes(
                response_id, req.model,
                {"content": f"\n[error: {type(e).__name__}: {e}]"},
                finish="stop",
            )
        finally:
            yield b"data: [DONE]\n\n"

    return app


def run(config: AppConfig) -> None:
    """Запустить мост через uvicorn."""
    import uvicorn

    configure_logging()
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.bridge.host,
        port=config.bridge.port,
        log_level="info",
        access_log=False,
    )
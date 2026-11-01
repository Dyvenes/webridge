"""FastAPI-приложение WebBridge: OpenAI-совместимый локальный API.

Единственный клиент — opencode. Приложение не содержит чат-интерфейса:
оно принимает OpenAI-запрос, проигрывает его в веб-чате через Playwright
и возвращает ответ в формате OpenAI, включая tool_calls.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

from webbridge.bridge.auth import check_auth, save_token
from webbridge.bridge.router import Router
from webbridge.bridge.schemas import ChatCompletionRequest, Model, ModelList
from webbridge.bridge.service import ConversationService
from webbridge.config import AppConfig, ensure_home
from webbridge.conversation.store import Store
from webbridge.errors import (
    CaptchaRequired,
    ChatLost,
    ConfigError,
    GenerationTimeout,
    MessageTooLong,
    ModelNotFound,
    ModelUnavailable,
    NeedsLogin,
    ProtocolError,
    RateLimited,
    RequestConflict,
    SelectorBroken,
    SessionBusy,
    UnsupportedContent,
    WebBridgeError,
)
from webbridge.limits.budget import BudgetManager
from webbridge.observability.logging import configure as configure_logging
from webbridge.observability.logging import get_logger
from webbridge.observability.metrics import Metrics

log = get_logger("app")

_STATUS: dict[type[Exception], tuple[int, str]] = {
    NeedsLogin: (503, "session_unavailable"),
    CaptchaRequired: (503, "session_unavailable"),
    SelectorBroken: (503, "service_unavailable"),
    ModelUnavailable: (503, "model_unavailable"),
    RateLimited: (429, "rate_limit_exceeded"),
    ModelNotFound: (404, "model_not_found"),
    MessageTooLong: (400, "invalid_request_error"),
    UnsupportedContent: (400, "invalid_request_error"),
    SessionBusy: (409, "session_busy"),
    RequestConflict: (409, "request_conflict"),
    ChatLost: (502, "chat_lost"),
    GenerationTimeout: (504, "timeout"),
    ProtocolError: (502, "bad_model_output"),
    ConfigError: (500, "server_error"),
}


def error_payload(
    message: str,
    type_: str = "server_error",
    *,
    code: str | None = None,
    param: str | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": type_,
            "param": param,
            "code": code,
        }
    }


def status_for(exc: Exception) -> tuple[int, str]:
    for cls, result in _STATUS.items():
        if isinstance(exc, cls):
            return result
    return 500, "server_error"


def _service_for_model(config: AppConfig, model: str) -> str:
    for name, svc in config.services.items():
        if not svc.enabled:
            continue
        if model in svc.models:
            return name
        for sep in (":", "/"):
            if sep in model and model.split(sep, 1)[0] == name:
                return name
    return "webbridge"


def create_app(
    config: AppConfig | None = None,
    *,
    config_path: Path | None = None,
    adapters: dict[str, Any] | None = None,
    store: Store | None = None,
    store_path: Path | None = None,
    api_token: str | None = None,
) -> FastAPI:
    """Создаёт приложение.

    Параметры с значениями по умолчанию нужны для тестов и встраивания:
    можно передать готовый конфиг, fake-адаптеры и Store.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging()
        if config is None:
            ensure_home()

        cfg = config or AppConfig.load(config_path)
        app.state.config = cfg
        app.state.api_token = api_token or save_token()
        app.state.metrics = Metrics()

        owns_store = store is None
        st = store or Store(store_path or cfg.bridge.db_path)
        if owns_store:
            await st.connect()
        app.state.store = st

        owns_adapters = adapters is None
        adapter_map: dict[str, Any] = dict(adapters or {})
        if owns_adapters:
            from webbridge.adapters.factory import create_adapter
            from webbridge.session.manager import SessionManager

            manager = SessionManager()
            for name, svc in cfg.services.items():
                if not svc.enabled:
                    continue
                session = manager.get(name, svc)
                adapter_map[name] = create_adapter(name, svc, session, cfg.humanize)

        router = Router(cfg, adapter_map)
        budget = BudgetManager(st)
        app.state.adapters = adapter_map
        app.state.router = router
        app.state.budget = budget
        app.state.service = ConversationService(cfg, st, router, budget)
        log.info(
            "bridge_started",
            host=cfg.bridge.host,
            port=cfg.bridge.port,
            services=sorted(adapter_map),
            models=router.list_models(),
        )
        try:
            yield
        finally:
            for adapter in adapter_map.values():
                try:
                    await adapter.close()
                except Exception as exc:  # noqa: BLE001 - shutdown не должен падать
                    log.warning("adapter_close_failed", error=str(exc))
            if owns_store:
                await st.close()

    app = FastAPI(
        title="WebBridge",
        version="0.2.0",
        docs_url="/docs" if (config and config.bridge.enable_docs) else None,
        redoc_url=None,
        openapi_url="/openapi.json" if (config and config.bridge.enable_docs) else None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def _metrics_middleware(request: Request, call_next):
        started = time.monotonic()
        metrics: Metrics | None = getattr(request.app.state, "metrics", None)
        if metrics is not None:
            metrics.inc("webbridge_active_requests")
        try:
            response = await call_next(request)
        except Exception:
            if metrics is not None:
                metrics.inc("webbridge_http_errors_total")
            raise
        finally:
            if metrics is not None:
                metrics.dec("webbridge_active_requests")
                metrics.inc("webbridge_http_requests_total")
                metrics.inc(
                    "webbridge_http_duration_seconds_sum",
                    time.monotonic() - started,
                )
        if metrics is not None:
            metrics.inc(f"webbridge_http_status_{response.status_code}_total")
        return response

    # --- error handlers -------------------------------------------------
    @app.exception_handler(WebBridgeError)
    async def _webbridge_error(_: Request, exc: WebBridgeError):
        status, type_ = status_for(exc)
        if status >= 500:
            log.error(
                "request_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                status=status,
            )
        headers = {"Retry-After": "5"} if status in (409, 429) else None
        return JSONResponse(
            status_code=status,
            content=error_payload(str(exc), type_, code=type(exc).__name__),
            headers=headers,
        )

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException):
        if exc.status_code == 401:
            type_ = "authentication_error"
        elif exc.status_code < 500:
            type_ = "invalid_request_error"
        else:
            type_ = "server_error"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(str(exc.detail), type_, code=str(exc.status_code)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        payload = error_payload(
            "invalid request body",
            "invalid_request_error",
            code="validation_error",
            param="body",
        )
        payload["error"]["details"] = exc.errors()
        return JSONResponse(status_code=400, content=payload)

    @app.exception_handler(Exception)
    async def _unhandled_error(_: Request, exc: Exception):
        log.error(
            "unhandled_exception",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content=error_payload(
                "internal server error",
                "server_error",
                code=type(exc).__name__,
            ),
        )

    # --- endpoints ------------------------------------------------------
    @app.get("/healthz")
    async def healthz(request: Request):
        return {
            "status": "ok",
            "version": "0.2.0",
            "services": sorted(getattr(request.app.state, "adapters", {})),
        }

    @app.get("/metrics", dependencies=[Depends(check_auth)])
    async def metrics(request: Request) -> Response:
        data = request.app.state.metrics.render_prometheus()
        return Response(content=data, media_type="text/plain; version=0.0.4")

    @app.get("/v1/models", dependencies=[Depends(check_auth)])
    async def list_models(request: Request) -> ModelList:
        cfg: AppConfig = request.app.state.config
        router: Router = request.app.state.router
        return ModelList(
            data=[
                Model(id=alias, owned_by=_service_for_model(cfg, alias))
                for alias in router.list_models()
            ]
        )

    @app.post("/v1/chat/completions", dependencies=[Depends(check_auth)])
    async def chat_completions(req: ChatCompletionRequest, request: Request):
        service: ConversationService = request.app.state.service
        headers = {k.lower(): v for k, v in request.headers.items()}

        turn = await service.begin(req, headers)
        if req.stream:
            return StreamingResponse(
                turn.stream_sse(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        try:
            await turn.complete()
            return turn.response()
        finally:
            await turn.aclose()

    @app.post("/admin/sessions/{session_id}/reset", dependencies=[Depends(check_auth)])
    async def reset_session(session_id: str, request: Request):
        service: ConversationService = request.app.state.service
        await service.reset_session(session_id)
        return {"ok": True, "session_id": session_id}

    return app


# Для `uvicorn webbridge.bridge.app:app` и обратной совместимости.
app = create_app()

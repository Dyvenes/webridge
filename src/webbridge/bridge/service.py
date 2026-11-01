"""Оркестрация одного хода: сессия → удалённый чат → ответ → commit."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from webbridge.adapters.base import ChatAdapter, ChatHandle
from webbridge.bridge.router import Route, Router
from webbridge.bridge.schemas import (
    AssistantMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    FunctionCall,
    ToolCall,
    Usage,
)
from webbridge.bridge.streaming import (
    chunk_bytes,
    done_chunk,
    error_chunk,
    stream_text,
    stream_tool_calls,
    usage_chunk,
)
from webbridge.bridge.titles import looks_like_title_prompt, make_title
from webbridge.config import AppConfig, ServiceConfig
from webbridge.conversation.history import (
    canonical_json,
    canonical_message,
    canonical_messages,
    conversation_messages,
    estimate_tokens,
    split_history,
    system_text,
    tools_hash,
    truncate_history,
)
from webbridge.conversation.store import Store
from webbridge.errors import (
    CaptchaRequired,
    ChatLost,
    GenerationTimeout,
    MessageTooLong,
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
from webbridge.observability.logging import get_logger
from webbridge.protocol.parse import has_tool_block, parse_tool_calls, strip_tool_blocks
from webbridge.protocol.render import (
    render_delta,
    render_initial,
    render_tools_update,
    tool_name_map,
)
from webbridge.protocol.schema_validate import validate_tool_call

log = get_logger("service")


def _request_id(headers: dict[str, str]) -> str:
    return headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return msg.get("content") or ""
    return ""


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") == "user":
            return msg.get("content") or ""
    return ""


def _looks_like_title(req: ChatCompletionRequest, messages: list[dict[str, Any]]) -> bool:
    # Некоторые клиенты присылают инструкцию отдельным system-сообщением,
    # другие — первым user-сообщением, а сам диалог идёт следом.
    if looks_like_title_prompt(system_text(messages)):
        return True
    return any(
        looks_like_title_prompt(msg.get("content") or "")
        for msg in messages
        if msg.get("role") == "user"
    )


_TEXT_PART_TYPES = {"text", "input_text", "output_text"}


def reject_unsupported_content(req: ChatCompletionRequest) -> None:
    """Отклоняет изображения, файлы и другие мультимодальные части.

    Веб-чаты в v1 принимают только текст. Лучше вернуть понятную ошибку до
    открытия браузера, чем отправить модели строку ``[image]`` и получить
    бессмысленный ответ.
    """
    if req.modalities:
        unsupported = [m for m in req.modalities if str(m).lower() != "text"]
        if unsupported:
            raise UnsupportedContent(
                f"unsupported modalities: {', '.join(map(str, unsupported))}; "
                "only text is supported"
            )

    for index, message in enumerate(req.messages):
        content = message.content
        if not isinstance(content, list):
            continue
        for part in content:
            part_type = None
            if isinstance(part, dict):
                part_type = part.get("type")
            else:
                part_type = getattr(part, "type", None)
            if part_type is None:
                # Строки и неизвестные объекты считаем текстом.
                continue
            if str(part_type) not in _TEXT_PART_TYPES:
                raise UnsupportedContent(
                    f"message {index}: unsupported content part "
                    f"{part_type!r}; only text is supported"
                )


def resolve_client_session_id(
    req: ChatCompletionRequest,
    headers: dict[str, str],
    messages: list[dict[str, Any]],
    ephemeral: bool,
) -> str:
    seed = system_text(messages) + "\n" + (_first_user_text(messages) or _last_user_text(messages))
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]

    # Title-запросы никогда не должны попадать в основной чат: иначе
    # opencode перемешает заголовок с историей диалога.
    if ephemeral:
        title_seed = system_text(messages) + "\n" + _last_user_text(messages)
        title_digest = hashlib.sha256(title_seed.encode("utf-8")).hexdigest()[:20]
        return f"title:{title_digest}"

    header_id = headers.get("x-webbridge-session-id")
    if header_id:
        return header_id.strip()
    if req.user:
        return f"user:{req.user}"
    return f"anon:{digest}"


def request_hash(
    req: ChatCompletionRequest,
    messages: list[dict[str, Any]],
    tools_fingerprint: str,
) -> str:
    h = hashlib.sha256()
    h.update(req.model.encode("utf-8"))
    h.update(b"\n")
    for msg in conversation_messages(messages):
        h.update(canonical_json(msg).encode("utf-8"))
        h.update(b"\n")
    h.update(tools_fingerprint.encode("utf-8"))
    h.update(json.dumps(req.tool_choice, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()


def _tool_choice_mode(tool_choice: Any) -> str:
    if tool_choice is None:
        return "auto"
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict):
        return "function"
    return "auto"


def _forced_tool_name(tool_choice: Any) -> str | None:
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        name = fn.get("name") if isinstance(fn, dict) else None
        return str(name) if name else None
    return None


@dataclass
class Turn:
    service: ConversationService
    req: ChatCompletionRequest
    route: Route
    service_cfg: ServiceConfig
    session: dict[str, Any]
    session_id: str
    turn_id: str | None
    req_hash: str
    incoming: list[dict[str, Any]]
    tools_fingerprint: str
    mode: str = "new"
    text: str = ""
    delta: list[dict[str, Any]] = field(default_factory=list)
    handle: ChatHandle | None = None
    idempotent_message: dict[str, Any] | None = None
    response_message: dict[str, Any] | None = None
    finish_reason: str = "stop"
    sent: bool = False
    committed: bool = False
    closed: bool = False
    failed: bool = False
    ephemeral: bool = False
    started_at: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock | None = None

    @property
    def adapter(self) -> ChatAdapter:
        return self.route.adapter

    @property
    def page_key(self) -> str:
        """Ключ вкладки. Служебные ходы не должны плодить вкладки."""
        return "ephemeral" if self.ephemeral else self.session_id

    @property
    def service_name(self) -> str:
        return self.route.service_name

    @property
    def service_model(self) -> str:
        return self.route.service_model

    @property
    def tool_mode(self) -> bool:
        return bool(self.req.tools) and _tool_choice_mode(self.req.tool_choice) != "none"

    # ------------------------------------------------------------------
    async def prepare(self) -> None:
        completed = await self.service.store.find_completed_turn(self.session_id, self.req_hash)
        if completed and completed.get("response_json"):
            self.mode = "idempotent"
            self.turn_id = None
            self.idempotent_message = json.loads(completed["response_json"])
            self.response_message = self.idempotent_message
            self.finish_reason = (
                "tool_calls" if self.idempotent_message.get("tool_calls") else "stop"
            )
            return

        inflight = await self.service.store.find_inflight_turn(self.session_id, self.req_hash)
        if inflight:
            raise RequestConflict("the same request is already in progress for this session")

        self.turn_id = await self.service.store.create_turn(
            self.session_id,
            self.req.request_id or f"req_{uuid.uuid4().hex[:12]}",
            self.req_hash,
        )

        if self.session.get("needs_reset") or (
            not self.session.get("chat_url") and not self.session.get("history")
        ):
            await self._start_new_chat(reason=self.session.get("reset_reason") or "new session")
            return

        delta_result = split_history(self.session["history"], self.incoming)
        self.mode = delta_result.mode

        if self.mode == "idempotent":
            # История совпала, но completed turn не найден. Повторять
            # пустую отправку нельзя — клиент должен повторить запрос.
            raise RequestConflict(
                "history is already committed but no completed response exists; retry the request"
            )

        if self.mode == "reset":
            await self._start_new_chat(reason=delta_result.reason)
            return

        try:
            self.handle = await self.adapter.open_chat(
                self.session.get("chat_url") or "", page_key=self.page_key
            )
        except ChatLost:
            await self._start_new_chat(reason="remote chat lost")
            return

        delta = list(delta_result.delta)

        if delta_result.system_changed:
            delta.insert(
                0,
                canonical_message({"role": "system", "content": delta_result.current_system}),
            )
        if self.tools_fingerprint and self.tools_fingerprint != self.session.get("tools_hash", ""):
            delta.insert(
                0,
                canonical_message(
                    {
                        "role": "system",
                        "content": render_tools_update(self.req.tools, self.req.tool_choice),
                    }
                ),
            )

        if not delta:
            raise RequestConflict("nothing to send; history already committed")

        name_map = tool_name_map(self.incoming)
        self.delta = delta
        self.text = render_delta(
            delta,
            tools_update=None,
            tool_choice=self.req.tool_choice,
            name_map=name_map,
        )

        if self._too_large(self.text):
            await self._start_new_chat(reason="delta too large")
            return

        if self._near_context_limit():
            await self._start_new_chat(reason="context limit")
            return

    def _too_large(self, text: str) -> bool:
        return len(text) > self.service_cfg.limits.max_message_chars

    def _near_context_limit(self) -> bool:
        estimated = estimate_tokens(json.dumps(self.incoming, ensure_ascii=False))
        return estimated > int(self.service_cfg.context_window * 0.8)

    async def _start_new_chat(self, reason: str) -> None:
        # Полный seed отправляется только в новый удалённый чат. Режим
        # нужен лишь для логов/диагностики: commit всегда пишет полную
        # историю, а не только дельту.
        log.warning("start_new_chat_reason", reason=reason, session_id=self.session_id)
        self.mode = "reset" if self.session.get("history") else "new"
        self.handle = await self.adapter.new_chat(self.service_model, page_key=self.page_key)
        await self.service.store.update_session_chat(self.session_id, self.handle.chat_url)

        seed = self.incoming
        text = render_initial(seed, self.req.tools, self.req.tool_choice)
        if self._too_large(text) or estimate_tokens(json.dumps(seed, ensure_ascii=False)) > int(
            self.service_cfg.context_window * 0.8
        ):
            seed = truncate_history(seed, int(self.service_cfg.limits.max_message_chars * 0.9))
            text = render_initial(seed, self.req.tools, self.req.tool_choice)

        if self._too_large(text):
            raise MessageTooLong(self.service_cfg.limits.max_message_chars, len(text))

        self.delta = seed
        self.text = text
        log.info(
            "new_remote_chat",
            session_id=self.session_id,
            service=self.service_name,
            reason=reason,
            chars=len(text),
        )

    # ------------------------------------------------------------------
    async def send(self) -> None:
        if self.mode == "idempotent":
            return
        if self.handle is None:
            raise WebBridgeError("turn has no chat handle")
        try:
            await self.adapter.send(self.handle, self.text)
            self.sent = True
            if self.turn_id:
                await self.service.store.update_turn(self.turn_id, "sent")
            await self.service.store.update_session_chat(self.session_id, self.handle.chat_url)
            await self.service.budget.consume(self.service_name)
        except Exception as exc:
            await self._fail(exc, reset=True)
            raise

    async def wait(self) -> dict[str, Any]:
        if self.mode == "idempotent" and self.idempotent_message is not None:
            return self.idempotent_message
        if self.handle is None:
            raise WebBridgeError("turn has no chat handle")
        try:
            reply = await self.adapter.wait_done(
                self.handle,
                timeout_s=self.service_cfg.limits.max_generation_s,
            )
            message, finish = await self._build_response(reply.text)
            self.response_message = message
            self.finish_reason = finish
            return message
        except Exception as exc:
            await self._fail(exc, reset=True)
            raise

    async def _build_response(self, text: str) -> tuple[dict[str, Any], str]:
        text = text or ""
        if not self.tool_mode:
            message = canonical_message({"role": "assistant", "content": text})
            return message, "stop"

        if not has_tool_block(text):
            if _tool_choice_mode(self.req.tool_choice) in ("required", "function"):
                raise ProtocolError(
                    "model did not emit a tool call, but tool_choice requires one",
                    raw_text=text,
                )
            message = canonical_message({"role": "assistant", "content": text})
            return message, "stop"

        try:
            parsed = parse_tool_calls(text, fmt=self.service_cfg.protocol_format)
        except ValueError as exc:
            raise ProtocolError(f"invalid tool block: {exc}", raw_text=text) from exc

        if not parsed:
            raise ProtocolError("tool block found but no calls parsed", raw_text=text)
        if len(parsed) > 1 and self.req.parallel_tool_calls is False:
            raise ProtocolError(
                "model emitted multiple tool calls but parallel_tool_calls is false",
                raw_text=text,
            )

        forced = _forced_tool_name(self.req.tool_choice)
        calls: list[dict[str, Any]] = []
        for call in parsed:
            if forced and call.name != forced:
                raise ProtocolError(
                    f"tool_choice requires {forced!r}, model called {call.name!r}",
                    raw_text=text,
                )
            args, raw_args = validate_tool_call(call, self.req.tools or [])
            calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": raw_args,
                    },
                }
            )

        content = strip_tool_blocks(text)
        message = canonical_message(
            {
                "role": "assistant",
                "content": content or None,
                "tool_calls": calls,
            }
        )
        return message, "tool_calls"

    async def complete(self) -> dict[str, Any]:
        """Нестриминговый ход целиком: send → wait → commit."""
        if self.mode == "idempotent" and self.idempotent_message is not None:
            return self.idempotent_message
        await self.send()
        message = await self.wait()
        await self.commit(message)
        return message

    async def commit(self, message: dict[str, Any]) -> None:
        if self.committed:
            return
        history = self.incoming + [canonical_message(message)]
        if self.turn_id is None:
            self.committed = True
            return
        await self.service.store.commit_turn(
            self.session_id,
            self.turn_id,
            history,
            message,
            chat_url=self.handle.chat_url if self.handle else None,
            tools_hash=self.tools_fingerprint,
        )
        self.response_message = message
        self.committed = True
        if self.ephemeral:
            await self.service.store.close_session(self.session_id)

    async def _fail(self, exc: Exception, reset: bool = True) -> None:
        if self.failed:
            return
        self.failed = True
        if self.turn_id:
            await self.service.store.fail_turn(
                self.turn_id,
                f"{type(exc).__name__}: {exc}",
                session_id=self.session_id,
                reset=reset,
            )

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.sent and not self.committed and not self.failed:
            # Клиент оборвал запрос (Esc/закрытие соединения). Останавливаем
            # генерацию на сайте, чтобы следующая дельта не ждала её конца.
            if self.handle is not None:
                cancel = getattr(self.adapter, "cancel", None)
                if cancel is not None:
                    try:
                        await cancel(self.handle)
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "cancel_failed",
                            session_id=self.session_id,
                            service=self.service_name,
                            error=str(exc),
                        )
            await self._fail(WebBridgeError("turn abandoned before commit"), reset=True)
        elif not self.sent and self.turn_id and not self.committed:
            await self._fail(WebBridgeError("turn closed before send"), reset=False)
        if self.lock is not None and self.lock.locked():
            self.lock.release()

    # ------------------------------------------------------------------
    def response(self) -> ChatCompletionResponse:
        message = self.response_message or {}
        tool_calls = None
        if message.get("tool_calls"):
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    function=FunctionCall(
                        name=tc["function"]["name"],
                        arguments=tc["function"]["arguments"],
                    ),
                )
                for tc in message["tool_calls"]
            ]
        return ChatCompletionResponse(
            model=self.req.model,
            choices=[
                Choice(
                    message=AssistantMessage(
                        content=message.get("content"),
                        tool_calls=tool_calls,
                    ),
                    finish_reason=self.finish_reason,  # type: ignore[arg-type]
                )
            ],
            usage=self.usage_model(),
        )

    def usage(self) -> dict[str, Any]:
        prompt = estimate_tokens(self.text)
        completion = estimate_tokens((self.response_message or {}).get("content") or "")
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    def usage_model(self) -> Usage:
        data = self.usage()
        return Usage(**data)

    # ------------------------------------------------------------------
    async def stream_sse(self) -> AsyncIterator[bytes]:
        response_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        include_usage = bool(self.req.stream_options and self.req.stream_options.include_usage)
        try:
            if self.mode == "idempotent" and self.idempotent_message is not None:
                async for chunk in self._replay(response_id, self.idempotent_message):
                    yield chunk
                return

            if self.handle is None:
                raise WebBridgeError("turn has no chat handle")

            if not self.sent:
                await self.send()

            if self.tool_mode:
                # Буферизуем ответ, чтобы не отправить клиенту текстовый
                # tool-блок вместо OpenAI tool_calls.
                parts: list[str] = []
                async for delta in self.adapter.stream(self.handle):
                    if delta:
                        parts.append(delta)
                text = "".join(parts)
                if not text:
                    raise GenerationTimeout(
                        f"{self.service_name}: stream finished without response"
                    )
                message, finish = await self._build_response(text)
                await self.commit(message)
                async for chunk in self._emit(response_id, message, finish, include_usage):
                    yield chunk
                return

            # SSE нужен opencode как транспорт, но текст отдаём только
            # после завершения генерации. DOM веб-чата может перерисовывать
            # markdown, и частичные дельты тогда теряют/дублируют текст.
            yield chunk_bytes(response_id, self.req.model, {"role": "assistant", "content": ""})
            text_parts: list[str] = []
            async for delta in self.adapter.stream(self.handle):
                if delta:
                    text_parts.append(delta)
            text = "".join(text_parts)
            if not text:
                raise GenerationTimeout(f"{self.service_name}: stream finished without response")
            message, finish = await self._build_response(text)
            await self.commit(message)
            content = message.get("content") or ""
            if content:
                yield chunk_bytes(response_id, self.req.model, {"content": content})
            yield chunk_bytes(response_id, self.req.model, {}, finish=finish)
            if include_usage:
                yield usage_chunk(response_id, self.req.model, self.usage())
            yield done_chunk()
        except asyncio.CancelledError:
            await self._fail(WebBridgeError("client disconnected during stream"), reset=True)
            raise
        except Exception as exc:
            await self._fail(exc, reset=True)
            log.error(
                "stream_failed",
                session_id=self.session_id,
                service=self.service_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            yield error_chunk(
                response_id,
                self.req.model,
                {
                    "message": str(exc) or type(exc).__name__,
                    "type": self.service.error_type(exc),
                    "code": type(exc).__name__,
                },
            )
            yield done_chunk()
        finally:
            await self.aclose()

    async def _replay(self, response_id: str, message: dict[str, Any]) -> AsyncIterator[bytes]:
        if message.get("tool_calls"):
            async for chunk in stream_tool_calls(
                response_id,
                self.req.model,
                message["tool_calls"],
                content=message.get("content"),
            ):
                yield chunk
        else:
            async for chunk in stream_text(
                response_id, self.req.model, message.get("content") or ""
            ):
                yield chunk

    async def _emit(
        self,
        response_id: str,
        message: dict[str, Any],
        finish: str,
        include_usage: bool,
    ) -> AsyncIterator[bytes]:
        if message.get("tool_calls"):
            async for chunk in stream_tool_calls(
                response_id,
                self.req.model,
                message["tool_calls"],
                content=message.get("content"),
                include_usage=include_usage,
                usage=self.usage(),
            ):
                yield chunk
        else:
            async for chunk in stream_text(
                response_id,
                self.req.model,
                message.get("content") or "",
                include_usage=include_usage,
                usage=self.usage(),
            ):
                yield chunk


class ConversationService:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        router: Router,
        budget: BudgetManager,
    ):
        self.config = config
        self.store = store
        self.router = router
        self.budget = budget
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def begin(
        self,
        req: ChatCompletionRequest,
        headers: dict[str, str],
    ) -> Turn:
        reject_unsupported_content(req)
        route = self.router.route(req.model)
        service_cfg = self.config.services[route.service_name]

        incoming = canonical_messages(req.messages)
        ephemeral = headers.get("x-webbridge-kind", "").lower() == "title" or _looks_like_title(
            req, incoming
        )

        if ephemeral:
            # Заголовок генерируется локально: без браузера, бюджета и
            # отдельной вкладки. Это также работает до `webbridge login`.
            title = make_title(incoming)
            title_message = canonical_message({"role": "assistant", "content": title})
            title_session_id = f"title:{uuid.uuid4().hex[:12]}"
            turn = Turn(
                service=self,
                req=req,
                route=route,
                service_cfg=service_cfg,
                session={"id": title_session_id, "history": []},
                session_id=title_session_id,
                turn_id=None,
                req_hash="",
                incoming=incoming,
                tools_fingerprint="",
                mode="idempotent",
                idempotent_message=title_message,
                response_message=title_message,
                finish_reason="stop",
                ephemeral=True,
            )
            turn.req.request_id = _request_id(headers)  # type: ignore[attr-defined]
            return turn

        status = await route.adapter.ensure_session()
        if status != "OK":
            if status == "NEEDS_LOGIN":
                raise NeedsLogin(route.service_name)
            if status == "CAPTCHA":
                raise CaptchaRequired(route.service_name)
            if status == "RATE_LIMITED":
                raise RateLimited(route.service_name)
            raise SelectorBroken("logged_in_indicator", route.service_name)

        client_session_id = resolve_client_session_id(req, headers, incoming, ephemeral)
        session = await self.store.get_or_create_session(
            client_session_id, route.service_name, req.model
        )

        lock = self._lock_for(session["id"])
        try:
            await asyncio.wait_for(lock.acquire(), timeout=0.05)
        except TimeoutError as exc:
            raise SessionBusy(
                f"session {client_session_id} already has an in-flight request"
            ) from exc

        req_hash = request_hash(req, incoming, tools_hash(req.tools))
        turn = Turn(
            service=self,
            req=req,
            route=route,
            service_cfg=service_cfg,
            session=session,
            session_id=session["id"],
            turn_id=None,
            req_hash=req_hash,
            incoming=incoming,
            tools_fingerprint=tools_hash(req.tools),
            ephemeral=ephemeral,
            lock=lock,
        )
        turn.req.request_id = _request_id(headers)  # type: ignore[attr-defined]

        try:
            await self.budget.check(route.service_name, service_cfg.limits)
            await turn.prepare()
        except Exception as exc:
            if turn.turn_id is not None:
                await self.store.fail_turn(
                    turn.turn_id,
                    f"{type(exc).__name__}: {exc}",
                    session_id=turn.session_id,
                    reset=False,
                )
            if lock.locked():
                lock.release()
            raise
        return turn

    async def reset_session(self, session_id: str, reason: str = "admin reset") -> None:
        await self.store.mark_needs_reset(session_id, reason)
        lock = self._locks.pop(session_id, None)
        if lock and lock.locked():
            lock.release()

    @staticmethod
    def error_type(exc: Exception) -> str:
        if isinstance(exc, (NeedsLogin, CaptchaRequired)):
            return "session_unavailable"
        if isinstance(exc, RateLimited):
            return "rate_limit_exceeded"
        if isinstance(exc, GenerationTimeout):
            return "timeout"
        if isinstance(exc, ProtocolError):
            return "bad_model_output"
        if isinstance(exc, MessageTooLong):
            return "invalid_request_error"
        if isinstance(exc, SessionBusy):
            return "session_busy"
        if isinstance(exc, RequestConflict):
            return "request_conflict"
        if isinstance(exc, ModelUnavailable):
            return "model_unavailable"
        if isinstance(exc, ChatLost):
            return "chat_lost"
        if isinstance(exc, SelectorBroken):
            return "service_unavailable"
        if isinstance(exc, UnsupportedContent):
            return "invalid_request_error"
        return "server_error"

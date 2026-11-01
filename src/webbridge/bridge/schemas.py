"""OpenAI-совместимые Pydantic-модели."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


# --- request ---------------------------------------------------------------
class FunctionCall(BaseModel):
    name: str
    arguments: Any = "{}"


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    refusal: str | None = None


class ToolDef(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    strict: bool | None = None


class Tool(BaseModel):
    type: Literal["function"] = "function"
    function: ToolDef


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    tools: list[Tool] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None

    # Внутреннее поле: заполняется из X-Request-ID. В JSON-схеме запроса
    # не документируется, клиенты его не присылают.
    request_id: str | None = Field(default=None, exclude=True)

    # Принимаются и игнорируются (веб-чат не поддерживает эти параметры).
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    n: int | None = None
    stop: Any | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    user: str | None = None
    response_format: Any | None = None
    seed: int | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    metadata: dict[str, Any] | None = None
    store: bool | None = None
    modalities: list[str] | None = None


# --- response --------------------------------------------------------------
class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_tokens_details: dict[str, Any] | None = None
    completion_tokens_details: dict[str, Any] | None = None
    x_webbridge_estimated: bool = True


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    refusal: str | None = None


class Choice(BaseModel):
    index: int = 0
    message: AssistantMessage
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter", "error"] = "stop"
    logprobs: Any | None = None


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:16]}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage | None = None
    system_fingerprint: str | None = None


class Model(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "webbridge"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[Model]


class ErrorBody(BaseModel):
    message: str
    type: str = "server_error"
    param: str | None = None
    code: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody

"""OpenAI-совместимые Pydantic-модели (раздел 11)."""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


# --- request ---
class FunctionCall(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Any | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class ToolDef(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class Tool(BaseModel):
    type: Literal["function"] = "function"
    function: ToolDef


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    tools: list[Tool] | None = None
    tool_choice: str | dict | None = None
    stream: bool = False
    # Остальные поля принимаются и игнорируются:
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    n: int | None = None
    stop: Any | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    user: str | None = None


# --- response ---
class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    x_webbridge_estimated: bool = True


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class Choice(BaseModel):
    index: int = 0
    message: AssistantMessage
    finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:16]}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage | None = None


class Model(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "webbridge"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[Model]


class ErrorBody(BaseModel):
    type: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
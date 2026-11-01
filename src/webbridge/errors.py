"""Иерархия ошибок WebBridge."""

from __future__ import annotations

from datetime import datetime


class WebBridgeError(Exception):
    """Базовая ошибка."""


class ConfigError(WebBridgeError):
    """Некорректная или отсутствующая конфигурация."""


class NeedsLogin(WebBridgeError):
    """Требуется ручной вход в сервис."""


class CaptchaRequired(WebBridgeError):
    """Сервис требует прохождения капчи."""


class RateLimited(WebBridgeError):
    def __init__(self, message: str = "rate limited", reset_at: datetime | None = None):
        super().__init__(message)
        self.reset_at = reset_at


class SelectorBroken(WebBridgeError):
    def __init__(self, element: str, service: str, message: str | None = None):
        self.element = element
        self.service = service
        text = message or f"selector broken for element '{element}' in service '{service}'"
        super().__init__(text)


class GenerationTimeout(WebBridgeError):
    """Модель не завершила генерацию за отведённое время."""


class MessageTooLong(WebBridgeError):
    def __init__(self, limit: int, actual: int | None = None):
        self.limit = limit
        self.actual = actual
        if actual is not None:
            text = f"message too long: {actual} chars > limit {limit}"
        else:
            text = f"message too long: limit is {limit}"
        super().__init__(text)


class UnsupportedContent(WebBridgeError):
    """Клиент прислал изображение, файл или иной не поддержанный контент."""

    def __init__(self, detail: str = "only text content is supported"):
        super().__init__(detail)


class ProtocolError(WebBridgeError):
    """Ответ модели не соответствует протоколу инструментов."""

    def __init__(self, message: str, raw_text: str | None = None):
        super().__init__(message)
        self.raw_text = raw_text


class ToolValidationError(ProtocolError):
    """Аргументы tool call не прошли JSON Schema."""


class ModelNotFound(WebBridgeError):
    def __init__(self, model: str, known: list[str] | None = None):
        self.model = model
        self.known = known or []
        text = f"model not found: {model}"
        if self.known:
            text += f"; known models: {', '.join(self.known)}"
        super().__init__(text)


class ModelUnavailable(WebBridgeError):
    """Модель есть в конфиге, но её не удалось выбрать в веб-интерфейсе."""


class ChatLost(WebBridgeError):
    """Удалённый чат больше недоступен; нужен новый чат."""


class SessionBusy(WebBridgeError):
    """По этой сессии уже выполняется запрос."""


class RequestConflict(WebBridgeError):
    """Повторный запрос не может быть выполнен идемпотентно."""

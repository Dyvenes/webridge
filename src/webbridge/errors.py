"""Иерархия ошибок (раздел 8.2 PROJECT.md)."""
from __future__ import annotations

from datetime import datetime


class WebBridgeError(Exception):
    """Базовая ошибка."""


class NeedsLogin(WebBridgeError):
    """Требуется ручной вход в сервис."""


class CaptchaRequired(WebBridgeError):
    """Сервис требует прохождения капчи."""


class RateLimited(WebBridgeError):
    def __init__(self, message: str = 'rate limited', reset_at: datetime | None = None):
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
            text = f'message too long: {actual} chars > limit {limit}'
        else:
            text = f'message too long: limit is {limit}'
        super().__init__(text)


class ProtocolError(WebBridgeError):
    """Ответ модели не соответствует протоколу после всех повторов."""


class Tier2Unavailable(WebBridgeError):
    """Внутренняя ошибка: сигнал к откату на tier 1."""
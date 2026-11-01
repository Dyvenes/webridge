"""Фабрика браузерных адаптеров по типу сервиса."""

from __future__ import annotations

from webbridge.config import HumanizeConfig, ServiceConfig
from webbridge.session.manager import BrowserSession

from .browser_base import BrowserChatAdapter
from .claude import ClaudeAdapter
from .deepseek import DeepSeekAdapter


def create_adapter(
    name: str,
    service: ServiceConfig,
    session: BrowserSession,
    humanize_cfg: HumanizeConfig,
) -> BrowserChatAdapter:
    """Возвращает специализированный адаптер для ``service.type``."""
    kind = (service.type or "generic").strip().lower()
    if kind == "deepseek":
        return DeepSeekAdapter(name, service, session, humanize_cfg)
    if kind == "claude":
        return ClaudeAdapter(name, service, session, humanize_cfg)
    return BrowserChatAdapter(name, service, session, humanize_cfg)

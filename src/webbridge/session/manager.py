"""Управление постоянными браузерными профилями (раздел 7)."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from webbridge.config import ServiceConfig
from webbridge.observability.logging import get_logger

log = get_logger("session")

SessionStatus = str

_IDLE_CLOSE_S = 15 * 60


@dataclass
class BrowserSession:
    service: str
    config: ServiceConfig
    _playwright: Any = None
    _context: Any = None
    _last_used: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    async def _launch_context(self):
        from playwright.async_api import async_playwright
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        opts = {
            "headless": self.config.browser.headless,
            "permissions": ["clipboard-read", "clipboard-write"],
            "accept_downloads": True,
            "viewport": {"width": 1366, "height": 900},
            "locale": "en-US",  # Добавляем локаль
            # Основной аргумент для скрытия автоматизации
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        channel = self.config.browser.channel
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.config.profile_dir), channel=channel, **opts,
            )
        except Exception as e:
            if not channel:
                raise
            log.warning("channel_launch_failed", service=self.service,
                        channel=channel, error=str(e))
            # Если Chrome не найден, пробуем без channel, но с аргументом
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.config.profile_dir), **opts,
            )

    async def ensure_started(self) -> None:
        async with self._lock:
            if self._context is None:
                await self._launch_context()

    async def get_page(self):
        await self.ensure_started()
        pages = self._context.pages
        page = pages[0] if pages else await self._context.new_page()
        self._last_used = time.monotonic()
        return page

    async def close(self) -> None:
        async with self._lock:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    async def any_visible(self, page, candidates: list[str], timeout_ms: int = 500) -> bool:
        if not candidates:
            return False
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_ms / 1000.0
        while True:
            for sel in candidates:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=100):
                        return True
                except Exception:
                    continue
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    async def check_health(self, page, selectors: dict) -> SessionStatus:
        # Приоритет: CAPTCHA > RATE_LIMITED > NEEDS_LOGIN > OK > UNKNOWN
        if await self.any_visible(page, selectors.get("captcha_indicator", [])):
            return "CAPTCHA"
        if await self.any_visible(page, selectors.get("rate_limit_banner", [])):
            return "RATE_LIMITED"
        if await self.any_visible(page, selectors.get("login_page_indicator", [])):
            return "NEEDS_LOGIN"
        if await self.any_visible(
                page, selectors.get("logged_in_indicator", []), timeout_ms=3000
        ):
            return "OK"
        return "UNKNOWN"

    async def maybe_close_idle(self) -> None:
        if self._context is not None and time.monotonic() - self._last_used > _IDLE_CLOSE_S:
            log.info("closing_idle_session", service=self.service)
            await self.close()


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSession] = {}

    def get(self, service: str, config: ServiceConfig) -> BrowserSession:
        sess = self._sessions.get(service)
        if sess is None:
            sess = BrowserSession(service=service, config=config)
            self._sessions[service] = sess
        return sess

    async def close_all(self) -> None:
        for s in list(self._sessions.values()):
            await s.close()
        self._sessions.clear()
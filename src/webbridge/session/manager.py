"""Управление постоянными браузерными профилями и вкладками.

Одна вкладка на ключ (обычно — client session id). Это позволяет держать
несколько диалогов одного сервиса, не перезаписывая input друг друга.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from webbridge.config import ServiceConfig
from webbridge.observability.logging import get_logger

log = get_logger("session")

SessionStatus = str

_DEFAULT_IDLE_CLOSE_S = 15 * 60


@dataclass
class BrowserSession:
    service: str
    config: ServiceConfig
    _playwright: Any = None
    _context: Any = None
    _pages: dict[str, Any] = field(default_factory=dict, repr=False)
    _last_used: dict[str, float] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
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
            "locale": "en-US",
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        channel = self.config.browser.channel
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.config.profile_dir), channel=channel, **opts
            )
        except Exception as exc:
            if not channel:
                raise
            log.warning(
                "channel_launch_failed",
                service=self.service,
                channel=channel,
                error=str(exc),
            )
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.config.profile_dir), **opts
            )

    async def ensure_started(self) -> None:
        async with self._lock:
            if self._context is not None:
                try:
                    if not self._context.pages and self._context.is_closed():
                        self._context = None
                except Exception:
                    self._context = None

            if self._context is None:
                await self._launch_context()
                log.info("browser_started", service=self.service)

    async def get_page(self, key: str = "default"):
        await self.ensure_started()
        page = self._pages.get(key)
        if page is not None:
            try:
                if not page.is_closed():
                    self._last_used[key] = time.monotonic()
                    return page
            except Exception:
                pass
            self._pages.pop(key, None)

        try:
            page = await self._context.new_page()
        except Exception:
            # Контекст мог умереть — перезапускаем один раз.
            async with self._lock:
                self._context = None
            await self.ensure_started()
            page = await self._context.new_page()

        self._pages[key] = page
        self._last_used[key] = time.monotonic()
        return page

    async def close_page(self, key: str) -> None:
        page = self._pages.pop(key, None)
        self._last_used.pop(key, None)
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

    async def close(self) -> None:
        async with self._lock:
            for page in list(self._pages.values()):
                try:
                    await page.close()
                except Exception:
                    pass
            self._pages.clear()
            self._last_used.clear()

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

    # ------------------------------------------------------------------
    async def any_visible(self, page, candidates: list[str], timeout_ms: int = 500) -> bool:
        if not candidates:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000.0
        while True:
            for selector in candidates:
                try:
                    loc = page.locator(selector).first
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
        if await self.any_visible(page, selectors.get("logged_in_indicator", []), timeout_ms=3000):
            return "OK"
        return "UNKNOWN"

    async def maybe_close_idle(self, max_idle_s: float = _DEFAULT_IDLE_CLOSE_S) -> None:
        now = time.monotonic()
        for key, last in list(self._last_used.items()):
            if now - last > max_idle_s:
                log.info("closing_idle_page", service=self.service, page_key=key)
                await self.close_page(key)


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
        for sess in list(self._sessions.values()):
            await sess.close()
        self._sessions.clear()

"""Интерактивный вход (раздел 7.2)."""

from __future__ import annotations

import asyncio
import time

from webbridge.config import ServiceConfig
from webbridge.observability.logging import get_logger

log = get_logger("login")


async def interactive_login(service_name: str, config: ServiceConfig, timeout_s: int = 600) -> bool:
    from webbridge.config import load_selectors
    from webbridge.session.manager import BrowserSession

    if config.browser.headless:
        log.warning("login_requires_headed", service=service_name)

    session = BrowserSession(service_name, config)
    try:
        await session.ensure_started()
        page = await session.get_page()
        await page.goto(config.base_url, wait_until="domcontentloaded")
        selectors = load_selectors(config)
        logged_in = selectors.get("logged_in_indicator", [])
        deadline = time.monotonic() + timeout_s
        log.info("login_waiting", service=service_name)
        while time.monotonic() < deadline:
            if await session.any_visible(page, logged_in, timeout_ms=2000):
                log.info("login_ok", service=service_name)
                return True
            await asyncio.sleep(2.0)
        log.error("login_timeout", service=service_name)
        return False
    except Exception as e:
        log.error("login_failed", service=service_name, error=str(e))
        return False
    finally:
        await session.close()

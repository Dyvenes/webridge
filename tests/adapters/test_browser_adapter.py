"""Тесты браузерного адаптера на фейковом сайте."""
from __future__ import annotations

from pathlib import Path

import pytest

from webbridge.adapters.browser_base import BrowserChatAdapter
from webbridge.config import BrowserConfig, HumanizeConfig, ServiceConfig
from webbridge.session.manager import BrowserSession

SELECTORS_YAML = """
version: 1
logged_in_indicator:
  - '[data-testid="sidebar"]'
login_page_indicator:
  - 'input[type="password"]'
new_chat_button:
  - 'role=link[name="New chat"]'
input:
  - 'role=textbox'
send_button:
  - 'role=button[name="Send"]'
stop_button:
  - 'role=button[name="Stop"]'
assistant_message:
  - '[data-message-author="assistant"]'
copy_button_in_message:
  - 'role=button[name="Copy"]'
captcha_indicator:
  - 'text=/verify you are human/i'
rate_limit_banner:
  - 'text=/limit|try again|usage cap/i'
"""


@pytest.fixture
async def adapter_env(tmp_path: Path):
    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright not installed")
    from playwright.async_api import async_playwright

    try:
        p = await async_playwright().start()
        try:
            b = await p.chromium.launch(headless=True)
            await b.close()
        finally:
            await p.stop()
    except Exception as e:
        pytest.skip(f"chromium not available: {e}")

    from tests.fake_site.server import FakeSite

    with FakeSite() as site:
        sel_file = tmp_path / "sel.yaml"
        sel_file.write_text(SELECTORS_YAML, encoding="utf-8")
        cfg = ServiceConfig(
            base_url=site.url,
            profile_dir=tmp_path / "profile",
            selectors_file=sel_file,
            browser=BrowserConfig(channel=None, headless=True),
            models={},
        )
        humanize = HumanizeConfig(
            min_delay_s=0.01, max_delay_s=0.02, between_requests_s=(0.01, 0.02)
        )
        session = BrowserSession(service="fake", config=cfg)
        adapter = BrowserChatAdapter("fake", cfg, session, humanize)

        # Pre-login
        page = await session.get_page()
        await page.goto(site.url + "do_login", wait_until="domcontentloaded")

        try:
            yield adapter, cfg, site
        finally:
            await adapter.close()


@pytest.mark.asyncio
async def test_ensure_session_ok(adapter_env):
    adapter, _, _ = adapter_env
    assert await adapter.ensure_session() == "OK"


@pytest.mark.asyncio
async def test_new_chat_and_send(adapter_env):
    adapter, _, _ = adapter_env
    handle = await adapter.new_chat()
    await adapter.send(handle, "hello")
    reply = await adapter.wait_done(handle, timeout_s=10.0, stable_s=0.3)
    assert reply.finished
    assert "hello" in reply.text


@pytest.mark.asyncio
async def test_multiturn(adapter_env):
    adapter, _, _ = adapter_env
    handle = await adapter.new_chat()
    await adapter.send(handle, "first")
    r1 = await adapter.wait_done(handle, timeout_s=10.0, stable_s=0.3)
    assert "first" in r1.text

    await adapter.send(handle, "second")
    r2 = await adapter.wait_done(handle, timeout_s=10.0, stable_s=0.3)
    assert "second" in r2.text
    assert r2.text != r1.text
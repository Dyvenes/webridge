"""Тесты слоя сессий на фейковом сайте."""

from __future__ import annotations

from pathlib import Path

import pytest

from webbridge.config import BrowserConfig, ServiceConfig, load_selectors
from webbridge.session.manager import BrowserSession

SELECTORS_YAML = """
version: 1
logged_in_indicator:
  - '[data-testid="sidebar"]'
login_page_indicator:
  - 'input[type="password"]'
captcha_indicator:
  - 'text=/verify you are human/i'
rate_limit_banner:
  - 'text=/limit|try again|usage cap/i'
"""


@pytest.fixture
def fake_site():
    from tests.fake_site.server import FakeSite

    with FakeSite() as site:
        yield site


def _skip_if_no_browser() -> None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright not installed")


@pytest.fixture
async def session_env(fake_site, tmp_path: Path):
    _skip_if_no_browser()
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

    sel_file = tmp_path / "selectors.yaml"
    sel_file.write_text(SELECTORS_YAML, encoding="utf-8")

    cfg = ServiceConfig(
        base_url=fake_site.url,
        profile_dir=tmp_path / "profile",
        selectors_file=sel_file,
        browser=BrowserConfig(channel=None, headless=True),
        models={},
    )
    session = BrowserSession(service="fake", config=cfg)
    try:
        yield session, cfg, fake_site
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_health_needs_login(session_env):
    session, cfg, site = session_env
    page = await session.get_page()
    await page.goto(site.url + "login", wait_until="domcontentloaded")
    assert await session.check_health(page, load_selectors(cfg)) == "NEEDS_LOGIN"


@pytest.mark.asyncio
async def test_health_ok_after_login(session_env):
    session, cfg, site = session_env
    page = await session.get_page()
    await page.goto(site.url + "do_login", wait_until="domcontentloaded")
    await page.wait_for_load_state("domcontentloaded")
    assert await session.check_health(page, load_selectors(cfg)) == "OK"


@pytest.mark.asyncio
async def test_health_captcha(session_env):
    session, cfg, site = session_env
    page = await session.get_page()
    await page.goto(site.url + "captcha", wait_until="domcontentloaded")
    assert await session.check_health(page, load_selectors(cfg)) == "CAPTCHA"


@pytest.mark.asyncio
async def test_health_rate_limited(session_env):
    session, cfg, site = session_env
    page = await session.get_page()
    await page.goto(site.url + "limited", wait_until="domcontentloaded")
    assert await session.check_health(page, load_selectors(cfg)) == "RATE_LIMITED"

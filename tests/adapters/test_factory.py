"""Тесты фабрики адаптеров и конвертации Markdown."""

from __future__ import annotations

from pathlib import Path

import pytest

from webbridge.adapters.browser_base import BrowserChatAdapter
from webbridge.adapters.claude import ClaudeAdapter, claude_html_to_markdown
from webbridge.adapters.deepseek import (
    DeepSeekAdapter,
    deepseek_html_to_markdown,
)
from webbridge.adapters.factory import create_adapter
from webbridge.config import BrowserConfig, HumanizeConfig, ServiceConfig
from webbridge.session.manager import BrowserSession


def _service(tmp_path: Path, kind: str) -> ServiceConfig:
    return ServiceConfig(
        type=kind,
        base_url="https://example.invalid/",
        profile_dir=tmp_path / kind,
        selectors_file=tmp_path / f"{kind}.yaml",
        browser=BrowserConfig(channel=None, headless=True),
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("deepseek", DeepSeekAdapter),
        ("claude", ClaudeAdapter),
        ("generic", BrowserChatAdapter),
    ],
)
def test_factory_selects_adapter(tmp_path: Path, kind: str, expected):
    service = _service(tmp_path, kind)
    session = BrowserSession(service=kind, config=service)
    adapter = create_adapter(kind, service, session, HumanizeConfig())
    assert isinstance(adapter, expected)


def test_deepseek_markdown_keeps_code_language():
    html = (
        '<div class="md-code-block">'
        '<div class="md-code-block-banner-wrap">'
        '<div class="md-code-block-banner">'
        '<span class="d813de27">python</span>'
        "</div></div>"
        "<pre><code>print(1)</code></pre></div>"
    )
    text = deepseek_html_to_markdown(html)
    assert "```python" in text
    assert "print(1)" in text
    assert "md-code-block-banner" not in text


def test_claude_markdown_uses_code_language_and_drops_buttons():
    html = (
        '<div class="font-claude-message">'
        '<button aria-label="Copy">Copy</button>'
        "<p>Hello</p>"
        '<pre><code class="language-python">print(1)</code></pre>'
        "</div>"
    )
    text = claude_html_to_markdown(html)
    assert "```python" in text
    assert "print(1)" in text
    assert "Copy" not in text

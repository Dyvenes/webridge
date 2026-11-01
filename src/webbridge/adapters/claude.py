"""Специализированный адаптер Claude (claude.ai)."""

from __future__ import annotations

from bs4 import BeautifulSoup, NavigableString
from markdownify import markdownify as html_to_markdown

from .browser_base import BrowserChatAdapter, strip_ui_nodes


def _code_language(pre, code) -> str | None:
    """Язык может быть в классе как ``<code>``, так и ``<pre>``."""
    for node in (code, pre):
        if not node:
            continue
        for cls in node.get("class", []) or []:
            if cls.startswith("language-"):
                return cls.removeprefix("language-")
    return None


def claude_html_to_markdown(html: str) -> str:
    """Конвертирует HTML ответа Claude в Markdown с языками блоков кода."""
    soup = BeautifulSoup(html, "html.parser")
    strip_ui_nodes(soup)

    for pre in soup.find_all("pre"):
        code = pre.find("code")
        language = _code_language(pre, code)
        if not language:
            # Без языка markdownify сам сделает обычный ```-блок.
            continue

        code_text = code.get_text() if code else pre.get_text()
        fence = "```"
        while fence in code_text:
            fence += "`"

        replacement = f"\n\n{fence}{language}\n{code_text.rstrip()}\n{fence}\n\n"
        pre.replace_with(NavigableString(replacement))

    return html_to_markdown(
        str(soup),
        heading_style="ATX",
        bullets="-",
    ).strip()


class ClaudeAdapter(BrowserChatAdapter):
    """Claude: стандартные ``pre/code`` с классами ``language-*``."""

    type = "claude"

    def extract_markdown(self, html: str) -> str:
        return claude_html_to_markdown(html)

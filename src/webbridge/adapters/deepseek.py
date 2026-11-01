"""Специализированный адаптер DeepSeek (chat.deepseek.com)."""

from __future__ import annotations

from bs4 import BeautifulSoup, NavigableString
from markdownify import markdownify as html_to_markdown

from .browser_base import BrowserChatAdapter, strip_ui_nodes

# Служебные подписи в баннере блока кода, которые не являются языком.
_BANNER_NOISE = {"copy", "копировать", "code", "код"}


def _code_language(pre, code) -> str | None:
    """Достаёт язык из ``language-*`` или из баннера DeepSeek."""
    if code:
        for cls in code.get("class", []) or []:
            if cls.startswith("language-"):
                return cls.removeprefix("language-")

    block = pre.find_parent(class_="md-code-block")
    if not block:
        return None

    banner = block.select_one(".md-code-block-banner .d813de27")
    if banner is None:
        banner = block.select_one(".md-code-block-banner span")
    if banner is None:
        banner = block.select_one(".md-code-block-banner")
    if banner is None:
        return None

    for part in banner.get_text(" ", strip=True).split():
        if part.lower() not in _BANNER_NOISE:
            return part
    return None


def deepseek_html_to_markdown(html: str) -> str:
    """Конвертирует HTML ответа DeepSeek в Markdown, сохраняя язык кода."""
    soup = BeautifulSoup(html, "html.parser")

    # Обрабатываем блоки кода отдельно.
    for pre in soup.find_all("pre"):
        code = pre.find("code")
        language = _code_language(pre, code)

        # Извлекаем содержимое без HTML-тегов подсветки.
        code_text = pre.get_text()

        fence = "```"
        while fence in code_text:
            fence += "`"

        replacement = f"\n\n{fence}{language or ''}\n{code_text.rstrip()}\n{fence}\n\n"
        pre.replace_with(NavigableString(replacement))

    # Удаляем баннеры с названиями языков и кнопки копирования.
    for banner in soup.select(".md-code-block-banner-wrap, .md-code-block-banner"):
        banner.decompose()
    strip_ui_nodes(soup)

    result = html_to_markdown(
        str(soup),
        heading_style="ATX",
        bullets="-",
    )
    return result.strip()


class DeepSeekAdapter(BrowserChatAdapter):
    """DeepSeek: особая разметка блоков кода (``.md-code-block``)."""

    type = "deepseek"

    def extract_markdown(self, html: str) -> str:
        return deepseek_html_to_markdown(html)

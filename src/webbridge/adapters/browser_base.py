"""Базовый браузерный адаптер (раздел 8.3–8.6)."""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from webbridge.config import HumanizeConfig, ServiceConfig, load_selectors
from webbridge.errors import (
    CaptchaRequired,
    GenerationTimeout,
    MessageTooLong,
    NeedsLogin,
    RateLimited,
    SelectorBroken,
    WebBridgeError,
)
from webbridge.observability.logging import get_logger
from webbridge.session.manager import BrowserSession
from markdownify import markdownify as html_to_markdown

from . import humanize
from .base import ChatHandle, Reply

log = get_logger("adapter.browser")

_BUTTON_RE = re.compile(r"<button\b[^>]*>.*?</button>", re.DOTALL | re.IGNORECASE)



from bs4 import BeautifulSoup, NavigableString
from markdownify import markdownify as html_to_markdown


def deepseek_html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # Обрабатываем блоки кода отдельно.
    for pre in soup.find_all("pre"):
        code = pre.find("code")

        # Извлекаем язык из класса language-*.
        language = None

        if code:
            for cls in code.get("class", []):
                if cls.startswith("language-"):
                    language = cls.removeprefix("language-")
                    break

        # В DeepSeek язык может находиться в баннере.
        if not language:
            block = pre.find_parent(class_="md-code-block")

            if block:
                banner = block.select_one(
                    ".md-code-block-banner .d813de27"
                )
                if banner:
                    language = banner.get_text(strip=True)

        # Извлекаем содержимое без HTML-тегов подсветки.
        code_text = pre.get_text()

        # Заменяем pre на готовый Markdown-блок.
        fence = "```"
        while fence in code_text:
            fence += "`"

        replacement = (
            f"\n\n{fence}{language or ''}\n"
            f"{code_text.rstrip()}\n"
            f"{fence}\n\n"
        )

        pre.replace_with(NavigableString(replacement))

    # Удаляем оставшиеся баннеры с названиями языков.
    for banner in soup.select(".md-code-block-banner-wrap"):
        banner.decompose()

    # Конвертируем остальную разметку.
    result = html_to_markdown(
        str(soup),
        heading_style="ATX",
        bullets="-",
    )

    return result.strip()


class BrowserChatAdapter:
    """Общая логика Playwright-адаптера; специализация — через selectors YAML."""

    def __init__(
        self,
        name: str,
        service: ServiceConfig,
        session: BrowserSession,
        humanize_cfg: HumanizeConfig,
    ):
        self.name = name
        self.config = service
        self._session = session
        self._humanize = humanize_cfg
        self._selectors = load_selectors(service)
        self.selectors = self._selectors

    # ------------------------------------------------------------------
    async def _find(self, page, key: str, timeout_ms: int = 5000):
        candidates = self._selectors.get(key, [])
        if not candidates:
            raise SelectorBroken(key, self.name)
        per = max(500, timeout_ms // len(candidates))
        last_exc: Exception | None = None
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=per)
                return loc
            except Exception as e:
                last_exc = e
                continue
        raise SelectorBroken(key, self.name, message=str(last_exc) if last_exc else None)

    async def _has(self, page, key: str, timeout_ms: int = 500) -> bool:
        try:
            await self._find(page, key, timeout_ms=timeout_ms)
            return True
        except SelectorBroken:
            return False

    # ------------------------------------------------------------------
    async def ensure_session(self) -> str:
        page = await self._session.get_page()
        if not page.url.startswith(self.config.base_url):
            await page.goto(self.config.base_url, wait_until="domcontentloaded")
        return await self._session.check_health(page, self._selectors)

    async def new_chat(self, model: str | None = None) -> ChatHandle:
        page = await self._session.get_page()
        status = await self._session.check_health(page, self.selectors)
        if status == "NEEDS_LOGIN":
            raise NeedsLogin()
        if status == "CAPTCHA":
            raise CaptchaRequired()
        if status == "RATE_LIMITED":
            raise RateLimited(self.name)

        opened = False
        try:
            btn = await self._find(page, "new_chat_button", timeout_ms=8000)
            try:
                async with page.expect_navigation(timeout=3000):
                    await btn.click()
            except Exception:
                pass
            opened = True
        except SelectorBroken as e:
            log.warning(
                "new_chat_button_missing_fallback_to_url",
                service=self.name, error=str(e),
            )

        if not opened:
            # Новый чат = корневой URL сайта. Это надёжнее любого селектора.
            await page.goto(self.config.base_url, wait_until="domcontentloaded")
            await asyncio.sleep(0.5)

        if model:
            await self._select_model(page, model)

        return ChatHandle(
            service=self.name, chat_url=page.url, model=model,
            meta={"page": page},
        )

    async def open_chat(self, chat_url: str) -> ChatHandle:
        page = await self._session.get_page()
        if page.url != chat_url:
            await page.goto(chat_url, wait_until="domcontentloaded")
        return ChatHandle(service=self.name, chat_url=page.url, model=None,
                          meta={"page": page})

    async def _select_model(self, page, model: str) -> None:
        try:
            opener = await self._find(page, "model_picker_open", timeout_ms=3000)
        except SelectorBroken:
            log.warning("model_picker_not_found", service=self.name)
            return
        await opener.click()
        options = self._selectors.get("model_picker_option", [])
        for cand in options:
            sel = cand.replace("{model}", model)
            try:
                opt = page.locator(sel).first
                await opt.wait_for(state="visible", timeout=3000)
                await opt.click()
                return
            except Exception:
                continue
        log.warning("model_option_not_found", service=self.name, model=model)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    async def send(
            self, chat: ChatHandle, text: str, files: Sequence[Path] = ()
    ) -> None:
        if len(text) > self.config.limits.max_message_chars:
            raise MessageTooLong(self.config.limits.max_message_chars, len(text))

        page = await self._session.get_page()
        if chat.chat_url and page.url != chat.chat_url:
            await page.goto(chat.chat_url, wait_until="domcontentloaded")

        if files:
            await self._upload_files(page, files)

        await humanize.pre_send(self._humanize)

        inp = await self._find(page, "input")
        # Запоминаем состояние переписки до отправки.
        assistant_count_before, assistant_text_before = (
            await self._assistant_state(page)
        )

        chat.meta["assistant_count_before"] = assistant_count_before
        chat.meta["assistant_text_before"] = assistant_text_before
        try:
            await inp.click()
            if self._humanize.typing_mode == "type" and len(text) < 2000:
                for ch in text:
                    await page.keyboard.type(ch)
                    await asyncio.sleep(0.03)
            else:
                try:
                    await inp.fill(text)
                except Exception:
                    await page.keyboard.insert_text(text)
        except Exception as e:
            raise SelectorBroken("input", self.name, message=str(e))

        # Сколько user-сообщений было до отправки — для верификации.
        user_before = 0
        for sel in self.selectors.get("user_message", []):
            try:
                user_before = await page.locator(sel).count()
                if user_before:
                    break
            except Exception:
                continue

        # Отправка: кнопка, при её отсутствии — Enter.
        sent = False
        try:
            send_btn = await self._find(page, "send_button", timeout_ms=2000)
            await send_btn.click()
            sent = True
        except SelectorBroken as e:
            log.warning(
                "send_button_missing_fallback_to_enter",
                service=self.name, error=str(e),
            )

        if not sent:
            await page.keyboard.press("Enter")

        # Верификация отправки по состоянию интерфейса.
        # Учитываем начало генерации и появление нового ответа ассистента.
        for _ in range(150):
            if await self._has(page, "stop_button", timeout_ms=100):
                return

            assistant_count, assistant_text = await self._assistant_state(page)

            if assistant_count > assistant_count_before:
                return

            if (
                    assistant_text
                    and assistant_text != assistant_text_before
            ):
                return

            # Дополнительная проверка появления сообщения пользователя.
            for sel in self.selectors.get("user_message", []):
                try:
                    if await page.locator(sel).count() > user_before:
                        return
                except Exception:
                    continue

            await asyncio.sleep(0.1)

        raise WebBridgeError(f"send_not_confirmed service={self.name}")

    async def _upload_files(self, page, files: Sequence[Path]) -> None:
        file_input = await self._find(page, "file_input", timeout_ms=3000)
        await file_input.set_input_files([str(f) for f in files])

    # ------------------------------------------------------------------
    async def _assistant_state(self, page) -> tuple[int, str]:
        """Возвращает количество сообщений ассистента и Markdown последнего."""

        for key in ("assistant_message", "assistant_message_content"):
            for sel in self.selectors.get(key, []):
                try:
                    msgs = page.locator(sel)
                    count = await msgs.count()

                    if count == 0:
                        continue

                    last = msgs.nth(count - 1)

                    if not await last.is_visible():
                        continue

                    html = await last.evaluate("""
                        (el) => {
                            const copy = el.cloneNode(true);
                        
                            // Удаляем элементы управления и служебные элементы.
                            copy.querySelectorAll(
                                'button, [role="button"], svg, script, style'
                            ).forEach(node => node.remove());
                        
                            // Восстанавливаем язык блоков кода.
                            copy.querySelectorAll('.md-code-block').forEach(block => {
                                const pre = block.querySelector('pre');
                        
                                if (!pre) return;
                        
                                const languageElement = block.querySelector(
                                    '.md-code-block-banner span'
                                );
                        
                                const language = languageElement?.textContent?.trim();
                        
                                if (language) {
                                    // markdownify распознаёт language-* в классе code.
                                    const code = document.createElement('code');
                        
                                    code.className = `language-${language}`;
                                    code.innerHTML = pre.innerHTML;
                        
                                    pre.innerHTML = '';
                                    pre.appendChild(code);
                                }
                        
                                // Удаляем баннер с названием языка,
                                // чтобы он не попал в Markdown как обычный текст.
                                block.querySelectorAll(
                                    '.md-code-block-banner-wrap'
                                ).forEach(node => node.remove());
                            });
                        
                            return copy.innerHTML;
                        }

                    """)

                    text = deepseek_html_to_markdown(html)

                    if text:
                        return count, text

                except Exception as e:
                    log.debug(
                        "assistant_state_failed",
                        selector=sel,
                        error=str(e),
                    )

        return 0, ""


    async def _extract_dom(self, page) -> str:
        _, text = await self._assistant_state(page)
        return text

    async def _extract_last_reply(self, page) -> str:
        return await self._extract_dom(page)

    async def _extract_final(self, page) -> str:
        for sel in self.selectors.get("copy_button_in_message", []):
            try:
                btn = page.locator(sel).last
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    await asyncio.sleep(0.15)
                    text = await page.evaluate("() => navigator.clipboard.readText()")
                    if text and text.strip():
                        return text
                    break  # кнопка есть, но буфер пуст → падаем в DOM
            except Exception as e:
                log.warning("clipboard_extract_failed", service=self.name, error=str(e))
        return await self._extract_dom(page)

    async def wait_done(
            self,
            chat: ChatHandle,
            timeout_s: float = 300.0,
            stable_s: float = 1.5,
    ) -> Reply:

        page = await self._session.get_page()

        started = time.monotonic()
        deadline = started + timeout_s

        baseline_count = chat.meta.get("assistant_count_before", 0)
        baseline_text = chat.meta.get("assistant_text_before", "")

        last_text = ""
        last_change = started
        saw_generation = False

        while time.monotonic() < deadline:

            if await self._has(page, "captcha_indicator", timeout_ms=100):
                raise CaptchaRequired(self.name)

            if await self._has(page, "rate_limit_banner", timeout_ms=100):
                raise RateLimited(self.name)

            stop_visible = await self._has(
                page, "stop_button", timeout_ms=100
            )

            if stop_visible:
                saw_generation = True

            assistant_count, current = await self._assistant_state(page)

            # Ответ должен отличаться от состояния до отправки.
            is_new_reply = (
                    assistant_count > baseline_count
                    or (current and current != baseline_text)
            )

            if is_new_reply and current:
                if current != last_text:
                    last_text = current
                    last_change = time.monotonic()

            stable_for = time.monotonic() - last_change

            if (
                    is_new_reply
                    and last_text
                    and not stop_visible
                    and stable_for >= stable_s
            ):
                log.info(
                    "generation_completed",
                    service=self.name,
                    length=len(last_text),
                    duration=round(time.monotonic() - started, 2),
                )

                return Reply(
                    text=last_text,
                    finished=True,
                    meta={
                        "duration_s": round(
                            time.monotonic() - started, 2
                        )
                    },
                )

            log.info(
                "wait_tick",
                service=self.name,
                stop=stop_visible,
                assistant_count=assistant_count,
                baseline_count=baseline_count,
                is_new_reply=is_new_reply,
                len_text=len(current),
                stable=round(stable_for, 2),
            )

            await asyncio.sleep(0.25)

        # Последняя попытка извлечения ответа.
        text = await self._extract_last_reply(page)

        raise GenerationTimeout(
            f"{self.name}: no completion after {timeout_s:.0f}s; "
            f"last len={len(text)}"
        )

    async def stream(self, chat: ChatHandle) -> AsyncIterator[str]:
        page = await self._session.get_page()

        started = time.monotonic()
        deadline = started + 300

        baseline_count = chat.meta.get("assistant_count_before", 0)
        baseline_text = chat.meta.get("assistant_text_before", "")

        prev = ""
        last_change = started
        saw_reply = False
        last_log = started

        log.info(
            "stream_started",
            service=self.name,
            url=page.url,
            baseline_count=baseline_count,
            selectors=self.selectors.get("assistant_message", []),
        )

        while time.monotonic() < deadline:
            try:
                count, current = await self._assistant_state(page)
            except Exception as e:
                log.warning(
                    "stream_extract_error",
                    service=self.name,
                    error=str(e),
                )
                count, current = 0, ""

            is_new_reply = (
                    count > baseline_count
                    or (bool(current) and current != baseline_text)
            )

            stop_visible = await self._has(
                page, "stop_button", timeout_ms=100
            )

            if is_new_reply and current:
                saw_reply = True

                if current != prev:
                    delta = current[len(prev):] if current.startswith(prev) else current

                    if delta:
                        yield delta

                    prev = current
                    last_change = time.monotonic()

            now = time.monotonic()

            # Диагностика раз в 2 секунды.
            if now - last_log >= 2:
                log.info(
                    "stream_tick",
                    service=self.name,
                    assistant_count=count,
                    baseline_count=baseline_count,
                    is_new_reply=is_new_reply,
                    saw_reply=saw_reply,
                    text_length=len(current),
                    stop_visible=stop_visible,
                )
                last_log = now

            # Завершаем поток только после появления ответа
            # и прекращения генерации.
            if (
                    saw_reply
                    and not stop_visible
                    and now - last_change >= 1.5
            ):
                log.info(
                    "stream_completed",
                    service=self.name,
                    text_length=len(prev),
                )
                return

            await asyncio.sleep(0.2)

        log.warning(
            "stream_timeout",
            service=self.name,
            saw_reply=saw_reply,
            text_length=len(prev),
        )

    async def close(self) -> None:
        await self._session.close()


        """
        .idea/inspectionProfiles/Project_Default.xml
        new file:   .idea/inspectionProfiles/profiles_settings.xml
        new file:   .idea/misc.xml
        new file:   .idea/modules.xml
        new file:   .idea/vcs.xml
        new file:   .idea/webbridge.iml
"""
"""Базовый браузерный адаптер (раздел 8.3–8.6).

Одна вкладка браузера соответствует одному ``page_key`` (обычно — id
клиентской сессии opencode). Адаптер никогда не берёт вкладку заново во
время работы с ``ChatHandle``: страница лежит в ``chat.meta["page"]``.
"""

from __future__ import annotations

import asyncio
import time
import re
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_markdown

from webbridge.config import HumanizeConfig, ServiceConfig, load_selectors
from webbridge.errors import (
    CaptchaRequired,
    ChatLost,
    GenerationTimeout,
    MessageTooLong,
    ModelUnavailable,
    NeedsLogin,
    RateLimited,
    SelectorBroken,
    WebBridgeError,
)
from webbridge.observability.logging import get_logger
from webbridge.session.manager import BrowserSession
from webbridge.adapters.chatgpt_markdown import restore_fence_languages

from . import humanize
from .base import ChatHandle, Reply

log = get_logger("adapter.browser")

_UI_SELECTOR = 'button, [role="button"], svg, script, style, noscript'

_LANG_LABEL_RE = re.compile(r"(?m)^[ \t]*tool[ \t]*\r?\n+[ \t]*(`{3,})[ \t]*\r?\n")

_REPEAT_TAIL_RE = re.compile(r"(?s)(\S{4,80}?)(?:\s*\1){9,}\s*\Z")


def _pre_language(el) -> str:
    """Язык блока для markdownify. Блок с <call name=…> всегда считаем tool."""
    if re.search(r"<call\s+name\s*=", el.get_text()):
        return "tool"
    code = el.find("code")
    for node in (code, el):
        if node is None:
            continue
        for cls in node.get("class") or []:
            if cls.startswith("language-"):
                return cls[len("language-"):]
    return ""


def _find_degenerate_loop(text: str) -> int | None:
    """Индекс начала зацикленного хвоста (10+ повторов тега DSML), иначе None."""
    tail_start = max(0, len(text) - 1500)
    m = _REPEAT_TAIL_RE.search(text[tail_start:])
    if not m:
        return None
    unit = m.group(1)
    if "｜" not in unit and "DSML" not in unit:
        return None  # обычные повторы (разделители, таблицы) не считаем зацикливанием
    return tail_start + m.start()


def strip_ui_nodes(soup: BeautifulSoup) -> None:
    """Удаляет кнопки и служебные элементы перед конвертацией в Markdown."""
    for node in soup.select(_UI_SELECTOR):
        node.decompose()


def _same_origin(left: str, right: str) -> bool:
    """True, если URL относятся к одному origin (схема + хост + порт)."""
    try:
        a = urlparse(left or "")
        b = urlparse(right or "")
        return bool(a.scheme and a.netloc) and ((a.scheme, a.netloc) == (b.scheme, b.netloc))
    except Exception:
        return False


class BrowserChatAdapter:
    """Общая логика Playwright-адаптера; специализация — через selectors YAML."""

    type = "generic"

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
        # Кэш Markdown: (id(page), selector, len(innerText), последние 200 символов).
        self._markdown_cache: dict[tuple[int, str, int, str], str] = {}

    # ------------------------------------------------------------------
    # Поиск элементов
    # ------------------------------------------------------------------
    async def _find(self, page, key: str, timeout_ms: int = 5000):
        candidates = self._selectors.get(key, [])
        if not candidates:
            raise SelectorBroken(key, self.name)
        per = max(250, timeout_ms // len(candidates))
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
        """Быстрая проверка видимости без ожидания каждого кандидата."""
        candidates = self._selectors.get(key, [])
        if not candidates:
            return False
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        while True:
            for sel in candidates:
                try:
                    if await page.locator(sel).first.is_visible():
                        return True
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)

    async def _match(self, page, key: str, timeout_ms: int = 100) -> str | None:
        """Как _has, но возвращает сработавший селектор (для диагностики)."""
        candidates = self._selectors.get(key, [])
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        while True:
            for sel in candidates:
                try:
                    if await page.locator(sel).first.is_visible():
                        return sel
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.05)

    async def _captcha_present(self, page) -> bool:
        sel = await self._match(page, "captcha_indicator", 100)
        if sel is None:
            return False
        if not sel.startswith("text="):
            log.warning("captcha_detected", service=self.name, selector=sel, kind="structural")
            return True
        # Текст мог просто встретиться в самой беседе (код, результаты инструментов).
        # Настоящая проверка закрывает поле ввода.
        if not await self._has(page, "input", timeout_ms=300):
            log.warning("captcha_detected", service=self.name, selector=sel, kind="text_no_input")
            return True
        return False

    async def _message_count(self, page, key: str) -> int:
        best = 0
        for sel in self._selectors.get(key, []):
            try:
                best = max(best, await page.locator(sel).count())
            except Exception:
                continue
        return best

    async def _messages_empty(self, page) -> bool:
        for key in (
                "user_message",
                "assistant_message",
                "assistant_message_content",
        ):
            if await self._message_count(page, key) > 0:
                return False
        return True

    def _raise_for_status(self, status: str) -> None:
        if status == "NEEDS_LOGIN":
            raise NeedsLogin(self.name)
        if status == "CAPTCHA":
            raise CaptchaRequired(self.name)
        if status == "RATE_LIMITED":
            raise RateLimited(self.name)
        if status == "UNKNOWN":
            log.warning("session_status_unknown", service=self.name)

    async def _ensure_base_page(self, page) -> None:
        """Открывает base_url только если вкладка ещё не на нужном сайте."""
        if not _same_origin(page.url, self.config.base_url):
            await page.goto(self.config.base_url, wait_until="domcontentloaded")

    # ------------------------------------------------------------------
    # Сессия и чаты
    # ------------------------------------------------------------------
    async def ensure_session(self) -> str:
        # Закрываем вкладки, которые давно не использовались: у opencode
        # сессий может быть много, а браузерный профиль — один.
        await self._session.maybe_close_idle()
        page = await self._session.get_page("default")
        await self._ensure_base_page(page)
        return await self._session.check_health(page, self._selectors)

    async def new_chat(self, model: str | None = None, page_key: str = "default") -> ChatHandle:
        log.warning("TRYING TO CREATE NEW CHAT")
        """Создаёт ChatHandle на отдельной странице браузера.

        Новая вкладка автоматически открывает чистый чат DeepSeek.
        Нажатие кнопки создания чата не требуется.
        """
        page = await self._session.get_page(page_key)
        # Новый чат: вкладка должна стоять на чистой стартовой странице.
        if page.url.rstrip("/") != self.config.base_url.rstrip("/"):
            await page.goto(self.config.base_url, wait_until="domcontentloaded")

        # Проверяем состояние авторизации и доступность сервиса.
        log.warning("Проверяем состояние авторизации и доступность сервиса.")
        status = await self._session.check_health(page, self.selectors)
        self._raise_for_status(status)

        log.warning("# Ждём появления поля ввода на уже открытом новом чате.")
        try:
            await self._find(page, "input", timeout_ms=15000)
        except SelectorBroken as e:
            raise ChatLost(
                f"{self.name}: chat input not found after opening page: {e}"
            ) from e

        log.warning("# Выбираем модель, если она была явно указана.")
        if model:
            selected = await self._select_model(page, model)
            if not selected and self.config.strict_model:
                raise ModelUnavailable(
                    f"{self.name}: model {model!r} is not available in UI"
                )

        log.info(
            "new_chat_page_ready",
            service=self.name,
            page_key=page_key,
            url=page.url,
            model=model,
        )

        return ChatHandle(
            service=self.name,
            chat_url=page.url,
            model=model,
            page_key=page_key,
            meta={"page": page},
        )

    async def _wait_chat_changed(self, page, before_url: str, timeout_s: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if page.url != before_url:
                return True
            if await self._messages_empty(page):
                return True
            await asyncio.sleep(0.1)
        return False

    async def open_chat(self, chat_url: str, page_key: str = "default") -> ChatHandle:
        page = await self._session.get_page(page_key)
        if chat_url and page.url != chat_url:
            await page.goto(chat_url, wait_until="domcontentloaded")

        try:
            await self._find(page, "input", timeout_ms=8000)
        except SelectorBroken as e:
            raise ChatLost(f"{self.name}: chat input not found at {chat_url}: {e}") from e

        if not await self._has(page, "chat_container", timeout_ms=500):
            log.warning(
                "chat_container_not_found",
                service=self.name,
                url=page.url,
            )

        return ChatHandle(
            service=self.name,
            chat_url=page.url,
            model=None,
            page_key=page_key,
            meta={"page": page},
        )

    async def _select_model(self, page, model: str) -> bool:
        ui_model = self.config.models.get(model, model)
        try:
            opener = await self._find(page, "model_picker_open", timeout_ms=3000)
        except SelectorBroken:
            log.warning("model_picker_not_found", service=self.name, model=ui_model)
            return False

        try:
            await opener.click()
        except Exception as e:
            log.warning(
                "model_picker_open_failed",
                service=self.name,
                model=ui_model,
                error=str(e),
            )
            return False

        for cand in self._selectors.get("model_picker_option", []):
            sel = cand.replace("{model}", ui_model)
            try:
                opt = page.locator(sel).first
                await opt.wait_for(state="visible", timeout=3000)
                await opt.click()
                log.info("model_selected", service=self.name, model=ui_model)
                return True
            except Exception:
                continue

        log.warning("model_option_not_found", service=self.name, model=ui_model)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Отправка и ожидание ответа
    # ------------------------------------------------------------------
    def _chat_page(self, chat: ChatHandle):
        page = chat.meta.get("page")
        if page is None:
            raise WebBridgeError(f"chat handle has no page (service={self.name})")
        return page

    async def _ensure_chat_url(self, page, chat: ChatHandle) -> None:
        target = chat.chat_url
        if not target:
            return
        if page.url == target or page.url.rstrip("/") == target.rstrip("/"):
            return
        # Если вкладка уже на том же сайте, не уводим её с текущего чата:
        # после первого сообщения сайт может сменить URL на /chat/<id>.
        if not _same_origin(page.url, target):
            await page.goto(target, wait_until="domcontentloaded")

    async def send(self, chat: ChatHandle, text: str, files: Sequence[Path] = ()) -> None:
        log.warning("send_start", service=self.name, text_len=len(text), files=len(files))

        if len(text) > self.config.limits.max_message_chars:
            log.error("send_message_too_long", service=self.name,
                      limit=self.config.limits.max_message_chars, actual=len(text))
            raise MessageTooLong(self.config.limits.max_message_chars, len(text))

        log.warning("send_step_chat_page", service=self.name)
        page = self._chat_page(chat)

        log.warning("send_step_ensure_chat_url", service=self.name)
        await self._ensure_chat_url(page, chat)

        if files:
            log.warning("send_step_upload_files", service=self.name, count=len(files))
            await self._upload_files(page, files)
            log.warning("send_step_upload_files_done", service=self.name)

        log.warning("send_step_humanize_pre_send", service=self.name)
        await humanize.pre_send(self._humanize)

        log.warning("send_step_find_input", service=self.name)
        inp = await self._find(page, "input")
        log.warning("send_step_find_input_done", service=self.name)

        # Snapshot conversation state before sending.
        log.warning("send_step_capture_state_before", service=self.name)
        assistant_count_before, assistant_text_before = await self._assistant_state(page)
        user_before = await self._message_count(page, "user_message")
        log.warning("send_state_before_captured", service=self.name,
                    assistant_count=assistant_count_before,
                    user_count=user_before,
                    assistant_text_len=len(assistant_text_before or ""))

        chat.meta["assistant_count_before"] = assistant_count_before
        chat.meta["assistant_text_before"] = assistant_text_before
        chat.meta["user_count_before"] = user_before

        try:
            log.warning("send_step_click_input", service=self.name)
            await inp.click()
            if self._humanize.typing_mode == "type" and len(text) < 2000:
                log.warning("send_step_typing_char_by_char", service=self.name, chars=len(text))
                for ch in text:
                    await page.keyboard.type(ch)
                    await asyncio.sleep(0.03)
                log.warning("send_step_typing_done", service=self.name)
            else:
                log.warning("send_step_fill_input", service=self.name, mode="fill")
                try:
                    await inp.fill(text)
                    log.warning("send_step_fill_done", service=self.name)
                except Exception as fill_err:
                    log.warning("send_fill_failed_fallback_insert_text",
                                service=self.name, error=str(fill_err))
                    await page.keyboard.insert_text(text)
                    log.warning("send_step_insert_text_done", service=self.name)
        except Exception as e:
            log.error("send_input_broken", service=self.name, error=str(e))
            raise SelectorBroken("input", self.name, message=str(e)) from e

        # Send: try button first, fall back to Enter.
        sent = False
        log.warning("send_step_find_send_button", service=self.name)
        try:
            send_btn = await self._find(page, "send_button", timeout_ms=2000)
            try:
                log.warning("send_step_click_send_button", service=self.name)
                await send_btn.click()
                sent = True
                log.warning("send_button_clicked", service=self.name)
            except Exception as e:
                log.warning("send_button_click_failed", service=self.name, error=str(e))
        except SelectorBroken as e:
            log.warning("send_button_missing_fallback_to_enter", service=self.name, error=str(e))

        if not sent:
            log.warning("send_step_press_enter", service=self.name)
            await page.keyboard.press("Enter")

        # Verify send via UI state (max 15 seconds).
        log.warning("send_step_verify_start", service=self.name)
        deadline = time.monotonic() + 15.0
        confirmed = False
        last_reason = "timeout"
        while time.monotonic() < deadline:
            if await self._has(page, "stop_button", timeout_ms=50):
                confirmed = True
                last_reason = "stop_button_present"
                break

            assistant_count, assistant_text = await self._assistant_state(page)
            if assistant_count > assistant_count_before:
                confirmed = True
                last_reason = "assistant_count_increased"
                break
            if assistant_text and assistant_text != assistant_text_before:
                confirmed = True
                last_reason = "assistant_text_changed"
                break

            if await self._message_count(page, "user_message") > user_before:
                confirmed = True
                last_reason = "user_count_increased"
                break

            await asyncio.sleep(0.1)

        if not confirmed:
            log.error("send_not_confirmed", service=self.name,
                      assistant_count_before=assistant_count_before,
                      user_before=user_before,
                      page_url=page.url)
            raise WebBridgeError(f"send_not_confirmed service={self.name}")

        log.warning("send_confirmed", service=self.name, reason=last_reason)

        # Site may assign a permanent chat URL — persist it into the handle.
        if page.url:
            log.warning("send_step_update_chat_url", service=self.name, url=page.url)
            chat.chat_url = page.url

        log.warning("send_done", service=self.name)

    async def _upload_files(self, page, files: Sequence[Path]) -> None:
        file_input = await self._find(page, "file_input", timeout_ms=3000)
        await file_input.set_input_files([str(f) for f in files])

    # ------------------------------------------------------------------
    # Извлечение ответа
    # ------------------------------------------------------------------
    async def _assistant_state(self, page) -> tuple[int, str]:
        """Возвращает количество сообщений ассистента и Markdown последнего."""
        empty_state: tuple[int, str] | None = None

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

                    # Дешёвая проверка: если текст не менялся, не парсим DOM.
                    raw_text = await last.evaluate("(el) => el.innerText || ''")
                    if not raw_text.strip():
                        if empty_state is None:
                            empty_state = (count, "")
                        continue

                    cache_key = (
                        id(page),
                        sel,
                        len(raw_text),
                        raw_text[-200:],
                    )
                    cached = self._markdown_cache.get(cache_key)
                    if cached is not None:
                        return count, cached

                    html = await last.evaluate(
                        """(el) => {
                            const copy = el.cloneNode(true);
                            // DeepSeek рендерит $...$ как формулы KaTeX и портит $_ и подобное:
                            // возвращаем исходный текст из <annotation>.
                            copy.querySelectorAll('.katex-display, .katex').forEach(node => {
                                if (!copy.contains(node)) return;  // вложенный узел уже заменён
                                const ann = node.querySelector('annotation[encoding="application/x-tex"]');
                                if (!ann) return;
                                const d = node.classList.contains('katex-display') ? '$$' : '$';
                                node.replaceWith(document.createTextNode(d + ann.textContent + d));
                            });
                            copy.querySelectorAll(
                                'button, [role="button"], svg, script, style'
                            ).forEach(node => node.remove());
                            return copy.innerHTML;
                        }"""
                    )
                    text = self.extract_markdown(html)
                    if text:
                        self._remember_markdown(cache_key, text)
                        return count, text

                    if empty_state is None:
                        empty_state = (count, "")
                except Exception as e:
                    log.warning(
                        "assistant_state_failed",
                        selector=sel,
                        error=str(e),
                    )

        return empty_state or (0, "")

    def _remember_markdown(self, key: tuple[int, str, int, str], text: str) -> None:
        if len(self._markdown_cache) > 256:
            self._markdown_cache.clear()
        self._markdown_cache[key] = text

    async def _extract_dom(self, page) -> str:
        _, text = await self._assistant_state(page)
        return text

    async def _extract_last_reply(self, page) -> str:
        return await self._extract_dom(page)

    async def _extract_final(self, page) -> str:
        """Пробует кнопку Copy, затем — DOM последнего сообщения."""
        for sel in self.selectors.get("copy_button_in_message", []):
            try:
                btn = page.locator(sel).last
                if await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(0.2)
                    text = await page.evaluate("() => navigator.clipboard.readText()")
                    if text and text.strip():
                        return text
            except Exception as e:
                log.warning(
                    "clipboard_extract_failed",
                    service=self.name,
                    selector=sel,
                    error=str(e),
                )
        return await self._extract_dom(page)

    def extract_markdown(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        strip_ui_nodes(soup)
        # Шапка блока кода (название языка, «Copy code») лежит внутри <pre>
        # и попадает в текст; оставляем только <code>.
        for pre in soup.find_all("pre"):
            code = pre.find("code")
            if code is not None:
                code.extract()
                pre.clear()
                pre.append(code)
        text = html_to_markdown(
            str(soup),
            heading_style="ATX",
            bullets="-",
            code_language_callback=_pre_language,
        ).strip()
        return _LANG_LABEL_RE.sub(r"\1tool\n", text)

    async def wait_done(
            self,
            chat: ChatHandle,
            timeout_s: float = 300.0,
            stable_s: float = 1.5,
    ) -> Reply:
        page = self._chat_page(chat)

        banner_since: float | None = None

        started = time.monotonic()
        deadline = started + timeout_s

        baseline_count = chat.meta.get("assistant_count_before", 0)
        baseline_text = chat.meta.get("assistant_text_before", "")

        last_text = ""
        last_change = started

        while time.monotonic() < deadline:
            if await self._captcha_present(page):
                raise CaptchaRequired(self.name)

            stop_visible = await self._has(page, "stop_button", timeout_ms=50)
            assistant_count, current = await self._assistant_state(page)

            # Ответ должен отличаться от состояния до отправки.
            is_new_reply = assistant_count > baseline_count or (
                    bool(current) and current != baseline_text
            )

            banner_visible = await self._has(page, "rate_limit_banner", timeout_ms=100)
            if banner_visible and not stop_visible and not is_new_reply:
                # Баннер без признаков генерации держится 15 секунд: это настоящее
                # ограничение. Совпадение текста в самой беседе сюда не попадает:
                # там блок ответа уже появился.
                if banner_since is None:
                    banner_since = time.monotonic()
                elif time.monotonic() - banner_since >= 15:
                    raise RateLimited(self.name)
            else:
                banner_since = None

            if is_new_reply and current:
                if current != last_text:
                    last_text = current
                    last_change = time.monotonic()

            stable_for = time.monotonic() - last_change

            if is_new_reply and last_text and not stop_visible and stable_for >= stable_s:
                final_text = last_text
                try:
                    extracted = await self._extract_final(page)
                    if extracted and extracted.strip():
                        final_text = extracted
                except Exception as e:
                    log.warning(
                        "final_extract_failed",
                        service=self.name,
                        error=str(e),
                    )

                if page.url:
                    chat.chat_url = page.url

                duration = round(time.monotonic() - started, 2)
                log.info(
                    "generation_completed",
                    service=self.name,
                    length=len(final_text),
                    duration=duration,
                )
                return Reply(
                    text=final_text,
                    finished=True,
                    meta={"duration_s": duration},
                )

            await asyncio.sleep(0.25)

        # Последняя попытка извлечения ответа.
        text = await self._extract_last_reply(page)

        raise GenerationTimeout(
            f"{self.name}: no completion after {timeout_s:.0f}s; last len={len(text)}"
        )

    async def stream(self, chat: ChatHandle) -> AsyncIterator[str]:
        page = self._chat_page(chat)

        started = time.monotonic()
        timeout_s = float(chat.meta.get("generation_timeout_s", 300.0))
        deadline = started + timeout_s

        baseline_count = chat.meta.get("assistant_count_before", 0)
        baseline_text = chat.meta.get("assistant_text_before", "")

        prev = ""
        last_change = started
        continues = 0
        saw_reply = False
        click_count: int | None = None
        click_prefix = ""
        banner_since: float | None = None
        last_log = started

        log.info(
            "stream_started",
            service=self.name,
            url=page.url,
            baseline_count=baseline_count,
            timeout_s=timeout_s,
        )

        while time.monotonic() < deadline:
            # Проверяем критические состояния страницы.
            if await self._captcha_present(page):
                raise CaptchaRequired(self.name)

            try:
                count, current = await self._assistant_state(page)
            except Exception as e:
                log.warning(
                    "stream_extract_error",
                    service=self.name,
                    error=str(e),
                )
                count, current = 0, ""

            is_new_reply = count > baseline_count or (bool(current) and current != baseline_text)
            stop_visible = await self._has(page, "stop_button", timeout_ms=50)

            banner_visible = await self._has(page, "rate_limit_banner", timeout_ms=100)
            if banner_visible and not stop_visible and not is_new_reply:
                # Баннер без признаков генерации держится 15 секунд: это настоящее
                # ограничение. Совпадение текста в самой беседе сюда не попадает:
                # там блок ответа уже появился.
                if banner_since is None:
                    banner_since = time.monotonic()
                elif time.monotonic() - banner_since >= 15:
                    raise RateLimited(self.name)
            else:
                banner_since = None

            # Продолжение после «Continue» может прийти новым сообщением ассистента.
            if click_count is not None and count > click_count:
                text_now = click_prefix + current
            else:
                text_now = current

            loop_at = _find_degenerate_loop(text_now) if is_new_reply else None
            if loop_at is not None:
                clean = text_now[:loop_at].rstrip()
                log.error(
                    "stream_degenerate_output",
                    service=self.name,
                    total_length=len(text_now),
                    kept_length=len(clean),
                    tail=text_now[-120:],
                )
                chat.meta["degenerate"] = True
                await self.cancel(chat)  # сработает, когда появится реальный селектор «Стоп»
                # Запасной вариант: ждём, пока модель сама упрётся в лимит ответа.
                quiet_since = time.monotonic()
                last_len = len(current)
                wait_until = time.monotonic() + 40
                while time.monotonic() < wait_until:
                    await asyncio.sleep(0.5)
                    _c, cur2 = await self._assistant_state(page)
                    if len(cur2) != last_len:
                        last_len = len(cur2)
                        quiet_since = time.monotonic()
                    elif time.monotonic() - quiet_since >= 2.0:
                        break
                yield clean
                return

            if is_new_reply and current:
                saw_reply = True

                if text_now != prev:
                    prev = text_now
                    last_change = time.monotonic()

            now = time.monotonic()

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

            # Завершаем поток после появления ответа и остановки генерации.
            if saw_reply and not stop_visible and now - last_change >= 1.5:
                if continues < 5 and await self._has(page, "continue_button", timeout_ms=200):
                    try:
                        btn = await self._find(page, "continue_button", timeout_ms=1000)
                        click_prefix = prev
                        click_count = count
                        await btn.click()
                        continues += 1
                        # Запас: генерация может стартовать не сразу.
                        last_change = time.monotonic() + 8.0
                        log.warning(
                            "stream_continue_clicked",
                            service=self.name,
                            n=continues,
                            assistant_count=count,
                            text_length=len(prev),
                        )
                        await asyncio.sleep(0.5)
                        continue
                    except Exception as e:
                        log.warning("stream_continue_failed", service=self.name, error=str(e))
                # Финальный текст может быть полнее DOM (например, Copy).
                try:
                    final_text = await self._extract_final(page)
                except Exception as e:
                    log.warning(
                        "stream_final_extract_failed",
                        service=self.name,
                        error=str(e),
                    )
                    final_text = ""

                # Текст копирования точнее DOM (нет экранирования и подмен), но
                # кнопка «Copy» может копировать только один блок кода. Защита:
                # берём его, только если он не короче 90% текста из DOM.
                use_copy = bool(final_text and final_text.strip()) and len(final_text) >= 0.9 * len(prev)
                result = restore_fence_languages(final_text, prev) if use_copy else prev
                log.info(
                    "stream_final_choice",
                    service=self.name,
                    dom_len=len(prev),
                    copy_len=len(final_text or ""),
                    used="copy" if use_copy else "dom",
                )
                yield result
                prev = result

                if page.url:
                    chat.chat_url = page.url

                log.info(
                    "stream_completed",
                    service=self.name,
                    text_length=len(prev),
                    duration=round(now - started, 2),
                )
                return

            await asyncio.sleep(0.25)

        log.warning(
            "stream_timeout",
            service=self.name,
            saw_reply=saw_reply,
            text_length=len(prev),
            timeout_s=timeout_s,
        )

        raise GenerationTimeout(
            f"{self.name}: stream did not complete after {timeout_s:.0f}s; "
            f"received {len(prev)} characters"
        )

    async def cancel(self, chat: ChatHandle) -> None:
        """Нажимает «Стоп», если генерация ещё идёт. Ошибки не критичны."""
        page = self._chat_page(chat)
        try:
            btn = await self._find(page, "stop_button", timeout_ms=800)
            await btn.click()
            log.info("generation_cancelled", service=self.name, url=page.url)
        except SelectorBroken:
            return
        except Exception as e:
            log.warning("cancel_click_failed", service=self.name, error=str(e))

    # ------------------------------------------------------------------
    async def close_page(self, page_key: str = "default") -> None:
        await self._session.close_page(page_key)

    async def close(self) -> None:
        await self._session.close()

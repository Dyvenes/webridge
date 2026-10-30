# WebBridge

Локальный мост: превращает авторизованные веб-чаты нейросетей в OpenAI-совместимый API
поверх Playwright + постоянных профилей браузера. См. `PROJECT.md` для полного описания.

## Установка

```bash
uv pip install -e ".[dev]"
playwright install chromium
```

## Статус M0

- [x] Выбран первый сервис: DeepSeek (`docs/services/deepseek.md`).
- [x] Зафиксированы ToS-риски и лимиты.
- [x] Выбран основной клиент: opencode (`docs/clients.md`, ADR 0002).
- [x] Зафиксировано: headed-браузер на старте (ADR 0003).
- [x] Созданы `docs/adr/`.
- [x] Чистая установка в venv — прогнать `uv pip install -e ".[dev]"` в пустом окружении.
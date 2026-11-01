# Changelog

## 2026-11-01 — WebBridge как API для opencode

### Добавлено

- `ConversationService`/`Turn`: сессия → удалённый чат → дельта → commit.
- Локальная генерация заголовков (`bridge/titles.py`): title-запросы не доходят
  до браузера и не тратят бюджет.
- Отмена генерации при обрыве соединения: адаптер нажимает «Стоп», ход
  помечается отменённым, блокировка сессии снимается.
- Отказ от изображений и файлов с OpenAI-совместимой ошибкой
  `invalid_request_error` до открытия браузера.
- Поддержка нескольких `<call>` в одном tool-блоке, single quotes и
  self-closing аргументов; fallback XML → JSON.
- Приведение аргументов по JSON Schema: object/array из JSON, `anyOf`/`oneOf`,
  nullable.
- Защита `<tool_result>` от закрытия обёртки враждебным содержимым.
- Метрики активных запросов, статусов и длительности; `Retry-After` для 409/429.
- Документация: `README.md`, `docs/architecture.md`, `docs/opencode.md`,
  `docs/troubleshooting.md`, `docs/BASELINE.md`.

### Изменено

- SSE теперь буферизует текст и отдаёт его после завершения генерации: DOM
  веб-чата может перерисовывать Markdown.
- Системный промпт разрешает несколько tool calls и требует JSON для object/array.
- Плагин opencode добавляет `X-WebBridge-Session-ID` и `X-WebBridge-Kind`.
- `webbridge serve` больше не печатает bearer-токен.

### Удалено

- Tier 2 / `http_base.py`, `Tier2Config`, поле `tier2`.
- Мёртвый код: `conversation/matcher.py`, `conversation/compaction.py`,
  `adapters/generic.py`.
- Широкие селекторы `text=/limit|sign in/`, которые ловили текст сообщений.

### Исправлено

- `request_id`, порядок idempotency/inflight, stale inflight, `open_chat("")`.
- Ошибка aiosqlite при доступе к соединению из другого потока.
- `ruff check`, `ruff format --check`, `mypy src` — без замечаний.

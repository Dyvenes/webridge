# Архитектура WebBridge

WebBridge — локальный процесс из двух частей: FastAPI-сервер с OpenAI-совместимым
контрактом и набор браузерных адаптеров, которые проигрывают запрос в веб-чате.
Состояние хранится в SQLite; история диалога принадлежит opencode, мост хранит
только последнюю зафиксированную копию для вычисления дельты.

## Модули

```text
src/webbridge/
├── bridge/            HTTP-слой и оркестрация хода
│   ├── app.py         FastAPI, lifespan, ошибки, метрики, endpoints
│   ├── auth.py        Bearer-токен, keyring, проверка Host
│   ├── router.py      model alias → (adapter, service, service_model)
│   ├── schemas.py     Pydantic-модели OpenAI
│   ├── service.py     ConversationService и Turn: begin/send/wait/commit
│   ├── streaming.py   SSE-чанки
│   └── titles.py      локальная эвристика заголовков
├── adapters/
│   ├── base.py        ChatAdapter, ChatHandle, Reply
│   ├── browser_base.py Playwright: вкладка, ввод, ожидание, отмена
│   ├── deepseek.py    HTML → Markdown для DeepSeek
│   ├── claude.py      HTML → Markdown для Claude
│   ├── factory.py     выбор адаптера по service.type
│   └── selectors/     YAML-селекторы
├── conversation/
│   ├── history.py     канонизация, хеши, дельта, оценка токенов
│   └── store.py       SQLite: sessions, turns, request_log, budgets
├── protocol/
│   ├── render.py      OpenAI-сообщения → текст для веб-чата
│   ├── parse.py       разбор ```tool-блоков
│   ├── schema_validate.py приведение и валидация аргументов
│   └── prompts/system.md системная инструкция
├── session/           постоянные профили и вкладки Playwright
├── limits/            часовые/суточные бюджеты
└── observability/     structlog, метрики, артефакты
```

## Поток одного запроса

1. `POST /v1/chat/completions` проверяет токен и `Host`.
2. `ConversationService.begin`:
   - отклоняет изображения/файлы (`UnsupportedContent`);
   - определяет клиентскую сессию: `X-WebBridge-Session-ID` → метаданные →
     хеш `system + первое user-сообщение`;
   - для title-запросов сразу возвращает локальный заголовок;
   - находит или создаёт строку `sessions`;
   - вычисляет дельту между входящей историей и сохранённой;
   - при расхождении (compaction, смена модели, редактирование) открывает новый
     веб-чат и засевает его системным промптом и историей;
   - берёт per-session `asyncio.Lock` (параллельный запрос получает 409).
3. `Turn.send` вводит дельту в веб-чат; `Turn.wait`/`stream` ждут завершения
   генерации и извлекают Markdown.
4. Если в запросе были `tools`, ответ буферизуется, разбирается как tool-блок,
   аргументы валидируются по JSON Schema и возвращаются как `tool_calls`.
   Иначе текст возвращается как обычный `content`.
5. `Turn.commit` атомарно сохраняет историю и ответ, помечает ход завершённым,
   освобождает блокировку.
6. При разрыве соединения `Turn.aclose` нажимает «Стоп» на сайте, помечает ход
   отменённым и снимает блокировку.

SSE используется как транспорт, но текст отдаётся после завершения генерации:
DOM веб-чата может перерисовывать Markdown, поэтому частичные дельты ненадёжны.

## Сессии и вкладки

- `client_session_id` — id сессии opencode, полученный от плагина.
- `page_key` — ключ вкладки браузера; обычно равен `client_session_id`.
  Title-ходы используют `ephemeral` и не создают вкладок.
- На каждый `client_session_id + service + model_alias` — одна строка `sessions`.
- `SessionManager` держит постоянный профиль браузера и вкладки; простаивающие
  вкладки закрываются через `maybe_close_idle`.

## Схема SQLite (v2)

```sql
sessions(
  id, client_session_id, service, model_alias,
  chat_url, remote_chat_id, status, needs_reset, reset_reason,
  history_json, history_hash, history_count,
  system_hash, tools_hash, created_at, last_used_at
)

turns(
  id, session_id, request_id, request_hash, state,
  response_json, response_text, error, created_at, updated_at
)

request_log(id, session_id, turn_id, service, model,
            started_at, duration_ms, outcome, error_type)

budgets(service, window_start, window_kind, used)

incidents(id, service, kind, artifacts_dir, occurred_at)
```

`turns.state`: `pending` → `sent` → `completed` | `failed`. Повторный запрос с тем
же `request_hash` возвращает сохранённый ответ; незавершённый ход даёт
`RequestConflict`.

## Конфигурация

`AppConfig` (`config.py`) состоит из:

- `bridge` — host/port, путь к токену и БД, docs;
- `humanize` — паузы перед отправкой;
- `services` — тип адаптера, URL, профиль, селекторы, модели, лимиты;
- `routing` — модель по умолчанию и fallback-цепочки.

При первом запуске `ensure_home()` создаёт `~/.webbridge`, конфиг и копии
селекторов. Пользовательские селекторы имеют приоритет над встроенными.

## Ошибки

`errors.py` задаёт иерархию; `app.py::status_for` переводит её в HTTP-коды и
OpenAI `error.type`. Ошибки после старта SSE передаются отдельным
`data: {"error": ...}`-событием, затем поток закрывается.

| Исключение | HTTP | `type` |
|---|---|---|
| `NeedsLogin` | 401 | `authentication_error` |
| `CaptchaRequired` | 403 | `captcha_required` |
| `RateLimited` | 429 | `rate_limited` |
| `SessionBusy` | 409 | `session_busy` |
| `RequestConflict` | 409 | `request_conflict` |
| `ModelNotFound` | 404 | `model_not_found` |
| `ModelUnavailable` | 503 | `model_unavailable` |
| `GenerationTimeout` | 504 | `timeout` |
| `ProtocolError` | 502 | `bad_model_output` |
| `UnsupportedContent` | 400 | `invalid_request_error` |

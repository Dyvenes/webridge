# WebBridge

Локальный OpenAI-совместимый API поверх **ваших собственных** веб-чатов DeepSeek и Claude.
WebBridge не содержит чат-клиента и не выполняет инструменты: он принимает запрос
opencode, проигрывает его в браузерной вкладке через Playwright и возвращает ответ
в формате OpenAI, включая `tool_calls`.

```text
opencode ── OpenAI API ──> WebBridge ── Playwright ──> chat.deepseek.com / claude.ai
   ^                                                            │
   └──────────────────── ответ, tool_calls ─────────────────────┘
```

## Что умеет

- `POST /v1/chat/completions` — обычные ответы и SSE-эмуляция (`stream=true`).
- `GET /v1/models` — только включённые в конфиге алиасы моделей.
- Tool calling: несколько вызовов в одном ответе, валидация аргументов по JSON Schema.
- Один удалённый чат на клиентскую сессию opencode; отправляется только дельта истории.
- Compaction и переписанная история распознаются и приводят к новому веб-чату.
- Заголовки сессий генерируются локально и не тратят лимиты веб-чата.
- Идемпотентность повторных запросов, отмена генерации по разрыву соединения,
  OpenAI-совместимые ошибки, `/healthz` и `/metrics`.

Чего нет: собственного агентного цикла, выполнения инструментов, загрузки файлов
и изображений, поддержки Aider/Cline. Клиент — только opencode.

## Требования

- Python 3.12+ (проверено на 3.13).
- Google Chrome или Chromium для Playwright.
- Аккаунты в веб-чатах, которые вы собираетесь использовать.
- opencode V2.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
playwright install chromium
```

Если используете `uv`:

```powershell
uv sync --extra dev
uv run playwright install chromium
```

## Быстрый старт

1. Создайте конфиг и войдите в сервисы:

```powershell
webbridge login deepseek
webbridge login claude
```

Профили браузера сохраняются в `~/.webbridge/profiles/`. Логин открывает видимое
окно: войдите в аккаунт вручную и дождитесь подтверждения.

2. Запустите мост:

```powershell
webbridge serve
```

3. Подключите opencode. В проекте уже есть `opencode.json` с провайдером
   `webbridge`. Токен подставляется плагином из `~/.webbridge/api_token`
   (или `WEBBRIDGE_API_TOKEN`), поэтому в конфиге стоит заглушка `apiKey`.

```powershell
opencode run --model webbridge/deepseek-chat "Ответь одним словом: ok"
```

Подробная настройка — в [`docs/opencode.md`](docs/opencode.md).

## Команды

| Команда | Назначение |
|---|---|
| `webbridge serve` | Поднять API на `127.0.0.1:8000`. |
| `webbridge login <service>` | Открыть браузер и войти в веб-чат. |
| `webbridge token` | Показать bearer-токен для opencode. |
| `webbridge token --rotate` | Выпустить новый токен. |

Полезные опции `serve`: `--config PATH`, `--host`, `--port`, `--reload`.

## Конфигурация

Файл по умолчанию — `~/.webbridge/config.yaml`; пример — [`config.example.yaml`](config.example.yaml).
При первом запуске он создаётся автоматически вместе с копиями селекторов.

Ключевые поля:

- `bridge.host` / `bridge.port` — адрес API. Слушайте только loopback.
- `bridge.api_token_ref` — ссылка на токен (`file:...`).
- `services.<name>.models` — алиасы, которые увидит opencode.
- `services.<name>.limits` — лимиты длины сообщения, запросов и времени генерации.
- `services.<name>.browser.headless` — `false` на время отладки, `true` для работы без окна.

## Безопасность и ограничения

- Мост слушает `127.0.0.1` и проверяет заголовок `Host`, чтобы исключить DNS-rebinding.
- Bearer-токен хранится в `~/.webbridge/api_token` (или keyring) и не печатается
  командой `serve`.
- Веб-чаты не являются официальным API. Автоматизация может нарушать условия
  сервиса и привести к ограничению аккаунта. Не используйте основной аккаунт,
  от которого зависит критичная работа.
- Ответы веб-модели не гарантируют формат tool-вызовов. WebBridge проверяет и
  валидирует их, но при частых нарушениях протокола запрос завершится
  `bad_model_output`.
- Изображения и файлы в v1 отклоняются с `invalid_request_error`.

## Разработка

```powershell
pytest -q
ruff check .
ruff format --check .
mypy src
```

Архитектура и схема БД — [`docs/architecture.md`](docs/architecture.md).
Диагностика — [`docs/troubleshooting.md`](docs/troubleshooting.md).
История изменений — [`CHANGELOG.md`](CHANGELOG.md).

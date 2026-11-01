# opencode + WebBridge

WebBridge — единственный клиент opencode. opencode отвечает за контекст, сессии,
агентный цикл и выполнение инструментов; мост только переигрывает запросы в
веб-чатах и возвращает ответы в формате OpenAI.

## 1. Запуск моста

```powershell
webbridge login deepseek
webbridge login claude
webbridge serve
```

По умолчанию API доступен на `http://127.0.0.1:8000/v1`.

## 2. Провайдер в `opencode.json`

Проектный файл уже содержит готовый блок. Минимальный вариант:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "webbridge/deepseek-chat",
  "providers": {
    "webbridge": {
      "name": "WebBridge",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "local",
        "headerTimeout": 600000,
        "chunkTimeout": false
      },
      "models": {
        "deepseek-chat": {
          "name": "DeepSeek Chat (web)",
          "capabilities": { "tools": true, "input": ["text"], "output": ["text"] },
          "limit": { "context": 64000, "output": 8192 }
        }
      }
    }
  }
}
```

`apiKey: "local"` — заглушка. Реальный bearer-токен подставляет плагин из
`~/.webbridge/api_token` или `WEBBRIDGE_API_TOKEN`. Если плагин не используется,
замените заглушку на `{file:~/.webbridge/api_token}` или `{env:WEBBRIDGE_API_TOKEN}`.

`headerTimeout` увеличен, потому что веб-модель может думать дольше обычного API;
`chunkTimeout: false` отключает таймаут между чанками.

### Модели

Алиасы должны совпадать с `services.*.models` в `~/.webbridge/config.yaml`:

| Алиас | Сервис |
|---|---|
| `deepseek-chat` | DeepSeek |
| `deepseek-reasoner` | DeepSeek |
| `claude-sonnet` | Claude |
| `claude-opus` | Claude |

Можно обращаться и как `webbridge/deepseek-chat`, и как `deepseek-chat` в
зависимости от того, как opencode выбирает модель.

## 3. Плагин сессий

Плагин лежит в `.opencode/plugins/webbridge-session/index.ts` и загружается
автоматически для проекта. Он:

- добавляет `X-WebBridge-Session-ID` к запросам провайдера `webbridge`;
- помечает title-запросы заголовком `X-WebBridge-Kind: title`;
- подставляет `Authorization: Bearer <token>`.

Без плагина мост определит сессию по хешу системного промпта и первого сообщения
пользователя. Это работает, но две сессии с одинаковым началом могут схлопнуться,
а compaction может выглядеть как новая сессия. Плагин — предпочтительный способ.

### Глобальная установка

Скопируйте каталог `.opencode/plugins/webbridge-session` в
`~/.config/opencode/plugins/webbridge-session` и добавьте плагин в глобальный
`opencode.jsonc`:

```jsonc
{
  "plugins": ["./plugins/webbridge-session"]
}
```

Проектный вариант (каталог `.opencode/plugins/`) не требует записи в конфиге.

### Отладка плагина

По умолчанию плагин не пишет файлов. Если нужно посмотреть, какие заголовки
уходят, включите `OPENCODE_LOG_LEVEL=DEBUG` и смотрите лог opencode. Не
оставляйте отладочные логи в репозитории.

## 4. Заголовки

| Заголовок | Назначение |
|---|---|
| `Authorization: Bearer <token>` | Аутентификация моста. |
| `X-WebBridge-Session-ID` | id сессии opencode → один удалённый чат. |
| `X-WebBridge-Kind: primary` | Обычный ход агентного цикла. |
| `X-WebBridge-Kind: title` | Заголовок; обрабатывается локально. |

## 5. Заголовки сессий и compaction

opencode периодически просит модель сгенерировать короткое название разговора.
Такие запросы не отправляются в веб-чат: мост строит заголовок детерминированно
из первого осмысленного сообщения пользователя. Это экономит лимиты и не
создаёт лишних вкладок.

При compaction opencode присылает переписанную историю. Мост видит, что хеш
истории не совпадает, открывает новый веб-чат и засевает его актуальной
историей. Это не ошибка и не требует ручных действий.

## 6. Проверка

```powershell
# API живо
curl http://127.0.0.1:8000/healthz

# Список моделей (нужен токен)
$env:TOKEN = webbridge token
curl -H "Authorization: Bearer $env:TOKEN" http://127.0.0.1:8000/v1/models

# Пробный ход
opencode run --model webbridge/deepseek-chat "Ответь одним словом: ok"
```

Если opencode не видит провайдера, проверьте:

- `opencode.json` лежит в корне проекта или в `.opencode/`;
- имя провайдера — `webbridge`, модель — `webbridge/<alias>`;
- мост запущен и `/v1/models` отвечает;
- токен в `~/.webbridge/api_token` совпадает с тем, что проверяет мост.

## 7. Ограничения

- Только текст: изображения и файлы отклоняются.
- Нет настоящего streaming: SSE-чанки приходят после завершения генерации.
- `usage` приблизительный (оценка по символам).
- Веб-чат может менять разметку; при `SelectorBroken` обновите селекторы в
  `~/.webbridge/selectors/`.

# Совместимость с клиентами

## Выбор основного клиента

Для первой интеграции выбран **opencode** (терминальный агентный клиент, поддерживает OpenAI-совместимые endpoints через `@ai-sdk/openai-compatible`)[reference:9].

Порядок проверки: opencode → Aider → Cline.

| Клиент | Поля запроса | Поведение при ошибках | Нужный timeout | Заметки |
|---|---|---|---|---|
| opencode | model, messages, tools, stream | TBD (M4) | TBD | Настроить `baseURL = http://127.0.0.1:8000/v1`[reference:10] |
| Aider | ? | ? | ? | Проверить после opencode |
| Cline | ? | ? | ? | Проверить после Aider |
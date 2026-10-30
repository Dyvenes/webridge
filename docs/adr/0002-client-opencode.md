# ADR 0002: Основной клиент — opencode

## Решение
Для M4 первым подключается opencode.

## Обоснование
opencode поддерживает OpenAI-совместимые endpoints через `@ai-sdk/openai-compatible`[reference:15] и настраивается через `opencode.jsonc` с `baseURL` на локальный мост[reference:16].

## Последствия
Проверка совместимости ведётся в `docs/clients.md`; Aider и Cline — после opencode.
# Architecture Decision Records

Формат имени: `NNNN-kebab-title.md` (NNNN — 4 цифры, с ведущими нулями).
Новый ADR — копия из шаблона ниже, статус `proposed` до утверждения.

## Шаблон

\`\`\`markdown
# ADR NNNN: <Title>

- **Статус:** proposed | accepted | deprecated | superseded by ADR-XXXX
- **Дата:** YYYY-MM-DD
- **Контекст:** M<0-8> · PROJECT.md §<раздел>

## Контекст
<Проблема, ограничения, альтернативы.>

## Решение
<Что решили, с обоснованием.>

## Последствия
**Положительные:** ...
**Отрицательные:** ...
**Нейтральные:** ...
\`\`\`

## Индекс

| ADR | Название | Статус |
|---|---|---|
| [0001](0001-first-service.md) | Выбор первого веб-сервиса | proposed |
| [0002](0002-browser-mode.md) | Headless или headed браузер на M1 | proposed |
| [0003](0003-tool-protocol-format.md) | Формат tool calling (XML / JSON) | deferred (M3) |
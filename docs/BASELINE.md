# Базовая линия

Дата: 2026-11-01.

## Окружение

- ОС: Windows 11.
- Python: 3.13.1 (venv `.venv`).
- pytest: 9.2.0.
- ruff: 0.17.3.
- mypy: 2.4.1.

## Команды

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy src
```

## Результат

| Проверка | Результат |
|---|---|
| `pytest -q` | 80 passed, 0 failed, 0 skipped (56 c) |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 66 files already formatted |
| `mypy src` | Success: no issues found in 36 source files |

## Что покрыто тестами

- OpenAI-контракт: модели, auth, обычный ответ, SSE, usage.
- Идемпотентность повторного запроса и расхождение истории (compaction).
- Tool calls: парсинг XML/JSON, несколько вызовов, валидация и приведение
  аргументов по JSON Schema, `tool_choice`.
- Локальные заголовки, отказ от изображений, отмена хода.
- Браузерный адаптер на фейковом сайте (Playwright; при отсутствии браузера
  тесты пропускаются).
- Сессии: health-check, вкладки, логин.

## Известные ограничения

- Реальные DeepSeek/Claude не прогонялись в этом прогоне: нужны залогиненные
  профили и ручная проверка `webbridge login`.
- `usage` приблизительный.
- Изображения и файлы не поддерживаются.

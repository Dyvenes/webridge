"""Структурированные логи (раздел 14)."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

import structlog

SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9\-_.]+"
    r"|api[_-]?key\s*[=:]\s*[a-z0-9\-_]{6,}"
    r"|sk-[a-z0-9]{20,}"
    r"|ghp_[a-z0-9]{20,}"
    r"|-----BEGIN [A-Z ]+PRIVATE KEY-----)"
)
REDACTED = "[REDACTED]"


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return SECRET_RE.sub(REDACTED, value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(v) for v in value)
    return value


def _redact(_logger, _method, event_dict: dict[str, Any]) -> dict[str, Any]:
    for k, v in list(event_dict.items()):
        event_dict[k] = _redact_value(v)
    return event_dict


def configure(log_dir: Path | None = None, level: int = logging.INFO) -> None:
    if log_dir is None:
        log_dir = Path.home() / ".webbridge" / "logs"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(
            log_dir / "webbridge.log", when="D", backupCount=14, encoding="utf-8"
        )
        handlers.append(fh)
    except OSError:
        pass
    logging.basicConfig(level=level, handlers=handlers, format="%(message)s")
    processors: list[Any] = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "webbridge"):
    return structlog.get_logger(name)

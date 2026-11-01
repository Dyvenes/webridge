"""Аутентификация локального API.

Проверка Host — точное сравнение hostname, а не startswith: иначе
`127.0.0.1.evil.com` проходит DNS-rebinding проверку.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

from webbridge.observability.logging import get_logger

log = get_logger("auth")

_KEYRING_SERVICE = "webbridge"
_KEYRING_USER = "api_token"
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}


def token_path() -> Path:
    return Path.home() / ".webbridge" / "api_token"


def _keyring_get() -> str | None:
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
    except Exception:
        log.warning("keyring_unavailable")
        return None


def _keyring_set(token: str) -> None:
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, token)
    except Exception:
        log.warning("keyring_unavailable_using_file")


def load_token() -> str | None:
    env = os.environ.get("WEBBRIDGE_API_TOKEN")
    if env:
        return env.strip() or None

    token = _keyring_get()
    if token:
        return token

    path = token_path()
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def save_token(token: str | None = None, force: bool = False) -> str:
    if not force:
        existing = load_token()
        if existing:
            return existing

    token = token or secrets.token_urlsafe(32)
    _keyring_set(token)
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


def _hostname(host_header: str) -> str:
    if not host_header:
        return ""
    # urlsplit("//host:port") корректно разбирает IPv6 и порт.
    return (urlsplit(f"//{host_header}").hostname or "").lower()


async def check_auth(request: Request, authorization: str | None = Header(default=None)) -> None:
    host = request.headers.get("host", "")
    hostname = _hostname(host)
    if hostname not in _ALLOWED_HOSTS:
        raise HTTPException(status_code=400, detail="invalid host header")

    expected = getattr(request.app.state, "api_token", None) or load_token()
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="api token not initialised; run `webbridge serve` or `webbridge token`",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not secrets.compare_digest(authorization[7:], expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")

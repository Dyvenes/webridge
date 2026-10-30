"""Аутентификация локального моста (раздел 11.6)."""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Header, HTTPException, Request

from webbridge.observability.logging import get_logger

log = get_logger("auth")

_KEYRING_SERVICE = "webbridge"
_KEYRING_USER = "api_token"


def _fallback_path() -> Path:
    return Path.home() / ".webbridge" / "api_token"


def _load_token() -> str | None:
    try:
        import keyring
        token = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        if token:
            return token
    except Exception:
        log.warning("keyring_unavailable")
    p = _fallback_path()
    if p.exists():
        return p.read_text(encoding="utf-8").strip() or None
    return None


def save_token(token: str | None = None) -> str:
    token = token or secrets.token_urlsafe(32)
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, token)
        return token
    except Exception:
        log.warning("keyring_unavailable_using_file")
    p = _fallback_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token, encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return token


async def check_auth(
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    host = request.headers.get("host", "")
    if host and not (host.startswith("127.0.0.1") or host.startswith("localhost")):
        raise HTTPException(status_code=400, detail="invalid host header")
    expected = _load_token()
    if not expected:
        raise HTTPException(
            status_code=500, detail="api token not initialised; run `webbridge init`"
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not secrets.compare_digest(authorization[7:], expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")
"""Тесты хранения и загрузки bearer-токена."""

from __future__ import annotations

from pathlib import Path

from webbridge.bridge import auth


def test_save_token_recreates_file_when_keyring_has_token(
    tmp_path: Path, monkeypatch
) -> None:
    token_file = tmp_path / "api_token"
    monkeypatch.setattr(auth, "token_path", lambda: token_file)
    monkeypatch.setattr(auth, "_keyring_get", lambda: "keyring-token")
    monkeypatch.setattr(auth, "_keyring_set", lambda token: None)
    monkeypatch.delenv("WEBBRIDGE_API_TOKEN", raising=False)

    assert auth.save_token() == "keyring-token"
    assert token_file.read_text(encoding="utf-8") == "keyring-token"


def test_load_token_prefers_file_over_keyring(tmp_path: Path, monkeypatch) -> None:
    token_file = tmp_path / "api_token"
    token_file.write_text("file-token", encoding="utf-8")
    monkeypatch.setattr(auth, "token_path", lambda: token_file)
    monkeypatch.setattr(auth, "_keyring_get", lambda: "keyring-token")
    monkeypatch.delenv("WEBBRIDGE_API_TOKEN", raising=False)

    assert auth.load_token() == "file-token"


def test_save_token_force_rotates_and_writes_file(tmp_path: Path, monkeypatch) -> None:
    token_file = tmp_path / "api_token"
    token_file.write_text("old-token", encoding="utf-8")
    written: list[str] = []
    monkeypatch.setattr(auth, "token_path", lambda: token_file)
    monkeypatch.setattr(auth, "_keyring_get", lambda: "old-token")
    monkeypatch.setattr(auth, "_keyring_set", written.append)
    monkeypatch.delenv("WEBBRIDGE_API_TOKEN", raising=False)

    token = auth.save_token(force=True)
    assert token != "old-token"
    assert token_file.read_text(encoding="utf-8") == token
    assert written == [token]

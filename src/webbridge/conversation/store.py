"""SQLite-хранилище (раздел 17 PROJECT.md)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
  id            TEXT PRIMARY KEY,
  service       TEXT NOT NULL,
  chat_url      TEXT,
  model         TEXT,
  fingerprint   TEXT NOT NULL,
  message_count INTEGER NOT NULL,
  char_count    INTEGER NOT NULL,
  status        TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  last_used_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
  chat_id   TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  idx       INTEGER NOT NULL,
  canonical TEXT NOT NULL,
  PRIMARY KEY (chat_id, idx)
);
CREATE TABLE IF NOT EXISTS requests (
  id          TEXT PRIMARY KEY,
  chat_id     TEXT REFERENCES chats(id),
  service     TEXT NOT NULL,
  tier        INTEGER NOT NULL,
  started_at  TEXT NOT NULL,
  duration_ms INTEGER,
  outcome     TEXT NOT NULL,
  retries     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS budgets (
  service      TEXT NOT NULL,
  window_start TEXT NOT NULL,
  window_kind  TEXT NOT NULL,
  used         INTEGER NOT NULL,
  PRIMARY KEY (service, window_start, window_kind)
);
CREATE TABLE IF NOT EXISTS incidents (
  id            TEXT PRIMARY KEY,
  service       TEXT NOT NULL,
  kind          TEXT NOT NULL,
  artifacts_dir TEXT,
  occurred_at   TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("store not connected; call connect() first")
        return self._conn

    # --- chats --------------------------------------------------------
    async def create_chat(
        self,
        service: str,
        chat_url: str | None,
        model: str | None,
        fingerprint: str,
        message_count: int,
        char_count: int,
    ) -> str:
        chat_id = uuid.uuid4().hex
        now = _now()
        await self.conn.execute(
            "INSERT INTO chats (id, service, chat_url, model, fingerprint, "
            "message_count, char_count, status, created_at, last_used_at) "
            "VALUES (?,?,?,?,?,?,?, 'active', ?, ?)",
            (chat_id, service, chat_url, model, fingerprint, message_count, char_count, now, now),
        )
        await self.conn.commit()
        return chat_id

    async def list_active_chats(self, service: str) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM chats WHERE service = ? AND status = 'active' "
            "ORDER BY last_used_at DESC",
            (service,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_chat_url(self, chat_id: str, chat_url: str | None) -> None:
        await self.conn.execute(
            "UPDATE chats SET chat_url = ?, last_used_at = ? WHERE id = ?",
            (chat_url, _now(), chat_id),
        )
        await self.conn.commit()

    async def close_chat(self, chat_id: str) -> None:
        await self.conn.execute(
            "UPDATE chats SET status = 'closed', last_used_at = ? WHERE id = ?",
            (_now(), chat_id),
        )
        await self.conn.commit()

    # --- chat messages --------------------------------------------------
    async def store_chat_messages(
        self, chat_id: str, canonical: list[str], fingerprint: str, char_count: int
    ) -> None:
        await self.conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        for i, c in enumerate(canonical):
            await self.conn.execute(
                "INSERT INTO chat_messages (chat_id, idx, canonical) VALUES (?,?,?)",
                (chat_id, i, c),
            )
        await self.conn.execute(
            "UPDATE chats SET fingerprint = ?, char_count = ?, message_count = ?, "
            "last_used_at = ? WHERE id = ?",
            (fingerprint, char_count, len(canonical), _now(), chat_id),
        )
        await self.conn.commit()

    async def append_chat_messages(
        self, chat_id: str, canonical: list[str], fingerprint: str, char_count: int
    ) -> None:
        cur = await self.conn.execute(
            "SELECT COALESCE(MAX(idx), -1) + 1 FROM chat_messages WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cur.fetchone()
        start = row[0] if row else 0
        for i, c in enumerate(canonical):
            await self.conn.execute(
                "INSERT INTO chat_messages (chat_id, idx, canonical) VALUES (?,?,?)",
                (chat_id, start + i, c),
            )
        await self.conn.execute(
            "UPDATE chats SET fingerprint = ?, char_count = ?, "
            "message_count = (SELECT COUNT(*) FROM chat_messages WHERE chat_id = ?), "
            "last_used_at = ? WHERE id = ?",
            (fingerprint, char_count, chat_id, _now(), chat_id),
        )
        await self.conn.commit()

    async def get_chat_messages(self, chat_id: str) -> list[str]:
        cur = await self.conn.execute(
            "SELECT canonical FROM chat_messages WHERE chat_id = ? ORDER BY idx",
            (chat_id,),
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]

    # --- budgets --------------------------------------------------------
    async def get_budget(self, service: str, window_start: str, kind: str) -> int:
        cur = await self.conn.execute(
            "SELECT used FROM budgets WHERE service = ? AND window_start = ? AND window_kind = ?",
            (service, window_start, kind),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def bump_budget(self, service: str, window_start: str, kind: str) -> int:
        await self.conn.execute(
            "INSERT INTO budgets (service, window_start, window_kind, used) VALUES (?,?,?,1) "
            "ON CONFLICT(service, window_start, window_kind) DO UPDATE SET used = used + 1",
            (service, window_start, kind),
        )
        await self.conn.commit()
        return await self.get_budget(service, window_start, kind)

    # --- logs -----------------------------------------------------------
    async def log_request(
        self,
        service: str,
        chat_id: str | None,
        tier: int,
        started_at: str,
        duration_ms: int,
        outcome: str,
        retries: int = 0,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO requests (id, chat_id, service, tier, started_at, "
            "duration_ms, outcome, retries) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, chat_id, service, tier, started_at, duration_ms, outcome, retries),
        )
        await self.conn.commit()

    async def log_incident(
        self, service: str, kind: str, artifacts_dir: str | None
    ) -> None:
        await self.conn.execute(
            "INSERT INTO incidents (id, service, kind, artifacts_dir, occurred_at) "
            "VALUES (?,?,?,?,?)",
            (uuid.uuid4().hex, service, kind, artifacts_dir, _now()),
        )
        await self.conn.commit()
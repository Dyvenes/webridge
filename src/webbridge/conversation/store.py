"""SQLite-хранилище WebBridge.

Схема v2: sessions (client session → remote chat + committed history),
turns (state machine одного запроса), request_log, budgets, incidents.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .history import history_hash

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id                TEXT PRIMARY KEY,
  client_session_id TEXT NOT NULL,
  service           TEXT NOT NULL,
  model_alias       TEXT NOT NULL,
  chat_url          TEXT,
  remote_chat_id    TEXT,
  status            TEXT NOT NULL DEFAULT 'active',
  needs_reset       INTEGER NOT NULL DEFAULT 0,
  reset_reason      TEXT,
  history_json      TEXT NOT NULL DEFAULT '[]',
  history_hash      TEXT NOT NULL DEFAULT '',
  history_count     INTEGER NOT NULL DEFAULT 0,
  system_hash       TEXT NOT NULL DEFAULT '',
  tools_hash        TEXT NOT NULL DEFAULT '',
  created_at        TEXT NOT NULL,
  last_used_at      TEXT NOT NULL,
  UNIQUE(client_session_id, service, model_alias)
);
CREATE INDEX IF NOT EXISTS idx_sessions_client
  ON sessions(client_session_id, service, model_alias);
CREATE INDEX IF NOT EXISTS idx_sessions_status
  ON sessions(status, last_used_at);

CREATE TABLE IF NOT EXISTS turns (
  id            TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  request_id    TEXT NOT NULL,
  request_hash  TEXT NOT NULL,
  state         TEXT NOT NULL,
  response_json TEXT,
  response_text TEXT,
  error         TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_request
  ON turns(request_id);
CREATE INDEX IF NOT EXISTS idx_turns_session
  ON turns(session_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_turns_hash
  ON turns(session_id, request_hash, state);

CREATE TABLE IF NOT EXISTS request_log (
  id          TEXT PRIMARY KEY,
  session_id  TEXT,
  turn_id     TEXT,
  service     TEXT NOT NULL,
  model       TEXT,
  started_at  TEXT NOT NULL,
  duration_ms INTEGER,
  outcome     TEXT NOT NULL,
  error_type  TEXT
);
CREATE INDEX IF NOT EXISTS idx_request_log_time
  ON request_log(started_at);

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
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        # Явные транзакции через BEGIN IMMEDIATE. Не трогаем
        # isolation_level напрямую: sqlite3-соединение живёт в потоке
        # aiosqlite, и запись атрибута из другого потока запрещена.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
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

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            await self.conn.commit()
        except BaseException:
            await self.conn.rollback()
            raise

    # --- sessions -------------------------------------------------------
    @staticmethod
    def _decode_session(row: aiosqlite.Row | dict[str, Any]) -> dict[str, Any]:
        data = dict(row)
        try:
            data["history"] = json.loads(data.get("history_json") or "[]")
        except (TypeError, ValueError):
            data["history"] = []
        data["needs_reset"] = bool(data.get("needs_reset"))
        return data

    async def get_session(
        self, client_session_id: str, service: str, model_alias: str
    ) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM sessions WHERE client_session_id=? AND service=? AND model_alias=?",
            (client_session_id, service, model_alias),
        )
        row = await cur.fetchone()
        return self._decode_session(row) if row else None

    async def get_session_by_id(self, session_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
        row = await cur.fetchone()
        return self._decode_session(row) if row else None

    async def create_session(
        self, client_session_id: str, service: str, model_alias: str
    ) -> dict[str, Any]:
        session_id = uuid.uuid4().hex
        now = _now()
        async with self.transaction():
            await self.conn.execute(
                "INSERT INTO sessions (id, client_session_id, service, model_alias, "
                "status, needs_reset, history_json, history_hash, history_count, "
                "system_hash, tools_hash, created_at, last_used_at) "
                "VALUES (?,?,?,?, 'active', 0, '[]', '', 0, '', '', ?, ?) "
                "ON CONFLICT(client_session_id, service, model_alias) DO NOTHING",
                (session_id, client_session_id, service, model_alias, now, now),
            )
        session = await self.get_session(client_session_id, service, model_alias)
        assert session is not None
        return session

    async def get_or_create_session(
        self, client_session_id: str, service: str, model_alias: str
    ) -> dict[str, Any]:
        session = await self.get_session(client_session_id, service, model_alias)
        if session is not None:
            return session
        return await self.create_session(client_session_id, service, model_alias)

    async def touch_session(self, session_id: str) -> None:
        await self.conn.execute(
            "UPDATE sessions SET last_used_at=? WHERE id=?", (_now(), session_id)
        )
        await self.conn.commit()

    async def update_session_chat(
        self,
        session_id: str,
        chat_url: str | None,
        remote_chat_id: str | None = None,
    ) -> None:
        await self.conn.execute(
            "UPDATE sessions SET chat_url=?, remote_chat_id=?, last_used_at=? WHERE id=?",
            (chat_url, remote_chat_id, _now(), session_id),
        )
        await self.conn.commit()

    async def mark_needs_reset(self, session_id: str, reason: str) -> None:
        await self.conn.execute(
            "UPDATE sessions SET needs_reset=1, reset_reason=?, status='error', "
            "last_used_at=? WHERE id=?",
            (reason, _now(), session_id),
        )
        await self.conn.commit()

    async def close_session(self, session_id: str) -> None:
        await self.conn.execute(
            "UPDATE sessions SET status='closed', last_used_at=? WHERE id=?",
            (_now(), session_id),
        )
        await self.conn.commit()

    async def list_sessions(self, active_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM sessions"
        if active_only:
            query += " WHERE status='active'"
        query += " ORDER BY last_used_at DESC"
        cur = await self.conn.execute(query)
        rows = await cur.fetchall()
        return [self._decode_session(r) for r in rows]

    # --- turns ----------------------------------------------------------
    async def create_turn(self, session_id: str, request_id: str, request_hash: str) -> str:
        turn_id = uuid.uuid4().hex
        now = _now()
        try:
            await self.conn.execute(
                "INSERT INTO turns (id, session_id, request_id, request_hash, state, "
                "created_at, updated_at) VALUES (?,?,?,?, 'prepared', ?, ?)",
                (turn_id, session_id, request_id, request_hash, now, now),
            )
            await self.conn.commit()
        except aiosqlite.IntegrityError:
            # Старые базы могли сохранить UNIQUE(request_id). Клиентский
            # request id не является ключом идемпотентности — он лишь
            # коррелирует логи, поэтому коллизия не должна ломать ход.
            await self.conn.rollback()
            await self.conn.execute(
                "INSERT INTO turns (id, session_id, request_id, request_hash, state, "
                "created_at, updated_at) VALUES (?,?,?,?, 'prepared', ?, ?)",
                (
                    turn_id,
                    session_id,
                    f"{request_id}:{uuid.uuid4().hex[:8]}",
                    request_hash,
                    now,
                    now,
                ),
            )
            await self.conn.commit()
        return turn_id

    async def update_turn(
        self,
        turn_id: str,
        state: str,
        response_json: str | None = None,
        response_text: str | None = None,
        error: str | None = None,
    ) -> None:
        await self.conn.execute(
            "UPDATE turns SET state=?, response_json=?, response_text=?, error=?, "
            "updated_at=? WHERE id=?",
            (state, response_json, response_text, error, _now(), turn_id),
        )
        await self.conn.commit()

    async def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_turn_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM turns WHERE request_id=?", (request_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def find_completed_turn(
        self, session_id: str, request_hash: str
    ) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM turns WHERE session_id=? AND request_hash=? AND state='completed' "
            "ORDER BY updated_at DESC LIMIT 1",
            (session_id, request_hash),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def find_inflight_turn(
        self, session_id: str, request_hash: str, max_age_s: int = 900
    ) -> dict[str, Any] | None:
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_s)).isoformat(timespec="seconds")
        cur = await self.conn.execute(
            "SELECT * FROM turns WHERE session_id=? AND request_hash=? "
            "AND state IN ('prepared','sent') AND updated_at >= ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (session_id, request_hash, cutoff),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def commit_turn(
        self,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        response_message: dict[str, Any],
        chat_url: str | None = None,
        tools_hash: str = "",
    ) -> None:
        now = _now()
        response_json = json.dumps(response_message, ensure_ascii=False)
        response_text = response_message.get("content") or ""
        h = history_hash(history)
        async with self.transaction():
            await self.conn.execute(
                "UPDATE sessions SET history_json=?, history_hash=?, history_count=?, "
                "needs_reset=0, reset_reason=NULL, status='active', tools_hash=?, "
                "chat_url=COALESCE(?, chat_url), last_used_at=? WHERE id=?",
                (
                    json.dumps(history, ensure_ascii=False),
                    h,
                    len(history),
                    tools_hash,
                    chat_url,
                    now,
                    session_id,
                ),
            )
            await self.conn.execute(
                "UPDATE turns SET state='completed', response_json=?, response_text=?, "
                "error=NULL, updated_at=? WHERE id=?",
                (response_json, response_text, now, turn_id),
            )

    async def fail_turn(
        self,
        turn_id: str,
        error: str,
        session_id: str | None = None,
        reset: bool = True,
    ) -> None:
        now = _now()
        async with self.transaction():
            await self.conn.execute(
                "UPDATE turns SET state='failed', error=?, updated_at=? WHERE id=?",
                (error, now, turn_id),
            )
            if session_id and reset:
                await self.conn.execute(
                    "UPDATE sessions SET needs_reset=1, reset_reason=?, status='error', "
                    "last_used_at=? WHERE id=?",
                    (error[:500], now, session_id),
                )

    # --- budgets --------------------------------------------------------
    async def get_budget(self, service: str, window_start: str, kind: str) -> int:
        cur = await self.conn.execute(
            "SELECT used FROM budgets WHERE service=? AND window_start=? AND window_kind=?",
            (service, window_start, kind),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def bump_budget(self, service: str, window_start: str, kind: str) -> int:
        await self.conn.execute(
            "INSERT INTO budgets (service, window_start, window_kind, used) VALUES (?,?,?,1) "
            "ON CONFLICT(service, window_start, window_kind) DO UPDATE SET used=used+1",
            (service, window_start, kind),
        )
        await self.conn.commit()
        return await self.get_budget(service, window_start, kind)

    # --- logs -----------------------------------------------------------
    async def log_request(
        self,
        service: str,
        session_id: str | None,
        turn_id: str | None,
        model: str | None,
        started_at: str,
        duration_ms: int,
        outcome: str,
        error_type: str | None = None,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO request_log (id, session_id, turn_id, service, model, "
            "started_at, duration_ms, outcome, error_type) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                session_id,
                turn_id,
                service,
                model,
                started_at,
                duration_ms,
                outcome,
                error_type,
            ),
        )
        await self.conn.commit()

    async def log_incident(self, service: str, kind: str, artifacts_dir: str | None) -> None:
        await self.conn.execute(
            "INSERT INTO incidents (id, service, kind, artifacts_dir, occurred_at) "
            "VALUES (?,?,?,?,?)",
            (uuid.uuid4().hex, service, kind, artifacts_dir, _now()),
        )
        await self.conn.commit()

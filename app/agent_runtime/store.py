from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .events import AgentEvent


class AgentEventStore:
    """Append-only event log with per-session sequence cursors."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        # One long-lived connection instead of one per operation: every access
        # is already serialized by `_lock`, and opening a connection costs more
        # than the statement itself, which a streaming turn pays on every delta.
        self._db: sqlite3.Connection | None = sqlite3.connect(
            self.path, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS agent_sessions (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS agent_events (session_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE, type TEXT NOT NULL, payload TEXT NOT NULL, occurred_at REAL NOT NULL, PRIMARY KEY(session_id, sequence))")

    def _connect(self) -> sqlite3.Connection:
        # Used as a context manager by callers, which commits on exit and rolls
        # back on error. The connection itself outlives the block.
        if self._db is None:
            raise RuntimeError("agent event store is closed")
        return self._db

    def close(self) -> None:
        """Release the connection and its -wal/-shm sidecar files."""
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def save_session(self, owner_id: str, session_id: str, data: dict[str, Any]) -> None:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO agent_sessions(id, owner_id, data) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data "
                "WHERE agent_sessions.owner_id=excluded.owner_id",
                (session_id, owner_id, json.dumps(data, separators=(",", ":"))),
            )
            if cursor.rowcount != 1:
                raise ValueError("agent session id belongs to another owner")

    def load_session(self, owner_id: str, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT data FROM agent_sessions WHERE id=? AND owner_id=?", (session_id, owner_id)).fetchone()
        return json.loads(row[0]) if row else None

    def list_sessions(self, owner_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT data FROM agent_sessions WHERE owner_id=? ORDER BY json_extract(data, '$.updated_at') DESC", (owner_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def all_sessions(self) -> list[dict[str, Any]]:
        """Every stored session, across owners. Used only by startup reconciliation."""
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT data FROM agent_sessions").fetchall()
        return [json.loads(row[0]) for row in rows]

    def append(self, value: AgentEvent) -> AgentEvent:
        committed, _created = self.append_once(value)
        return committed

    def append_once(self, value: AgentEvent) -> tuple[AgentEvent, bool]:
        with self._lock, self._connect() as db:
            existing = db.execute(
                "SELECT sequence,event_id,type,payload,occurred_at,session_id "
                "FROM agent_events WHERE event_id=?",
                (value.id,),
            ).fetchone()
            if existing is not None:
                payload = json.loads(existing[3])
                if (
                    str(existing[5]) != value.session_id
                    or str(existing[2]) != value.type
                    or payload != value.payload
                ):
                    raise ValueError("agent event id is bound to different contents")
                return (
                    AgentEvent(
                        int(existing[0]),
                        str(existing[1]),
                        str(existing[5]),
                        str(existing[2]),
                        payload,
                        float(existing[4]),
                    ),
                    False,
                )
            row = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM agent_events WHERE session_id=?", (value.session_id,)).fetchone()
            sequence = int(row[0])
            db.execute("INSERT INTO agent_events(session_id,sequence,event_id,type,payload,occurred_at) VALUES(?,?,?,?,?,?)", (value.session_id, sequence, value.id, value.type, json.dumps(value.payload, separators=(",", ":")), value.occurred_at))
        return AgentEvent(sequence, value.id, value.session_id, value.type, value.payload, value.occurred_at), True

    def events(self, owner_id: str, session_id: str, after: int = 0, limit: int = 1000) -> list[AgentEvent]:
        with self._lock, self._connect() as db:
            allowed = db.execute("SELECT 1 FROM agent_sessions WHERE id=? AND owner_id=?", (session_id, owner_id)).fetchone()
            if not allowed:
                return []
            rows = db.execute("SELECT sequence,event_id,type,payload,occurred_at FROM agent_events WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?", (session_id, max(0, after), min(max(1, limit), 5000))).fetchall()
        return [AgentEvent(int(row[0]), row[1], session_id, row[2], json.loads(row[3]), float(row[4])) for row in rows]

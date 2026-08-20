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
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS agent_sessions (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS agent_events (session_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE, type TEXT NOT NULL, payload TEXT NOT NULL, occurred_at REAL NOT NULL, PRIMARY KEY(session_id, sequence))")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db

    def save_session(self, owner_id: str, session_id: str, data: dict[str, Any]) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO agent_sessions(id, owner_id, data) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET owner_id=excluded.owner_id,data=excluded.data", (session_id, owner_id, json.dumps(data, separators=(",", ":"))))

    def load_session(self, owner_id: str, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT data FROM agent_sessions WHERE id=? AND owner_id=?", (session_id, owner_id)).fetchone()
        return json.loads(row[0]) if row else None

    def list_sessions(self, owner_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT data FROM agent_sessions WHERE owner_id=? ORDER BY json_extract(data, '$.updated_at') DESC", (owner_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def append(self, value: AgentEvent) -> AgentEvent:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM agent_events WHERE session_id=?", (value.session_id,)).fetchone()
            sequence = int(row[0])
            db.execute("INSERT INTO agent_events(session_id,sequence,event_id,type,payload,occurred_at) VALUES(?,?,?,?,?,?)", (value.session_id, sequence, value.id, value.type, json.dumps(value.payload, separators=(",", ":")), value.occurred_at))
        return AgentEvent(sequence, value.id, value.session_id, value.type, value.payload, value.occurred_at)

    def events(self, owner_id: str, session_id: str, after: int = 0, limit: int = 1000) -> list[AgentEvent]:
        with self._lock, self._connect() as db:
            allowed = db.execute("SELECT 1 FROM agent_sessions WHERE id=? AND owner_id=?", (session_id, owner_id)).fetchone()
            if not allowed:
                return []
            rows = db.execute("SELECT sequence,event_id,type,payload,occurred_at FROM agent_events WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?", (session_id, max(0, after), min(max(1, limit), 5000))).fetchall()
        return [AgentEvent(int(row[0]), row[1], session_id, row[2], json.loads(row[3]), float(row[4])) for row in rows]

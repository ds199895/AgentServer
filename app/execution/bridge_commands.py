from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_COMMAND_BYTES = 64 * 1024
ACK_STATUSES = frozenset({"accepted", "rejected", "completed"})
TERMINAL_STATUSES = frozenset({"rejected", "completed", "expired"})
SERVER_STATUSES = frozenset(
    {"queued", "delivered", "accepted", "rejected", "completed", "expired"}
)


class BridgeCommandJournalError(ValueError):
    pass


@dataclass(frozen=True)
class BridgeCommandAck:
    sequence: int
    command_id: str
    status: str
    payload: Mapping[str, Any]
    ack_id: str
    fingerprint: str
    delivery_state: str
    server_acknowledged: bool
    response: Mapping[str, Any] | None

    def request_body(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ack_id": self.ack_id,
            "payload": dict(self.payload),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "status": self.status,
            "ack_id": self.ack_id,
            "payload": dict(self.payload),
            "delivery_state": self.delivery_state,
            "server_acknowledged": self.server_acknowledged,
            "response": dict(self.response) if self.response is not None else None,
        }


class BridgeCommandJournal:
    """Durable device-side command cursor and acknowledgement journal.

    Only authenticated server responses may call ``record_server_commands``.
    Local socket callers can list those durable records and ACK them, but have
    no operation that creates a command.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS bridge_command_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bridge_commands (
                    server_sequence INTEGER NOT NULL UNIQUE,
                    command_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    handler_attempts INTEGER NOT NULL DEFAULT 0,
                    last_handler_at REAL,
                    uncertain_reason TEXT,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    received_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS bridge_commands_pending
                ON bridge_commands(status, server_sequence);

                CREATE TABLE IF NOT EXISTS bridge_command_acks (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    ack_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    delivery_state TEXT NOT NULL DEFAULT 'pending',
                    response_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(command_id, status),
                    FOREIGN KEY(command_id) REFERENCES bridge_commands(command_id)
                );
                CREATE INDEX IF NOT EXISTS bridge_command_acks_pending
                ON bridge_command_acks(delivery_state, sequence);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(bridge_commands)"
                ).fetchall()
            }
            if "uncertain_reason" not in columns:
                connection.execute(
                    "ALTER TABLE bridge_commands ADD COLUMN uncertain_reason TEXT"
                )
            if "recovery_count" not in columns:
                connection.execute(
                    "ALTER TABLE bridge_commands ADD COLUMN "
                    "recovery_count INTEGER NOT NULL DEFAULT 0"
                )
            # An executing row has crossed the side-effect boundary but never
            # recorded a result.  Reopening must quarantine it rather than
            # silently executing the command a second time.
            connection.execute(
                """
                UPDATE bridge_commands
                SET status = 'uncertain',
                    uncertain_reason = COALESCE(
                        uncertain_reason,
                        'bridge_restarted_while_handler_executing'
                    ),
                    updated_at = ?
                WHERE status = 'executing'
                """,
                (float(self.clock()),),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _text(value: object, label: str, *, optional: bool = False) -> str:
        result = str(value or "").strip()
        if not result and optional:
            return ""
        if not result or len(result) > 255:
            raise BridgeCommandJournalError(
                f"{label} must contain 1..255 characters"
            )
        return result

    @staticmethod
    def _json_object(value: object, label: str) -> tuple[dict[str, Any], str]:
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise BridgeCommandJournalError(f"{label} must be an object")
        try:
            encoded = json.dumps(
                dict(value), separators=(",", ":"), sort_keys=True, allow_nan=False
            )
            normalized = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise BridgeCommandJournalError(
                f"{label} must contain valid JSON values"
            ) from error
        if len(encoded.encode("utf-8")) > MAX_COMMAND_BYTES:
            raise BridgeCommandJournalError(f"{label} exceeds 64 KiB")
        return normalized, encoded

    @staticmethod
    def _timestamp(value: object, label: str, *, optional: bool = False) -> float | None:
        if value is None and optional:
            return None
        if isinstance(value, bool):
            raise BridgeCommandJournalError(f"{label} must be a finite timestamp")
        try:
            result = float(value)
        except (TypeError, ValueError) as error:
            raise BridgeCommandJournalError(
                f"{label} must be a finite timestamp"
            ) from error
        if not math.isfinite(result):
            raise BridgeCommandJournalError(f"{label} must be a finite timestamp")
        return result

    @classmethod
    def _normalize_command(
        cls, value: Mapping[str, Any]
    ) -> tuple[int, str, str, str, str]:
        if not isinstance(value, Mapping):
            raise BridgeCommandJournalError("server command must be an object")
        sequence = value.get("sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence <= 0
        ):
            raise BridgeCommandJournalError(
                "server command sequence must be a positive integer"
            )
        command_id = cls._text(value.get("command_id"), "command_id")
        owner_id = cls._text(value.get("owner_id"), "owner_id")
        target_kind = cls._text(value.get("target_kind"), "target_kind")
        target_id = cls._text(value.get("target_id"), "target_id")
        command_type = cls._text(value.get("type"), "command type")
        payload, _ = cls._json_object(value.get("payload"), "command payload")
        expected_revision = value.get("expected_revision")
        if expected_revision is not None and (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise BridgeCommandJournalError(
                "expected_revision must be a non-negative integer"
            )
        created_at = cls._timestamp(value.get("created_at"), "created_at")
        expires_at = cls._timestamp(
            value.get("expires_at"), "expires_at", optional=True
        )
        server_status = str(value.get("status") or "delivered")
        if server_status not in SERVER_STATUSES:
            raise BridgeCommandJournalError(
                f"unsupported server command status: {server_status}"
            )
        canonical = {
            "sequence": sequence,
            "command_id": command_id,
            "owner_id": owner_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "type": command_type,
            "payload": payload,
            "expected_revision": expected_revision,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        encoded = json.dumps(
            canonical, separators=(",", ":"), sort_keys=True, allow_nan=False
        )
        if len(encoded.encode("utf-8")) > MAX_COMMAND_BYTES:
            raise BridgeCommandJournalError("server command exceeds 64 KiB")
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        local_status = (
            server_status
            if server_status in {"accepted", "rejected", "completed", "expired"}
            else "pending"
        )
        return sequence, command_id, fingerprint, encoded, local_status

    @staticmethod
    def _expire_due(connection: sqlite3.Connection, now: float) -> None:
        due = connection.execute(
            """
            SELECT command_id FROM bridge_commands
            WHERE status IN ('pending', 'executing', 'uncertain', 'accepted')
              AND CAST(json_extract(command_json, '$.expires_at') AS REAL) <= ?
              AND json_extract(command_json, '$.expires_at') IS NOT NULL
            """,
            (now,),
        ).fetchall()
        if not due:
            return
        identifiers = [str(row["command_id"]) for row in due]
        placeholders = ",".join("?" for _ in identifiers)
        connection.execute(
            f"UPDATE bridge_commands SET status = 'expired', updated_at = ? "
            f"WHERE command_id IN ({placeholders})",
            (now, *identifiers),
        )
        connection.execute(
            f"UPDATE bridge_command_acks SET delivery_state = 'abandoned', "
            f"updated_at = ? WHERE delivery_state = 'pending' "
            f"AND command_id IN ({placeholders})",
            (now, *identifiers),
        )

    @property
    def cursor(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM bridge_command_metadata WHERE key = 'server_cursor'"
            ).fetchone()
        return int(row["value"]) if row else 0

    def record_server_commands(
        self,
        commands: Iterable[Mapping[str, Any]],
        *,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        normalized = [self._normalize_command(command) for command in commands]
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_due(connection, timestamp)
            cursor_row = connection.execute(
                "SELECT value FROM bridge_command_metadata WHERE key = 'server_cursor'"
            ).fetchone()
            cursor = int(cursor_row["value"]) if cursor_row else 0
            for sequence, command_id, fingerprint, encoded, incoming_status in normalized:
                by_id = connection.execute(
                    "SELECT * FROM bridge_commands WHERE command_id = ?",
                    (command_id,),
                ).fetchone()
                by_sequence = connection.execute(
                    "SELECT * FROM bridge_commands WHERE server_sequence = ?",
                    (sequence,),
                ).fetchone()
                if by_id is not None:
                    if (
                        by_id["fingerprint"] != fingerprint
                        or int(by_id["server_sequence"]) != sequence
                    ):
                        raise BridgeCommandJournalError(
                            "command_id was reused for different command contents"
                        )
                    current = str(by_id["status"])
                    if current not in TERMINAL_STATUSES and incoming_status in {
                        "accepted",
                        "rejected",
                        "completed",
                        "expired",
                    }:
                        connection.execute(
                            "UPDATE bridge_commands SET status = ?, updated_at = ? "
                            "WHERE command_id = ?",
                            (incoming_status, timestamp, command_id),
                        )
                elif by_sequence is not None:
                    raise BridgeCommandJournalError(
                        "server command sequence was reused by another command"
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO bridge_commands(
                            server_sequence, command_id, fingerprint, command_json,
                            status, received_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sequence,
                            command_id,
                            fingerprint,
                            encoded,
                            incoming_status,
                            timestamp,
                            timestamp,
                        ),
                    )
                cursor = max(cursor, sequence)
            connection.execute(
                """
                INSERT INTO bridge_command_metadata(key, value)
                VALUES ('server_cursor', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(cursor),),
            )
        return self.pending(now=timestamp)

    @staticmethod
    def _command_from_row(row: sqlite3.Row) -> dict[str, Any]:
        command = json.loads(str(row["command_json"]))
        command.update(
            status=str(row["status"]),
            fingerprint=str(row["fingerprint"]),
            handler_attempts=int(row["handler_attempts"]),
            last_handler_at=(
                float(row["last_handler_at"])
                if row["last_handler_at"] is not None
                else None
            ),
            uncertain_reason=(
                str(row["uncertain_reason"])
                if row["uncertain_reason"] is not None
                else None
            ),
            recovery_count=int(row["recovery_count"]),
        )
        return command

    def pending(self, *, now: float | None = None) -> list[dict[str, Any]]:
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            self._expire_due(connection, timestamp)
            rows = connection.execute(
                """
                SELECT * FROM bridge_commands
                WHERE status IN ('pending', 'executing', 'uncertain', 'accepted')
                ORDER BY server_sequence
                """
            ).fetchall()
        return [self._command_from_row(row) for row in rows]

    def dispatchable_ids(self, *, now: float | None = None) -> tuple[str, ...]:
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            self._expire_due(connection, timestamp)
            rows = connection.execute(
                """
                SELECT command_id FROM bridge_commands
                WHERE status = 'pending' ORDER BY server_sequence
                """
            ).fetchall()
        return tuple(str(row["command_id"]) for row in rows)

    def begin_handler(
        self, command_id: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        identifier = self._text(command_id, "command_id")
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_due(connection, timestamp)
            row = connection.execute(
                "SELECT * FROM bridge_commands WHERE command_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise BridgeCommandJournalError("command does not exist")
            if row["status"] != "pending":
                return None
            connection.execute(
                """
                UPDATE bridge_commands
                SET status = 'executing',
                    handler_attempts = handler_attempts + 1,
                    last_handler_at = ?, updated_at = ?
                WHERE command_id = ?
                """,
                (timestamp, timestamp, identifier),
            )
            result = connection.execute(
                "SELECT * FROM bridge_commands WHERE command_id = ?",
                (identifier,),
            ).fetchone()
        return self._command_from_row(result) if result is not None else None

    def mark_uncertain(
        self,
        command_id: str,
        reason: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        identifier = self._text(command_id, "command_id")
        summary = str(reason or "handler_outcome_unknown")[:1000]
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_due(connection, timestamp)
            row = connection.execute(
                "SELECT * FROM bridge_commands WHERE command_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise BridgeCommandJournalError("command does not exist")
            if row["status"] == "executing":
                connection.execute(
                    """
                    UPDATE bridge_commands
                    SET status = 'uncertain', uncertain_reason = ?, updated_at = ?
                    WHERE command_id = ?
                    """,
                    (summary, timestamp, identifier),
                )
            elif row["status"] != "uncertain":
                raise BridgeCommandJournalError(
                    f"cannot mark {row['status']} command uncertain"
                )
            result = connection.execute(
                "SELECT * FROM bridge_commands WHERE command_id = ?",
                (identifier,),
            ).fetchone()
        if result is None:  # pragma: no cover - SQLite update contract
            raise BridgeCommandJournalError("command disappeared")
        return self._command_from_row(result)

    def retry_uncertain(
        self,
        command_id: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Explicitly declare an uncertain command safe to execute again."""

        identifier = self._text(command_id, "command_id")
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_due(connection, timestamp)
            row = connection.execute(
                "SELECT * FROM bridge_commands WHERE command_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise BridgeCommandJournalError("command does not exist")
            if row["status"] != "uncertain":
                raise BridgeCommandJournalError(
                    f"cannot retry {row['status']} command as uncertain"
                )
            connection.execute(
                """
                UPDATE bridge_commands
                SET status = 'pending', recovery_count = recovery_count + 1,
                    updated_at = ?
                WHERE command_id = ?
                """,
                (timestamp, identifier),
            )
            result = connection.execute(
                "SELECT * FROM bridge_commands WHERE command_id = ?",
                (identifier,),
            ).fetchone()
        if result is None:  # pragma: no cover - SQLite update contract
            raise BridgeCommandJournalError("command disappeared")
        return self._command_from_row(result)

    def command(
        self, command_id: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        identifier = self._text(command_id, "command_id")
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            self._expire_due(connection, timestamp)
            row = connection.execute(
                "SELECT * FROM bridge_commands WHERE command_id = ?",
                (identifier,),
            ).fetchone()
        return self._command_from_row(row) if row is not None else None

    def status_summary(self, *, now: float | None = None) -> dict[str, Any]:
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            self._expire_due(connection, timestamp)
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count FROM bridge_commands
                GROUP BY status ORDER BY status
                """
            ).fetchall()
            uncertain = connection.execute(
                """
                SELECT * FROM bridge_commands
                WHERE status IN ('executing', 'uncertain')
                ORDER BY server_sequence
                """
            ).fetchall()
        return {
            "cursor": self.cursor,
            "counts": {str(row["status"]): int(row["count"]) for row in rows},
            "uncertain": [self._command_from_row(row) for row in uncertain],
        }

    @staticmethod
    def _ack_from_row(row: sqlite3.Row) -> BridgeCommandAck:
        response = (
            json.loads(str(row["response_json"]))
            if row["response_json"] is not None
            else None
        )
        return BridgeCommandAck(
            sequence=int(row["sequence"]),
            command_id=str(row["command_id"]),
            status=str(row["status"]),
            payload=json.loads(str(row["payload_json"])),
            ack_id=str(row["ack_id"]),
            fingerprint=str(row["fingerprint"]),
            delivery_state=str(row["delivery_state"]),
            server_acknowledged=row["delivery_state"] == "acknowledged",
            response=response,
        )

    def prepare_ack(
        self,
        *,
        command_id: str,
        status: str,
        payload: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> BridgeCommandAck:
        identifier = self._text(command_id, "command_id")
        target = str(status or "").strip()
        if target not in ACK_STATUSES:
            raise BridgeCommandJournalError(
                "command ACK status must be accepted, rejected, or completed"
            )
        body, body_json = self._json_object(payload, "command ACK payload")
        ack_fingerprint = hashlib.sha256(
            json.dumps(
                {"command_id": identifier, "status": target, "payload": body},
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_due(connection, timestamp)
            command = connection.execute(
                "SELECT * FROM bridge_commands WHERE command_id = ?",
                (identifier,),
            ).fetchone()
            if command is None:
                raise BridgeCommandJournalError("command does not exist")
            existing = connection.execute(
                """
                SELECT * FROM bridge_command_acks
                WHERE command_id = ? AND status = ?
                """,
                (identifier, target),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != ack_fingerprint:
                    raise BridgeCommandJournalError(
                        "command ACK status was reused with different payload"
                    )
                if existing["delivery_state"] == "abandoned":
                    raise BridgeCommandJournalError(
                        "command expired before its ACK reached the server"
                    )
                return self._ack_from_row(existing)

            current = str(command["status"])
            allowed = {
                "pending": ACK_STATUSES,
                "executing": ACK_STATUSES,
                "uncertain": ACK_STATUSES,
                "accepted": frozenset({"completed"}),
            }
            if target not in allowed.get(current, frozenset()):
                raise BridgeCommandJournalError(
                    f"cannot acknowledge {current} command as {target}"
                )
            ack_id = uuid.uuid4().hex
            cursor = connection.execute(
                """
                INSERT INTO bridge_command_acks(
                    command_id, status, fingerprint, ack_id, payload_json,
                    delivery_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    identifier,
                    target,
                    ack_fingerprint,
                    ack_id,
                    body_json,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE bridge_commands SET status = ?, updated_at = ? "
                "WHERE command_id = ?",
                (target, timestamp, identifier),
            )
            result = connection.execute(
                "SELECT * FROM bridge_command_acks WHERE sequence = ?",
                (cursor.lastrowid,),
            ).fetchone()
        if result is None:  # pragma: no cover - SQLite insert contract
            raise BridgeCommandJournalError("command ACK insert did not return a row")
        return self._ack_from_row(result)

    def pending_acks(self, *, now: float | None = None) -> list[BridgeCommandAck]:
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            self._expire_due(connection, timestamp)
            rows = connection.execute(
                """
                SELECT * FROM bridge_command_acks
                WHERE delivery_state = 'pending' ORDER BY sequence
                """
            ).fetchall()
        return [self._ack_from_row(row) for row in rows]

    def acknowledgement(
        self, *, command_id: str, status: str
    ) -> BridgeCommandAck | None:
        identifier = self._text(command_id, "command_id")
        target = str(status or "").strip()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM bridge_command_acks
                WHERE command_id = ? AND status = ?
                """,
                (identifier, target),
            ).fetchone()
        return self._ack_from_row(row) if row is not None else None

    def mark_acknowledged(
        self,
        ack_id: str,
        response: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> BridgeCommandAck:
        identifier = self._text(ack_id, "ack_id")
        body, body_json = self._json_object(response, "command ACK response")
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM bridge_command_acks WHERE ack_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise BridgeCommandJournalError("command ACK does not exist")
            if row["delivery_state"] == "acknowledged":
                existing = json.loads(str(row["response_json"]))
                if existing != body:
                    raise BridgeCommandJournalError(
                        "acknowledged command response cannot change"
                    )
                return self._ack_from_row(row)
            if row["delivery_state"] != "pending":
                raise BridgeCommandJournalError(
                    "expired command ACK cannot be acknowledged"
                )
            connection.execute(
                """
                UPDATE bridge_command_acks
                SET delivery_state = 'acknowledged', response_json = ?, updated_at = ?
                WHERE ack_id = ?
                """,
                (body_json, timestamp, identifier),
            )
            result = connection.execute(
                "SELECT * FROM bridge_command_acks WHERE ack_id = ?",
                (identifier,),
            ).fetchone()
        if result is None:  # pragma: no cover - SQLite update contract
            raise BridgeCommandJournalError("command ACK disappeared")
        return self._ack_from_row(result)

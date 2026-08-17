from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .errors import CommandConflict, ValidationError
from .events import new_id
from .models import (
    Command,
    CommandStatus,
    TERMINAL_COMMAND_STATUSES,
    enum_value,
    json_object,
)


class CommandQueue:
    """Durable, owner-scoped command delivery with explicit acknowledgements."""

    def __init__(
        self,
        database_path: Path,
        *,
        lock: threading.RLock | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = lock or threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_commands (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    command_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expected_revision INTEGER,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    delivered_at REAL,
                    acked_at REAL,
                    ack_payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS execution_commands_target
                ON execution_commands(owner_id, target_kind, target_id, sequence);

                CREATE TABLE IF NOT EXISTS execution_command_acks (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    ack_id TEXT NOT NULL UNIQUE,
                    command_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    acked_at REAL NOT NULL,
                    FOREIGN KEY(command_id) REFERENCES execution_commands(command_id)
                );
                CREATE INDEX IF NOT EXISTS execution_command_acks_command
                ON execution_command_acks(command_id, sequence);
                """
            )

    @staticmethod
    def _require(value: str, label: str) -> str:
        result = str(value or "").strip()
        if not result or len(result) > 255:
            raise ValidationError(f"{label} must contain 1..255 characters")
        return result

    @staticmethod
    def _fingerprint(values: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            values, separators=(",", ":"), sort_keys=True, allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Command:
        return Command(
            sequence=int(row["sequence"]),
            id=row["command_id"],
            owner_id=row["owner_id"],
            target_kind=row["target_kind"],
            target_id=row["target_id"],
            type=row["command_type"],
            payload=json.loads(row["payload_json"]),
            status=CommandStatus(row["status"]),
            expected_revision=row["expected_revision"],
            created_at=float(row["created_at"]),
            expires_at=(
                float(row["expires_at"]) if row["expires_at"] is not None else None
            ),
            delivered_at=(
                float(row["delivered_at"])
                if row["delivered_at"] is not None
                else None
            ),
            acked_at=float(row["acked_at"]) if row["acked_at"] is not None else None,
            ack_payload=json.loads(row["ack_payload_json"]),
        )

    def enqueue(
        self,
        *,
        owner_id: str,
        target_kind: str,
        target_id: str,
        command_type: str,
        payload: Mapping[str, Any] | None = None,
        command_id: str | None = None,
        expires_at: float | None = None,
        expected_revision: int | None = None,
        created_at: float | None = None,
    ) -> Command:
        owner_id = self._require(owner_id, "owner_id")
        target_kind = self._require(enum_value(target_kind), "target_kind")
        target_id = self._require(target_id, "target_id")
        command_type = self._require(command_type, "command_type")
        command_id = self._require(command_id or new_id(), "command_id")
        body = json_object(payload, field_name="command payload")
        if expected_revision is not None and (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValidationError("expected_revision must be non-negative")
        timestamp = time.time() if created_at is None else float(created_at)
        if expires_at is not None and float(expires_at) <= timestamp:
            raise ValidationError("expires_at must be later than created_at")
        values = {
            "command_id": command_id,
            "owner_id": owner_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "command_type": command_type,
            "payload": body,
            "expires_at": expires_at,
            "expected_revision": expected_revision,
        }
        fingerprint = self._fingerprint(values)
        with self._lock:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT * FROM execution_commands WHERE command_id = ?",
                    (command_id,),
                ).fetchone()
                if existing is not None:
                    if existing["fingerprint"] != fingerprint:
                        raise CommandConflict(
                            "command_id was reused for different command contents"
                        )
                    return self._from_row(existing)
                cursor = connection.execute(
                    """
                    INSERT INTO execution_commands(
                        command_id, fingerprint, owner_id, target_kind, target_id,
                        command_type, payload_json, status, expected_revision,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command_id,
                        fingerprint,
                        owner_id,
                        target_kind,
                        target_id,
                        command_type,
                        json.dumps(body, separators=(",", ":"), sort_keys=True),
                        CommandStatus.QUEUED.value,
                        expected_revision,
                        timestamp,
                        expires_at,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM execution_commands WHERE sequence = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            if row is None:  # pragma: no cover - SQLite insert contract
                raise CommandConflict("command insert did not return a row")
            return self._from_row(row)

    @staticmethod
    def _expire_due(connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            """
            UPDATE execution_commands
            SET status = ?, acked_at = ?
            WHERE expires_at IS NOT NULL AND expires_at <= ?
              AND status IN (?, ?, ?)
            """,
            (
                CommandStatus.EXPIRED.value,
                now,
                now,
                CommandStatus.QUEUED.value,
                CommandStatus.DELIVERED.value,
                CommandStatus.ACCEPTED.value,
            ),
        )

    def list(
        self,
        *,
        owner_id: str,
        target_kind: str,
        target_id: str,
        after_sequence: int = 0,
        include_terminal: bool = False,
        limit: int = 100,
        now: float | None = None,
    ) -> list[Command]:
        if after_sequence < 0:
            raise ValidationError("after_sequence must be non-negative")
        if limit <= 0 or limit > 1000:
            raise ValidationError("limit must be between 1 and 1000")
        timestamp = time.time() if now is None else float(now)
        terminal_values = tuple(status.value for status in TERMINAL_COMMAND_STATUSES)
        query = """
            SELECT * FROM execution_commands
            WHERE owner_id = ? AND target_kind = ? AND target_id = ?
              AND sequence > ?
        """
        parameters: list[Any] = [
            owner_id,
            enum_value(target_kind),
            target_id,
            after_sequence,
        ]
        if not include_terminal:
            query += " AND status NOT IN (?, ?, ?)"
            parameters.extend(terminal_values)
        query += " ORDER BY sequence LIMIT ?"
        parameters.append(limit)
        with self._lock:
            with self._connect() as connection:
                self._expire_due(connection, timestamp)
                rows = connection.execute(query, parameters).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, *, owner_id: str, command_id: str, now: float | None = None) -> Command | None:
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            with self._connect() as connection:
                self._expire_due(connection, timestamp)
                row = connection.execute(
                    "SELECT * FROM execution_commands WHERE owner_id = ? AND command_id = ?",
                    (owner_id, command_id),
                ).fetchone()
        return self._from_row(row) if row is not None else None

    def mark_delivered(
        self, *, owner_id: str, command_id: str, now: float | None = None
    ) -> Command:
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            with self._connect() as connection:
                self._expire_due(connection, timestamp)
                row = connection.execute(
                    "SELECT * FROM execution_commands WHERE owner_id = ? AND command_id = ?",
                    (owner_id, command_id),
                ).fetchone()
                if row is None:
                    raise CommandConflict("command does not exist in owner scope")
                status = CommandStatus(row["status"])
                if status is CommandStatus.QUEUED:
                    connection.execute(
                        """
                        UPDATE execution_commands
                        SET status = ?, delivered_at = ?
                        WHERE command_id = ?
                        """,
                        (CommandStatus.DELIVERED.value, timestamp, command_id),
                    )
                elif status not in {
                    CommandStatus.DELIVERED,
                    CommandStatus.ACCEPTED,
                }:
                    raise CommandConflict(f"cannot deliver a {status.value} command")
                result = connection.execute(
                    "SELECT * FROM execution_commands WHERE command_id = ?",
                    (command_id,),
                ).fetchone()
        return self._from_row(result)

    def ack(
        self,
        *,
        owner_id: str,
        command_id: str,
        status: CommandStatus | str,
        ack_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> Command:
        try:
            target = status if isinstance(status, CommandStatus) else CommandStatus(status)
        except ValueError as exc:
            raise ValidationError(f"unsupported command ACK status: {status}") from exc
        if target not in {
            CommandStatus.ACCEPTED,
            CommandStatus.REJECTED,
            CommandStatus.COMPLETED,
        }:
            raise ValidationError("ACK status must be accepted, rejected, or completed")
        ack_id = self._require(ack_id or new_id(), "ack_id")
        body = json_object(payload, field_name="command ACK payload")
        body_json = json.dumps(body, separators=(",", ":"), sort_keys=True)
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            with self._connect() as connection:
                self._expire_due(connection, timestamp)
                duplicate = connection.execute(
                    "SELECT * FROM execution_command_acks WHERE ack_id = ?",
                    (ack_id,),
                ).fetchone()
                if duplicate is not None:
                    if (
                        duplicate["command_id"] != command_id
                        or duplicate["status"] != target.value
                        or duplicate["payload_json"] != body_json
                    ):
                        raise CommandConflict("ack_id was reused for different ACK contents")
                    row = connection.execute(
                        "SELECT * FROM execution_commands WHERE owner_id = ? AND command_id = ?",
                        (owner_id, command_id),
                    ).fetchone()
                    if row is None:
                        raise CommandConflict("ACK is outside the owner scope")
                    return self._from_row(row)
                row = connection.execute(
                    "SELECT * FROM execution_commands WHERE owner_id = ? AND command_id = ?",
                    (owner_id, command_id),
                ).fetchone()
                if row is None:
                    raise CommandConflict("command does not exist in owner scope")
                current = CommandStatus(row["status"])
                allowed = {
                    CommandStatus.QUEUED: {
                        CommandStatus.ACCEPTED,
                        CommandStatus.REJECTED,
                        CommandStatus.COMPLETED,
                    },
                    CommandStatus.DELIVERED: {
                        CommandStatus.ACCEPTED,
                        CommandStatus.REJECTED,
                        CommandStatus.COMPLETED,
                    },
                    CommandStatus.ACCEPTED: {CommandStatus.COMPLETED},
                }
                if target == current:
                    # A new acknowledgement for the same state is harmless and
                    # remains auditable, while changing a terminal result is not.
                    if current in TERMINAL_COMMAND_STATUSES and json.loads(
                        row["ack_payload_json"]
                    ) != body:
                        raise CommandConflict(
                            "a terminal command ACK cannot change its result"
                        )
                elif target not in allowed.get(current, set()):
                    raise CommandConflict(
                        f"cannot acknowledge {current.value} command as {target.value}"
                    )
                connection.execute(
                    """
                    INSERT INTO execution_command_acks(
                        ack_id, command_id, status, payload_json, acked_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (ack_id, command_id, target.value, body_json, timestamp),
                )
                connection.execute(
                    """
                    UPDATE execution_commands
                    SET status = ?, acked_at = ?, ack_payload_json = ?
                    WHERE command_id = ?
                    """,
                    (target.value, timestamp, body_json, command_id),
                )
                result = connection.execute(
                    "SELECT * FROM execution_commands WHERE command_id = ?",
                    (command_id,),
                ).fetchone()
        return self._from_row(result)

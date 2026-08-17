from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import httpx


SCHEMA_VERSION = "agentserver.event/1"
CRITICAL_EVENT_SUFFIXES = (
    ".started",
    ".succeeded",
    ".failed",
    ".cancelled",
    ".stopping",
    ".requested",
)
COALESCIBLE_EVENT_TYPES = frozenset(
    {"run.progress.updated", "observation.resource.sampled", "agent.heartbeat"}
)


class ReporterConfigurationError(ValueError):
    pass


class ReporterSpoolFull(RuntimeError):
    pass


def load_reporter_token_file(path: Path | str) -> str:
    """Read a short-lived token from a private regular file, never argv/env."""
    resolved = Path(path).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as error:
        raise ReporterConfigurationError(
            f"cannot open reporter token file: {resolved}"
        ) from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ReporterConfigurationError("reporter token path must be a regular file")
        if os.name != "nt" and info.st_mode & 0o077:
            raise ReporterConfigurationError(
                "reporter token file must not be accessible by group or others"
            )
        encoded = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if len(encoded) > 4096:
        raise ReporterConfigurationError("reporter token file exceeds 4096 bytes")
    try:
        token = encoded.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ReporterConfigurationError("reporter token file is not UTF-8") from error
    if not token or any(character.isspace() for character in token):
        raise ReporterConfigurationError("reporter token file contains an invalid token")
    return token


@dataclass(frozen=True)
class ReporterContext:
    owner_id: str
    device_id: str | None
    terminal_id: str
    launch_id: str
    run_id: str
    assignment_id: str | None = None
    task_id: str | None = None
    agent_instance_id: str | None = None
    parent_run_id: str | None = None

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "ReporterContext":
        values = os.environ if environment is None else environment
        required = {
            "owner_id": values.get("AGENTSERVER_OWNER_ID", ""),
            "terminal_id": values.get("AGENTSERVER_TERMINAL_ID", ""),
            "launch_id": values.get("AGENTSERVER_LAUNCH_ID", ""),
            "run_id": values.get("AGENTSERVER_RUN_ID", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ReporterConfigurationError(
                f"missing managed runtime context: {', '.join(missing)}"
            )
        return cls(
            **required,
            device_id=values.get("AGENTSERVER_DEVICE_ID") or None,
            assignment_id=values.get("AGENTSERVER_ASSIGNMENT_ID") or None,
            task_id=values.get("AGENTSERVER_TASK_ID") or None,
            agent_instance_id=values.get("AGENTSERVER_AGENT_INSTANCE_ID") or None,
            parent_run_id=values.get("AGENTSERVER_PARENT_RUN_ID") or None,
        )


class ReporterSpool:
    """Durable at-least-once queue used by a device bridge or standalone CLI."""

    def __init__(self, database_path: Path, *, max_events: int = 10_000) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.database_path.parent.chmod(0o700)
        descriptor = os.open(
            self.database_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        os.close(descriptor)
        if os.name != "nt":
            self.database_path.chmod(0o600)
        self.max_events = max(32, int(max_events))
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reporter_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reporter_events (
                    producer_seq INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    coalescible INTEGER NOT NULL DEFAULT 0,
                    attempted INTEGER NOT NULL DEFAULT 0,
                    envelope_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(reporter_events)"
                ).fetchall()
            }
            if "attempted" not in columns:
                connection.execute(
                    "ALTER TABLE reporter_events "
                    "ADD COLUMN attempted INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS reporter_events_created ON reporter_events(created_at)"
            )
            if not connection.execute(
                "SELECT 1 FROM reporter_metadata WHERE key = 'epoch'"
            ).fetchone():
                connection.execute(
                    "INSERT INTO reporter_metadata(key, value) VALUES ('epoch', ?)",
                    (uuid.uuid4().hex,),
                )
        self._harden_sqlite_files()

    def _harden_sqlite_files(self) -> None:
        if os.name == "nt":
            return
        for path in (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            try:
                path.chmod(0o600)
            except FileNotFoundError:
                pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        self._harden_sqlite_files()
        return connection

    @property
    def epoch(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM reporter_metadata WHERE key = 'epoch'"
            ).fetchone()
        if not row:
            raise RuntimeError("reporter epoch is unavailable")
        return str(row["value"])

    def next_sequence(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(producer_seq), 0) + 1 AS value FROM reporter_events"
        ).fetchone()
        last = int(row["value"] if row else 1)
        cursor = connection.execute(
            "SELECT value FROM reporter_metadata WHERE key = 'last_sequence'"
        ).fetchone()
        if cursor:
            last = max(last, int(cursor["value"]) + 1)
        connection.execute(
            """
            INSERT INTO reporter_metadata(key, value) VALUES ('last_sequence', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(last),),
        )
        return last

    def enqueue(
        self,
        envelope_factory: Callable[[int, str], dict[str, Any]],
        *,
        event_type: str,
        run_id: str,
    ) -> dict[str, Any]:
        coalescible = event_type in COALESCIBLE_EVENT_TYPES
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = int(
                connection.execute("SELECT COUNT(*) FROM reporter_events").fetchone()[0]
            )
            if count >= self.max_events:
                # Reusing an unsent coalescible slot preserves a contiguous
                # producer sequence while allowing a terminal lifecycle event to
                # displace low-value progress.  Once a row has been attempted it
                # may already exist server-side, so changing or deleting it would
                # turn a retry into an idempotency conflict and a permanent gap.
                reusable = connection.execute(
                    """
                    SELECT producer_seq FROM reporter_events
                    WHERE producer_seq = (
                        SELECT MAX(producer_seq) FROM reporter_events
                    ) AND coalescible = 1 AND attempted = 0
                    """
                ).fetchone()
                if reusable is None:
                    raise ReporterSpoolFull(
                        "reporter spool is full; attempted events were preserved"
                    )
                sequence = int(reusable["producer_seq"])
            else:
                sequence = self.next_sequence(connection)
            epoch = str(
                connection.execute(
                    "SELECT value FROM reporter_metadata WHERE key = 'epoch'"
                ).fetchone()["value"]
            )
            envelope = envelope_factory(sequence, epoch)
            values = (
                str(envelope["event_id"]),
                event_type,
                run_id,
                int(coalescible),
                json.dumps(envelope, separators=(",", ":")),
                time.time(),
            )
            if count >= self.max_events:
                connection.execute(
                    """
                    UPDATE reporter_events
                    SET event_id = ?, event_type = ?, run_id = ?,
                        coalescible = ?, attempted = 0,
                        envelope_json = ?, created_at = ?
                    WHERE producer_seq = ? AND attempted = 0
                    """,
                    (*values, sequence),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO reporter_events(
                        producer_seq, event_id, event_type, run_id, coalescible,
                        attempted, envelope_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (sequence, *values),
                )
        return envelope

    def pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM reporter_events ORDER BY producer_seq LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [json.loads(str(row["envelope_json"])) for row in rows]

    def delivery_batch(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return a retry batch and make its contents immutable for coalescing."""
        bounded = max(1, min(int(limit), 1000))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT producer_seq, envelope_json FROM reporter_events
                ORDER BY producer_seq LIMIT ?
                """,
                (bounded,),
            ).fetchall()
            if rows:
                sequences = [int(row["producer_seq"]) for row in rows]
                placeholders = ",".join("?" for _ in sequences)
                connection.execute(
                    f"UPDATE reporter_events SET attempted = 1 "
                    f"WHERE producer_seq IN ({placeholders})",
                    sequences,
                )
        return [json.loads(str(row["envelope_json"])) for row in rows]

    def rebase_revision(self, *, producer_seq: int, current_revision: int) -> bool:
        """Rebase an event the server explicitly proved was not committed."""
        if producer_seq < 0 or current_revision < 0:
            raise ValueError("producer sequence and revision must be non-negative")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT envelope_json FROM reporter_events
                WHERE producer_seq = ?
                """,
                (producer_seq,),
            ).fetchone()
            if row is None:
                return False
            envelope = json.loads(str(row["envelope_json"]))
            envelope["expected_revision"] = current_revision
            connection.execute(
                """
                UPDATE reporter_events
                SET envelope_json = ?, attempted = 0
                WHERE producer_seq = ?
                """,
                (
                    json.dumps(envelope, separators=(",", ":")),
                    producer_seq,
                ),
            )
        return True

    def acknowledge(
        self,
        accepted_through_seq: int,
        *,
        missing_ranges: Iterable[tuple[int, int]] = (),
    ) -> int:
        missing: set[int] = set()
        for start, end in missing_ranges:
            if start < 0 or end < start or end - start > 10_000:
                raise ValueError("invalid missing reporter sequence range")
            missing.update(range(start, end + 1))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT producer_seq FROM reporter_events WHERE producer_seq <= ?",
                (int(accepted_through_seq),),
            ).fetchall()
            acknowledged = [int(row["producer_seq"]) for row in rows if int(row["producer_seq"]) not in missing]
            if acknowledged:
                placeholders = ",".join("?" for _ in acknowledged)
                connection.execute(
                    f"DELETE FROM reporter_events WHERE producer_seq IN ({placeholders})",
                    acknowledged,
                )
        return len(acknowledged)

    def __len__(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM reporter_events").fetchone()[0])


class RuntimeReporter:
    def __init__(
        self,
        context: ReporterContext,
        spool: ReporterSpool,
        *,
        producer_id: str,
        adapter: str = "generic",
        adapter_version: str = "1.0.0",
        mode: str = "active",
    ) -> None:
        if not producer_id or len(producer_id) > 255:
            raise ReporterConfigurationError("producer_id must be 1..255 characters")
        self.context = context
        self.spool = spool
        self.producer_id = producer_id
        self.adapter = adapter[:80]
        self.adapter_version = adapter_version[:40]
        self.mode = mode

    def emit(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        expected_revision: int | None = None,
        evidence_confidence: float = 1.0,
        valid_for_ms: int | None = None,
        causation_id: str | None = None,
        traceparent: str | None = None,
        occurred_at: float | None = None,
    ) -> dict[str, Any]:
        event_payload = dict(payload or {})
        encoded_payload = json.dumps(event_payload, separators=(",", ":")).encode("utf-8")
        if len(encoded_payload) > 64 * 1024:
            raise ValueError("reporter event payload exceeds 64 KiB")
        confidence = min(1.0, max(0.0, float(evidence_confidence)))

        def build(sequence: int, epoch: str) -> dict[str, Any]:
            return {
                "schema": SCHEMA_VERSION,
                "event_id": uuid.uuid4().hex,
                "type": event_type,
                "scope": {
                    "owner_id": self.context.owner_id,
                    "device_id": self.context.device_id,
                    "terminal_id": self.context.terminal_id,
                    "launch_id": self.context.launch_id,
                    "agent_instance_id": self.context.agent_instance_id,
                    "task_id": self.context.task_id,
                    "assignment_id": self.context.assignment_id,
                    "run_id": self.context.run_id,
                    "parent_run_id": self.context.parent_run_id,
                    "span_id": (
                        str(event_payload.get("span_id"))
                        if event_type.startswith("span.")
                        and event_payload.get("span_id")
                        else None
                    ),
                },
                "producer": {
                    "id": self.producer_id,
                    "epoch": epoch,
                    "seq": sequence,
                    "adapter": self.adapter,
                    "version": self.adapter_version,
                    "mode": self.mode,
                },
                "expected_revision": expected_revision,
                "occurred_at": occurred_at if occurred_at is not None else time.time(),
                "causation_id": causation_id,
                "correlation_id": self.context.task_id,
                "traceparent": traceparent,
                "evidence": {
                    "confidence": confidence,
                    "valid_for_ms": valid_for_ms,
                },
                "payload": event_payload,
            }

        return self.spool.enqueue(build, event_type=event_type, run_id=self.context.run_id)

    def flush(
        self,
        base_url: str,
        token: str,
        *,
        limit: int = 100,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> dict[str, Any]:
        events = self.spool.delivery_batch(limit=limit)
        if not events:
            return {"accepted_through_seq": 0, "missing_ranges": [], "results": []}
        with httpx.Client(transport=transport, timeout=timeout, trust_env=False) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/api/runtime/v1/events:batch",
                headers={"Authorization": f"Bearer {token}"},
                json={"events": events},
            )
            response.raise_for_status()
            result = response.json()
        for item in result.get("results", []):
            if (
                isinstance(item, Mapping)
                and item.get("status") == "rejected"
                and item.get("code") == "revision_conflict"
                and isinstance(item.get("producer_seq"), int)
                and isinstance(item.get("current_revision"), int)
            ):
                self.spool.rebase_revision(
                    producer_seq=int(item["producer_seq"]),
                    current_revision=int(item["current_revision"]),
                )
        accepted = int(result.get("accepted_through_seq", 0))
        ranges = [
            (int(item[0]), int(item[1]))
            for item in result.get("missing_ranges", [])
            if isinstance(item, list) and len(item) == 2
        ]
        self.spool.acknowledge(accepted, missing_ranges=ranges)
        return result


# Compatibility import: callers historically imported adapter declarations
# from reporter.py.  Their implementation now lives at a provider boundary so
# the durable spool remains independent of any vendor event format.
from .provider_adapters import (  # noqa: E402
    ADAPTERS,
    ClaudeAdapter,
    CodexAdapter,
    KimiAdapter,
    ProviderAdapter,
)

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .commands import CommandQueue
from .errors import (
    EntityNotFound,
    IdempotencyConflict,
    InvalidTransition,
    LeaseConflict,
    MissingExpectedRevision,
    RelationConstraintError,
    RevisionConflict,
    ValidationError,
)
from .events import (
    AppendResult,
    EventEnvelope,
    ExecutionSnapshot,
    StoredEvent,
    new_id,
)
from .models import (
    AppendStatus,
    Command,
    CommandStatus,
    Entity,
    EntityKind,
    EntityRelation,
    Lease,
    LeaseStatus,
    Projection,
    ProducerAcknowledgement,
    RelationKind,
    ResyncRequired,
    RunLifecycle,
    TaskLifecycle,
    TerminalLifecycle,
    enum_value,
    json_object,
)
from .projector import aggregate_for_event, project_event, stream_for_event


SubscriptionItem = StoredEvent | ResyncRequired
_SUBSCRIPTION_CLOSED = object()


@dataclass(eq=False)
class ExecutionSubscription:
    """An atomic state/replay snapshot followed by committed live events."""

    snapshot: ExecutionSnapshot
    _store: ExecutionStore
    _queue: asyncio.Queue[SubscriptionItem | object]
    _loop: asyncio.AbstractEventLoop
    owner_id: str
    aggregate_kind: str | None
    aggregate_id: str | None
    _closed: bool = False
    _overflowed: bool = False
    _poll_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._last_sequence = self.snapshot.as_of_sequence
        self._scan_sequence = self.snapshot.as_of_sequence
        self._pending_events: dict[int, StoredEvent] = {}
        self._poll_wakeup: asyncio.Event | None = None

    def _start_polling(self) -> None:
        if self._poll_task is None:
            self._poll_task, self._poll_wakeup = (
                self._store._start_subscription_poller(self._loop)
            )

    def __aiter__(self) -> ExecutionSubscription:
        return self

    async def __anext__(self) -> SubscriptionItem:
        if self._closed:
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is _SUBSCRIPTION_CLOSED:
            raise StopAsyncIteration
        if isinstance(item, StoredEvent):
            self._last_sequence = item.global_sequence
            return item
        if isinstance(item, ResyncRequired):
            return item
        raise StopAsyncIteration  # pragma: no cover - private queue contract

    async def next(self) -> SubscriptionItem:
        return await self.__anext__()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._store._unsubscribe(self)
            task = self._poll_task
            should_stop = self._store._stop_subscription_poller_if_idle(
                self._loop, task
            )
            if should_stop and task is not None and task is not asyncio.current_task():
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._queue.put_nowait(_SUBSCRIPTION_CLOSED)

    async def __aenter__(self) -> ExecutionSubscription:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


class ExecutionStore:
    """SQLite execution core: event log, projections and coordination atoms.

    Every projected append is one transaction: validate CAS/state machine,
    persist the immutable event, update its projection and write the outbox.
    In-process subscribers are notified only after that transaction commits.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        max_subscription_queue: int = 1024,
        max_parent_depth: int = 16,
        subscription_poll_interval: float = 0.25,
        subscription_poll_limit: int = 256,
    ) -> None:
        if max_subscription_queue < 1:
            raise ValueError("max_subscription_queue must be positive")
        if max_parent_depth < 1:
            raise ValueError("max_parent_depth must be positive")
        if subscription_poll_interval <= 0:
            raise ValueError("subscription_poll_interval must be positive")
        if subscription_poll_limit < 1:
            raise ValueError("subscription_poll_limit must be positive")
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_subscription_queue = max_subscription_queue
        self.max_parent_depth = max_parent_depth
        self.subscription_poll_interval = float(subscription_poll_interval)
        self.subscription_poll_limit = int(subscription_poll_limit)
        self._lock = threading.RLock()
        self._subscribers: set[ExecutionSubscription] = set()
        # Polling is shared per event loop. A browser may open several execution
        # WebSockets, but they all consume the same global SQLite event page.
        self._subscription_pollers: dict[
            asyncio.AbstractEventLoop, asyncio.Task[None]
        ] = {}
        self._subscription_wakeups: dict[
            asyncio.AbstractEventLoop, asyncio.Event
        ] = {}
        self._initialize()
        self.command_queue = CommandQueue(self.database_path, lock=self._lock)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_events (
                    global_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    device_id TEXT,
                    terminal_id TEXT,
                    agent_instance_id TEXT,
                    task_id TEXT,
                    assignment_id TEXT,
                    run_id TEXT,
                    span_id TEXT,
                    aggregate_kind TEXT,
                    aggregate_id TEXT,
                    producer_id TEXT NOT NULL,
                    producer_epoch TEXT NOT NULL,
                    producer_seq INTEGER NOT NULL,
                    stream_version INTEGER,
                    envelope_json TEXT NOT NULL,
                    recorded_at REAL NOT NULL,
                    UNIQUE(producer_id, producer_epoch, producer_seq)
                );
                CREATE INDEX IF NOT EXISTS execution_events_owner_sequence
                ON execution_events(owner_id, global_sequence);
                CREATE INDEX IF NOT EXISTS execution_events_stream
                ON execution_events(
                    owner_id, aggregate_kind, aggregate_id, stream_version
                );
                CREATE TRIGGER IF NOT EXISTS execution_events_no_update
                BEFORE UPDATE ON execution_events
                BEGIN
                    SELECT RAISE(ABORT, 'execution events are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS execution_events_no_delete
                BEFORE DELETE ON execution_events
                BEGIN
                    SELECT RAISE(ABORT, 'execution events are immutable');
                END;

                CREATE TABLE IF NOT EXISTS execution_producer_cursors (
                    producer_id TEXT NOT NULL,
                    producer_epoch TEXT NOT NULL,
                    max_sequence INTEGER NOT NULL,
                    received_count INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(producer_id, producer_epoch)
                );

                CREATE TABLE IF NOT EXISTS execution_projections (
                    owner_id TEXT NOT NULL,
                    aggregate_kind TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_sequence INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(owner_id, aggregate_kind, aggregate_id)
                );

                CREATE TABLE IF NOT EXISTS execution_entities (
                    owner_id TEXT NOT NULL,
                    entity_kind TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    attributes_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(owner_id, entity_kind, entity_id)
                );

                CREATE TABLE IF NOT EXISTS execution_entity_relations (
                    relation_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    attributes_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(
                        owner_id, relation_type,
                        source_kind, source_id, target_kind, target_id
                    )
                );
                CREATE INDEX IF NOT EXISTS execution_relations_source
                ON execution_entity_relations(
                    owner_id, relation_type, source_kind, source_id
                );
                CREATE INDEX IF NOT EXISTS execution_relations_target
                ON execution_entity_relations(
                    owner_id, relation_type, target_kind, target_id
                );

                CREATE TABLE IF NOT EXISTS execution_leases (
                    lease_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    resource_kind TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    holder_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    acquired_at REAL NOT NULL,
                    renewed_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    released_at REAL,
                    metadata_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS execution_active_lease_resource
                ON execution_leases(owner_id, resource_kind, resource_id)
                WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS execution_leases_holder
                ON execution_leases(owner_id, holder_id, status);

                CREATE TABLE IF NOT EXISTS execution_outbox (
                    global_sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    published_at REAL
                );

                CREATE TABLE IF NOT EXISTS execution_event_effects (
                    event_id TEXT PRIMARY KEY,
                    producer_id TEXT NOT NULL,
                    producer_epoch TEXT NOT NULL,
                    producer_seq INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES execution_events(event_id)
                );
                CREATE INDEX IF NOT EXISTS execution_event_effects_producer
                ON execution_event_effects(
                    producer_id, producer_epoch, producer_seq, status
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(execution_events)"
                ).fetchall()
            }
            for column in (
                "device_id",
                "terminal_id",
                "agent_instance_id",
                "task_id",
                "assignment_id",
                "run_id",
                "span_id",
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE execution_events ADD COLUMN {column} TEXT"
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS execution_events_run_timeline
                ON execution_events(owner_id, run_id, global_sequence)
                WHERE run_id IS NOT NULL
                """
            )

    @staticmethod
    def _require(value: str, label: str) -> str:
        result = str(value or "").strip()
        if not result or len(result) > 255:
            raise ValidationError(f"{label} must contain 1..255 characters")
        return result

    @staticmethod
    def _projection_from_row(row: sqlite3.Row | None) -> Projection | None:
        if row is None:
            return None
        return Projection(
            owner_id=row["owner_id"],
            aggregate_kind=row["aggregate_kind"],
            aggregate_id=row["aggregate_id"],
            revision=int(row["revision"]),
            state=json.loads(row["state_json"]),
            updated_sequence=int(row["updated_sequence"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> StoredEvent:
        return StoredEvent(
            global_sequence=int(row["global_sequence"]),
            stream_version=(
                int(row["stream_version"])
                if row["stream_version"] is not None
                else None
            ),
            recorded_at=float(row["recorded_at"]),
            envelope=EventEnvelope.from_dict(json.loads(row["envelope_json"])),
            aggregate_kind=row["aggregate_kind"],
            aggregate_id=row["aggregate_id"],
        )

    def _projection_row(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        aggregate_kind: str,
        aggregate_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM execution_projections
            WHERE owner_id = ? AND aggregate_kind = ? AND aggregate_id = ?
            """,
            (owner_id, aggregate_kind, aggregate_id),
        ).fetchone()

    def projection(
        self, *, owner_id: str, aggregate_kind: str, aggregate_id: str
    ) -> Projection | None:
        with self._connect() as connection:
            row = self._projection_row(
                connection,
                owner_id,
                enum_value(aggregate_kind),
                aggregate_id,
            )
        return self._projection_from_row(row)

    def projections(
        self,
        *,
        owner_id: str,
        aggregate_kind: EntityKind | str | None = None,
    ) -> tuple[Projection, ...]:
        """Return current projections without replaying the event log.

        Background reconciliation only needs durable current state. Routing it
        through :meth:`snapshot` made every five-second pass deserialize the
        owner's complete event history, which eventually monopolized the GIL
        as the production log grew.
        """
        owner_id = self._require(owner_id, "owner_id")
        conditions = ["owner_id = ?"]
        parameters: list[Any] = [owner_id]
        if aggregate_kind is not None:
            conditions.append("aggregate_kind = ?")
            parameters.append(enum_value(aggregate_kind))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_projections WHERE "
                + " AND ".join(conditions)
                + " ORDER BY aggregate_kind, aggregate_id",
                parameters,
            ).fetchall()
        return tuple(
            projection
            for projection in map(self._projection_from_row, rows)
            if projection is not None
        )

    def _duplicate_result(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        event: EventEnvelope,
        fingerprint: str,
        *,
        key_name: str,
    ) -> AppendResult:
        if row["fingerprint"] != fingerprint:
            raise IdempotencyConflict(
                f"{key_name} was reused for different event contents"
            )
        stored = self._event_from_row(row)
        projection = None
        if (
            stored.stream_version is not None
            and stored.aggregate_kind
            and stored.aggregate_id
        ):
            projection = self._projection_from_row(
                self._projection_row(
                    connection,
                    event.scope.owner_id,
                    stored.aggregate_kind,
                    stored.aggregate_id,
                )
            )
        return AppendResult(AppendStatus.DUPLICATE, stored, projection)

    def append(
        self, event: EventEnvelope, *, require_effect_ack: bool = False
    ) -> AppendResult:
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                result = self._append_in_transaction(
                    connection,
                    event,
                    require_effect_ack=require_effect_ack,
                )

            # The transaction above has committed. Keep the process lock until
            # subscriber notification is scheduled, closing the snapshot/live gap.
            self._publish_committed((result,))
            return result

    def _append_in_transaction(
        self,
        connection: sqlite3.Connection,
        event: EventEnvelope,
        *,
        require_effect_ack: bool = False,
        recorded_at: float | None = None,
    ) -> AppendResult:
        """Append on a caller-owned SQLite transaction.

        Workflow methods use this primitive so entity, lease, relation and event
        changes commit together.  The caller must publish accepted results only
        after the surrounding transaction commits.
        """
        fingerprint = event.fingerprint()
        target = aggregate_for_event(event)
        stream_target = target or stream_for_event(event)
        timestamp = time.time() if recorded_at is None else float(recorded_at)
        duplicate = connection.execute(
            "SELECT * FROM execution_events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        if duplicate is not None:
            result = self._duplicate_result(
                connection,
                duplicate,
                event,
                fingerprint,
                key_name="event_id",
            )
            if require_effect_ack:
                self._ensure_event_effect(connection, result.event.envelope, timestamp)
            return result
        duplicate = connection.execute(
            """
            SELECT * FROM execution_events
            WHERE producer_id = ? AND producer_epoch = ? AND producer_seq = ?
            """,
            (event.producer.id, event.producer.epoch, event.producer.seq),
        ).fetchone()
        if duplicate is not None:
            result = self._duplicate_result(
                connection,
                duplicate,
                event,
                fingerprint,
                key_name="producer (id, epoch, seq)",
            )
            if require_effect_ack:
                self._ensure_event_effect(connection, result.event.envelope, timestamp)
            return result

        aggregate_kind = stream_target[0] if stream_target else None
        aggregate_id = stream_target[1] if stream_target else None
        next_projection = None
        stream_version = None
        next_state = None
        if target:
            if event.expected_revision is None:
                raise MissingExpectedRevision(
                    f"{event.type} requires expected_revision"
                )
            current_projection = self._projection_from_row(
                self._projection_row(
                    connection,
                    event.scope.owner_id,
                    aggregate_kind,
                    aggregate_id,
                )
            )
            actual_revision = current_projection.revision if current_projection else 0
            if event.expected_revision != actual_revision:
                raise RevisionConflict(
                    event.expected_revision,
                    actual_revision,
                    dict(current_projection.state) if current_projection else None,
                )
            next_state = project_event(
                current_projection.state if current_projection else None, event
            )
            stream_version = actual_revision + 1

        envelope_json = json.dumps(
            event.as_dict(),
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        cursor = connection.execute(
            """
            INSERT INTO execution_events(
                event_id, fingerprint, event_type, owner_id,
                device_id, terminal_id, agent_instance_id,
                task_id, assignment_id, run_id, span_id,
                aggregate_kind, aggregate_id,
                producer_id, producer_epoch, producer_seq,
                stream_version, envelope_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                fingerprint,
                event.type,
                event.scope.owner_id,
                event.scope.device_id,
                event.scope.terminal_id,
                event.scope.agent_instance_id,
                event.scope.task_id,
                event.scope.assignment_id,
                event.scope.run_id,
                event.scope.span_id,
                aggregate_kind,
                aggregate_id,
                event.producer.id,
                event.producer.epoch,
                event.producer.seq,
                stream_version,
                envelope_json,
                timestamp,
            ),
        )
        global_sequence = int(cursor.lastrowid)
        if target:
            state_json = json.dumps(
                next_state,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            connection.execute(
                """
                INSERT INTO execution_projections(
                    owner_id, aggregate_kind, aggregate_id, revision,
                    state_json, updated_sequence, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, aggregate_kind, aggregate_id)
                DO UPDATE SET
                    revision = excluded.revision,
                    state_json = excluded.state_json,
                    updated_sequence = excluded.updated_sequence,
                    updated_at = excluded.updated_at
                """,
                (
                    event.scope.owner_id,
                    aggregate_kind,
                    aggregate_id,
                    stream_version,
                    state_json,
                    global_sequence,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO execution_entities(
                    owner_id, entity_kind, entity_id, attributes_json, created_at
                ) VALUES (?, ?, ?, '{}', ?)
                """,
                (
                    event.scope.owner_id,
                    aggregate_kind,
                    aggregate_id,
                    timestamp,
                ),
            )
            next_projection = Projection(
                owner_id=event.scope.owner_id,
                aggregate_kind=aggregate_kind,
                aggregate_id=aggregate_id,
                revision=stream_version,
                state=next_state,
                updated_sequence=global_sequence,
                updated_at=timestamp,
            )
        connection.execute(
            """
            INSERT INTO execution_producer_cursors(
                producer_id, producer_epoch, max_sequence,
                received_count, updated_at
            ) VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(producer_id, producer_epoch)
            DO UPDATE SET
                max_sequence = MAX(max_sequence, excluded.max_sequence),
                received_count = received_count + 1,
                updated_at = excluded.updated_at
            """,
            (
                event.producer.id,
                event.producer.epoch,
                event.producer.seq,
                timestamp,
            ),
        )
        if require_effect_ack:
            self._ensure_event_effect(connection, event, timestamp)
        row = connection.execute(
            "SELECT * FROM execution_events WHERE global_sequence = ?",
            (global_sequence,),
        ).fetchone()
        if row is None:  # pragma: no cover - SQLite insert contract
            raise RuntimeError("execution event insert did not return a row")
        stored = self._event_from_row(row)
        outbox_payload = json.dumps(
            {
                "event": stored.as_dict(),
                "projection": (
                    next_projection.as_dict() if next_projection else None
                ),
            },
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        connection.execute(
            """
            INSERT INTO execution_outbox(
                global_sequence, event_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                global_sequence,
                event.event_id,
                outbox_payload,
                timestamp,
            ),
        )
        return AppendResult(AppendStatus.ACCEPTED, stored, next_projection)

    def _publish_committed(self, results: tuple[AppendResult, ...]) -> None:
        accepted = [result.event for result in results if result.status is AppendStatus.ACCEPTED]
        if not accepted:
            return
        for stored in accepted:
            self._publish(stored)
        with contextlib.suppress(sqlite3.Error):
            with self._connect() as connection:
                connection.executemany(
                    """
                    UPDATE execution_outbox SET published_at = ?
                    WHERE global_sequence = ?
                    """,
                    [(time.time(), stored.global_sequence) for stored in accepted],
                )

    @staticmethod
    def _ensure_event_effect(
        connection: sqlite3.Connection,
        event: EventEnvelope,
        timestamp: float,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO execution_event_effects(
                event_id, producer_id, producer_epoch, producer_seq,
                status, attempts, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', 0, ?)
            """,
            (
                event.event_id,
                event.producer.id,
                event.producer.epoch,
                event.producer.seq,
                timestamp,
            ),
        )

    def complete_event_effect(self, *, event_id: str) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE execution_event_effects
                SET status = 'complete', attempts = attempts + 1,
                    last_error = NULL, updated_at = ?
                WHERE event_id = ?
                """,
                (time.time(), event_id),
            )
            if cursor.rowcount == 0:
                raise ValidationError("runtime event effect does not exist")

    def fail_event_effect(self, *, event_id: str, error: str) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE execution_event_effects
                SET status = 'pending', attempts = attempts + 1,
                    last_error = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (str(error)[:2000], time.time(), event_id),
            )
            if cursor.rowcount == 0:
                raise ValidationError("runtime event effect does not exist")

    def event_effect_pending(self, *, event_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM execution_event_effects WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return bool(row is not None and row["status"] != "complete")

    def timeline(
        self,
        *,
        owner_id: str,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> ExecutionSnapshot:
        """Read every event whose immutable scope belongs to one Run.

        Span and Artifact events have their own aggregate stream, so filtering
        only by ``aggregate_kind=run`` loses them. New rows use the indexed
        scope column; the JSON fallback keeps pre-migration databases readable.
        """
        owner_id = self._require(owner_id, "owner_id")
        run_id = self._require(run_id, "run_id")
        if after_sequence < 0:
            raise ValidationError("after_sequence must be non-negative")
        if limit <= 0:
            raise ValidationError("limit must be positive")
        with self._lock, self._connect() as connection:
            as_of = int(
                connection.execute(
                    "SELECT COALESCE(MAX(global_sequence), 0) FROM execution_events"
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT * FROM execution_events
                WHERE owner_id = ?
                  AND global_sequence > ?
                  AND global_sequence <= ?
                  AND (
                    run_id = ? OR (
                      run_id IS NULL
                      AND json_extract(envelope_json, '$.scope.run_id') = ?
                    )
                  )
                ORDER BY global_sequence
                LIMIT ?
                """,
                (owner_id, after_sequence, as_of, run_id, run_id, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            projection = self._projection_from_row(
                self._projection_row(
                    connection, owner_id, EntityKind.RUN.value, run_id
                )
            )
        return ExecutionSnapshot(
            owner_id=owner_id,
            as_of_sequence=as_of,
            after_sequence=after_sequence,
            events=tuple(self._event_from_row(row) for row in rows),
            projections=(projection,) if projection else (),
            resync_required=has_more or after_sequence > as_of,
        )

    def owners(self) -> tuple[str, ...]:
        """Return durable owner scopes that need background reconciliation."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT owner_id FROM execution_entities
                UNION SELECT owner_id FROM execution_projections
                UNION SELECT owner_id FROM execution_events
                ORDER BY owner_id
                """
            ).fetchall()
        return tuple(str(row["owner_id"]) for row in rows)

    def producer_cursor(self, *, producer_id: str, producer_epoch: str) -> tuple[int, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT max_sequence, received_count
                FROM execution_producer_cursors
                WHERE producer_id = ? AND producer_epoch = ?
                """,
                (producer_id, producer_epoch),
            ).fetchone()
        if row is None:
            return None
        return int(row["max_sequence"]), int(row["received_count"])

    def producer_ack(
        self, *, producer_id: str, producer_epoch: str
    ) -> ProducerAcknowledgement | None:
        """Describe durable producer sequences for an at-least-once batch ACK."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT events.producer_seq FROM execution_events AS events
                LEFT JOIN execution_event_effects AS effects
                  ON effects.event_id = events.event_id
                WHERE events.producer_id = ? AND events.producer_epoch = ?
                  AND (effects.event_id IS NULL OR effects.status = 'complete')
                ORDER BY events.producer_seq
                """,
                (producer_id, producer_epoch),
            ).fetchall()
        if not rows:
            return None
        sequences = [int(row["producer_seq"]) for row in rows]
        expected = 0 if sequences[0] == 0 else 1
        missing: list[tuple[int, int]] = []
        for sequence in sequences:
            if sequence > expected:
                missing.append((expected, sequence - 1))
            expected = sequence + 1
        return ProducerAcknowledgement(
            producer_id=producer_id,
            producer_epoch=producer_epoch,
            accepted_through_seq=sequences[-1],
            missing_ranges=tuple(missing),
            received_count=len(sequences),
        )

    def _snapshot_locked(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        after_sequence: int,
        aggregate_kind: str | None,
        aggregate_id: str | None,
        limit: int | None,
    ) -> ExecutionSnapshot:
        as_of = int(
            connection.execute(
                "SELECT COALESCE(MAX(global_sequence), 0) FROM execution_events"
            ).fetchone()[0]
        )
        cursor_ahead = after_sequence > as_of
        conditions = [
            "owner_id = ?",
            "global_sequence > ?",
            "global_sequence <= ?",
        ]
        parameters: list[Any] = [owner_id, after_sequence, as_of]
        if aggregate_kind is not None:
            conditions.append("aggregate_kind = ?")
            parameters.append(aggregate_kind)
        if aggregate_id is not None:
            conditions.append("aggregate_id = ?")
            parameters.append(aggregate_id)
        event_query = (
            "SELECT * FROM execution_events WHERE "
            + " AND ".join(conditions)
            + " ORDER BY global_sequence"
        )
        resync_required = cursor_ahead
        if limit is not None:
            event_query += " LIMIT ?"
            parameters.append(limit + 1)
        rows = connection.execute(event_query, parameters).fetchall()
        if limit is not None and len(rows) > limit:
            # Keep a useful replay page, but make truncation explicit. A live
            # subscriber must resync before trusting deltas; a REST timeline
            # reader can continue after the last returned global sequence.
            rows = rows[:limit]
            resync_required = True

        projection_conditions = ["owner_id = ?"]
        projection_parameters: list[Any] = [owner_id]
        if aggregate_kind is not None:
            projection_conditions.append("aggregate_kind = ?")
            projection_parameters.append(aggregate_kind)
        if aggregate_id is not None:
            projection_conditions.append("aggregate_id = ?")
            projection_parameters.append(aggregate_id)
        projection_rows = connection.execute(
            "SELECT * FROM execution_projections WHERE "
            + " AND ".join(projection_conditions)
            + " ORDER BY aggregate_kind, aggregate_id",
            projection_parameters,
        ).fetchall()
        return ExecutionSnapshot(
            owner_id=owner_id,
            as_of_sequence=as_of,
            after_sequence=after_sequence,
            events=tuple(self._event_from_row(row) for row in rows),
            projections=tuple(
                projection
                for projection in map(self._projection_from_row, projection_rows)
                if projection is not None
            ),
            resync_required=resync_required,
        )

    def snapshot(
        self,
        *,
        owner_id: str,
        after_sequence: int = 0,
        aggregate_kind: str | None = None,
        aggregate_id: str | None = None,
        limit: int | None = None,
    ) -> ExecutionSnapshot:
        if after_sequence < 0:
            raise ValidationError("after_sequence must be non-negative")
        if aggregate_id is not None and aggregate_kind is None:
            raise ValidationError("aggregate_id requires aggregate_kind")
        if limit is not None and limit <= 0:
            raise ValidationError("limit must be positive")
        owner_id = self._require(owner_id, "owner_id")
        kind = enum_value(aggregate_kind) if aggregate_kind is not None else None
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN")
                return self._snapshot_locked(
                    connection,
                    owner_id=owner_id,
                    after_sequence=after_sequence,
                    aggregate_kind=kind,
                    aggregate_id=aggregate_id,
                    limit=limit,
                )

    def subscribe(
        self,
        *,
        owner_id: str,
        after_sequence: int = 0,
        aggregate_kind: str | None = None,
        aggregate_id: str | None = None,
        replay_limit: int | None = None,
    ) -> ExecutionSubscription:
        loop = asyncio.get_running_loop()
        if after_sequence < 0:
            raise ValidationError("after_sequence must be non-negative")
        if aggregate_id is not None and aggregate_kind is None:
            raise ValidationError("aggregate_id requires aggregate_kind")
        if replay_limit is not None and replay_limit <= 0:
            raise ValidationError("replay_limit must be positive")
        owner_id = self._require(owner_id, "owner_id")
        kind = enum_value(aggregate_kind) if aggregate_kind is not None else None
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN")
                snapshot = self._snapshot_locked(
                    connection,
                    owner_id=owner_id,
                    after_sequence=after_sequence,
                    aggregate_kind=kind,
                    aggregate_id=aggregate_id,
                    limit=replay_limit,
                )
            subscription = ExecutionSubscription(
                snapshot=snapshot,
                _store=self,
                _queue=asyncio.Queue(maxsize=self.max_subscription_queue),
                _loop=loop,
                owner_id=owner_id,
                aggregate_kind=kind,
                aggregate_id=aggregate_id,
            )
            self._subscribers.add(subscription)
            subscription._start_polling()
            return subscription

    def _start_subscription_poller(
        self, loop: asyncio.AbstractEventLoop
    ) -> tuple[asyncio.Task[None], asyncio.Event]:
        task = self._subscription_pollers.get(loop)
        wakeup = self._subscription_wakeups.get(loop)
        if task is None or task.done() or wakeup is None:
            wakeup = asyncio.Event()
            wakeup.set()
            task = loop.create_task(self._poll_subscriptions(loop, wakeup))
            self._subscription_pollers[loop] = task
            self._subscription_wakeups[loop] = wakeup
        return task, wakeup

    def _subscriptions_for_loop(
        self, loop: asyncio.AbstractEventLoop
    ) -> tuple[ExecutionSubscription, ...]:
        return tuple(
            subscription
            for subscription in self._subscribers
            if subscription._loop is loop
            and not subscription._closed
            and not subscription._overflowed
        )

    async def _poll_subscriptions(
        self,
        loop: asyncio.AbstractEventLoop,
        wakeup: asyncio.Event,
    ) -> None:
        task = asyncio.current_task()
        try:
            while True:
                subscriptions = self._subscriptions_for_loop(loop)
                if not subscriptions:
                    return
                if not wakeup.is_set():
                    try:
                        await asyncio.wait_for(
                            wakeup.wait(),
                            timeout=self.subscription_poll_interval,
                        )
                    except asyncio.TimeoutError:
                        pass
                wakeup.clear()
                subscriptions = self._subscriptions_for_loop(loop)
                if not subscriptions:
                    return
                after_sequence = min(
                    subscription._scan_sequence
                    for subscription in subscriptions
                )
                events = await asyncio.to_thread(
                    self._subscription_events_after,
                    after_sequence,
                    self.subscription_poll_limit,
                )
                for subscription in self._subscriptions_for_loop(loop):
                    self._apply_polled_events(subscription, events)
                if len(events) >= self.subscription_poll_limit:
                    wakeup.set()
        except asyncio.CancelledError:
            return
        except sqlite3.Error:
            for subscription in self._subscriptions_for_loop(loop):
                self._mark_subscription_resync(
                    subscription,
                    latest_sequence=subscription._scan_sequence,
                )
        finally:
            if self._subscription_pollers.get(loop) is task:
                self._subscription_pollers.pop(loop, None)
                self._subscription_wakeups.pop(loop, None)

    def _stop_subscription_poller_if_idle(
        self,
        loop: asyncio.AbstractEventLoop,
        task: asyncio.Task[None] | None,
    ) -> bool:
        if self._subscriptions_for_loop(loop):
            return False
        if self._subscription_pollers.get(loop) is task:
            self._subscription_pollers.pop(loop, None)
            self._subscription_wakeups.pop(loop, None)
        if task is not None and not task.done():
            task.cancel()
        return task is not None

    def _wake_subscription_poller(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        wakeup = self._subscription_wakeups.get(loop)
        if wakeup is not None:
            wakeup.set()

    @staticmethod
    def _matches(subscription: ExecutionSubscription, event: StoredEvent) -> bool:
        if event.scope.owner_id != subscription.owner_id:
            return False
        if (
            subscription.aggregate_kind is not None
            and event.aggregate_kind != subscription.aggregate_kind
        ):
            return False
        if (
            subscription.aggregate_id is not None
            and event.aggregate_id != subscription.aggregate_id
        ):
            return False
        return True

    def _subscription_events_after(
        self, after_sequence: int, limit: int
    ) -> tuple[StoredEvent, ...]:
        """Read a bounded global page so filtered subscriptions can advance."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_events
                WHERE global_sequence > ?
                ORDER BY global_sequence
                LIMIT ?
                """,
                (after_sequence, limit),
            ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    @staticmethod
    def _clear_subscription_queue(
        subscription: ExecutionSubscription,
    ) -> None:
        while True:
            try:
                subscription._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _mark_subscription_resync(
        self,
        subscription: ExecutionSubscription,
        *,
        latest_sequence: int,
    ) -> None:
        if subscription._closed or subscription._overflowed:
            return
        self._clear_subscription_queue(subscription)
        subscription._pending_events.clear()
        subscription._overflowed = True
        subscription._queue.put_nowait(
            ResyncRequired(
                after_sequence=subscription._last_sequence,
                latest_sequence=max(
                    latest_sequence, subscription._last_sequence
                ),
            )
        )

    def _queue_subscription_event(
        self,
        subscription: ExecutionSubscription,
        event: StoredEvent,
        *,
        latest_sequence: int,
    ) -> None:
        if subscription._closed or subscription._overflowed:
            return
        try:
            subscription._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._mark_subscription_resync(
                subscription, latest_sequence=latest_sequence
            )

    def _drain_contiguous_local_events(
        self, subscription: ExecutionSubscription
    ) -> None:
        while not subscription._closed and not subscription._overflowed:
            sequence = subscription._scan_sequence + 1
            event = subscription._pending_events.pop(sequence, None)
            if event is None:
                return
            subscription._scan_sequence = sequence
            if self._matches(subscription, event):
                self._queue_subscription_event(
                    subscription,
                    event,
                    latest_sequence=max(
                        sequence,
                        max(subscription._pending_events, default=sequence),
                    ),
                )

    def _receive_local_event(
        self, subscription: ExecutionSubscription, event: StoredEvent
    ) -> None:
        if subscription._closed or subscription._overflowed:
            return
        sequence = event.global_sequence
        if sequence <= subscription._scan_sequence:
            return
        subscription._pending_events.setdefault(sequence, event)
        self._drain_contiguous_local_events(subscription)
        if subscription._overflowed:
            return
        pending_bound = max(
            subscription._queue.maxsize,
            self.subscription_poll_limit,
        ) * 2
        if len(subscription._pending_events) > pending_bound:
            self._mark_subscription_resync(
                subscription,
                latest_sequence=max(subscription._pending_events),
            )
            return
        # A contiguous in-process publish has already advanced this subscriber
        # and needs no SQLite round trip. Wake immediately only when a sequence
        # gap proves that another Store/process committed an event in between;
        # otherwise the low-rate fallback poll is sufficient for cross-process
        # commits that have no local notification path.
        if subscription._pending_events:
            self._wake_subscription_poller(subscription._loop)

    def _apply_polled_events(
        self,
        subscription: ExecutionSubscription,
        events: tuple[StoredEvent, ...],
    ) -> None:
        if subscription._closed or subscription._overflowed or not events:
            return
        latest_sequence = events[-1].global_sequence
        for event in events:
            sequence = event.global_sequence
            if sequence <= subscription._scan_sequence:
                subscription._pending_events.pop(sequence, None)
                continue
            subscription._pending_events.pop(sequence, None)
            subscription._scan_sequence = sequence
            if self._matches(subscription, event):
                self._queue_subscription_event(
                    subscription,
                    event,
                    latest_sequence=latest_sequence,
                )
                if subscription._overflowed:
                    return
        for sequence in tuple(subscription._pending_events):
            if sequence <= subscription._scan_sequence:
                subscription._pending_events.pop(sequence, None)
        self._drain_contiguous_local_events(subscription)

    def _publish(self, event: StoredEvent) -> None:
        stale: list[ExecutionSubscription] = []
        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for subscription in tuple(self._subscribers):
            if subscription._closed or subscription._loop.is_closed():
                stale.append(subscription)
                continue
            try:
                if subscription._loop is current_loop:
                    self._receive_local_event(subscription, event)
                else:
                    subscription._loop.call_soon_threadsafe(
                        self._receive_local_event, subscription, event
                    )
            except RuntimeError:
                stale.append(subscription)
        for subscription in stale:
            self._unsubscribe(subscription)

    def _unsubscribe(self, subscription: ExecutionSubscription) -> None:
        with self._lock:
            self._subscribers.discard(subscription)

    @staticmethod
    def _entity_from_row(row: sqlite3.Row) -> Entity:
        return Entity(
            owner_id=row["owner_id"],
            kind=row["entity_kind"],
            id=row["entity_id"],
            attributes=json.loads(row["attributes_json"]),
            created_at=float(row["created_at"]),
        )

    def _register_entity_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        kind_value: str,
        entity_id: str,
        body: Mapping[str, Any],
        timestamp: float,
    ) -> Entity:
        body_json = json.dumps(body, separators=(",", ":"), sort_keys=True)
        row = connection.execute(
            """
            SELECT * FROM execution_entities
            WHERE owner_id = ? AND entity_kind = ? AND entity_id = ?
            """,
            (owner_id, kind_value, entity_id),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO execution_entities(
                    owner_id, entity_kind, entity_id,
                    attributes_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (owner_id, kind_value, entity_id, body_json, timestamp),
            )
        elif row["attributes_json"] == "{}" and body:
            connection.execute(
                """
                UPDATE execution_entities SET attributes_json = ?
                WHERE owner_id = ? AND entity_kind = ? AND entity_id = ?
                """,
                (body_json, owner_id, kind_value, entity_id),
            )
        elif json.loads(row["attributes_json"]) != body and body:
            raise ValidationError(
                "entity is already registered with different attributes"
            )
        result = connection.execute(
            """
            SELECT * FROM execution_entities
            WHERE owner_id = ? AND entity_kind = ? AND entity_id = ?
            """,
            (owner_id, kind_value, entity_id),
        ).fetchone()
        if result is None:  # pragma: no cover - SQLite insert contract
            raise RuntimeError("execution entity insert did not return a row")
        return self._entity_from_row(result)

    def register_entity(
        self,
        *,
        owner_id: str,
        kind: EntityKind | str,
        entity_id: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> Entity:
        owner_id = self._require(owner_id, "owner_id")
        kind_value = self._require(enum_value(kind), "entity kind")
        entity_id = self._require(entity_id, "entity_id")
        body = json_object(attributes, field_name="entity attributes")
        timestamp = time.time()
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self._register_entity_in_transaction(
                    connection,
                    owner_id=owner_id,
                    kind_value=kind_value,
                    entity_id=entity_id,
                    body=body,
                    timestamp=timestamp,
                )

    def register_entity_with_initial_event(
        self,
        *,
        owner_id: str,
        kind: EntityKind | str,
        entity_id: str,
        attributes: Mapping[str, Any] | None,
        event: EventEnvelope,
    ) -> tuple[Entity, Projection]:
        """Atomically register an entity and its first projected event.

        A competing store instance waits on ``BEGIN IMMEDIATE`` and then sees
        the committed projection.  Exact retries therefore return the same
        entity/projection instead of racing an expected-revision-zero append.
        """
        owner_id = self._require(owner_id, "owner_id")
        kind_value = self._require(enum_value(kind), "entity kind")
        entity_id = self._require(entity_id, "entity_id")
        body = json_object(attributes, field_name="entity attributes")
        target = aggregate_for_event(event)
        if target != (kind_value, entity_id) or event.scope.owner_id != owner_id:
            raise ValidationError("initial event must target the registered entity")
        if event.expected_revision != 0:
            raise ValidationError("initial event must expect revision 0")
        timestamp = time.time()
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                idempotency_key = str(body.get("idempotency_key") or "")
                if idempotency_key:
                    keyed_rows = connection.execute(
                        """
                        SELECT * FROM execution_entities
                        WHERE owner_id = ? AND entity_kind = ?
                        """,
                        (owner_id, kind_value),
                    ).fetchall()
                    for keyed_row in keyed_rows:
                        keyed_attributes = json.loads(
                            keyed_row["attributes_json"]
                        )
                        if (
                            keyed_attributes.get("idempotency_key")
                            == idempotency_key
                            and str(keyed_row["entity_id"]) != entity_id
                        ):
                            raise IdempotencyConflict(
                                "idempotency_key is already bound to another entity"
                            )
                entity = self._register_entity_in_transaction(
                    connection,
                    owner_id=owner_id,
                    kind_value=kind_value,
                    entity_id=entity_id,
                    body=body,
                    timestamp=timestamp,
                )
                projection = self._projection_from_row(
                    self._projection_row(
                        connection, owner_id, kind_value, entity_id
                    )
                )
                result: AppendResult | None = None
                if projection is None:
                    result = self._append_in_transaction(
                        connection, event, recorded_at=timestamp
                    )
                    projection = result.projection
                if projection is None:  # pragma: no cover - projected event contract
                    raise RuntimeError("initial event did not create a projection")

            if result is not None:
                self._publish_committed((result,))
            return entity, projection

    def _workflow_checkpoint(self, operation: str, step: str) -> None:
        """Fault-injection seam for transaction-boundary regression tests."""

    def commit_assignment(
        self,
        *,
        owner_id: str,
        task_id: str,
        terminal_id: str,
        assignment_id: str,
        run_id: str,
        agent_instance_id: str,
        expected_task_revision: int,
        assignment_attributes: Mapping[str, Any],
        run_attributes: Mapping[str, Any],
        agent_attributes: Mapping[str, Any],
        events: tuple[EventEnvelope, EventEnvelope, EventEnvelope],
        parent_run_id: str | None = None,
        lease_ttl: float,
        default_max_child_runs: int,
        now: float | None = None,
    ) -> Projection:
        """Commit the complete Task assignment workflow in one transaction.

        The task/terminal preconditions are re-read after ``BEGIN IMMEDIATE``;
        no lease, entity, relation, or event can survive a losing race or an
        exception at any later workflow step.
        """
        owner_id = self._require(owner_id, "owner_id")
        task_id = self._require(task_id, "task_id")
        terminal_id = self._require(terminal_id, "terminal_id")
        assignment_id = self._require(assignment_id, "assignment_id")
        run_id = self._require(run_id, "run_id")
        agent_instance_id = self._require(agent_instance_id, "agent_instance_id")
        if parent_run_id is not None:
            parent_run_id = self._require(parent_run_id, "parent_run_id")
        if (
            not isinstance(expected_task_revision, int)
            or isinstance(expected_task_revision, bool)
            or expected_task_revision < 0
        ):
            raise ValidationError("expected_task_revision must be non-negative")
        if lease_ttl <= 0:
            raise ValidationError("lease_ttl must be positive")
        if default_max_child_runs < 1:
            raise ValidationError("default_max_child_runs must be positive")
        assignment_body = json_object(
            assignment_attributes, field_name="assignment attributes"
        )
        run_body = json_object(run_attributes, field_name="run attributes")
        agent_body = json_object(agent_attributes, field_name="agent attributes")
        expected_events = (
            ("assignment.created", EntityKind.ASSIGNMENT.value, assignment_id),
            ("run.requested", EntityKind.RUN.value, run_id),
            ("task.assigned", EntityKind.TASK.value, task_id),
        )
        if len(events) != len(expected_events):
            raise ValidationError("assignment workflow requires exactly three events")
        for event, (event_type, kind, identifier) in zip(events, expected_events):
            if (
                event.type != event_type
                or event.scope.owner_id != owner_id
                or aggregate_for_event(event) != (kind, identifier)
            ):
                raise ValidationError("assignment workflow event scope is inconsistent")
        timestamp = time.time() if now is None else float(now)
        active_run_states = {
            RunLifecycle.PENDING.value,
            RunLifecycle.STARTING.value,
            RunLifecycle.RUNNING.value,
        }
        published: tuple[AppendResult, ...] = ()
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                entity_rows = connection.execute(
                    """
                    SELECT * FROM execution_entities
                    WHERE owner_id = ? AND (
                        (entity_kind = ? AND entity_id = ?) OR
                        (entity_kind = ? AND entity_id = ?)
                    )
                    """,
                    (
                        owner_id,
                        EntityKind.TASK.value,
                        task_id,
                        EntityKind.TERMINAL.value,
                        terminal_id,
                    ),
                ).fetchall()
                by_kind = {str(row["entity_kind"]): row for row in entity_rows}
                task_row = by_kind.get(EntityKind.TASK.value)
                terminal_row = by_kind.get(EntityKind.TERMINAL.value)
                if task_row is None or terminal_row is None:
                    raise EntityNotFound(
                        "task and terminal must exist in the same owner scope"
                    )
                task = self._entity_from_row(task_row)
                terminal = self._entity_from_row(terminal_row)
                task_projection = self._projection_from_row(
                    self._projection_row(
                        connection, owner_id, EntityKind.TASK.value, task_id
                    )
                )
                if task_projection is None:
                    raise EntityNotFound("task projection does not exist")
                if task_projection.revision != expected_task_revision:
                    raise RevisionConflict(
                        expected_task_revision,
                        task_projection.revision,
                        dict(task_projection.state),
                    )
                if (
                    task_projection.state.get("lifecycle")
                    != TaskLifecycle.SUBMITTED.value
                ):
                    raise InvalidTransition(
                        "task", task_projection.state.get("lifecycle"), "assigned"
                    )
                terminal_projection = self._projection_from_row(
                    self._projection_row(
                        connection, owner_id, EntityKind.TERMINAL.value, terminal_id
                    )
                )
                if (
                    terminal_projection is None
                    or terminal_projection.state.get("lifecycle")
                    != TerminalLifecycle.READY.value
                ):
                    raise InvalidTransition(
                        "terminal",
                        (
                            terminal_projection.state.get("lifecycle")
                            if terminal_projection
                            else None
                        ),
                        "assign",
                    )
                launch_id = str(terminal.attributes.get("launch_id") or "")
                if not launch_id:
                    raise ValidationError(
                        "task assignment requires a managed terminal launch"
                    )
                if any(
                    str(body.get("launch_id") or "") != launch_id
                    for body in (assignment_body, run_body, agent_body)
                ):
                    raise ValidationError(
                        "assignment workflow does not match terminal launch"
                    )
                reserved = connection.execute(
                    """
                    SELECT entity_kind, entity_id FROM execution_entities
                    WHERE owner_id = ? AND (
                        (entity_kind = ? AND entity_id = ?) OR
                        (entity_kind = ? AND entity_id = ?) OR
                        (entity_kind = ? AND entity_id = ?)
                    )
                    """,
                    (
                        owner_id,
                        EntityKind.ASSIGNMENT.value,
                        assignment_id,
                        EntityKind.RUN.value,
                        run_id,
                        EntityKind.AGENT_INSTANCE.value,
                        agent_instance_id,
                    ),
                ).fetchone()
                if reserved is not None:
                    raise IdempotencyConflict(
                        f"{reserved['entity_kind']} identifier is already registered"
                    )

                parent_row: sqlite3.Row | None = None
                child_relations: list[sqlite3.Row] = []
                if parent_run_id:
                    if str(task.attributes.get("parent_run_id") or "") != parent_run_id:
                        raise RelationConstraintError(
                            "child Task and Assignment must name the same parent Run"
                        )
                    parent_row = connection.execute(
                        """
                        SELECT * FROM execution_entities
                        WHERE owner_id = ? AND entity_kind = ? AND entity_id = ?
                        """,
                        (owner_id, EntityKind.RUN.value, parent_run_id),
                    ).fetchone()
                    parent_projection = self._projection_from_row(
                        self._projection_row(
                            connection,
                            owner_id,
                            EntityKind.RUN.value,
                            parent_run_id,
                        )
                    )
                    if parent_row is None or parent_projection is None:
                        raise EntityNotFound("parent Run does not exist")
                    if parent_projection.state.get("lifecycle") not in active_run_states:
                        raise InvalidTransition(
                            "run",
                            parent_projection.state.get("lifecycle"),
                            "assign_child",
                        )
                    graph = self._run_graph(connection, owner_id)
                    if self._ancestor_depth(graph, parent_run_id) + 1 > self.max_parent_depth:
                        raise RelationConstraintError(
                            f"parent_run relation exceeds max depth {self.max_parent_depth}"
                        )
                    child_relations = connection.execute(
                        """
                        SELECT target_id FROM execution_entity_relations
                        WHERE owner_id = ? AND relation_type = ?
                          AND source_kind = ? AND source_id = ?
                          AND target_kind = ?
                        """,
                        (
                            owner_id,
                            RelationKind.PARENT_RUN.value,
                            EntityKind.RUN.value,
                            parent_run_id,
                            EntityKind.RUN.value,
                        ),
                    ).fetchall()
                    active_children = 0
                    for relation in child_relations:
                        child_projection = self._projection_from_row(
                            self._projection_row(
                                connection,
                                owner_id,
                                EntityKind.RUN.value,
                                str(relation["target_id"]),
                            )
                        )
                        if (
                            child_projection is not None
                            and child_projection.state.get("lifecycle")
                            in active_run_states
                        ):
                            active_children += 1
                    parent = self._entity_from_row(parent_row)
                    maximum = int(
                        parent.attributes.get("max_child_runs")
                        or default_max_child_runs
                    )
                    if active_children >= maximum:
                        raise RelationConstraintError(
                            "parent Run active child concurrency limit was reached"
                        )
                    deadline = task.attributes.get("deadline_at")
                    if deadline is not None and float(deadline) <= timestamp:
                        raise ValidationError("child Task deadline has expired")
                    for label in ("token_budget", "cost_budget_micros"):
                        limit = parent.attributes.get(label)
                        requested = task.attributes.get(label)
                        if limit is None:
                            continue
                        allocated = 0
                        for relation in child_relations:
                            row = connection.execute(
                                """
                                SELECT attributes_json FROM execution_entities
                                WHERE owner_id = ? AND entity_kind = ? AND entity_id = ?
                                """,
                                (
                                    owner_id,
                                    EntityKind.RUN.value,
                                    str(relation["target_id"]),
                                ),
                            ).fetchone()
                            if row is not None:
                                allocated += int(
                                    json.loads(row["attributes_json"]).get(label) or 0
                                )
                        if (
                            requested is None
                            or allocated + int(requested) > int(limit)
                        ):
                            raise RelationConstraintError(
                                f"parent Run {label} allocation would be exceeded"
                            )

                self._workflow_checkpoint("assign_task", "validated")
                self._expire_leases(connection, timestamp)
                active_lease = connection.execute(
                    """
                    SELECT * FROM execution_leases
                    WHERE owner_id = ? AND resource_kind = ? AND resource_id = ?
                      AND status = ?
                    """,
                    (
                        owner_id,
                        EntityKind.TERMINAL.value,
                        terminal_id,
                        LeaseStatus.ACTIVE.value,
                    ),
                ).fetchone()
                if active_lease is not None:
                    lease = self._lease_from_row(active_lease)
                    raise LeaseConflict(
                        f"resource is leased by {lease.holder_id} until {lease.expires_at}"
                    )
                lease_id = new_id()
                connection.execute(
                    """
                    INSERT INTO execution_leases(
                        lease_id, owner_id, resource_kind, resource_id,
                        holder_id, status, revision, acquired_at, renewed_at,
                        expires_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        lease_id,
                        owner_id,
                        EntityKind.TERMINAL.value,
                        terminal_id,
                        assignment_id,
                        LeaseStatus.ACTIVE.value,
                        timestamp,
                        timestamp,
                        timestamp + float(lease_ttl),
                        json.dumps(
                            {"task_id": task_id, "run_id": run_id},
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    ),
                )
                self._workflow_checkpoint("assign_task", "lease")
                for step, kind, identifier, body in (
                    (
                        "assignment_entity",
                        EntityKind.ASSIGNMENT.value,
                        assignment_id,
                        assignment_body,
                    ),
                    ("run_entity", EntityKind.RUN.value, run_id, run_body),
                    (
                        "agent_entity",
                        EntityKind.AGENT_INSTANCE.value,
                        agent_instance_id,
                        agent_body,
                    ),
                ):
                    self._register_entity_in_transaction(
                        connection,
                        owner_id=owner_id,
                        kind_value=kind,
                        entity_id=identifier,
                        body=body,
                        timestamp=timestamp,
                    )
                    self._workflow_checkpoint("assign_task", step)

                relation_specs = (
                    (
                        RelationKind.CONTAINS.value,
                        EntityKind.TASK.value,
                        task_id,
                        EntityKind.ASSIGNMENT.value,
                        assignment_id,
                    ),
                    (
                        RelationKind.BOUND_TO.value,
                        EntityKind.ASSIGNMENT.value,
                        assignment_id,
                        EntityKind.TERMINAL.value,
                        terminal_id,
                    ),
                    (
                        RelationKind.EXECUTES.value,
                        EntityKind.ASSIGNMENT.value,
                        assignment_id,
                        EntityKind.RUN.value,
                        run_id,
                    ),
                    (
                        RelationKind.BOUND_TO.value,
                        EntityKind.AGENT_INSTANCE.value,
                        agent_instance_id,
                        EntityKind.TERMINAL.value,
                        terminal_id,
                    ),
                    (
                        RelationKind.EXECUTES.value,
                        EntityKind.AGENT_INSTANCE.value,
                        agent_instance_id,
                        EntityKind.RUN.value,
                        run_id,
                    ),
                )
                for relation, source_kind, source_id, target_kind, target_id in relation_specs:
                    connection.execute(
                        """
                        INSERT INTO execution_entity_relations(
                            relation_id, owner_id, relation_type,
                            source_kind, source_id, target_kind, target_id,
                            attributes_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?)
                        """,
                        (
                            new_id(),
                            owner_id,
                            relation,
                            source_kind,
                            source_id,
                            target_kind,
                            target_id,
                            timestamp,
                        ),
                    )
                self._workflow_checkpoint("assign_task", "relations")
                if parent_run_id:
                    connection.execute(
                        """
                        INSERT INTO execution_entity_relations(
                            relation_id, owner_id, relation_type,
                            source_kind, source_id, target_kind, target_id,
                            attributes_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?)
                        """,
                        (
                            new_id(),
                            owner_id,
                            RelationKind.PARENT_RUN.value,
                            EntityKind.RUN.value,
                            parent_run_id,
                            EntityKind.RUN.value,
                            run_id,
                            timestamp,
                        ),
                    )
                    self._workflow_checkpoint("assign_task", "parent_relation")

                accepted: list[AppendResult] = []
                for step, event in zip(
                    ("assignment_event", "run_event", "task_event"), events
                ):
                    result = self._append_in_transaction(
                        connection, event, recorded_at=timestamp
                    )
                    if result.status is not AppendStatus.ACCEPTED:
                        raise IdempotencyConflict(
                            "assignment workflow event unexpectedly already exists"
                        )
                    accepted.append(result)
                    self._workflow_checkpoint("assign_task", step)
                self._workflow_checkpoint("assign_task", "before_commit")
                published = tuple(accepted)
                task_result = accepted[-1].projection
                if task_result is None:  # pragma: no cover - projected event contract
                    raise RuntimeError("task assignment did not update its projection")

            self._publish_committed(published)
            return task_result

    def get_entity(
        self, *, owner_id: str, kind: EntityKind | str, entity_id: str
    ) -> Entity | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM execution_entities
                WHERE owner_id = ? AND entity_kind = ? AND entity_id = ?
                """,
                (owner_id, enum_value(kind), entity_id),
            ).fetchone()
        return self._entity_from_row(row) if row is not None else None

    def entities(
        self,
        *,
        owner_id: str,
        kind: EntityKind | str | None = None,
    ) -> tuple[Entity, ...]:
        """Return an owner's entities in one query for bulk state views."""
        owner_id = self._require(owner_id, "owner_id")
        conditions = ["owner_id = ?"]
        parameters: list[Any] = [owner_id]
        if kind is not None:
            conditions.append("entity_kind = ?")
            parameters.append(enum_value(kind))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_entities WHERE "
                + " AND ".join(conditions)
                + " ORDER BY entity_kind, entity_id",
                parameters,
            ).fetchall()
        return tuple(self._entity_from_row(row) for row in rows)

    @staticmethod
    def _relation_from_row(row: sqlite3.Row) -> EntityRelation:
        return EntityRelation(
            id=row["relation_id"],
            owner_id=row["owner_id"],
            relation=row["relation_type"],
            source_kind=row["source_kind"],
            source_id=row["source_id"],
            target_kind=row["target_kind"],
            target_id=row["target_id"],
            attributes=json.loads(row["attributes_json"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _run_graph(connection: sqlite3.Connection, owner_id: str) -> dict[str, set[str]]:
        rows = connection.execute(
            """
            SELECT source_id, target_id FROM execution_entity_relations
            WHERE owner_id = ? AND relation_type = ?
              AND source_kind = ? AND target_kind = ?
            """,
            (
                owner_id,
                RelationKind.PARENT_RUN.value,
                EntityKind.RUN.value,
                EntityKind.RUN.value,
            ),
        ).fetchall()
        graph: dict[str, set[str]] = {}
        for row in rows:
            graph.setdefault(row["source_id"], set()).add(row["target_id"])
        return graph

    @staticmethod
    def _reachable(graph: Mapping[str, set[str]], start: str, target: str) -> bool:
        pending = [start]
        seen: set[str] = set()
        while pending:
            node = pending.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            pending.extend(graph.get(node, ()))
        return False

    @staticmethod
    def _longest_descendant_path(graph: Mapping[str, set[str]], start: str) -> int:
        def visit(node: str, seen: set[str]) -> int:
            children = graph.get(node, set()) - seen
            if not children:
                return 0
            return 1 + max(visit(child, seen | {child}) for child in children)

        return visit(start, {start})

    @staticmethod
    def _ancestor_depth(graph: Mapping[str, set[str]], node: str) -> int:
        parents = {
            child: parent
            for parent, children in graph.items()
            for child in children
        }
        depth = 0
        seen = {node}
        while node in parents:
            node = parents[node]
            if node in seen:
                raise RelationConstraintError("parent_run graph already contains a cycle")
            seen.add(node)
            depth += 1
        return depth

    def link_entities(
        self,
        *,
        owner_id: str,
        relation: RelationKind | str,
        source_kind: EntityKind | str,
        source_id: str,
        target_kind: EntityKind | str,
        target_id: str,
        relation_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> EntityRelation:
        owner_id = self._require(owner_id, "owner_id")
        relation_value = self._require(enum_value(relation), "relation")
        source_kind_value = self._require(enum_value(source_kind), "source_kind")
        target_kind_value = self._require(enum_value(target_kind), "target_kind")
        source_id = self._require(source_id, "source_id")
        target_id = self._require(target_id, "target_id")
        relation_id = self._require(relation_id or new_id(), "relation_id")
        body = json_object(attributes, field_name="relation attributes")
        body_json = json.dumps(body, separators=(",", ":"), sort_keys=True)
        timestamp = time.time()
        if source_kind_value == target_kind_value and source_id == target_id:
            raise RelationConstraintError("an entity cannot be linked to itself")
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                relation_row = connection.execute(
                    "SELECT * FROM execution_entity_relations WHERE relation_id = ?",
                    (relation_id,),
                ).fetchone()
                if relation_row is not None:
                    values = self._relation_from_row(relation_row)
                    if (
                        values.owner_id != owner_id
                        or values.relation != relation_value
                        or values.source_kind != source_kind_value
                        or values.source_id != source_id
                        or values.target_kind != target_kind_value
                        or values.target_id != target_id
                        or dict(values.attributes) != body
                    ):
                        raise RelationConstraintError(
                            "relation_id was reused for a different relation"
                        )
                    return values
                entities = connection.execute(
                    """
                    SELECT entity_kind, entity_id FROM execution_entities
                    WHERE owner_id = ? AND (
                        (entity_kind = ? AND entity_id = ?) OR
                        (entity_kind = ? AND entity_id = ?)
                    )
                    """,
                    (
                        owner_id,
                        source_kind_value,
                        source_id,
                        target_kind_value,
                        target_id,
                    ),
                ).fetchall()
                found = {(row["entity_kind"], row["entity_id"]) for row in entities}
                required = {
                    (source_kind_value, source_id),
                    (target_kind_value, target_id),
                }
                if found != required:
                    raise EntityNotFound(
                        "both relation endpoints must exist in the same owner scope"
                    )
                duplicate = connection.execute(
                    """
                    SELECT * FROM execution_entity_relations
                    WHERE owner_id = ? AND relation_type = ?
                      AND source_kind = ? AND source_id = ?
                      AND target_kind = ? AND target_id = ?
                    """,
                    (
                        owner_id,
                        relation_value,
                        source_kind_value,
                        source_id,
                        target_kind_value,
                        target_id,
                    ),
                ).fetchone()
                if duplicate is not None:
                    existing = self._relation_from_row(duplicate)
                    if dict(existing.attributes) != body:
                        raise RelationConstraintError(
                            "relation already exists with different attributes"
                        )
                    return existing
                if relation_value == RelationKind.PARENT_RUN.value:
                    if (
                        source_kind_value != EntityKind.RUN.value
                        or target_kind_value != EntityKind.RUN.value
                    ):
                        raise RelationConstraintError(
                            "parent_run relations require two run entities"
                        )
                    existing_parent = connection.execute(
                        """
                        SELECT source_id FROM execution_entity_relations
                        WHERE owner_id = ? AND relation_type = ?
                          AND target_kind = ? AND target_id = ?
                        """,
                        (
                            owner_id,
                            RelationKind.PARENT_RUN.value,
                            EntityKind.RUN.value,
                            target_id,
                        ),
                    ).fetchone()
                    if existing_parent is not None:
                        raise RelationConstraintError("a child run may have only one parent")
                    graph = self._run_graph(connection, owner_id)
                    if self._reachable(graph, target_id, source_id):
                        raise RelationConstraintError("parent_run relation would create a cycle")
                    ancestor_depth = self._ancestor_depth(graph, source_id)
                    descendant_depth = self._longest_descendant_path(graph, target_id)
                    if ancestor_depth + 1 + descendant_depth > self.max_parent_depth:
                        raise RelationConstraintError(
                            f"parent_run relation exceeds max depth {self.max_parent_depth}"
                        )
                connection.execute(
                    """
                    INSERT INTO execution_entity_relations(
                        relation_id, owner_id, relation_type,
                        source_kind, source_id, target_kind, target_id,
                        attributes_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        relation_id,
                        owner_id,
                        relation_value,
                        source_kind_value,
                        source_id,
                        target_kind_value,
                        target_id,
                        body_json,
                        timestamp,
                    ),
                )
                result = connection.execute(
                    "SELECT * FROM execution_entity_relations WHERE relation_id = ?",
                    (relation_id,),
                ).fetchone()
        return self._relation_from_row(result)

    def link_runs(
        self,
        *,
        owner_id: str,
        parent_run_id: str,
        child_run_id: str,
        relation_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> EntityRelation:
        return self.link_entities(
            owner_id=owner_id,
            relation=RelationKind.PARENT_RUN,
            source_kind=EntityKind.RUN,
            source_id=parent_run_id,
            target_kind=EntityKind.RUN,
            target_id=child_run_id,
            relation_id=relation_id,
            attributes=attributes,
        )

    def validate_new_child_run(self, *, owner_id: str, parent_run_id: str) -> None:
        """Preflight the depth constraint before an assignment Saga starts."""
        owner_id = self._require(owner_id, "owner_id")
        parent_run_id = self._require(parent_run_id, "parent_run_id")
        with self._lock, self._connect() as connection:
            parent = connection.execute(
                """
                SELECT 1 FROM execution_entities
                WHERE owner_id = ? AND entity_kind = ? AND entity_id = ?
                """,
                (owner_id, EntityKind.RUN.value, parent_run_id),
            ).fetchone()
            if parent is None:
                raise EntityNotFound("parent run does not exist in the owner scope")
            graph = self._run_graph(connection, owner_id)
            if self._ancestor_depth(graph, parent_run_id) + 1 > self.max_parent_depth:
                raise RelationConstraintError(
                    f"parent_run relation exceeds max depth {self.max_parent_depth}"
                )

    def validate_run_link(
        self, *, owner_id: str, parent_run_id: str, child_run_id: str
    ) -> None:
        """Read-only validation used before persisting child_run.linked."""
        owner_id = self._require(owner_id, "owner_id")
        parent_run_id = self._require(parent_run_id, "parent_run_id")
        child_run_id = self._require(child_run_id, "child_run_id")
        if parent_run_id == child_run_id:
            raise RelationConstraintError("a Run cannot be its own child")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT entity_id FROM execution_entities
                WHERE owner_id = ? AND entity_kind = ?
                  AND entity_id IN (?, ?)
                """,
                (
                    owner_id,
                    EntityKind.RUN.value,
                    parent_run_id,
                    child_run_id,
                ),
            ).fetchall()
            if {str(row["entity_id"]) for row in rows} != {
                parent_run_id,
                child_run_id,
            }:
                raise EntityNotFound("both parent and child Run must exist")
            existing = connection.execute(
                """
                SELECT source_id FROM execution_entity_relations
                WHERE owner_id = ? AND relation_type = ?
                  AND target_kind = ? AND target_id = ?
                """,
                (
                    owner_id,
                    RelationKind.PARENT_RUN.value,
                    EntityKind.RUN.value,
                    child_run_id,
                ),
            ).fetchone()
            if existing is not None:
                if str(existing["source_id"]) == parent_run_id:
                    return
                raise RelationConstraintError("a child run may have only one parent")
            graph = self._run_graph(connection, owner_id)
            if self._reachable(graph, child_run_id, parent_run_id):
                raise RelationConstraintError("parent_run relation would create a cycle")
            if (
                self._ancestor_depth(graph, parent_run_id)
                + 1
                + self._longest_descendant_path(graph, child_run_id)
                > self.max_parent_depth
            ):
                raise RelationConstraintError(
                    f"parent_run relation exceeds max depth {self.max_parent_depth}"
                )

    def relations(
        self,
        *,
        owner_id: str,
        relation: RelationKind | str | None = None,
        source_kind: EntityKind | str | None = None,
        source_id: str | None = None,
        target_kind: EntityKind | str | None = None,
        target_id: str | None = None,
    ) -> list[EntityRelation]:
        conditions = ["owner_id = ?"]
        parameters: list[Any] = [owner_id]
        for column, value in (
            ("relation_type", enum_value(relation) if relation is not None else None),
            ("source_kind", enum_value(source_kind) if source_kind is not None else None),
            ("source_id", source_id),
            ("target_kind", enum_value(target_kind) if target_kind is not None else None),
            ("target_id", target_id),
        ):
            if value is not None:
                conditions.append(f"{column} = ?")
                parameters.append(value)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_entity_relations WHERE "
                + " AND ".join(conditions)
                + " ORDER BY created_at, relation_id",
                parameters,
            ).fetchall()
        return [self._relation_from_row(row) for row in rows]

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> Lease:
        return Lease(
            id=row["lease_id"],
            owner_id=row["owner_id"],
            resource_kind=row["resource_kind"],
            resource_id=row["resource_id"],
            holder_id=row["holder_id"],
            status=LeaseStatus(row["status"]),
            revision=int(row["revision"]),
            acquired_at=float(row["acquired_at"]),
            renewed_at=float(row["renewed_at"]),
            expires_at=float(row["expires_at"]),
            released_at=(
                float(row["released_at"])
                if row["released_at"] is not None
                else None
            ),
            metadata=json.loads(row["metadata_json"]),
        )

    @staticmethod
    def _expire_leases(connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            """
            UPDATE execution_leases
            SET status = ?, revision = revision + 1, released_at = ?
            WHERE status = ? AND expires_at <= ?
            """,
            (
                LeaseStatus.EXPIRED.value,
                now,
                LeaseStatus.ACTIVE.value,
                now,
            ),
        )

    def acquire_lease(
        self,
        *,
        owner_id: str,
        resource_kind: EntityKind | str,
        resource_id: str,
        holder_id: str,
        ttl_seconds: float,
        lease_id: str | None = None,
        now: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Lease:
        owner_id = self._require(owner_id, "owner_id")
        kind = self._require(enum_value(resource_kind), "resource_kind")
        resource_id = self._require(resource_id, "resource_id")
        holder_id = self._require(holder_id, "holder_id")
        lease_id = self._require(lease_id or new_id(), "lease_id")
        if ttl_seconds <= 0:
            raise ValidationError("ttl_seconds must be positive")
        timestamp = time.time() if now is None else float(now)
        body = json_object(metadata, field_name="lease metadata")
        body_json = json.dumps(body, separators=(",", ":"), sort_keys=True)
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_leases(connection, timestamp)
                duplicate = connection.execute(
                    "SELECT * FROM execution_leases WHERE lease_id = ?",
                    (lease_id,),
                ).fetchone()
                if duplicate is not None:
                    lease = self._lease_from_row(duplicate)
                    if (
                        lease.owner_id != owner_id
                        or lease.resource_kind != kind
                        or lease.resource_id != resource_id
                        or lease.holder_id != holder_id
                        or dict(lease.metadata) != body
                    ):
                        raise LeaseConflict("lease_id was reused for a different lease")
                    return lease
                entity = connection.execute(
                    """
                    SELECT 1 FROM execution_entities
                    WHERE owner_id = ? AND entity_kind = ? AND entity_id = ?
                    """,
                    (owner_id, kind, resource_id),
                ).fetchone()
                if entity is None:
                    raise EntityNotFound("lease resource does not exist in owner scope")
                active = connection.execute(
                    """
                    SELECT * FROM execution_leases
                    WHERE owner_id = ? AND resource_kind = ? AND resource_id = ?
                      AND status = ?
                    """,
                    (owner_id, kind, resource_id, LeaseStatus.ACTIVE.value),
                ).fetchone()
                if active is not None:
                    lease = self._lease_from_row(active)
                    raise LeaseConflict(
                        f"resource is leased by {lease.holder_id} until {lease.expires_at}"
                    )
                connection.execute(
                    """
                    INSERT INTO execution_leases(
                        lease_id, owner_id, resource_kind, resource_id,
                        holder_id, status, revision, acquired_at, renewed_at,
                        expires_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        lease_id,
                        owner_id,
                        kind,
                        resource_id,
                        holder_id,
                        LeaseStatus.ACTIVE.value,
                        timestamp,
                        timestamp,
                        timestamp + float(ttl_seconds),
                        body_json,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM execution_leases WHERE lease_id = ?",
                    (lease_id,),
                ).fetchone()
        return self._lease_from_row(row)

    def renew_lease(
        self,
        *,
        owner_id: str,
        lease_id: str,
        holder_id: str,
        ttl_seconds: float,
        expected_revision: int | None = None,
        now: float | None = None,
    ) -> Lease:
        if ttl_seconds <= 0:
            raise ValidationError("ttl_seconds must be positive")
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_leases(connection, timestamp)
                row = connection.execute(
                    """
                    SELECT * FROM execution_leases
                    WHERE owner_id = ? AND lease_id = ?
                    """,
                    (owner_id, lease_id),
                ).fetchone()
                if row is None:
                    raise LeaseConflict("lease does not exist in owner scope")
                lease = self._lease_from_row(row)
                if lease.holder_id != holder_id:
                    raise LeaseConflict("lease holder does not match")
                if lease.status is not LeaseStatus.ACTIVE:
                    raise LeaseConflict(f"cannot renew a {lease.status.value} lease")
                if expected_revision is not None and expected_revision != lease.revision:
                    raise RevisionConflict(expected_revision, lease.revision, lease.as_dict())
                connection.execute(
                    """
                    UPDATE execution_leases
                    SET revision = revision + 1, renewed_at = ?, expires_at = ?
                    WHERE lease_id = ?
                    """,
                    (timestamp, timestamp + float(ttl_seconds), lease_id),
                )
                result = connection.execute(
                    "SELECT * FROM execution_leases WHERE lease_id = ?",
                    (lease_id,),
                ).fetchone()
        return self._lease_from_row(result)

    def heartbeat_leases(
        self,
        *,
        owner_id: str,
        run_id: str,
        agent_instance_id: str,
        agent_holder_id: str,
        agent_ttl_seconds: float,
        terminal_id: str,
        assignment_id: str,
        terminal_ttl_seconds: float,
        now: float | None = None,
    ) -> tuple[Lease, Lease]:
        """Atomically acquire/renew Agent and assignment Terminal leases.

        The Terminal lease is validated before either lease is mutated, then
        both revisions are advanced in one SQLite transaction.  A lost or
        reassigned Terminal therefore cannot leave a newly acquired or renewed
        Agent lease behind when a heartbeat is rejected.
        """
        owner_id = self._require(owner_id, "owner_id")
        run_id = self._require(run_id, "run_id")
        agent_instance_id = self._require(
            agent_instance_id, "agent_instance_id"
        )
        agent_holder_id = self._require(agent_holder_id, "agent_holder_id")
        terminal_id = self._require(terminal_id, "terminal_id")
        assignment_id = self._require(assignment_id, "assignment_id")
        if agent_ttl_seconds <= 0 or terminal_ttl_seconds <= 0:
            raise ValidationError("heartbeat lease TTLs must be positive")
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_leases(connection, timestamp)
                terminal_row = connection.execute(
                    """
                    SELECT * FROM execution_leases
                    WHERE owner_id = ? AND resource_kind = ? AND resource_id = ?
                      AND status = ?
                    """,
                    (
                        owner_id,
                        EntityKind.TERMINAL.value,
                        terminal_id,
                        LeaseStatus.ACTIVE.value,
                    ),
                ).fetchone()
                if terminal_row is None:
                    raise LeaseConflict(
                        "terminal assignment lease is not active"
                    )
                terminal_lease = self._lease_from_row(terminal_row)
                if terminal_lease.holder_id != assignment_id:
                    raise LeaseConflict(
                        "terminal assignment lease is held by another Assignment"
                    )
                terminal_run_id = str(
                    terminal_lease.metadata.get("run_id") or ""
                )
                if terminal_run_id and terminal_run_id != run_id:
                    raise LeaseConflict(
                        "terminal assignment lease belongs to another Run"
                    )

                agent_row = connection.execute(
                    """
                    SELECT * FROM execution_leases
                    WHERE owner_id = ? AND resource_kind = ? AND resource_id = ?
                      AND status = ?
                    """,
                    (
                        owner_id,
                        EntityKind.AGENT_INSTANCE.value,
                        agent_instance_id,
                        LeaseStatus.ACTIVE.value,
                    ),
                ).fetchone()
                if agent_row is None:
                    entity = connection.execute(
                        """
                        SELECT 1 FROM execution_entities
                        WHERE owner_id = ? AND entity_kind = ? AND entity_id = ?
                        """,
                        (
                            owner_id,
                            EntityKind.AGENT_INSTANCE.value,
                            agent_instance_id,
                        ),
                    ).fetchone()
                    if entity is None:
                        raise EntityNotFound(
                            "Agent lease resource does not exist in owner scope"
                        )
                    agent_lease_id = new_id()
                    connection.execute(
                        """
                        INSERT INTO execution_leases(
                            lease_id, owner_id, resource_kind, resource_id,
                            holder_id, status, revision, acquired_at, renewed_at,
                            expires_at, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                        """,
                        (
                            agent_lease_id,
                            owner_id,
                            EntityKind.AGENT_INSTANCE.value,
                            agent_instance_id,
                            agent_holder_id,
                            LeaseStatus.ACTIVE.value,
                            timestamp,
                            timestamp,
                            timestamp + float(agent_ttl_seconds),
                            json.dumps(
                                {"run_id": run_id},
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        ),
                    )
                else:
                    agent_lease = self._lease_from_row(agent_row)
                    if str(agent_lease.metadata.get("run_id") or "") != run_id:
                        raise LeaseConflict("Agent lease belongs to another Run")
                    agent_lease_id = agent_lease.id
                    connection.execute(
                        """
                        UPDATE execution_leases
                        SET revision = revision + 1,
                            renewed_at = ?, expires_at = ?
                        WHERE lease_id = ?
                        """,
                        (
                            timestamp,
                            timestamp + float(agent_ttl_seconds),
                            agent_lease_id,
                        ),
                    )
                self._workflow_checkpoint("heartbeat", "agent_lease")
                connection.execute(
                    """
                    UPDATE execution_leases
                    SET revision = revision + 1,
                        renewed_at = ?, expires_at = ?
                    WHERE lease_id = ?
                    """,
                    (
                        timestamp,
                        timestamp + float(terminal_ttl_seconds),
                        terminal_lease.id,
                    ),
                )
                self._workflow_checkpoint("heartbeat", "terminal_lease")
                renewed_agent_row = connection.execute(
                    "SELECT * FROM execution_leases WHERE lease_id = ?",
                    (agent_lease_id,),
                ).fetchone()
                renewed_terminal_row = connection.execute(
                    "SELECT * FROM execution_leases WHERE lease_id = ?",
                    (terminal_lease.id,),
                ).fetchone()
                if renewed_agent_row is None or renewed_terminal_row is None:
                    raise RuntimeError("heartbeat lease update lost a row")
        return (
            self._lease_from_row(renewed_agent_row),
            self._lease_from_row(renewed_terminal_row),
        )

    def release_lease(
        self,
        *,
        owner_id: str,
        lease_id: str,
        holder_id: str,
        expected_revision: int | None = None,
        now: float | None = None,
    ) -> Lease:
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_leases(connection, timestamp)
                row = connection.execute(
                    "SELECT * FROM execution_leases WHERE owner_id = ? AND lease_id = ?",
                    (owner_id, lease_id),
                ).fetchone()
                if row is None:
                    raise LeaseConflict("lease does not exist in owner scope")
                lease = self._lease_from_row(row)
                if lease.holder_id != holder_id:
                    raise LeaseConflict("lease holder does not match")
                if lease.status is LeaseStatus.RELEASED:
                    return lease
                if lease.status is not LeaseStatus.ACTIVE:
                    raise LeaseConflict(f"cannot release a {lease.status.value} lease")
                if expected_revision is not None and expected_revision != lease.revision:
                    raise RevisionConflict(expected_revision, lease.revision, lease.as_dict())
                connection.execute(
                    """
                    UPDATE execution_leases
                    SET status = ?, revision = revision + 1, released_at = ?
                    WHERE lease_id = ?
                    """,
                    (LeaseStatus.RELEASED.value, timestamp, lease_id),
                )
                result = connection.execute(
                    "SELECT * FROM execution_leases WHERE lease_id = ?",
                    (lease_id,),
                ).fetchone()
        return self._lease_from_row(result)

    def get_lease(
        self,
        *,
        owner_id: str,
        resource_kind: EntityKind | str,
        resource_id: str,
        now: float | None = None,
    ) -> Lease | None:
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            with self._connect() as connection:
                self._expire_leases(connection, timestamp)
                row = connection.execute(
                    """
                    SELECT * FROM execution_leases
                    WHERE owner_id = ? AND resource_kind = ? AND resource_id = ?
                      AND status = ?
                    """,
                    (
                        owner_id,
                        enum_value(resource_kind),
                        resource_id,
                        LeaseStatus.ACTIVE.value,
                    ),
                ).fetchone()
        return self._lease_from_row(row) if row is not None else None

    def enqueue_command(
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
        return self.command_queue.enqueue(
            owner_id=owner_id,
            target_kind=target_kind,
            target_id=target_id,
            command_type=command_type,
            payload=payload,
            command_id=command_id,
            expires_at=expires_at,
            expected_revision=expected_revision,
            created_at=created_at,
        )

    def commands(
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
        return self.command_queue.list(
            owner_id=owner_id,
            target_kind=target_kind,
            target_id=target_id,
            after_sequence=after_sequence,
            include_terminal=include_terminal,
            limit=limit,
            now=now,
        )

    def mark_command_delivered(
        self, *, owner_id: str, command_id: str, now: float | None = None
    ) -> Command:
        return self.command_queue.mark_delivered(
            owner_id=owner_id, command_id=command_id, now=now
        )

    def ack_command(
        self,
        *,
        owner_id: str,
        command_id: str,
        status: CommandStatus | str,
        ack_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> Command:
        return self.command_queue.ack(
            owner_id=owner_id,
            command_id=command_id,
            status=status,
            ack_id=ack_id,
            payload=payload,
            now=now,
        )

    def pending_outbox(self, *, after_sequence: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if after_sequence < 0 or limit <= 0:
            raise ValidationError("invalid outbox cursor or limit")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM execution_outbox
                WHERE global_sequence > ? AND published_at IS NULL
                ORDER BY global_sequence LIMIT ?
                """,
                (after_sequence, limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

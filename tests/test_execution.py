from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from app.execution import (
    AppendStatus,
    CommandConflict,
    CommandStatus,
    EntityKind,
    EntityNotFound,
    EventEnvelope,
    EventScope,
    Evidence,
    ExecutionStore,
    IdempotencyConflict,
    InvalidTransition,
    LeaseConflict,
    LeaseStatus,
    MissingExpectedRevision,
    ProducerMode,
    ProducerRef,
    RelationConstraintError,
    ResyncRequired,
    RevisionConflict,
    RunActivity,
    RunLifecycle,
    ValidationError,
)


def event(
    event_type: str,
    sequence: int,
    *,
    owner_id: str = "alice",
    run_id: str | None = None,
    agent_instance_id: str | None = None,
    expected_revision: int | None = None,
    payload: dict[str, object] | None = None,
    event_id: str | None = None,
    epoch: str = "boot-1",
) -> EventEnvelope:
    values = {
        "type": event_type,
        "scope": EventScope(
            owner_id=owner_id,
            run_id=run_id,
            agent_instance_id=agent_instance_id,
        ),
        "producer": ProducerRef(
            id="bridge:device-1",
            epoch=epoch,
            seq=sequence,
            adapter="kimi",
            version="1.0",
            mode=ProducerMode.ACTIVE,
        ),
        "expected_revision": expected_revision,
        "occurred_at": 1000.0 + sequence,
        "evidence": Evidence(confidence=1.0, valid_for_ms=15_000),
        "payload": payload or {},
    }
    if event_id is not None:
        values["event_id"] = event_id
    return EventEnvelope(**values)


class ExecutionStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "execution.db"
        self.store = ExecutionStore(self.database_path)

    def tearDown(self) -> None:
        self.directory.cleanup()


class EventEnvelopeTests(ExecutionStoreTestCase):
    def test_envelope_round_trip_uses_v1_schema(self) -> None:
        original = event(
            "observation.process.started",
            1,
            run_id="run-1",
            payload={"pid": 42},
        )
        restored = EventEnvelope.from_dict(original.as_dict())

        self.assertEqual(original, restored)
        self.assertEqual("agentserver.event/1", restored.schema)
        self.assertEqual("active", restored.producer.as_dict()["mode"])

    def test_invalid_scope_producer_and_evidence_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            EventScope(owner_id="")
        with self.assertRaises(ValidationError):
            ProducerRef(id="p", epoch="e", seq=-1)
        with self.assertRaises(ValidationError):
            Evidence(confidence=1.1)


class AppendAndProjectionTests(ExecutionStoreTestCase):
    def test_event_id_and_producer_keys_are_idempotent(self) -> None:
        original = event(
            "observation.process.started",
            1,
            run_id="run-1",
            event_id="event-one",
            payload={"pid": 42},
        )
        accepted = self.store.append(original)
        duplicate = self.store.append(original)

        self.assertEqual(AppendStatus.ACCEPTED, accepted.status)
        self.assertEqual(AppendStatus.DUPLICATE, duplicate.status)
        self.assertEqual(accepted.event.global_sequence, duplicate.event.global_sequence)

        with self.assertRaises(IdempotencyConflict):
            self.store.append(replace(original, payload={"pid": 43}))

        with self.assertRaises(IdempotencyConflict):
            self.store.append(replace(original, event_id="different-event-id"))

    def test_out_of_order_producer_sequences_are_durable_but_never_reordered(self) -> None:
        later = event("observation.process.started", 5, payload={"pid": 5})
        earlier = event("observation.process.started", 2, payload={"pid": 2})

        first = self.store.append(later).event
        second = self.store.append(earlier).event

        self.assertLess(first.global_sequence, second.global_sequence)
        self.assertEqual((5, 2), self.store.producer_cursor(
            producer_id="bridge:device-1", producer_epoch="boot-1"
        ))
        acknowledgement = self.store.producer_ack(
            producer_id="bridge:device-1", producer_epoch="boot-1"
        )
        self.assertEqual(5, acknowledgement.accepted_through_seq)
        self.assertEqual(((1, 1), (3, 4)), acknowledgement.missing_ranges)
        self.assertEqual(
            [5, 2],
            [item.producer.seq for item in self.store.snapshot(owner_id="alice").events],
        )

    def test_observation_and_extension_events_persist_without_projection(self) -> None:
        observed = self.store.append(
            event(
                "observation.pty.signature",
                1,
                run_id="run-1",
                payload={"signature": "kimi"},
            )
        )
        extension = self.store.append(
            event("vendor.example.sample", 2, payload={"safe": True})
        )

        self.assertIsNone(observed.projection)
        self.assertIsNone(extension.projection)
        snapshot = self.store.snapshot(owner_id="alice")
        self.assertEqual(2, len(snapshot.events))
        self.assertEqual((), snapshot.projections)
        run_timeline = self.store.snapshot(
            owner_id="alice", aggregate_kind="run", aggregate_id="run-1"
        )
        self.assertEqual((observed.event,), run_timeline.events)

    def test_projected_events_require_cas_and_rollback_on_conflict(self) -> None:
        missing = event("run.requested", 1, run_id="run-1")
        with self.assertRaises(MissingExpectedRevision):
            self.store.append(missing)
        self.assertEqual((), self.store.snapshot(owner_id="alice").events)

        requested = replace(missing, expected_revision=0)
        self.store.append(requested)
        stale_start = event(
            "run.started", 2, run_id="run-1", expected_revision=0
        )
        with self.assertRaises(RevisionConflict) as caught:
            self.store.append(stale_start)
        self.assertEqual(1, caught.exception.actual)

        retried = replace(stale_start, expected_revision=1)
        started = self.store.append(retried)
        self.assertEqual(2, started.projection.revision)
        self.assertEqual(RunLifecycle.RUNNING.value, started.projection.state["lifecycle"])
        self.assertEqual((2, 2), self.store.producer_cursor(
            producer_id="bridge:device-1", producer_epoch="boot-1"
        ))

    def test_run_state_machine_activity_waiting_and_terminal_state(self) -> None:
        self.store.append(event("run.requested", 1, run_id="run-1", expected_revision=0))
        self.store.append(event("run.started", 2, run_id="run-1", expected_revision=1))

        invalid_wait = event(
            "run.activity.changed",
            3,
            run_id="run-1",
            expected_revision=2,
            payload={"activity": "waiting"},
        )
        with self.assertRaises(ValidationError):
            self.store.append(invalid_wait)
        self.assertEqual(2, self.store.projection(
            owner_id="alice", aggregate_kind="run", aggregate_id="run-1"
        ).revision)

        waiting = replace(
            invalid_wait,
            payload={
                "activity": RunActivity.WAITING.value,
                "wait_reason": "child_run",
                "wait_target_run_id": "run-child",
            },
        )
        projected = self.store.append(waiting).projection
        self.assertEqual("waiting", projected.state["activity"])
        self.assertEqual("child_run", projected.state["wait_reason"])

        finished = self.store.append(
            event(
                "run.succeeded",
                4,
                run_id="run-1",
                expected_revision=3,
                payload={"summary": "done"},
            )
        ).projection
        self.assertEqual("succeeded", finished.state["lifecycle"])
        self.assertNotIn("activity", finished.state)

        with self.assertRaises(InvalidTransition):
            self.store.append(
                event(
                    "run.activity.changed",
                    5,
                    run_id="run-1",
                    expected_revision=4,
                    payload={"activity": "coding"},
                )
            )
        self.assertEqual(4, self.store.projection(
            owner_id="alice", aggregate_kind="run", aggregate_id="run-1"
        ).revision)

    def test_store_reopens_with_events_and_projection_intact(self) -> None:
        result = self.store.append(
            event("run.requested", 1, run_id="run-1", expected_revision=0)
        )

        reopened = ExecutionStore(self.database_path)

        self.assertEqual(
            result.event,
            reopened.snapshot(owner_id="alice").events[0],
        )
        self.assertEqual(1, reopened.projection(
            owner_id="alice", aggregate_kind="run", aggregate_id="run-1"
        ).revision)

    def test_outbox_is_written_in_event_transaction_then_marked_published(self) -> None:
        result = self.store.append(event("observation.cwd.changed", 1))
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT event_id, published_at FROM execution_outbox WHERE global_sequence = ?",
                (result.event.global_sequence,),
            ).fetchone()
        self.assertEqual(result.event.id, row[0])
        self.assertIsNotNone(row[1])
        self.assertEqual([], self.store.pending_outbox())

    def test_sqlite_event_log_rejects_update_and_delete(self) -> None:
        result = self.store.append(event("observation.cwd.changed", 1))
        with sqlite3.connect(self.database_path) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE execution_events SET event_type = 'changed' WHERE event_id = ?",
                    (result.event.id,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM execution_events WHERE event_id = ?",
                    (result.event.id,),
                )


class SnapshotAndSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "execution.db"
        self.store = ExecutionStore(self.database_path)

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_snapshot_and_live_subscription_have_no_gap_across_threads(self) -> None:
        initial = self.store.append(
            event("observation.process.started", 1, payload={"pid": 1})
        ).event
        subscription = self.store.subscribe(owner_id="alice")
        self.assertEqual((initial,), subscription.snapshot.events)
        self.assertEqual(initial.global_sequence, subscription.snapshot.as_of_sequence)

        expected = await asyncio.to_thread(
            self.store.append,
            event("observation.cwd.changed", 2, payload={"cwd": "/work"}),
        )
        received = await asyncio.wait_for(anext(subscription), 1)

        self.assertEqual(expected.event, received)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM execution_events WHERE event_id = ?",
                    (received.id,),
                ).fetchone()[0],
            )
        await subscription.aclose()
        self.assertNotIn(subscription, self.store._subscribers)

    async def test_cross_store_commit_in_snapshot_registration_window_is_delivered(self) -> None:
        writer = ExecutionStore(self.database_path)
        snapshot_taken = threading.Event()
        writer_done = threading.Event()
        written: list[object] = []
        failures: list[BaseException] = []
        original_snapshot = self.store._snapshot_locked

        def paused_snapshot(*args, **kwargs):
            result = original_snapshot(*args, **kwargs)
            snapshot_taken.set()
            if not writer_done.wait(timeout=5):
                raise RuntimeError("writer did not finish inside subscription window")
            return result

        def append_from_other_store() -> None:
            try:
                if not snapshot_taken.wait(timeout=5):
                    raise RuntimeError("snapshot was not taken")
                written.append(
                    writer.append(
                        event(
                            "run.requested",
                            1,
                            run_id="run-window",
                            expected_revision=0,
                        )
                    ).event
                )
            except BaseException as error:
                failures.append(error)
            finally:
                writer_done.set()

        thread = threading.Thread(target=append_from_other_store)
        thread.start()
        self.store._snapshot_locked = paused_snapshot  # type: ignore[method-assign]
        try:
            subscription = self.store.subscribe(owner_id="alice")
        finally:
            self.store._snapshot_locked = original_snapshot  # type: ignore[method-assign]
            thread.join(timeout=5)
        self.assertEqual([], failures)
        self.assertEqual(1, len(written))
        self.assertEqual((), subscription.snapshot.events)
        self.assertEqual((), subscription.snapshot.projections)
        received = await asyncio.wait_for(anext(subscription), 1)
        self.assertEqual(written[0], received)
        await subscription.aclose()

    async def test_cross_store_poll_and_local_publish_are_ordered_and_deduplicated(self) -> None:
        writer = ExecutionStore(
            self.database_path,
            subscription_poll_interval=0.01,
        )
        subscription = self.store.subscribe(owner_id="alice")
        external = writer.append(
            event("observation.process.started", 1, payload={"pid": 1})
        ).event
        local = self.store.append(
            event("observation.cwd.changed", 2, payload={"cwd": "/work"})
        ).event

        received = [
            await asyncio.wait_for(anext(subscription), 1),
            await asyncio.wait_for(anext(subscription), 1),
        ]
        self.assertEqual([external, local], received)
        await asyncio.sleep(0.15)
        self.assertTrue(subscription._queue.empty())
        await subscription.aclose()

    async def test_cross_store_poll_overflow_emits_one_resync_marker(self) -> None:
        database = Path(self.directory.name) / "cross-store-bounded.db"
        reader = ExecutionStore(
            database,
            max_subscription_queue=1,
            subscription_poll_interval=0.01,
            subscription_poll_limit=8,
        )
        writer = ExecutionStore(database)
        subscription = reader.subscribe(owner_id="alice")
        writer.append(event("observation.process.started", 1))
        latest = writer.append(event("observation.cwd.changed", 2)).event

        marker = await asyncio.wait_for(anext(subscription), 1)
        self.assertIsInstance(marker, ResyncRequired)
        assert isinstance(marker, ResyncRequired)
        self.assertEqual(0, marker.after_sequence)
        self.assertEqual(latest.global_sequence, marker.latest_sequence)
        await subscription.aclose()

    async def test_close_stops_poller_and_unblocks_waiting_consumer(self) -> None:
        subscription = self.store.subscribe(owner_id="alice")
        poll_task = subscription._poll_task
        waiter = asyncio.create_task(anext(subscription))
        await asyncio.sleep(0)

        await subscription.aclose()

        with self.assertRaises(StopAsyncIteration):
            await waiter
        self.assertIsNotNone(poll_task)
        assert poll_task is not None
        self.assertTrue(poll_task.done())
        self.assertNotIn(subscription, self.store._subscribers)

    async def test_subscriptions_on_one_loop_share_one_database_poller(self) -> None:
        first = self.store.subscribe(owner_id="alice")
        second = self.store.subscribe(owner_id="bob")

        self.assertIs(first._poll_task, second._poll_task)
        self.assertEqual(1, len(self.store._subscription_pollers))

        await first.aclose()
        assert second._poll_task is not None
        self.assertFalse(second._poll_task.done())
        await second.aclose()
        self.assertTrue(second._poll_task.done())
        self.assertEqual({}, self.store._subscription_pollers)

    async def test_owner_filter_and_cursor_replay(self) -> None:
        first = self.store.append(event("observation.process.started", 1)).event
        self.store.append(
            event("observation.process.started", 1, owner_id="bob", epoch="bob")
        )
        expected = self.store.append(event("observation.cwd.changed", 2)).event

        replay = self.store.snapshot(
            owner_id="alice", after_sequence=first.global_sequence
        )
        self.assertEqual((expected,), replay.events)

        subscription = self.store.subscribe(
            owner_id="alice", after_sequence=expected.global_sequence
        )
        self.store.append(
            event("observation.cwd.changed", 2, owner_id="bob", epoch="bob")
        )
        live = self.store.append(event("observation.files.changed", 3)).event
        self.assertEqual(live, await asyncio.wait_for(anext(subscription), 1))
        await subscription.aclose()

    async def test_queue_overflow_emits_explicit_resync_marker(self) -> None:
        store = ExecutionStore(
            Path(self.directory.name) / "bounded.db", max_subscription_queue=1
        )
        subscription = store.subscribe(owner_id="alice")
        store.append(event("observation.process.started", 1))
        latest = store.append(event("observation.cwd.changed", 2)).event

        marker = await asyncio.wait_for(anext(subscription), 1)
        self.assertIsInstance(marker, ResyncRequired)
        self.assertEqual(0, marker.after_sequence)
        self.assertEqual(latest.global_sequence, marker.latest_sequence)
        self.assertEqual("subscription.resync_required", marker.as_dict()["type"])
        await subscription.aclose()

    async def test_replay_limit_returns_page_projection_and_resync_signal(self) -> None:
        self.store.append(event("run.requested", 1, run_id="run-1", expected_revision=0))
        self.store.append(event("run.started", 2, run_id="run-1", expected_revision=1))

        subscription = self.store.subscribe(owner_id="alice", replay_limit=1)

        self.assertTrue(subscription.snapshot.resync_required)
        self.assertEqual(1, len(subscription.snapshot.events))
        self.assertEqual("run.requested", subscription.snapshot.events[0].type)
        self.assertEqual(1, len(subscription.snapshot.projections))
        self.assertEqual(2, subscription.snapshot.projections[0].revision)
        await subscription.aclose()


class EntityRelationTests(ExecutionStoreTestCase):
    def _register_runs(self, *run_ids: str, owner_id: str = "alice") -> None:
        for run_id in run_ids:
            self.store.register_entity(
                owner_id=owner_id,
                kind=EntityKind.RUN,
                entity_id=run_id,
            )

    def test_parent_run_relation_is_idempotent_and_owner_scoped(self) -> None:
        self._register_runs("parent", "child")
        linked = self.store.link_runs(
            owner_id="alice",
            parent_run_id="parent",
            child_run_id="child",
            relation_id="relation-1",
        )
        duplicate = self.store.link_runs(
            owner_id="alice",
            parent_run_id="parent",
            child_run_id="child",
            relation_id="relation-1",
        )

        self.assertEqual(linked, duplicate)
        self.assertEqual(
            [linked],
            self.store.relations(owner_id="alice", relation="parent_run"),
        )
        with self.assertRaises(EntityNotFound):
            self.store.link_runs(
                owner_id="bob",
                parent_run_id="parent",
                child_run_id="child",
            )

    def test_parent_run_prevents_cycles_second_parent_and_excess_depth(self) -> None:
        store = ExecutionStore(
            Path(self.directory.name) / "relations.db", max_parent_depth=2
        )
        for run_id in ("one", "two", "three", "four"):
            store.register_entity(owner_id="alice", kind="run", entity_id=run_id)
        store.link_runs(owner_id="alice", parent_run_id="one", child_run_id="two")
        store.link_runs(owner_id="alice", parent_run_id="two", child_run_id="three")

        with self.assertRaises(RelationConstraintError):
            store.link_runs(owner_id="alice", parent_run_id="three", child_run_id="one")
        with self.assertRaises(RelationConstraintError):
            store.link_runs(owner_id="alice", parent_run_id="one", child_run_id="three")
        with self.assertRaises(RelationConstraintError):
            store.link_runs(owner_id="alice", parent_run_id="three", child_run_id="four")

    def test_parent_run_relation_requires_run_endpoints(self) -> None:
        self.store.register_entity(owner_id="alice", kind="task", entity_id="task-1")
        self.store.register_entity(owner_id="alice", kind="run", entity_id="run-1")
        with self.assertRaises(RelationConstraintError):
            self.store.link_entities(
                owner_id="alice",
                relation="parent_run",
                source_kind="task",
                source_id="task-1",
                target_kind="run",
                target_id="run-1",
            )


class LeaseTests(ExecutionStoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.register_entity(
            owner_id="alice", kind=EntityKind.ASSIGNMENT, entity_id="assignment-1"
        )

    def test_lease_acquire_renew_release_and_cas(self) -> None:
        lease = self.store.acquire_lease(
            owner_id="alice",
            resource_kind="assignment",
            resource_id="assignment-1",
            holder_id="agent-1",
            ttl_seconds=10,
            lease_id="lease-1",
            now=100,
        )
        self.assertEqual(LeaseStatus.ACTIVE, lease.status)

        with self.assertRaises(LeaseConflict):
            self.store.acquire_lease(
                owner_id="alice",
                resource_kind="assignment",
                resource_id="assignment-1",
                holder_id="agent-2",
                ttl_seconds=10,
                now=101,
            )
        with self.assertRaises(RevisionConflict):
            self.store.renew_lease(
                owner_id="alice",
                lease_id="lease-1",
                holder_id="agent-1",
                ttl_seconds=10,
                expected_revision=0,
                now=102,
            )

        renewed = self.store.renew_lease(
            owner_id="alice",
            lease_id="lease-1",
            holder_id="agent-1",
            ttl_seconds=20,
            expected_revision=1,
            now=102,
        )
        self.assertEqual(2, renewed.revision)
        self.assertEqual(122, renewed.expires_at)

        released = self.store.release_lease(
            owner_id="alice",
            lease_id="lease-1",
            holder_id="agent-1",
            expected_revision=2,
            now=103,
        )
        self.assertEqual(LeaseStatus.RELEASED, released.status)
        self.assertIsNone(self.store.get_lease(
            owner_id="alice",
            resource_kind="assignment",
            resource_id="assignment-1",
            now=104,
        ))

    def test_expired_lease_can_be_acquired_by_another_holder(self) -> None:
        first = self.store.acquire_lease(
            owner_id="alice",
            resource_kind="assignment",
            resource_id="assignment-1",
            holder_id="agent-1",
            ttl_seconds=2,
            now=100,
        )
        self.assertIsNone(self.store.get_lease(
            owner_id="alice",
            resource_kind="assignment",
            resource_id="assignment-1",
            now=103,
        ))
        second = self.store.acquire_lease(
            owner_id="alice",
            resource_kind="assignment",
            resource_id="assignment-1",
            holder_id="agent-2",
            ttl_seconds=5,
            now=103,
        )
        self.assertNotEqual(first.id, second.id)
        self.assertEqual("agent-2", second.holder_id)

    def test_lease_resource_must_exist_in_owner_scope(self) -> None:
        with self.assertRaises(EntityNotFound):
            self.store.acquire_lease(
                owner_id="bob",
                resource_kind="assignment",
                resource_id="assignment-1",
                holder_id="agent-1",
                ttl_seconds=10,
            )

    def test_heartbeat_renews_agent_and_terminal_leases_atomically(self) -> None:
        self.store.register_entity(
            owner_id="alice", kind=EntityKind.TERMINAL, entity_id="terminal-1"
        )
        self.store.register_entity(
            owner_id="alice",
            kind=EntityKind.AGENT_INSTANCE,
            entity_id="agent-1",
        )
        terminal = self.store.acquire_lease(
            owner_id="alice",
            resource_kind=EntityKind.TERMINAL,
            resource_id="terminal-1",
            holder_id="assignment-1",
            ttl_seconds=20,
            now=100,
            metadata={"run_id": "run-1"},
        )

        agent, renewed_terminal = self.store.heartbeat_leases(
            owner_id="alice",
            run_id="run-1",
            agent_instance_id="agent-1",
            agent_holder_id="token-1",
            agent_ttl_seconds=10,
            terminal_id="terminal-1",
            assignment_id="assignment-1",
            terminal_ttl_seconds=30,
            now=105,
        )

        self.assertEqual(1, agent.revision)
        self.assertEqual(115, agent.expires_at)
        self.assertEqual(terminal.revision + 1, renewed_terminal.revision)
        self.assertEqual(135, renewed_terminal.expires_at)

    def test_heartbeat_terminal_failure_has_zero_agent_lease_side_effects(self) -> None:
        self.store.register_entity(
            owner_id="alice", kind=EntityKind.TERMINAL, entity_id="terminal-1"
        )
        for agent_id in ("agent-existing", "agent-new"):
            self.store.register_entity(
                owner_id="alice",
                kind=EntityKind.AGENT_INSTANCE,
                entity_id=agent_id,
            )
        terminal = self.store.acquire_lease(
            owner_id="alice",
            resource_kind=EntityKind.TERMINAL,
            resource_id="terminal-1",
            holder_id="assignment-1",
            ttl_seconds=20,
            now=100,
            metadata={"run_id": "run-1"},
        )
        existing, _terminal = self.store.heartbeat_leases(
            owner_id="alice",
            run_id="run-1",
            agent_instance_id="agent-existing",
            agent_holder_id="token-1",
            agent_ttl_seconds=10,
            terminal_id="terminal-1",
            assignment_id="assignment-1",
            terminal_ttl_seconds=30,
            now=101,
        )
        self.store.release_lease(
            owner_id="alice",
            lease_id=terminal.id,
            holder_id="assignment-1",
            expected_revision=2,
            now=102,
        )

        for agent_id in ("agent-existing", "agent-new"):
            with self.subTest(agent_id=agent_id):
                with self.assertRaises(LeaseConflict):
                    self.store.heartbeat_leases(
                        owner_id="alice",
                        run_id="run-1",
                        agent_instance_id=agent_id,
                        agent_holder_id="token-1",
                        agent_ttl_seconds=10,
                        terminal_id="terminal-1",
                        assignment_id="assignment-1",
                        terminal_ttl_seconds=30,
                        now=103,
                    )
        unchanged = self.store.get_lease(
            owner_id="alice",
            resource_kind=EntityKind.AGENT_INSTANCE,
            resource_id="agent-existing",
            now=103,
        )
        self.assertEqual(existing, unchanged)
        self.assertIsNone(
            self.store.get_lease(
                owner_id="alice",
                resource_kind=EntityKind.AGENT_INSTANCE,
                resource_id="agent-new",
                now=103,
            )
        )

    def test_heartbeat_fault_after_agent_mutation_rolls_back_both_leases(self) -> None:
        self.store.register_entity(
            owner_id="alice", kind=EntityKind.TERMINAL, entity_id="terminal-1"
        )
        self.store.register_entity(
            owner_id="alice",
            kind=EntityKind.AGENT_INSTANCE,
            entity_id="agent-1",
        )
        terminal = self.store.acquire_lease(
            owner_id="alice",
            resource_kind=EntityKind.TERMINAL,
            resource_id="terminal-1",
            holder_id="assignment-1",
            ttl_seconds=20,
            now=100,
            metadata={"run_id": "run-1"},
        )

        for injected_step in ("agent_lease", "terminal_lease"):
            with self.subTest(step=injected_step):
                def checkpoint(operation: str, step: str) -> None:
                    if operation == "heartbeat" and step == injected_step:
                        raise RuntimeError(f"injected heartbeat crash at {step}")

                self.store._workflow_checkpoint = checkpoint  # type: ignore[method-assign]
                with self.assertRaisesRegex(RuntimeError, "heartbeat crash"):
                    self.store.heartbeat_leases(
                        owner_id="alice",
                        run_id="run-1",
                        agent_instance_id="agent-1",
                        agent_holder_id="token-1",
                        agent_ttl_seconds=10,
                        terminal_id="terminal-1",
                        assignment_id="assignment-1",
                        terminal_ttl_seconds=30,
                        now=101,
                    )
                self.assertIsNone(
                    self.store.get_lease(
                        owner_id="alice",
                        resource_kind=EntityKind.AGENT_INSTANCE,
                        resource_id="agent-1",
                        now=101,
                    )
                )
                self.assertEqual(
                    terminal,
                    self.store.get_lease(
                        owner_id="alice",
                        resource_kind=EntityKind.TERMINAL,
                        resource_id="terminal-1",
                        now=101,
                    ),
                )

    def test_heartbeat_loses_cross_store_release_race_without_agent_side_effect(self) -> None:
        self.store.register_entity(
            owner_id="alice", kind=EntityKind.TERMINAL, entity_id="terminal-1"
        )
        self.store.register_entity(
            owner_id="alice",
            kind=EntityKind.AGENT_INSTANCE,
            entity_id="agent-1",
        )
        terminal = self.store.acquire_lease(
            owner_id="alice",
            resource_kind=EntityKind.TERMINAL,
            resource_id="terminal-1",
            holder_id="assignment-1",
            ttl_seconds=20,
            now=100,
            metadata={"run_id": "run-1"},
        )
        releaser = ExecutionStore(self.database_path)
        release_has_write_lock = threading.Event()
        allow_release = threading.Event()
        heartbeat_started = threading.Event()
        release_failures: list[BaseException] = []
        heartbeat_failures: list[BaseException] = []
        original_expire = releaser._expire_leases

        def paused_expire(connection, timestamp: float) -> None:
            original_expire(connection, timestamp)
            release_has_write_lock.set()
            if not allow_release.wait(timeout=5):
                raise RuntimeError("release race was not allowed to continue")

        releaser._expire_leases = paused_expire  # type: ignore[method-assign]

        def release() -> None:
            try:
                releaser.release_lease(
                    owner_id="alice",
                    lease_id=terminal.id,
                    holder_id="assignment-1",
                    expected_revision=terminal.revision,
                    now=101,
                )
            except BaseException as error:
                release_failures.append(error)

        def heartbeat() -> None:
            heartbeat_started.set()
            try:
                self.store.heartbeat_leases(
                    owner_id="alice",
                    run_id="run-1",
                    agent_instance_id="agent-1",
                    agent_holder_id="token-1",
                    agent_ttl_seconds=10,
                    terminal_id="terminal-1",
                    assignment_id="assignment-1",
                    terminal_ttl_seconds=30,
                    now=101,
                )
            except BaseException as error:
                heartbeat_failures.append(error)

        release_thread = threading.Thread(target=release)
        release_thread.start()
        self.assertTrue(release_has_write_lock.wait(timeout=5))
        heartbeat_thread = threading.Thread(target=heartbeat)
        heartbeat_thread.start()
        self.assertTrue(heartbeat_started.wait(timeout=5))
        allow_release.set()
        release_thread.join(timeout=5)
        heartbeat_thread.join(timeout=5)

        self.assertEqual([], release_failures)
        self.assertEqual(1, len(heartbeat_failures))
        self.assertIsInstance(heartbeat_failures[0], LeaseConflict)
        self.assertIsNone(
            self.store.get_lease(
                owner_id="alice",
                resource_kind=EntityKind.AGENT_INSTANCE,
                resource_id="agent-1",
                now=101,
            )
        )


class CommandQueueTests(ExecutionStoreTestCase):
    def test_command_delivery_ack_and_idempotency(self) -> None:
        command = self.store.enqueue_command(
            owner_id="alice",
            target_kind="run",
            target_id="run-1",
            command_type="cancel",
            payload={"reason": "user"},
            command_id="command-1",
            expected_revision=4,
            created_at=100,
        )
        duplicate = self.store.enqueue_command(
            owner_id="alice",
            target_kind="run",
            target_id="run-1",
            command_type="cancel",
            payload={"reason": "user"},
            command_id="command-1",
            expected_revision=4,
            created_at=999,
        )
        self.assertEqual(command, duplicate)

        with self.assertRaises(CommandConflict):
            self.store.enqueue_command(
                owner_id="alice",
                target_kind="run",
                target_id="run-1",
                command_type="cancel",
                payload={"reason": "different"},
                command_id="command-1",
                expected_revision=4,
            )

        self.assertEqual([command], self.store.commands(
            owner_id="alice", target_kind="run", target_id="run-1", now=101
        ))
        delivered = self.store.mark_command_delivered(
            owner_id="alice", command_id="command-1", now=101
        )
        self.assertEqual(CommandStatus.DELIVERED, delivered.status)
        accepted = self.store.ack_command(
            owner_id="alice",
            command_id="command-1",
            status="accepted",
            ack_id="ack-1",
            payload={"will_cancel": True},
            now=102,
        )
        self.assertEqual(CommandStatus.ACCEPTED, accepted.status)
        # A bridge may lose its local cursor on restart. Re-delivering an
        # accepted, non-terminal command must be idempotent and must not regress
        # the durable ACK state back to delivered.
        redelivered = self.store.mark_command_delivered(
            owner_id="alice", command_id="command-1", now=102.5
        )
        self.assertEqual(CommandStatus.ACCEPTED, redelivered.status)
        duplicate_ack = self.store.ack_command(
            owner_id="alice",
            command_id="command-1",
            status="accepted",
            ack_id="ack-1",
            payload={"will_cancel": True},
            now=103,
        )
        self.assertEqual(accepted, duplicate_ack)
        completed = self.store.ack_command(
            owner_id="alice",
            command_id="command-1",
            status="completed",
            ack_id="ack-2",
            payload={"return_code": 0},
            now=104,
        )
        self.assertEqual(CommandStatus.COMPLETED, completed.status)
        self.assertEqual([], self.store.commands(
            owner_id="alice", target_kind="run", target_id="run-1", now=105
        ))
        self.assertEqual([completed], self.store.commands(
            owner_id="alice",
            target_kind="run",
            target_id="run-1",
            include_terminal=True,
            now=105,
        ))

    def test_ack_scope_transition_and_ack_key_are_enforced(self) -> None:
        self.store.enqueue_command(
            owner_id="alice",
            target_kind="run",
            target_id="run-1",
            command_type="input",
            command_id="command-1",
        )
        with self.assertRaises(CommandConflict):
            self.store.ack_command(
                owner_id="bob", command_id="command-1", status="accepted"
            )
        self.store.ack_command(
            owner_id="alice",
            command_id="command-1",
            status="rejected",
            ack_id="ack-1",
        )
        with self.assertRaises(CommandConflict):
            self.store.ack_command(
                owner_id="alice",
                command_id="command-1",
                status="completed",
                ack_id="ack-2",
            )
        with self.assertRaises(CommandConflict):
            self.store.ack_command(
                owner_id="alice",
                command_id="command-1",
                status="rejected",
                ack_id="ack-1",
                payload={"different": True},
            )

    def test_expired_command_is_not_delivered(self) -> None:
        self.store.enqueue_command(
            owner_id="alice",
            target_kind="run",
            target_id="run-1",
            command_type="input",
            command_id="command-1",
            created_at=100,
            expires_at=101,
        )
        self.assertEqual([], self.store.commands(
            owner_id="alice", target_kind="run", target_id="run-1", now=102
        ))
        [expired] = self.store.commands(
            owner_id="alice",
            target_kind="run",
            target_id="run-1",
            include_terminal=True,
            now=102,
        )
        self.assertEqual(CommandStatus.EXPIRED, expired.status)


if __name__ == "__main__":
    unittest.main()

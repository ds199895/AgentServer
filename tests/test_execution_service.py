from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.execution import (
    EntityKind,
    EventEnvelope,
    EventScope,
    Evidence,
    ExecutionStore,
    ProducerMode,
    ProducerRef,
)
from app.execution.errors import (
    EntityNotFound,
    InvalidTransition,
    LeaseConflict,
    RelationConstraintError,
)
from app.execution.reporter import ReporterContext, ReporterSpool, RuntimeReporter
from app.execution.security import ReporterTokenRegistry, ReporterTokenSigner
from app.execution.service import ExecutionService


class ExecutionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "execution.db"
        self.store = ExecutionStore(self.database_path)
        self.tokens = ReporterTokenRegistry(
            self.database_path, ReporterTokenSigner(b"t" * 32)
        )
        self.service = ExecutionService(
            self.store, reporter_tokens=self.tokens, lease_ttl=30, lost_grace=90
        )
        self.service.register_terminal(
            owner_id="alice",
            terminal_id="terminal-1",
            launch_id="launch-1",
            device_id="device-1",
        )
        self.service.terminal_ready(owner_id="alice", terminal_id="terminal-1")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def assignment(self) -> tuple[dict[str, object], dict[str, object], str]:
        task = self.service.create_task(
            owner_id="alice", title="Implement status sync"
        )
        result = self.service.assign_task(
            owner_id="alice",
            task_id=str(task["id"]),
            terminal_id="terminal-1",
            device_id="device-1",
            agent_kind="kimi",
            expected_task_revision=int(task["revision"]),
        )
        run = result["runs"][0]
        token = self.service.issue_reporter_token(
            owner_id="alice", run_id=str(run["id"])
        )
        return result["task"], run, token

    @staticmethod
    def runtime_event(
        event_type: str,
        run: dict[str, object],
        *,
        seq: int,
        expected_revision: int,
        payload: dict[str, object] | None = None,
        valid_for_ms: int | None = None,
    ) -> EventEnvelope:
        attributes = run["attributes"]
        assert isinstance(attributes, dict)
        return EventEnvelope(
            type=event_type,
            scope=EventScope(
                owner_id="alice",
                device_id="device-1",
                terminal_id="terminal-1",
                launch_id="launch-1",
                agent_instance_id=str(attributes["agent_instance_id"]),
                task_id=str(attributes["task_id"]),
                assignment_id=str(attributes["assignment_id"]),
                run_id=str(run["id"]),
            ),
            producer=ProducerRef(
                id="agent:kimi",
                epoch="boot-1",
                seq=seq,
                adapter="kimi",
                mode=ProducerMode.ACTIVE,
            ),
            expected_revision=expected_revision,
            payload=payload or {},
            evidence=Evidence(confidence=1, valid_for_ms=valid_for_ms),
        )

    def claims(self, token: str):
        return self.tokens.verify(token, capability="report")

    def test_terminal_task_assignment_and_binding_are_replayable(self) -> None:
        task, run, _token = self.assignment()
        view = self.service.execution_view(owner_id="alice")

        self.assertEqual("assigned", task["state"]["lifecycle"])
        self.assertEqual("pending", run["state"]["lifecycle"])
        self.assertEqual(str(run["id"]), view["terminal_bindings"][0]["active_run_id"])
        reopened = ExecutionService(
            ExecutionStore(self.database_path), reporter_tokens=self.tokens
        ).execution_view(owner_id="alice")
        self.assertEqual(view["terminal_bindings"], reopened["terminal_bindings"])

    def test_register_phase_and_completion_advance_related_state(self) -> None:
        _task, run, token = self.assignment()
        claims = self.claims(token)
        registered = self.runtime_event(
            "agent.registered", run, seq=1, expected_revision=0, payload={"kind": "kimi"}
        )
        self.service.ingest_runtime_event(registered, claims=claims)

        current = self.service.get_run(owner_id="alice", run_id=str(run["id"]))
        self.assertEqual("running", current["state"]["lifecycle"])
        self.assertEqual("unknown", current["state"]["activity"])
        phase = self.runtime_event(
            "run.activity.changed",
            run,
            seq=2,
            expected_revision=int(current["revision"]),
            payload={"activity": "coding", "summary": "Editing execution API"},
            valid_for_ms=15_000,
        )
        self.service.ingest_runtime_event(phase, claims=claims)
        current = self.service.get_run(owner_id="alice", run_id=str(run["id"]))
        self.assertEqual("coding", current["state"]["activity"])
        self.assertEqual("reported", current["evidence"]["activity"]["source"])

        completed = self.runtime_event(
            "run.succeeded",
            run,
            seq=3,
            expected_revision=int(current["revision"]),
            payload={"summary": "Done"},
        )
        self.service.ingest_runtime_event(completed, claims=claims)
        task_view = self.service.get_task(
            owner_id="alice", task_id=str(run["attributes"]["task_id"])
        )
        self.assertEqual("succeeded", task_view["runs"][0]["state"]["lifecycle"])
        self.assertEqual("completed", task_view["task"]["state"]["lifecycle"])
        self.assertEqual(
            "completed", task_view["assignments"][0]["state"]["lifecycle"]
        )
        # Keep the short-lived credential valid long enough to ACK/replay the
        # exact terminal event after a lost response, but reject any new write
        # and never mint a fresh credential for a terminal Run.
        duplicate = self.service.ingest_runtime_event(completed, claims=claims)
        self.assertTrue(duplicate.duplicate)
        self.tokens.verify(token)
        with self.assertRaises(InvalidTransition):
            self.service.ingest_runtime_event(
                self.runtime_event(
                    "run.activity.changed",
                    run,
                    seq=4,
                    expected_revision=0,
                    payload={"activity": "coding"},
                ),
                claims=claims,
            )
        with self.assertRaises(InvalidTransition):
            self.service.issue_reporter_token(
                owner_id="alice", run_id=str(run["id"])
            )

    def test_expired_phase_degrades_to_unknown_without_changing_lifecycle(self) -> None:
        _task, run, token = self.assignment()
        claims = self.claims(token)
        self.service.ingest_runtime_event(
            self.runtime_event(
                "agent.registered", run, seq=1, expected_revision=0
            ),
            claims=claims,
        )
        current = self.service.get_run(owner_id="alice", run_id=str(run["id"]))
        result = self.service.ingest_runtime_event(
            self.runtime_event(
                "run.activity.changed",
                run,
                seq=2,
                expected_revision=int(current["revision"]),
                payload={"activity": "testing"},
                valid_for_ms=1,
            ),
            claims=claims,
        )
        view = self.service.execution_view(
            owner_id="alice", now=result.event.recorded_at + 1
        )
        projected = next(item for item in view["runs"] if item["id"] == run["id"])
        self.assertEqual("running", projected["state"]["lifecycle"])
        self.assertEqual("unknown", projected["state"]["activity"])
        self.assertEqual("stale", projected["evidence"]["activity"]["source"])

    def test_legacy_report_without_revision_is_atomically_normalized_and_replayable(self) -> None:
        _task, run, _token = self.assignment()
        claims = self.claims(
            self.service.issue_bridge_tokens(
                owner_id="alice", run_id=str(run["id"])
            )["report_token"]
        )
        event = self.runtime_event(
            "agent.registered", run, seq=1, expected_revision=0
        )
        event = EventEnvelope.from_dict(
            {
                **event.as_dict(),
                "expected_revision": None,
                "producer": {
                    **event.producer.as_dict(),
                    "mode": "adapter",
                },
            }
        )
        first = self.service.ingest_runtime_event(event, claims=claims)
        duplicate = self.service.ingest_runtime_event(event, claims=claims)
        self.assertFalse(first.duplicate)
        self.assertTrue(duplicate.duplicate)

    def test_active_runtime_state_event_requires_explicit_cas(self) -> None:
        _task, run, token = self.assignment()
        event = self.runtime_event(
            "agent.registered", run, seq=1, expected_revision=0
        )
        event = EventEnvelope.from_dict(
            {**event.as_dict(), "expected_revision": None}
        )
        with self.assertRaisesRegex(ValueError, "expected_revision"):
            self.service.ingest_runtime_event(event, claims=self.claims(token))
        adapter_claims = self.claims(
            self.service.issue_bridge_tokens(
                owner_id="alice", run_id=str(run["id"])
            )["report_token"]
        )
        with self.assertRaisesRegex(ValueError, "producer authority"):
            self.service.ingest_runtime_event(
                self.runtime_event(
                    "agent.registered", run, seq=2, expected_revision=0
                ),
                claims=adapter_claims,
            )

    def test_trusted_adapter_first_fact_activates_preexisting_assignment(self) -> None:
        _task, run, _token = self.assignment()
        bridge_token = self.service.issue_bridge_tokens(
            owner_id="alice", run_id=str(run["id"])
        )["report_token"]
        claims = self.claims(bridge_token)
        event = self.runtime_event(
            "run.activity.changed",
            run,
            seq=1,
            expected_revision=0,
            payload={"activity": "coding"},
        )
        event = EventEnvelope.from_dict(
            {
                **event.as_dict(),
                "expected_revision": None,
                "producer": {**event.producer.as_dict(), "mode": "adapter"},
            }
        )

        self.service.ingest_runtime_event(event, claims=claims)

        current = self.service.get_run(owner_id="alice", run_id=str(run["id"]))
        self.assertEqual("running", current["state"]["lifecycle"])
        self.assertEqual("coding", current["state"]["activity"])
        task_view = self.service.get_task(
            owner_id="alice", task_id=str(run["attributes"]["task_id"])
        )
        self.assertEqual("accepted", task_view["assignments"][0]["state"]["lifecycle"])
        self.assertEqual("working", task_view["task"]["state"]["lifecycle"])

    def test_runtime_cannot_link_an_unrelated_run_as_a_child(self) -> None:
        _task, parent, parent_token = self.assignment()
        parent_claims = self.claims(parent_token)
        self.service.ingest_runtime_event(
            self.runtime_event(
                "agent.registered", parent, seq=1, expected_revision=0
            ),
            claims=parent_claims,
        )
        self.service.register_terminal(
            owner_id="alice",
            terminal_id="terminal-2",
            launch_id="launch-2",
            device_id="device-1",
        )
        self.service.terminal_ready(owner_id="alice", terminal_id="terminal-2")
        child_task = self.service.create_task(owner_id="alice", title="Unrelated")
        unrelated = self.service.assign_task(
            owner_id="alice",
            task_id=str(child_task["id"]),
            terminal_id="terminal-2",
            device_id="device-1",
            agent_kind="kimi",
            expected_task_revision=int(child_task["revision"]),
        )["runs"][0]
        current_parent = self.service.get_run(
            owner_id="alice", run_id=str(parent["id"])
        )
        linked = self.runtime_event(
            "child_run.linked",
            parent,
            seq=2,
            expected_revision=int(current_parent["revision"]),
            payload={"child_run_id": str(unrelated["id"])},
        )

        with self.assertRaises(RelationConstraintError):
            self.service.ingest_runtime_event(linked, claims=parent_claims)
        self.assertEqual(
            [],
            self.store.relations(
                owner_id="alice",
                relation="parent_run",
                source_id=str(parent["id"]),
                target_id=str(unrelated["id"]),
            ),
        )

    def test_failed_post_commit_effect_blocks_ack_until_exact_replay_succeeds(self) -> None:
        _task, run, token = self.assignment()
        claims = self.claims(token)
        self.service.ingest_runtime_event(
            self.runtime_event("agent.registered", run, seq=1, expected_revision=0),
            claims=claims,
        )
        attempts = 0

        def publish(_event: EventEnvelope) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("injected artifact store outage")

        self.service.artifact_publisher = publish
        artifact = self.runtime_event(
            "artifact.published",
            run,
            seq=2,
            expected_revision=0,
            payload={"path": "reports/result.json"},
        )
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.service.ingest_runtime_event(artifact, claims=claims)
        producer_id = self.service.runtime_producer_id(
            claims=claims, reported_producer_id="agent:kimi"
        )
        acknowledgement = self.store.producer_ack(
            producer_id=producer_id, producer_epoch="boot-1"
        )
        assert acknowledgement is not None
        self.assertEqual(1, acknowledgement.accepted_through_seq)

        replay = self.service.ingest_runtime_event(artifact, claims=claims)
        self.assertTrue(replay.duplicate)
        acknowledgement = self.store.producer_ack(
            producer_id=producer_id, producer_epoch="boot-1"
        )
        assert acknowledgement is not None
        self.assertEqual(2, acknowledgement.accepted_through_seq)
        self.assertEqual(2, attempts)

    def test_cancel_is_a_command_not_a_terminal_outcome(self) -> None:
        _task, run, token = self.assignment()
        claims = self.claims(token)
        self.service.ingest_runtime_event(
            self.runtime_event("agent.registered", run, seq=1, expected_revision=0),
            claims=claims,
        )
        command = self.service.request_cancel(owner_id="alice", run_id=str(run["id"]))
        current = self.service.get_run(owner_id="alice", run_id=str(run["id"]))
        self.assertEqual("cancel", command.type)
        self.assertEqual("running", current["state"]["lifecycle"])
        self.assertTrue(current["state"]["cancel_requested"])

    def test_lease_expiry_marks_unreachable_then_lost_without_success_inference(self) -> None:
        _task, run, token = self.assignment()
        claims = self.claims(token)
        self.service.ingest_runtime_event(
            self.runtime_event("agent.registered", run, seq=1, expected_revision=0),
            claims=claims,
        )
        lease = self.store.get_lease(
            owner_id="alice",
            resource_kind=EntityKind.AGENT_INSTANCE,
            resource_id=str(run["attributes"]["agent_instance_id"]),
        )
        assert lease is not None
        self.assertEqual(
            1,
            self.service.reconcile_liveness(
                owner_id="alice", now=lease.expires_at + 1
            ),
        )
        stale = self.service.get_run(owner_id="alice", run_id=str(run["id"]))
        self.assertEqual("running", stale["state"]["lifecycle"])
        self.assertTrue(stale["state"]["stale"])
        agent = self.service.projection(
            owner_id="alice",
            kind=EntityKind.AGENT_INSTANCE,
            entity_id=str(run["attributes"]["agent_instance_id"]),
        )
        assert agent is not None
        self.service.reconcile_liveness(
            owner_id="alice", now=agent.updated_at + self.service.lost_grace + 1
        )
        lost = self.service.get_run(owner_id="alice", run_id=str(run["id"]))
        self.assertEqual("lost", lost["state"]["lifecycle"])
        self.assertNotEqual("succeeded", lost["state"]["lifecycle"])

        late = self.runtime_event(
            "run.succeeded",
            run,
            seq=2,
            expected_revision=int(lost["revision"]),
        )
        recorded = self.service.ingest_runtime_event(late, claims=claims)
        self.assertEqual("state.conflict.detected", recorded.event.type)
        self.assertEqual(
            "lost",
            self.service.get_run(owner_id="alice", run_id=str(run["id"]))["state"]["lifecycle"],
        )

    def test_liveness_reconciliation_does_not_replay_event_history(self) -> None:
        _task, run, token = self.assignment()
        claims = self.claims(token)
        self.service.ingest_runtime_event(
            self.runtime_event("agent.registered", run, seq=1, expected_revision=0),
            claims=claims,
        )
        lease = self.store.get_lease(
            owner_id="alice",
            resource_kind=EntityKind.AGENT_INSTANCE,
            resource_id=str(run["attributes"]["agent_instance_id"]),
        )
        assert lease is not None

        with patch.object(
            self.store,
            "snapshot",
            side_effect=AssertionError("liveness must use current projections"),
        ):
            changed = self.service.reconcile_liveness(
                owner_id="alice", now=lease.expires_at + 1
            )

        self.assertEqual(1, changed)

    def test_heartbeat_with_lost_terminal_lease_does_not_create_agent_lease(self) -> None:
        _task, run, token = self.assignment()
        claims = self.claims(token)
        attributes = run["attributes"]
        assert isinstance(attributes, dict)
        terminal_id = str(attributes["terminal_id"])
        assignment_id = str(attributes["assignment_id"])
        agent_id = str(attributes["agent_instance_id"])
        terminal_lease = self.store.get_lease(
            owner_id="alice",
            resource_kind=EntityKind.TERMINAL,
            resource_id=terminal_id,
        )
        assert terminal_lease is not None
        self.store.release_lease(
            owner_id="alice",
            lease_id=terminal_lease.id,
            holder_id=assignment_id,
            expected_revision=terminal_lease.revision,
        )

        with self.assertRaises(LeaseConflict):
            self.service.heartbeat(claims=claims, holder_id=claims.token_id)

        self.assertIsNone(
            self.store.get_lease(
                owner_id="alice",
                resource_kind=EntityKind.AGENT_INSTANCE,
                resource_id=agent_id,
            )
        )

    def test_retry_creates_a_new_task_and_run(self) -> None:
        _task, run, token = self.assignment()
        claims = self.claims(token)
        self.service.ingest_runtime_event(
            self.runtime_event("agent.registered", run, seq=1, expected_revision=0),
            claims=claims,
        )
        current = self.service.get_run(owner_id="alice", run_id=str(run["id"]))
        self.service.ingest_runtime_event(
            self.runtime_event(
                "run.failed",
                run,
                seq=2,
                expected_revision=int(current["revision"]),
                payload={"summary": "Tests failed"},
            ),
            claims=claims,
        )
        retry = self.service.retry_run(owner_id="alice", run_id=str(run["id"]))
        self.assertNotEqual(str(run["id"]), retry["runs"][0]["id"])
        self.assertNotEqual(run["attributes"]["task_id"], retry["task"]["id"])
        self.assertEqual(2, retry["runs"][0]["attributes"]["attempt"])

    def test_owner_scope_and_terminal_lease_prevent_cross_assignment(self) -> None:
        task, _run, _token = self.assignment()
        with self.assertRaises(EntityNotFound):
            self.service.get_task(owner_id="bob", task_id=str(task["id"]))
        second = self.service.create_task(owner_id="alice", title="Second")
        with self.assertRaises(Exception):
            self.service.assign_task(
                owner_id="alice",
                task_id=str(second["id"]),
                terminal_id="terminal-1",
                agent_kind="codex",
                expected_task_revision=int(second["revision"]),
            )

    def test_invalid_second_assignment_has_no_entities_or_lease_side_effects(self) -> None:
        task, _run, _token = self.assignment()
        self.service.register_terminal(
            owner_id="alice",
            terminal_id="terminal-2",
            launch_id="launch-2",
        )
        self.service.terminal_ready(owner_id="alice", terminal_id="terminal-2")
        before = self.service.execution_view(owner_id="alice")
        with self.assertRaises(InvalidTransition):
            self.service.assign_task(
                owner_id="alice",
                task_id=str(task["id"]),
                terminal_id="terminal-2",
                agent_kind="codex",
                expected_task_revision=int(task["revision"]),
            )
        after = self.service.execution_view(owner_id="alice")
        self.assertEqual(len(before["assignments"]), len(after["assignments"]))
        self.assertEqual(len(before["runs"]), len(after["runs"]))
        self.assertIsNone(
            self.store.get_lease(
                owner_id="alice",
                resource_kind=EntityKind.TERMINAL,
                resource_id="terminal-2",
            )
        )

    def test_concurrent_idempotent_create_and_terminal_registration_converge(self) -> None:
        second = ExecutionService(ExecutionStore(self.database_path))

        task_barrier = threading.Barrier(2)

        def create(service: ExecutionService) -> dict[str, object]:
            task_barrier.wait(timeout=5)
            return service.create_task(
                owner_id="alice",
                title="Exactly once task",
                task_id="task-idempotent",
                idempotency_key="same-task-request",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks = list(executor.map(create, (self.service, second)))
        self.assertEqual(tasks[0]["id"], tasks[1]["id"])
        self.assertEqual(tasks[0]["revision"], tasks[1]["revision"])
        task_events = [
            event
            for event in self.store.snapshot(owner_id="alice").events
            if event.type == "task.created"
            and event.scope.task_id == tasks[0]["id"]
        ]
        self.assertEqual(1, len(task_events))

        terminal_barrier = threading.Barrier(2)

        def register(service: ExecutionService) -> dict[str, object]:
            terminal_barrier.wait(timeout=5)
            return service.register_terminal(
                owner_id="alice",
                terminal_id="terminal-idempotent",
                launch_id="launch-idempotent",
                device_id="device-2",
                idempotency_key="same-terminal-request",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            terminals = list(executor.map(register, (self.service, second)))
        self.assertEqual(terminals[0], terminals[1])
        terminal_events = [
            event
            for event in self.store.snapshot(owner_id="alice").events
            if event.type == "terminal.launch.requested"
            and event.scope.terminal_id == terminals[0]["id"]
        ]
        self.assertEqual(1, len(terminal_events))

    def test_two_services_concurrently_assign_one_task_without_orphans(self) -> None:
        task = self.service.create_task(owner_id="alice", title="Race assignment")
        self.service.register_terminal(
            owner_id="alice",
            terminal_id="terminal-2",
            launch_id="launch-2",
            device_id="device-2",
        )
        self.service.terminal_ready(owner_id="alice", terminal_id="terminal-2")
        second = ExecutionService(ExecutionStore(self.database_path))
        barrier = threading.Barrier(2)
        first_preflight = self.service._assignment_preflight
        second_preflight = second._assignment_preflight

        def synchronized(preflight):
            def check(**kwargs):
                result = preflight(**kwargs)
                barrier.wait(timeout=5)
                return result

            return check

        self.service._assignment_preflight = synchronized(first_preflight)  # type: ignore[method-assign]
        second._assignment_preflight = synchronized(second_preflight)  # type: ignore[method-assign]

        def assign(
            service: ExecutionService, terminal_id: str
        ) -> dict[str, object]:
            return service.assign_task(
                owner_id="alice",
                task_id=str(task["id"]),
                terminal_id=terminal_id,
                agent_kind="kimi",
                expected_task_revision=int(task["revision"]),
            )

        outcomes: list[dict[str, object]] = []
        failures: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(assign, self.service, "terminal-1"),
                executor.submit(assign, second, "terminal-2"),
            ]
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10))
                except BaseException as error:
                    failures.append(error)
        self.assertEqual(1, len(outcomes))
        self.assertEqual(1, len(failures))
        view = self.service.execution_view(owner_id="alice")
        self.assertEqual(1, len(view["assignments"]))
        self.assertEqual(1, len(view["runs"]))
        agent_id = str(view["runs"][0]["attributes"]["agent_instance_id"])
        self.assertIsNotNone(
            self.store.get_entity(
                owner_id="alice",
                kind=EntityKind.AGENT_INSTANCE,
                entity_id=agent_id,
            )
        )
        winning_terminal = str(view["runs"][0]["attributes"]["terminal_id"])
        leases = [
            self.store.get_lease(
                owner_id="alice",
                resource_kind=EntityKind.TERMINAL,
                resource_id=terminal_id,
            )
            for terminal_id in ("terminal-1", "terminal-2")
        ]
        active_leases = [lease for lease in leases if lease is not None]
        self.assertEqual(1, len(active_leases))
        self.assertEqual(winning_terminal, active_leases[0].resource_id)
        self.assertEqual(
            view["assignments"][0]["id"], active_leases[0].holder_id
        )

    def test_assignment_transaction_rolls_back_every_fault_boundary(self) -> None:
        checkpoints = (
            "validated",
            "lease",
            "assignment_entity",
            "run_entity",
            "agent_entity",
            "relations",
            "assignment_event",
            "run_event",
            "task_event",
            "before_commit",
        )
        for index, injected_step in enumerate(checkpoints):
            with self.subTest(step=injected_step):
                database = Path(self.directory.name) / f"fault-{index}.db"
                store = ExecutionStore(database)
                service = ExecutionService(store)
                service.register_terminal(
                    owner_id="alice",
                    terminal_id="terminal-fault",
                    launch_id="launch-fault",
                )
                service.terminal_ready(
                    owner_id="alice", terminal_id="terminal-fault"
                )
                task = service.create_task(owner_id="alice", title="Atomic fault")

                def checkpoint(operation: str, step: str) -> None:
                    if operation == "assign_task" and step == injected_step:
                        raise RuntimeError(f"injected at {step}")

                store._workflow_checkpoint = checkpoint  # type: ignore[method-assign]
                with self.assertRaisesRegex(RuntimeError, injected_step):
                    service.assign_task(
                        owner_id="alice",
                        task_id=str(task["id"]),
                        terminal_id="terminal-fault",
                        agent_kind="codex",
                        expected_task_revision=int(task["revision"]),
                        assignment_id="assignment-fault",
                        run_id="run-fault",
                        agent_instance_id="agent-fault",
                    )
                view = service.execution_view(owner_id="alice")
                self.assertEqual([], view["assignments"])
                self.assertEqual([], view["runs"])
                self.assertEqual([], view["agents"])
                self.assertEqual([], view["relations"])
                for kind, identifier in (
                    (EntityKind.ASSIGNMENT, "assignment-fault"),
                    (EntityKind.RUN, "run-fault"),
                    (EntityKind.AGENT_INSTANCE, "agent-fault"),
                ):
                    self.assertIsNone(
                        store.get_entity(
                            owner_id="alice", kind=kind, entity_id=identifier
                        )
                    )
                self.assertIsNone(
                    store.get_lease(
                        owner_id="alice",
                        resource_kind=EntityKind.TERMINAL,
                        resource_id="terminal-fault",
                    )
                )
                current = service.get_task(
                    owner_id="alice", task_id=str(task["id"])
                )["task"]
                self.assertEqual("submitted", current["state"]["lifecycle"])
                self.assertEqual(task["revision"], current["revision"])

    def test_child_parent_relation_fault_rolls_back_child_workflow(self) -> None:
        database = Path(self.directory.name) / "parent-relation-fault.db"
        store = ExecutionStore(database)
        service = ExecutionService(store)
        for suffix in ("parent", "child"):
            service.register_terminal(
                owner_id="alice",
                terminal_id=f"terminal-{suffix}",
                launch_id=f"launch-{suffix}",
            )
            service.terminal_ready(
                owner_id="alice", terminal_id=f"terminal-{suffix}"
            )
        parent_task = service.create_task(owner_id="alice", title="Parent")
        parent = service.assign_task(
            owner_id="alice",
            task_id=str(parent_task["id"]),
            terminal_id="terminal-parent",
            agent_kind="kimi",
            expected_task_revision=int(parent_task["revision"]),
        )
        parent_run_id = str(parent["runs"][0]["id"])
        child_task = service.create_task(
            owner_id="alice", title="Child", parent_run_id=parent_run_id
        )
        before = service.execution_view(owner_id="alice")

        def checkpoint(operation: str, step: str) -> None:
            if operation == "assign_task" and step == "parent_relation":
                raise RuntimeError("injected at parent_relation")

        store._workflow_checkpoint = checkpoint  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "parent_relation"):
            service.assign_task(
                owner_id="alice",
                task_id=str(child_task["id"]),
                terminal_id="terminal-child",
                agent_kind="codex",
                expected_task_revision=int(child_task["revision"]),
                parent_run_id=parent_run_id,
                assignment_id="child-assignment-fault",
                run_id="child-run-fault",
                agent_instance_id="child-agent-fault",
            )
        after = service.execution_view(owner_id="alice")
        for key in ("assignments", "runs", "relations"):
            self.assertEqual(len(before[key]), len(after[key]))
        for kind, identifier in (
            (EntityKind.ASSIGNMENT, "child-assignment-fault"),
            (EntityKind.RUN, "child-run-fault"),
            (EntityKind.AGENT_INSTANCE, "child-agent-fault"),
        ):
            self.assertIsNone(
                store.get_entity(owner_id="alice", kind=kind, entity_id=identifier)
            )
        self.assertIsNone(
            store.get_lease(
                owner_id="alice",
                resource_kind=EntityKind.TERMINAL,
                resource_id="terminal-child",
            )
        )

    def test_expired_or_released_terminal_fence_rejects_old_reporter(self) -> None:
        _task, run, token = self.assignment()
        claims = self.claims(token)
        scope = self.service._scope_for_run("alice", str(run["id"]))
        lease = self.store.get_lease(
            owner_id="alice",
            resource_kind=EntityKind.TERMINAL,
            resource_id="terminal-1",
        )
        assert lease is not None and scope.assignment_id is not None
        self.store.release_lease(
            owner_id="alice",
            lease_id=lease.id,
            holder_id=scope.assignment_id,
            expected_revision=lease.revision,
        )
        before = self.store.snapshot(owner_id="alice").as_of_sequence
        with self.assertRaises(LeaseConflict):
            self.service.ingest_runtime_event(
                self.runtime_event(
                    "agent.registered", run, seq=1, expected_revision=0
                ),
                claims=claims,
            )
        self.assertEqual(before, self.store.snapshot(owner_id="alice").as_of_sequence)
        self.assertIsNone(
            self.store.get_lease(
                owner_id="alice",
                resource_kind=EntityKind.AGENT_INSTANCE,
                resource_id=str(run["attributes"]["agent_instance_id"]),
            )
        )

    def test_runtime_context_exposes_and_enforces_exact_terminal_lease(self) -> None:
        _task, run, token = self.assignment()
        claims = self.claims(token)
        scope = self.service._scope_for_run("alice", str(run["id"]))
        lease = self.store.get_lease(
            owner_id="alice",
            resource_kind=EntityKind.TERMINAL,
            resource_id="terminal-1",
        )
        assert lease is not None and scope.assignment_id is not None

        context = self.service.runtime_context(claims=claims)
        self.assertIsInstance(context["server_time"], float)
        self.assertEqual(
            {
                "id": lease.id,
                "revision": lease.revision,
                "expires_at": lease.expires_at,
            },
            context["terminal_lease"],
        )

        self.store.release_lease(
            owner_id="alice",
            lease_id=lease.id,
            holder_id=scope.assignment_id,
            expected_revision=lease.revision,
        )
        replacement_task = self.service.create_task(
            owner_id="alice", title="Replacement assignment"
        )
        replacement = self.service.assign_task(
            owner_id="alice",
            task_id=str(replacement_task["id"]),
            terminal_id="terminal-1",
            agent_kind="codex",
            expected_task_revision=int(replacement_task["revision"]),
        )
        self.assertNotEqual(run["id"], replacement["runs"][0]["id"])

        # The old Run projection is still pending, but its context must not
        # advertise the replacement Assignment's lease as executable.
        with self.assertRaises(LeaseConflict):
            self.service.runtime_context(claims=claims)

    def test_child_reporter_carries_parent_scope_end_to_end(self) -> None:
        _parent_task, parent_run, _parent_token = self.assignment()
        self.service.register_terminal(
            owner_id="alice",
            terminal_id="terminal-2",
            launch_id="launch-2",
            device_id="device-1",
        )
        self.service.terminal_ready(owner_id="alice", terminal_id="terminal-2")
        child_task = self.service.create_task(
            owner_id="alice",
            title="Child",
            parent_run_id=str(parent_run["id"]),
        )
        assigned = self.service.assign_task(
            owner_id="alice",
            task_id=str(child_task["id"]),
            terminal_id="terminal-2",
            device_id="device-1",
            agent_kind="codex",
            expected_task_revision=int(child_task["revision"]),
            parent_run_id=str(parent_run["id"]),
        )
        child = assigned["runs"][0]
        attributes = child["attributes"]
        assert isinstance(attributes, dict)
        reporter = RuntimeReporter(
            ReporterContext(
                owner_id="alice",
                device_id="device-1",
                terminal_id="terminal-2",
                launch_id="launch-2",
                run_id=str(child["id"]),
                assignment_id=str(attributes["assignment_id"]),
                task_id=str(attributes["task_id"]),
                agent_instance_id=str(attributes["agent_instance_id"]),
                parent_run_id=str(parent_run["id"]),
            ),
            ReporterSpool(Path(self.directory.name) / "child-spool.db"),
            producer_id="agent:child",
        )
        event = EventEnvelope.from_dict(
            reporter.emit("agent.registered", expected_revision=0)
        )
        token = self.service.issue_reporter_token(
            owner_id="alice", run_id=str(child["id"])
        )
        result = self.service.ingest_runtime_event(event, claims=self.claims(token))
        self.assertFalse(result.duplicate)
        self.assertEqual(str(parent_run["id"]), result.event.scope.parent_run_id)

    def test_parent_cancel_propagates_to_active_child_run_idempotently(self) -> None:
        _parent_task, parent_run, _parent_token = self.assignment()
        self.service.register_terminal(
            owner_id="alice", terminal_id="terminal-2", launch_id="launch-2"
        )
        self.service.terminal_ready(owner_id="alice", terminal_id="terminal-2")
        child_task = self.service.create_task(
            owner_id="alice", title="Child", parent_run_id=str(parent_run["id"])
        )
        child_assignment = self.service.assign_task(
            owner_id="alice",
            task_id=str(child_task["id"]),
            terminal_id="terminal-2",
            agent_kind="codex",
            expected_task_revision=int(child_task["revision"]),
            parent_run_id=str(parent_run["id"]),
        )
        child_run = child_assignment["runs"][0]

        first = self.service.request_cancel(
            owner_id="alice", run_id=str(parent_run["id"])
        )
        replay = self.service.request_cancel(
            owner_id="alice", run_id=str(parent_run["id"])
        )
        self.assertEqual(first.id, replay.id)
        self.assertTrue(
            self.service.get_run(
                owner_id="alice", run_id=str(child_run["id"])
            )["state"]["cancel_requested"]
        )
        child_scope = self.service._scope_for_run("alice", str(child_run["id"]))
        child_commands = self.store.commands(
            owner_id="alice",
            target_kind=EntityKind.AGENT_INSTANCE,
            target_id=str(child_scope.agent_instance_id),
        )
        self.assertEqual(["cancel"], [item.type for item in child_commands])

    def test_child_concurrency_and_aggregate_budget_are_enforced_before_mutation(self) -> None:
        parent_task = self.service.create_task(
            owner_id="alice",
            title="Budgeted parent",
            token_budget=100,
            max_child_runs=2,
        )
        parent_assignment = self.service.assign_task(
            owner_id="alice",
            task_id=str(parent_task["id"]),
            terminal_id="terminal-1",
            agent_kind="kimi",
            expected_task_revision=int(parent_task["revision"]),
        )
        parent_run = parent_assignment["runs"][0]
        for index in (2, 3):
            self.service.register_terminal(
                owner_id="alice",
                terminal_id=f"terminal-{index}",
                launch_id=f"launch-{index}",
            )
            self.service.terminal_ready(
                owner_id="alice", terminal_id=f"terminal-{index}"
            )
        first_child = self.service.create_task(
            owner_id="alice",
            title="First child",
            parent_run_id=str(parent_run["id"]),
            token_budget=60,
        )
        self.service.assign_task(
            owner_id="alice",
            task_id=str(first_child["id"]),
            terminal_id="terminal-2",
            agent_kind="codex",
            expected_task_revision=int(first_child["revision"]),
            parent_run_id=str(parent_run["id"]),
        )
        second_child = self.service.create_task(
            owner_id="alice",
            title="Second child",
            parent_run_id=str(parent_run["id"]),
            token_budget=50,
        )
        before = self.service.execution_view(owner_id="alice")
        with self.assertRaisesRegex(ValueError, "budget"):
            self.service.assign_task(
                owner_id="alice",
                task_id=str(second_child["id"]),
                terminal_id="terminal-3",
                agent_kind="claude",
                expected_task_revision=int(second_child["revision"]),
                parent_run_id=str(parent_run["id"]),
            )
        after = self.service.execution_view(owner_id="alice")
        self.assertEqual(len(before["runs"]), len(after["runs"]))
        self.assertIsNone(
            self.store.get_lease(
                owner_id="alice",
                resource_kind=EntityKind.TERMINAL,
                resource_id="terminal-3",
            )
        )


if __name__ == "__main__":
    unittest.main()

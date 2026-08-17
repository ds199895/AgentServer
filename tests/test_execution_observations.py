from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.execution import (
    EventEnvelope,
    EventScope,
    Evidence,
    ExecutionStore,
    ProducerMode,
    ProducerRef,
)
from app.execution.errors import ValidationError
from app.execution.observations import (
    FieldAuthority,
    ObservationDraft,
    ObservationMerger,
    ObservationPublisher,
    ProcessFingerprint,
)


def active_event(
    event_type: str,
    sequence: int,
    *,
    payload: dict[str, object],
    valid_for_ms: int | None = None,
    confidence: float = 1.0,
    run_id: str = "run-1",
) -> EventEnvelope:
    return EventEnvelope(
        type=event_type,
        event_id=f"active-{sequence}",
        scope=EventScope(
            owner_id="alice",
            device_id="device-1",
            terminal_id="terminal-1",
            run_id=run_id,
        ),
        producer=ProducerRef(
            id="agent:kimi",
            epoch="boot-1",
            seq=sequence,
            adapter="kimi",
            mode=ProducerMode.ACTIVE,
        ),
        evidence=Evidence(
            confidence=confidence,
            valid_for_ms=valid_for_ms,
        ),
        payload=payload,
    )


class ProcessFingerprintTests(unittest.TestCase):
    def test_pid_reuse_creates_distinct_process_incarnations(self) -> None:
        first = ProcessFingerprint(
            device_id="device-1",
            pid=4242,
            start_time=100,
            boot_id="boot-a",
            pgid=4200,
            tty="pts/1",
        )
        reused = ProcessFingerprint(
            device_id="device-1",
            pid=4242,
            start_time=200,
            boot_id="boot-a",
            pgid=4200,
            tty="pts/1",
        )
        enriched = ProcessFingerprint.from_dict(first.as_dict())

        self.assertNotEqual(first.instance_id, reused.instance_id)
        self.assertFalse(first.same_incarnation(reused))
        self.assertEqual(first.instance_id, enriched.instance_id)

    def test_fingerprint_requires_process_creation_time(self) -> None:
        with self.assertRaises(ValidationError):
            ProcessFingerprint(device_id="device-1", pid=10, start_time="")


class FieldMergerTests(unittest.TestCase):
    def test_active_phase_wins_over_newer_observed_phase_until_it_expires(self) -> None:
        merger = ObservationMerger()
        merger.ingest(
            active_event(
                "run.activity.changed",
                1,
                payload={"activity": "coding"},
                valid_for_ms=1_000,
            ),
            global_sequence=1,
            recorded_at=100,
        )
        merger.ingest(
            ObservationDraft.phase(
                owner_id="alice",
                device_id="device-1",
                terminal_id="terminal-1",
                run_id="run-1",
                activity="testing",
                confidence=1.0,
                valid_for_ms=10_000,
                observed_at=100,
            ),
            global_sequence=2,
            recorded_at=100,
        )

        fresh = merger.state_for(owner_id="alice", run_id="run-1", now=100.5)
        self.assertEqual("coding", fresh.state["activity"])
        self.assertEqual("reported", fresh.fields["activity"].source)
        self.assertEqual(FieldAuthority.ACTIVE, fresh.fields["activity"].authority)
        candidates = merger.candidates_for(
            owner_id="alice", run_id="run-1", field_name="activity"
        )
        self.assertEqual(
            [FieldAuthority.ACTIVE, FieldAuthority.HEURISTIC],
            [candidate.authority for candidate in candidates],
        )

        fallback = merger.state_for(owner_id="alice", run_id="run-1", now=102)
        self.assertEqual("testing", fallback.state["activity"])
        self.assertEqual("observed", fallback.fields["activity"].source)
        self.assertFalse(fallback.fields["activity"].stale)

    def test_expired_phase_degrades_to_unknown_and_retains_last_value(self) -> None:
        merger = ObservationMerger()
        merger.ingest(
            active_event(
                "run.activity.changed",
                1,
                payload={"activity": "thinking"},
                valid_for_ms=1_000,
            ),
            global_sequence=1,
            recorded_at=100,
        )

        result = merger.state_for(owner_id="alice", run_id="run-1", now=101)

        self.assertEqual("unknown", result.state["activity"])
        self.assertTrue(result.stale)
        self.assertTrue(result.fields["activity"].stale)
        self.assertEqual("stale", result.fields["activity"].source)
        self.assertEqual("thinking", result.fields["activity"].last_value)

    def test_same_authority_uses_confidence_then_global_sequence(self) -> None:
        merger = ObservationMerger()
        for sequence, activity, confidence in (
            (1, "coding", 0.9),
            (2, "testing", 0.5),
            (3, "reviewing", 0.9),
        ):
            merger.ingest(
                ObservationDraft.phase(
                    owner_id="alice",
                    device_id="device-1",
                    run_id="run-1",
                    activity=activity,
                    confidence=confidence,
                    valid_for_ms=10_000,
                    observed_at=100,
                ),
                global_sequence=sequence,
                recorded_at=100,
            )

        result = merger.state_for(owner_id="alice", run_id="run-1", now=101)

        self.assertEqual("reviewing", result.state["activity"])
        self.assertEqual(3, result.fields["activity"].global_sequence)

    def test_invalid_evidence_does_not_reserve_event_or_sequence(self) -> None:
        merger = ObservationMerger()
        invalid = active_event(
            "run.activity.changed",
            1,
            payload={"activity": "secret-thinking-substate"},
        )
        with self.assertRaises(ValidationError):
            merger.ingest(invalid, global_sequence=1, recorded_at=100)

        corrected = replace(invalid, payload={"activity": "thinking"})
        merger.ingest(corrected, global_sequence=1, recorded_at=100)

        self.assertEqual(1, len(merger.records()))
        self.assertEqual(
            "thinking",
            merger.state_for(owner_id="alice", run_id="run-1", now=101).state[
                "activity"
            ],
        )

    def test_non_waiting_phase_clears_older_wait_reason(self) -> None:
        merger = ObservationMerger()
        merger.ingest(
            active_event(
                "run.activity.changed",
                1,
                payload={"activity": "waiting", "wait_reason": "approval"},
                valid_for_ms=10_000,
            ),
            global_sequence=1,
            recorded_at=100,
        )
        merger.ingest(
            active_event(
                "run.activity.changed",
                2,
                payload={"activity": "coding"},
                valid_for_ms=10_000,
            ),
            global_sequence=2,
            recorded_at=101,
        )

        state = merger.state_for(owner_id="alice", run_id="run-1", now=102)

        self.assertEqual("coding", state.state["activity"])
        self.assertIsNone(state.state["wait_reason"])


class ProcessObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first = ProcessFingerprint(
            device_id="device-1",
            pid=77,
            start_time="1000",
            boot_id="boot-a",
            tty="pts/1",
        )
        self.reused = ProcessFingerprint(
            device_id="device-1",
            pid=77,
            start_time="2000",
            boot_id="boot-a",
            tty="pts/1",
        )

    def test_exit_of_old_pid_incarnation_does_not_stop_reused_pid(self) -> None:
        merger = ObservationMerger()
        merger.ingest(
            ObservationDraft.process_started(
                owner_id="alice",
                device_id="device-1",
                terminal_id="terminal-1",
                fingerprint=self.first,
                agent_kind="kimi",
                observed_at=100,
                valid_for_ms=10_000,
            ),
            global_sequence=1,
            recorded_at=100,
        )
        merger.ingest(
            ObservationDraft.process_started(
                owner_id="alice",
                device_id="device-1",
                terminal_id="terminal-1",
                fingerprint=self.reused,
                agent_kind="codex",
                observed_at=101,
                valid_for_ms=10_000,
            ),
            global_sequence=2,
            recorded_at=101,
        )
        merger.ingest(
            ObservationDraft.process_exited(
                owner_id="alice",
                device_id="device-1",
                terminal_id="terminal-1",
                fingerprint=self.first,
                return_code=0,
                observed_at=102,
            ),
            global_sequence=3,
            recorded_at=102,
        )

        state = merger.state_for(
            owner_id="alice", terminal_id="terminal-1", now=103
        )

        self.assertTrue(state.state["process_alive"])
        self.assertEqual(2, len(state.processes))
        by_id = {process.instance_id: process for process in state.processes}
        self.assertFalse(by_id[self.first.instance_id].alive)
        self.assertTrue(by_id[self.reused.instance_id].alive)

    def test_process_start_ttl_expires_to_unknown_stale(self) -> None:
        merger = ObservationMerger()
        merger.ingest(
            ObservationDraft.process_started(
                owner_id="alice",
                device_id="device-1",
                terminal_id="terminal-1",
                fingerprint=self.first,
                valid_for_ms=1_000,
                observed_at=100,
            ),
            global_sequence=1,
            recorded_at=100,
        )

        state = merger.state_for(
            owner_id="alice", terminal_id="terminal-1", now=101
        )

        self.assertIsNone(state.state["process_alive"])
        self.assertTrue(state.fields["process_alive"].stale)
        self.assertTrue(state.fields["process_alive"].last_value)

    def test_process_exit_never_derives_run_success(self) -> None:
        merger = ObservationMerger()
        merger.ingest(
            active_event(
                "run.started",
                1,
                payload={"activity": "coding"},
                valid_for_ms=10_000,
            ),
            global_sequence=1,
            recorded_at=100,
        )
        merger.ingest(
            ObservationDraft.process_started(
                owner_id="alice",
                device_id="device-1",
                terminal_id="terminal-1",
                run_id="run-1",
                fingerprint=self.first,
                observed_at=100,
            ),
            global_sequence=2,
            recorded_at=100,
        )
        merger.ingest(
            ObservationDraft.process_exited(
                owner_id="alice",
                device_id="device-1",
                terminal_id="terminal-1",
                run_id="run-1",
                fingerprint=self.first,
                return_code=0,
                observed_at=101,
            ),
            global_sequence=3,
            recorded_at=101,
        )

        state = merger.state_for(owner_id="alice", run_id="run-1", now=102)

        self.assertEqual("running", state.state["lifecycle"])
        self.assertFalse(state.state["process_alive"])
        self.assertNotIn("outcome", state.state)
        self.assertNotEqual("succeeded", state.state["lifecycle"])

    def test_observed_source_cannot_claim_a_successful_run_outcome(self) -> None:
        merger = ObservationMerger()
        claimed = active_event("run.succeeded", 1, payload={})
        claimed = replace(
            claimed,
            producer=replace(claimed.producer, mode=ProducerMode.OBSERVED),
        )

        record = merger.ingest(claimed, global_sequence=1, recorded_at=100)
        state = merger.state_for(owner_id="alice", run_id="run-1", now=101)

        self.assertEqual("run.succeeded", record.event_type)
        self.assertNotIn("lifecycle", state.state)


class AttributionTests(unittest.TestCase):
    def test_same_device_terminals_never_share_observation_state(self) -> None:
        merger = ObservationMerger()
        for sequence, terminal_id, pid, kind in (
            (1, "terminal-a", 10, "kimi"),
            (2, "terminal-b", 11, "codex"),
        ):
            merger.ingest(
                ObservationDraft.process_started(
                    owner_id="alice",
                    device_id="device-1",
                    terminal_id=terminal_id,
                    fingerprint=ProcessFingerprint(
                        device_id="device-1",
                        pid=pid,
                        start_time=sequence,
                    ),
                    agent_kind=kind,
                    observed_at=100,
                    valid_for_ms=10_000,
                ),
                global_sequence=sequence,
                recorded_at=100,
            )

        first = merger.state_for(owner_id="alice", terminal_id="terminal-a", now=101)
        second = merger.state_for(owner_id="alice", terminal_id="terminal-b", now=101)
        device = merger.unattributed(owner_id="alice", device_id="device-1", now=101)

        self.assertEqual("kimi", first.state["agent_kind"])
        self.assertEqual("codex", second.state["agent_kind"])
        self.assertEqual(1, len(first.processes))
        self.assertEqual(1, len(second.processes))
        self.assertEqual((), device.records)

    def test_exact_process_incarnation_cannot_migrate_between_terminals(self) -> None:
        merger = ObservationMerger()
        fingerprint = ProcessFingerprint(
            device_id="device-1", pid=20, start_time=100
        )
        merger.ingest(
            ObservationDraft.process_started(
                owner_id="alice",
                device_id="device-1",
                terminal_id="terminal-a",
                fingerprint=fingerprint,
            ),
            global_sequence=1,
            recorded_at=100,
        )

        with self.assertRaises(ValidationError):
            merger.ingest(
                ObservationDraft.process_exited(
                    owner_id="alice",
                    device_id="device-1",
                    terminal_id="terminal-b",
                    fingerprint=fingerprint,
                ),
                global_sequence=2,
                recorded_at=101,
            )

        self.assertEqual(1, len(merger.records()))

    def test_unattributed_device_observation_is_preserved_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ExecutionStore(Path(directory) / "execution.db")
            publisher = ObservationPublisher(
                store.append,
                producer_id="terminal-manager:device-1",
                producer_epoch="server-boot-1",
            )
            draft = ObservationDraft.process_started(
                owner_id="alice",
                device_id="device-1",
                fingerprint=ProcessFingerprint(
                    device_id="device-1", pid=99, start_time=123
                ),
                agent_kind="kimi",
                observed_at=100,
            )

            committed = publisher(draft)
            merger = ObservationMerger()
            record = merger.ingest(committed.event)

            self.assertTrue(record.unattributed)
            self.assertFalse(draft.attributed)
            self.assertEqual("device", committed.event.aggregate_kind)
            self.assertEqual("device-1", committed.event.aggregate_id)
            replay = store.snapshot(
                owner_id="alice",
                aggregate_kind="device",
                aggregate_id="device-1",
            )
            self.assertEqual((committed.event,), replay.events)
            unassigned = merger.unattributed(
                owner_id="alice", device_id="device-1", now=101
            )
            self.assertTrue(unassigned.unattributed)
            self.assertEqual((record,), unassigned.records)
            self.assertEqual((record,), merger.records(unattributed_only=True))

    def test_publisher_is_structured_and_monotonic(self) -> None:
        events: list[EventEnvelope] = []
        publisher = ObservationPublisher(
            events.append,
            producer_id="terminal-manager",
            producer_epoch="epoch-1",
            initial_sequence=4,
        )
        fingerprint = ProcessFingerprint(
            device_id="device-1", pid=10, start_time=100
        )

        publisher(
            ObservationDraft.process_started(
                owner_id="alice",
                device_id="device-1",
                terminal_id="terminal-1",
                fingerprint=fingerprint,
            )
        )
        publisher(
            ObservationDraft.process_exited(
                owner_id="alice",
                device_id="device-1",
                terminal_id="terminal-1",
                fingerprint=fingerprint,
            )
        )

        self.assertEqual([5, 6], [item.producer.seq for item in events])
        self.assertTrue(all(item.producer.mode is ProducerMode.OBSERVED for item in events))
        self.assertTrue(all(item.type.startswith("observation.") for item in events))
        with self.assertRaises(TypeError):
            publisher("run.succeeded")  # type: ignore[arg-type]

    def test_draft_cannot_encode_a_control_event(self) -> None:
        with self.assertRaises(ValidationError):
            ObservationDraft(
                type="run.succeeded",
                owner_id="alice",
                device_id="device-1",
            )


if __name__ == "__main__":
    unittest.main()

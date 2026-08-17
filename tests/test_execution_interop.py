from __future__ import annotations

import json
import unittest

from app.execution import EventEnvelope, EventScope, ProducerMode, ProducerRef, StoredEvent
from app.execution.interop import (
    to_a2a_task,
    to_cloudevent,
    to_mcp_progress,
    to_mcp_task,
    to_otel_log_record,
)


class ExecutionInteropTests(unittest.TestCase):
    PSEUDONYM_KEY = b"test-only-persistent-export-key!!"
    CLOUD_SINK = "https://events.example.test/agentserver"
    OTEL_SINK = "otel-collector-production"
    A2A_SINK = "a2a-partner-production"
    MCP_SINK = "mcp-client-production"

    def event(self, event_type: str = "run.activity.changed") -> StoredEvent:
        return StoredEvent(
            global_sequence=7,
            stream_version=2,
            recorded_at=1_700_000_000.125,
            aggregate_kind="run",
            aggregate_id="raw-run-id-8472",
            envelope=EventEnvelope(
                type=event_type,
                event_id="raw-event-id-8472",
                scope=EventScope(
                    owner_id="raw-owner-id-8472",
                    device_id="raw-device-id-8472",
                    terminal_id="raw-terminal-id-8472",
                    launch_id="raw-launch-id-8472",
                    agent_instance_id="raw-agent-id-8472",
                    task_id="raw-task-id-8472",
                    assignment_id="raw-assignment-id-8472",
                    run_id="raw-run-id-8472",
                    parent_run_id="raw-parent-run-id-8472",
                    span_id="raw-span-id-8472",
                ),
                producer=ProducerRef(
                    id="raw-producer-id-8472",
                    epoch="raw-producer-epoch-8472",
                    seq=1,
                    adapter="raw-secret-adapter-8472",
                    mode=ProducerMode.ADAPTER,
                ),
                expected_revision=1,
                causation_id="raw-causation-id-8472",
                correlation_id="raw-correlation-id-8472",
                traceparent=(
                    "00-4bf92f3577b34da6a3ce929d0e0e4736-"
                    "00f067aa0ba902b7-01"
                ),
                payload={
                    "activity": "coding",
                    "wait_target_run_id": "raw-wait-target-id-8472",
                    "summary": "secret task text",
                    "command": "secret shell command",
                },
            ),
        )

    def test_cloudevent_is_private_by_default_with_explicit_lossless_opt_in(self) -> None:
        value = to_cloudevent(
            self.event(),
            pseudonym_key=self.PSEUDONYM_KEY,
            sink_id=self.CLOUD_SINK,
        )
        self.assertEqual("1.0", value["specversion"])
        self.assertTrue(value["id"].startswith("event-"))
        self.assertEqual("dev.agentserver.run.activity.changed", value["type"])
        self.assertEqual("application/json", value["datacontenttype"])
        self.assertNotIn("sequence", value)
        self.assertNotIn("global_sequence", value["data"])
        self.assertEqual("coding", value["data"]["payload"]["activity"])
        self.assertTrue(
            value["data"]["payload"]["wait_target_run_id"].startswith("run-")
        )
        self.assertNotIn("secret task text", str(value))
        raw_values = (
            "raw-owner-id-8472",
            "raw-device-id-8472",
            "raw-terminal-id-8472",
            "raw-launch-id-8472",
            "raw-agent-id-8472",
            "raw-task-id-8472",
            "raw-assignment-id-8472",
            "raw-run-id-8472",
            "raw-parent-run-id-8472",
            "raw-span-id-8472",
            "raw-event-id-8472",
            "raw-producer-id-8472",
            "raw-producer-epoch-8472",
            "raw-secret-adapter-8472",
            "raw-causation-id-8472",
            "raw-correlation-id-8472",
            "raw-wait-target-id-8472",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        )
        encoded = json.dumps(value, sort_keys=True)
        for raw in raw_values:
            self.assertNotIn(raw, encoded)

        event = self.event()
        lossless = to_cloudevent(event, include_sensitive_data=True)
        self.assertEqual(event.id, lossless["id"])
        self.assertEqual("run/raw-run-id-8472", lossless["subject"])
        self.assertIn("raw-owner-id-8472", lossless["source"])
        self.assertEqual(event.as_dict(), lossless["data"])
        self.assertEqual(7, lossless["sequence"])

    def test_export_pseudonyms_are_keyed_stable_and_reject_weak_keys(self) -> None:
        key = b"a" * 32
        other_key = b"b" * 32
        first = to_cloudevent(
            self.event(), pseudonym_key=key, sink_id=self.CLOUD_SINK
        )
        second = to_cloudevent(
            self.event(), pseudonym_key=key, sink_id=self.CLOUD_SINK
        )
        different = to_cloudevent(
            self.event(), pseudonym_key=other_key, sink_id=self.CLOUD_SINK
        )
        other_sink = to_cloudevent(
            self.event(), pseudonym_key=key, sink_id="backup-event-receiver"
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["data"]["scope"], second["data"]["scope"])
        self.assertNotEqual(first["id"], different["id"])
        self.assertNotEqual(
            first["data"]["scope"]["owner_id"],
            different["data"]["scope"]["owner_id"],
        )
        self.assertNotEqual(first["id"], other_sink["id"])
        self.assertNotEqual(
            first["data"]["scope"]["owner_id"],
            other_sink["data"]["scope"]["owner_id"],
        )
        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            to_cloudevent(
                self.event(), pseudonym_key=b"weak", sink_id=self.CLOUD_SINK
            )
        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            to_cloudevent(self.event(), sink_id=self.CLOUD_SINK)
        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            to_otel_log_record(self.event(), sink_id=self.OTEL_SINK)
        with self.assertRaisesRegex(ValueError, "stable receiving boundary"):
            to_cloudevent(self.event(), pseudonym_key=key)

    def test_otel_record_excludes_text_commands_and_payload_blob(self) -> None:
        value = to_otel_log_record(
            self.event(),
            pseudonym_key=self.PSEUDONYM_KEY,
            sink_id=self.OTEL_SINK,
        )
        encoded = str(value)
        self.assertNotIn("secret task text", encoded)
        self.assertNotIn("secret shell command", encoded)
        self.assertIn("coding", encoded)
        self.assertEqual(32, len(value["traceId"]))
        self.assertEqual(16, len(value["spanId"]))
        for raw in (
            "raw-owner-id-8472",
            "raw-device-id-8472",
            "raw-terminal-id-8472",
            "raw-launch-id-8472",
            "raw-agent-id-8472",
            "raw-task-id-8472",
            "raw-assignment-id-8472",
            "raw-run-id-8472",
            "raw-parent-run-id-8472",
            "raw-span-id-8472",
            "raw-event-id-8472",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        ):
            self.assertNotIn(raw, encoded)
        lossless = to_otel_log_record(
            self.event(), include_sensitive_data=True
        )
        lossless_encoded = json.dumps(lossless, sort_keys=True)
        self.assertEqual("4bf92f3577b34da6a3ce929d0e0e4736", lossless["traceId"])
        self.assertEqual("00f067aa0ba902b7", lossless["spanId"])
        self.assertIn("raw-owner-id-8472", lossless_encoded)
        self.assertIn("raw-run-id-8472", lossless_encoded)
        self.assertIn(
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            lossless_encoded,
        )

    def test_default_exports_drop_untrusted_classification_and_name_fields(self) -> None:
        event = self.event("run.failed")
        event = StoredEvent(
            global_sequence=event.global_sequence,
            stream_version=event.stream_version,
            recorded_at=event.recorded_at,
            aggregate_kind=event.aggregate_kind,
            aggregate_id=event.aggregate_id,
            envelope=EventEnvelope(
                type=event.type,
                event_id=event.id,
                scope=event.scope,
                producer=event.producer,
                payload={
                    "activity": "TOP SECRET ACTIVITY",
                    "kind": "TOP_SECRET_KIND",
                    "name": "TOP_SECRET_NAME",
                    "code": "sk_live_deadbeef",
                    "reason": "secret_token_123",
                    "progress": 0.5,
                },
            ),
        )
        cloud = to_cloudevent(
            event,
            pseudonym_key=self.PSEUDONYM_KEY,
            sink_id=self.CLOUD_SINK,
        )
        self.assertEqual({"progress": 0.5}, cloud["data"]["payload"])
        otel = json.dumps(
            to_otel_log_record(
                event,
                pseudonym_key=self.PSEUDONYM_KEY,
                sink_id=self.OTEL_SINK,
            ),
            sort_keys=True,
        )
        for secret in (
            "TOP SECRET ACTIVITY",
            "TOP_SECRET_KIND",
            "TOP_SECRET_NAME",
            "sk_live_deadbeef",
            "secret_token_123",
        ):
            self.assertNotIn(secret, otel)

    def test_a2a_and_mcp_task_lifecycles_are_explicit(self) -> None:
        task = {
            "id": "task-1",
            "owner_id": "alice",
            "revision": 3,
            "created_at": 1_700_000_000.0,
            "updated_at": 1_700_000_010.0,
            "attributes": {
                "context_id": "context-1",
                "title": "private title",
                "description": "private description",
            },
            "state": {"lifecycle": "auth_required"},
        }
        a2a = to_a2a_task(
            task,
            pseudonym_key=self.PSEUDONYM_KEY,
            sink_id=self.A2A_SINK,
        )
        self.assertEqual("TASK_STATE_AUTH_REQUIRED", a2a["status"]["state"])
        self.assertNotEqual("task-1", a2a["id"])
        self.assertNotEqual("context-1", a2a["contextId"])
        self.assertNotEqual("alice", a2a["metadata"]["agentserver.owner_id"])
        self.assertNotIn("alice", json.dumps(a2a, sort_keys=True))
        self.assertNotIn("private title", str(a2a))
        lossless_a2a = to_a2a_task(task, include_sensitive_data=True)
        self.assertEqual("task-1", lossless_a2a["id"])
        self.assertEqual("context-1", lossless_a2a["contextId"])
        self.assertEqual("alice", lossless_a2a["metadata"]["agentserver.owner_id"])

        mcp = to_mcp_task(
            task,
            pseudonym_key=self.PSEUDONYM_KEY,
            sink_id=self.MCP_SINK,
        )
        self.assertEqual("input_required", mcp["status"])
        self.assertNotEqual("task-1", mcp["taskId"])
        self.assertNotIn("private description", str(mcp))
        self.assertEqual(
            "task-1",
            to_mcp_task(task, include_sensitive_data=True)["taskId"],
        )
        self.assertIn("tenant ACL", to_a2a_task.__doc__ or "")
        self.assertIn("tenant ACL", to_mcp_task.__doc__ or "")

        other_tenant = to_a2a_task(
            {**task, "owner_id": "bob"},
            pseudonym_key=self.PSEUDONYM_KEY,
            sink_id=self.A2A_SINK,
        )
        other_a2a_sink = to_a2a_task(
            task,
            pseudonym_key=self.PSEUDONYM_KEY,
            sink_id="a2a-partner-backup",
        )
        self.assertNotEqual(a2a["id"], other_tenant["id"])
        self.assertNotEqual(a2a["id"], other_a2a_sink["id"])
        self.assertNotEqual(a2a["id"], mcp["taskId"])

        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            to_a2a_task(task, sink_id=self.A2A_SINK)
        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            to_mcp_task(task, sink_id=self.MCP_SINK)

    def test_mcp_progress_requires_numeric_progress_and_preserves_token(self) -> None:
        self.assertIsNone(
            to_mcp_progress({"state": {"lifecycle": "running"}}, progress_token="p")
        )
        value = to_mcp_progress(
            {"state": {"progress": 0.5, "current": 5, "total": 10}},
            progress_token="opaque-1",
        )
        assert value is not None
        self.assertEqual("notifications/progress", value["method"])
        self.assertEqual("opaque-1", value["params"]["progressToken"])
        self.assertEqual(5, value["params"]["progress"])
        self.assertEqual(10, value["params"]["total"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.execution.terminal_observer import TerminalObservationTranslator


class TerminalObservationTranslatorTests(unittest.TestCase):
    def session(self, **values):
        defaults = {
            "id": "terminal-1",
            "launch_id": "launch-1",
            "owner": "alice",
            "device_id": None,
        }
        defaults.update(values)
        return SimpleNamespace(**defaults)

    @staticmethod
    def context(_owner: str, _terminal: str, _launch: str):
        return {
            "active_run_id": "run-1",
            "recent_run": {
                "id": "run-1",
                "attributes": {
                    "agent_instance_id": "assigned-agent",
                    "task_id": "task-1",
                    "assignment_id": "assignment-1",
                },
            },
        }

    def test_exact_process_uses_assigned_identity_and_same_fingerprint_on_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "sys/kernel/random").mkdir(parents=True)
            (proc / "sys/kernel/random/boot_id").write_text("boot-1\n")
            (proc / "41").mkdir()
            # comm may contain spaces; 19 suffix values place starttime at 22.
            suffix = ["S"] + [str(value) for value in range(1, 19)] + ["4242"]
            (proc / "41/stat").write_text(f"41 (agent worker) {' '.join(suffix)}")
            translator = TerminalObservationTranslator(
                default_owner="alice",
                context_resolver=self.context,
                proc_root=proc,
            )
            started = translator.translate(
                self.session(),
                {
                    "type": "observation.process.started",
                    "pid": 41,
                    "agent_kind": "codex",
                    "confidence": 0.99,
                },
            )
            exited = translator.translate(
                self.session(),
                {
                    "type": "observation.process.exited",
                    "pid": 41,
                    "agent_kind": "codex",
                    "confidence": 0.99,
                },
            )

        self.assertIsNotNone(started)
        self.assertIsNotNone(exited)
        assert started is not None and exited is not None
        self.assertEqual("assigned-agent", started.agent_instance_id)
        self.assertEqual("task-1", started.task_id)
        self.assertEqual(
            started.fingerprint.instance_id, exited.fingerprint.instance_id
        )
        self.assertEqual("4242", started.fingerprint.start_time)

    def test_inferred_remote_process_gets_separate_observed_identity(self):
        translator = TerminalObservationTranslator(
            default_owner="alice", context_resolver=self.context
        )
        draft = translator.translate(
            self.session(device_id="device-1"),
            {
                "type": "observation.process.started",
                "pid": 7,
                "agent_kind": "kimi",
                "confidence": 0.6,
            },
        )
        self.assertIsNotNone(draft)
        assert draft is not None and draft.fingerprint is not None
        self.assertEqual(draft.fingerprint.instance_id, draft.agent_instance_id)
        self.assertNotEqual("assigned-agent", draft.agent_instance_id)

    def test_pid_reuse_after_exit_creates_a_new_incarnation(self):
        translator = TerminalObservationTranslator(default_owner="alice")
        session = self.session(device_id="device-1")
        first = translator.translate(
            session,
            {
                "type": "observation.process.started",
                "pid": 9,
                "confidence": 0.6,
            },
        )
        translator.translate(
            session,
            {
                "type": "observation.process.exited",
                "pid": 9,
                "confidence": 0.6,
            },
        )
        reused = translator.translate(
            session,
            {
                "type": "observation.process.started",
                "pid": 9,
                "confidence": 0.6,
            },
        )
        assert first is not None and reused is not None
        self.assertNotEqual(
            first.fingerprint.instance_id, reused.fingerprint.instance_id
        )

    def test_banner_without_pid_is_observation_not_exit_proof(self):
        translator = TerminalObservationTranslator(
            default_owner="alice", context_resolver=self.context
        )
        banner = translator.translate(
            self.session(),
            {
                "type": "observation.pty.signature",
                "agent_kind": "claude",
                "confidence": 0.7,
            },
        )
        missing = translator.translate(
            self.session(),
            {
                "type": "observation.process.exited",
                "agent_kind": "claude",
                "confidence": 0.7,
            },
        )
        self.assertIsNotNone(banner)
        assert banner is not None
        self.assertEqual("assigned-agent", banner.agent_instance_id)
        self.assertIsNone(missing)

    def test_unknown_callback_payload_is_ignored(self):
        translator = TerminalObservationTranslator(default_owner="alice")
        self.assertIsNone(
            translator.translate(self.session(), {"type": "agent.registered"})
        )


if __name__ == "__main__":
    unittest.main()

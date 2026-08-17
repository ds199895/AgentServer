from __future__ import annotations

import concurrent.futures
import unittest
import tempfile
from pathlib import Path

from app.execution.security import (
    COMMAND_CAPABILITIES,
    REPORT_CAPABILITIES,
    ReporterTokenError,
    ReporterTokenRegistry,
    ReporterTokenSigner,
    load_or_create_reporter_secret,
)


class ReporterTokenSignerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.signer = ReporterTokenSigner(b"r" * 32, default_ttl=60)

    def issue(self, **overrides: object) -> str:
        values: dict[str, object] = {
            "owner_id": "alice",
            "run_id": "run-1",
            "terminal_id": "terminal-1",
            "launch_id": "launch-1",
            "device_id": "device-1",
            "agent_instance_id": "agent-1",
            "now": 1_000,
        }
        values.update(overrides)
        return self.signer.issue(**values)  # type: ignore[arg-type]

    def test_token_is_short_lived_and_bound_to_exact_scope(self) -> None:
        token = self.issue()
        claims = self.signer.verify(
            token,
            capability="report",
            owner_id="alice",
            run_id="run-1",
            terminal_id="terminal-1",
            launch_id="launch-1",
            now=1_030,
        )
        self.assertEqual("agent-1", claims.agent_instance_id)
        self.assertTrue(claims.permits("heartbeat"))

        for arguments in (
            {"owner_id": "bob"},
            {"run_id": "run-2"},
            {"terminal_id": "terminal-2"},
            {"launch_id": "launch-2"},
            {"device_id": "device-2"},
            {"agent_instance_id": "agent-2"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ReporterTokenError):
                self.signer.verify(token, now=1_030, **arguments)

    def test_expired_tampered_and_future_tokens_are_rejected(self) -> None:
        token = self.issue(ttl=10)
        with self.assertRaisesRegex(ReporterTokenError, "expired"):
            self.signer.verify(token, now=1_016, clock_skew=5)
        encoded, signature = token.split(".", 1)
        replacement = "A" if signature[-1] != "A" else "B"
        with self.assertRaisesRegex(ReporterTokenError, "signature"):
            self.signer.verify(f"{encoded}.{signature[:-1]}{replacement}", now=1_000)
        future = self.issue(now=2_000)
        with self.assertRaisesRegex(ReporterTokenError, "not valid yet"):
            self.signer.verify(future, now=1_000)

    def test_capabilities_are_allowlisted_and_enforced(self) -> None:
        token = self.issue(capabilities=("context", "report"))
        self.signer.verify(token, capability="report", now=1_000)
        with self.assertRaisesRegex(ReporterTokenError, "does not permit"):
            self.signer.verify(token, capability="commands", now=1_000)
        with self.assertRaisesRegex(ValueError, "unknown reporter capabilities"):
            self.issue(capabilities=("admin",))


class ReporterTokenRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.signer = ReporterTokenSigner(b"s" * 32, default_ttl=60)
        self.registry = ReporterTokenRegistry(
            Path(self.directory.name) / "execution.db", self.signer
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def issue(self, *, run_id: str = "run-1") -> str:
        return self.registry.issue(
            owner_id="alice",
            run_id=run_id,
            terminal_id="terminal-1",
            launch_id="launch-1",
            device_id="device-1",
            agent_instance_id="agent-1",
            now=1_000,
        )

    def test_unregistered_signed_token_is_rejected(self) -> None:
        token = self.signer.issue(
            owner_id="alice",
            run_id="run-1",
            terminal_id="terminal-1",
            launch_id="launch-1",
            now=1_000,
        )
        with self.assertRaisesRegex(ReporterTokenError, "not registered"):
            self.registry.verify(token, now=1_000)

    def test_registered_token_can_be_verified_and_revoked(self) -> None:
        token = self.issue()
        claims = self.registry.verify(
            token,
            capability="report",
            owner_id="alice",
            run_id="run-1",
            terminal_id="terminal-1",
            launch_id="launch-1",
            device_id="device-1",
            agent_instance_id="agent-1",
            now=1_010,
        )
        self.assertTrue(self.registry.revoke(claims.token_id, owner_id="alice", now=1_011))
        with self.assertRaisesRegex(ReporterTokenError, "revoked"):
            self.registry.verify(token, now=1_012)

    def test_revoke_run_is_owner_scoped(self) -> None:
        first = self.issue(run_id="run-1")
        second = self.issue(run_id="run-2")
        self.assertEqual(
            1,
            self.registry.revoke_run(owner_id="alice", run_id="run-1", now=1_001),
        )
        with self.assertRaises(ReporterTokenError):
            self.registry.verify(first, now=1_002)
        self.registry.verify(second, now=1_002)

    def test_refresh_preserves_scope_and_capabilities_with_overlap(self) -> None:
        original = self.registry.issue(
            owner_id="alice",
            run_id="run-1",
            terminal_id="terminal-1",
            launch_id="launch-1",
            device_id="device-1",
            agent_instance_id="agent-1",
            capabilities=REPORT_CAPABILITIES,
            now=1_000,
        )
        claims = self.registry.verify(original, now=1_050)
        replacement = self.registry.refresh(claims, ttl=120, now=1_050)
        duplicate = self.registry.refresh(claims, ttl=120, now=1_051)
        refreshed = self.registry.verify(replacement, now=1_051)

        self.assertEqual(replacement, duplicate)
        self.assertNotEqual(claims.token_id, refreshed.token_id)
        self.assertEqual(claims.owner_id, refreshed.owner_id)
        self.assertEqual(claims.run_id, refreshed.run_id)
        self.assertEqual(claims.capabilities, refreshed.capabilities)
        self.assertEqual(1_170, refreshed.expires_at)
        # Rotation overlaps so persisting the replacement cannot strand the
        # Bridge if it crashes before swapping its in-memory credential.
        self.registry.verify(original, now=1_051)
        with self.registry._connect() as connection:
            rows = connection.execute(
                "SELECT COUNT(*) FROM execution_reporter_tokens"
            ).fetchone()[0]
        self.assertEqual(2, rows)

    def test_refresh_before_the_rotation_window_is_rejected(self) -> None:
        original = self.issue()
        claims = self.registry.verify(original, now=1_001)
        with self.assertRaisesRegex(ValueError, "not yet"):
            self.registry.refresh(claims, now=1_001)

    def test_refresh_is_single_flight_across_registry_instances(self) -> None:
        original = self.issue()
        claims = self.registry.verify(original, now=1_050)
        registries = [
            ReporterTokenRegistry(self.registry.database_path, self.signer)
            for _index in range(8)
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            replacements = list(
                pool.map(
                    lambda registry: registry.refresh(claims, now=1_050),
                    registries,
                )
            )

        self.assertEqual(1, len(set(replacements)))
        with self.registry._connect() as connection:
            rows = connection.execute(
                "SELECT COUNT(*) FROM execution_reporter_tokens"
            ).fetchone()[0]
        self.assertEqual(2, rows)

    def test_refresh_rejects_expired_claims_and_prunes_old_audit_rows(self) -> None:
        expired = self.registry.issue(
            owner_id="alice",
            run_id="old-run",
            terminal_id="terminal-1",
            launch_id="launch-1",
            now=-200_000,
        )
        expired_claims = self.signer.verify(expired, now=-199_950)
        with self.assertRaisesRegex(ReporterTokenError, "expired"):
            self.registry.refresh(expired_claims, now=1_050)

        current = self.issue()
        current_claims = self.registry.verify(current, now=1_050)
        self.registry.refresh(current_claims, now=1_050)
        with self.registry._connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM execution_reporter_tokens ORDER BY run_id"
            ).fetchall()
        self.assertEqual(["run-1", "run-1"], [row["run_id"] for row in rows])

    def test_selective_run_revocation_keeps_final_report_replay_authority(self) -> None:
        report = self.registry.issue(
            owner_id="alice",
            run_id="run-1",
            terminal_id="terminal-1",
            launch_id="launch-1",
            capabilities=REPORT_CAPABILITIES,
            now=1_000,
        )
        command = self.registry.issue(
            owner_id="alice",
            run_id="run-1",
            terminal_id="terminal-1",
            launch_id="launch-1",
            capabilities=COMMAND_CAPABILITIES,
            now=1_000,
        )

        self.assertEqual(
            1,
            self.registry.revoke_run_capabilities(
                owner_id="alice",
                run_id="run-1",
                capabilities={"commands", "ack"},
                now=1_010,
            ),
        )
        self.registry.verify(report, capability="report", now=1_011)
        with self.assertRaisesRegex(ReporterTokenError, "revoked"):
            self.registry.verify(command, capability="commands", now=1_011)


class ReporterSecretTests(unittest.TestCase):
    def test_concurrent_initialization_publishes_one_complete_private_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                values = list(
                    pool.map(
                        lambda _index: load_or_create_reporter_secret(root),
                        range(64),
                    )
                )

            self.assertEqual(1, len(set(values)))
            self.assertGreaterEqual(len(values[0]), 32)
            path = root / "reporter_token_secret"
            self.assertEqual(values[0], path.read_bytes())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()

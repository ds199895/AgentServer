from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class RuntimeInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary_root = Path(self.temporary.name)
        self.root = temporary_root / "runtime install with spaces"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / ".venv" / "bin").mkdir(parents=True)
        self.installer = self.root / "scripts" / "install_agentserver_runtime.sh"
        shutil.copy2(
            REPOSITORY_ROOT / "scripts" / "install_agentserver_runtime.sh",
            self.installer,
        )

        self.arguments_file = temporary_root / "python-arguments"
        fake_python = self.root / ".venv" / "bin" / "python"
        fake_python.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = '-c' ]; then exit 0; fi\n"
            "printf '%s\\n' \"$@\" > \"$FAKE_ARGS_FILE\"\n"
            "exit \"${FAKE_PYTHON_EXIT:-0}\"\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o700)

        self.codex_bin_dir = temporary_root / "nvm codex bin"
        self.node_bin_dir = temporary_root / "nvm node bin"
        self.codex_bin_dir.mkdir()
        self.node_bin_dir.mkdir()
        for path in (
            self.codex_bin_dir / "codex",
            self.codex_bin_dir / "bwrap",
            self.node_bin_dir / "node",
        ):
            path.write_text(
                "#!/bin/sh\nexit \"${FAKE_BWRAP_EXIT:-0}\"\n"
                if path.name == "bwrap"
                else "#!/bin/sh\nexit 0\n",
                encoding="utf-8",
            )
            path.chmod(0o700)

        # Make the systemd availability branch deterministic while retaining
        # standard utilities used by the installer.
        fake_systemctl = self.codex_bin_dir / "systemctl"
        fake_systemctl.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_systemctl.chmod(0o700)

        self.home = temporary_root / "home"
        self.home.mkdir()
        self.config = temporary_root / "config"
        self.state = temporary_root / "state with spaces"
        self.token = temporary_root / "enrollment-token"
        self.token.write_text("secret-enrollment-value\n", encoding="utf-8")
        self.token.chmod(0o600)
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_STATE_HOME": str(temporary_root / "xdg-state"),
            "PATH": (
                f"{self.codex_bin_dir}:{self.node_bin_dir}:"
                "/usr/local/bin:/usr/bin:/bin"
            ),
            "FAKE_ARGS_FILE": str(self.arguments_file),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_installer(self, *extra_arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--state-dir",
                str(self.state),
                *extra_arguments,
            ],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def create_existing_credential(self) -> Path:
        self.state.mkdir(parents=True, exist_ok=True)
        self.state.chmod(0o700)
        credential = self.state / "device.credential"
        credential.write_text("existing-device-credential\n", encoding="utf-8")
        credential.chmod(0o600)
        return credential

    def test_unit_pins_codex_and_node_paths_without_persisting_secret(self) -> None:
        result = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--enrollment-token-file",
                str(self.token),
                "--state-dir",
                str(self.state),
            ],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

        unit = self.config / "systemd" / "user" / "agentserver-runtime.service"
        content = unit.read_text(encoding="utf-8")
        expected_path = (
            f"{self.codex_bin_dir}:{self.node_bin_dir}:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        self.assertIn(f'Environment="PATH={expected_path}"', content)
        self.assertIn(f'--codex-binary "{self.codex_bin_dir / "codex"}"', content)
        self.assertIn(
            f'--bubblewrap-binary "{self.codex_bin_dir / "bwrap"}"', content
        )
        self.assertNotIn("--node-binary", content)
        self.assertIn(f'--state-dir "{self.state}"', content)
        self.assertIn(f"WorkingDirectory={self.root}", content)
        self.assertNotIn("secret-enrollment-value", content)
        self.assertNotIn("secret-enrollment-value", result.stdout + result.stderr)
        self.assertEqual(0o600, stat.S_IMODE(unit.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(self.state.stat().st_mode))

        enrollment_arguments = self.arguments_file.read_text(encoding="utf-8")
        self.assertIn("enroll", enrollment_arguments.splitlines())
        self.assertNotIn(
            "--replace-existing-credential", enrollment_arguments.splitlines()
        )
        self.assertIn(str(self.token), enrollment_arguments.splitlines())
        self.assertNotIn("secret-enrollment-value", enrollment_arguments)

    def test_systemd_escape_characters_are_rejected_before_enrollment(self) -> None:
        result = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--enrollment-token-file",
                str(self.token),
                "--state-dir",
                str(self.state) + "\\unsafe",
            ],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("unsafe in a systemd unit", result.stderr)
        self.assertFalse(self.arguments_file.exists())

    def test_failed_reenrollment_restarts_the_previously_active_service(self) -> None:
        systemctl_log = Path(self.temporary.name) / "systemctl-log"
        fake_systemctl = self.codex_bin_dir / "systemctl"
        fake_systemctl.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_SYSTEMCTL_LOG\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_systemctl.chmod(0o700)
        environment = {
            **self.environment,
            "FAKE_PYTHON_EXIT": "7",
            "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
        }
        result = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--enrollment-token-file",
                str(self.token),
                "--state-dir",
                str(self.state),
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(7, result.returncode)
        calls = systemctl_log.read_text(encoding="utf-8").splitlines()
        self.assertIn("--user stop agentserver-runtime.service", calls)
        self.assertIn("--user start agentserver-runtime.service", calls)

    def test_failed_unit_start_restores_the_previous_unit(self) -> None:
        unit = self.config / "systemd" / "user" / "agentserver-runtime.service"
        unit.parent.mkdir(parents=True)
        old_content = "[Service]\nExecStart=/old/runtime\n"
        unit.write_text(old_content, encoding="utf-8")
        self.create_existing_credential()
        for name, value in (
            ("bootstrap.device_id", "device-01"),
            ("bootstrap.base_url", "https://agentserver.example"),
        ):
            binding = self.state / name
            binding.write_text(value + "\n", encoding="utf-8")
            binding.chmod(0o600)
        systemctl_log = Path(self.temporary.name) / "rollback-systemctl-log"
        fake_systemctl = self.codex_bin_dir / "systemctl"
        fake_systemctl.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$FAKE_SYSTEMCTL_LOG\"\n"
            "case \"$*\" in\n"
            "  *'show-environment'*) exit 0 ;;\n"
            "  *'is-active --quiet'*) exit 0 ;;\n"
            "  *'enable --now'*) exit 9 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_systemctl.chmod(0o700)
        result = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--state-dir",
                str(self.state),
            ],
            env={**self.environment, "FAKE_SYSTEMCTL_LOG": str(systemctl_log)},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(9, result.returncode)
        self.assertEqual(old_content, unit.read_text(encoding="utf-8"))
        calls = systemctl_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, calls.count("--user stop agentserver-runtime.service"))
        self.assertIn("--user start agentserver-runtime.service", calls)

    def test_unit_symlink_is_rejected_before_enrollment(self) -> None:
        unit = self.config / "systemd" / "user" / "agentserver-runtime.service"
        unit.parent.mkdir(parents=True)
        target = Path(self.temporary.name) / "unrelated-unit"
        target.write_text("[Service]\nExecStart=/unrelated\n", encoding="utf-8")
        unit.symlink_to(target)

        result = self.run_installer(
            "--enrollment-token-file",
            str(self.token),
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("must be a regular file", result.stderr)
        self.assertFalse(self.arguments_file.exists())
        self.assertEqual(
            "[Service]\nExecStart=/unrelated\n",
            target.read_text(encoding="utf-8"),
        )

    def test_relative_state_directory_is_pinned_as_an_absolute_unit_path(self) -> None:
        working_directory = Path(self.temporary.name) / "installer-cwd"
        working_directory.mkdir()
        result = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--enrollment-token-file",
                str(self.token),
                "--state-dir",
                "relative-state",
            ],
            cwd=working_directory,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        expected = str((working_directory / "relative-state").resolve())
        unit = self.config / "systemd" / "user" / "agentserver-runtime.service"
        self.assertIn(f'--state-dir "{expected}"', unit.read_text(encoding="utf-8"))
        self.assertIn(expected, self.arguments_file.read_text(encoding="utf-8"))

    def test_bubblewrap_preflight_fails_before_enrollment(self) -> None:
        result = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--enrollment-token-file",
                str(self.token),
                "--state-dir",
                str(self.state),
            ],
            env={**self.environment, "FAKE_BWRAP_EXIT": "9"},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("bubblewrap sandbox preflight failed", result.stderr)
        self.assertFalse(self.arguments_file.exists())

    def test_missing_explicit_bubblewrap_is_rejected_before_enrollment(self) -> None:
        result = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--enrollment-token-file",
                str(self.token),
                "--bubblewrap-binary",
                str(Path(self.temporary.name) / "missing-bwrap"),
            ],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("--bubblewrap-binary", result.stderr)
        self.assertFalse(self.arguments_file.exists())

    def test_existing_credential_is_reused_and_state_binding_blocks_retarget(self) -> None:
        self.state.mkdir(mode=0o700)
        credential = self.state / "device.credential"
        credential.write_text("asdc1.credential-id.secret\n", encoding="utf-8")
        credential.chmod(0o600)
        for name, value in (
            ("bootstrap.device_id", "device-01"),
            ("bootstrap.base_url", "https://agentserver.example"),
        ):
            path = self.state / name
            path.write_text(value + "\n", encoding="utf-8")
            path.chmod(0o600)

        reused = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--state-dir",
                str(self.state),
            ],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, reused.returncode, reused.stderr)
        self.assertIn("keeping it", reused.stdout)
        self.assertNotIn(
            "enroll", self.arguments_file.read_text(encoding="utf-8").splitlines()
        )

        retargeted = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-02",
                "--base-url",
                "https://agentserver.example",
                "--state-dir",
                str(self.state),
            ],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, retargeted.returncode)
        self.assertIn("already bound to device device-01", retargeted.stderr)

    def test_reenroll_is_the_only_path_that_replaces_a_credential(self) -> None:
        self.state.mkdir(mode=0o700)
        credential = self.state / "device.credential"
        credential.write_text("asdc1.credential-id.old-secret\n", encoding="utf-8")
        credential.chmod(0o600)

        result = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--state-dir",
                str(self.state),
                "--enrollment-token-file",
                str(self.token),
                "--reenroll",
            ],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        arguments = self.arguments_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("enroll", arguments)
        self.assertIn("--replace-existing-credential", arguments)

    def test_required_systemd_and_preflight_fail_before_consuming_token(self) -> None:
        required = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--state-dir",
                str(self.state),
                "--enrollment-token-file",
                str(self.token),
                "--require-systemd",
            ],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, required.returncode)
        self.assertIn("active user systemd", required.stderr)
        self.assertFalse(self.arguments_file.exists())

        preflight_state = Path(self.temporary.name) / "preflight-state"
        preflight = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--state-dir",
                str(preflight_state),
                "--preflight-only",
            ],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, preflight.returncode, preflight.stderr)
        self.assertIn("preflight passed", preflight.stdout)
        self.assertFalse(preflight_state.exists())

    def test_preflight_rejects_existing_device_binding_mismatch(self) -> None:
        self.state.mkdir(parents=True)
        binding = self.state / "bootstrap.device_id"
        binding.write_text("another-device\n", encoding="utf-8")
        binding.chmod(0o600)

        result = self.run_installer("--preflight-only")

        self.assertEqual(2, result.returncode)
        self.assertIn("already bound to device another-device", result.stderr)
        self.assertFalse(self.arguments_file.exists())

    def test_failed_first_enrollment_keeps_state_bound_to_requested_identity(self) -> None:
        result = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--device-id",
                "device-01",
                "--base-url",
                "https://agentserver.example",
                "--enrollment-token-file",
                str(self.token),
                "--state-dir",
                str(self.state),
            ],
            env={**self.environment, "FAKE_PYTHON_EXIT": "7"},
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(7, result.returncode)
        self.assertEqual(
            "device-01\n",
            (self.state / "bootstrap.device_id").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "https://agentserver.example\n",
            (self.state / "bootstrap.base_url").read_text(encoding="utf-8"),
        )

    def test_existing_credential_keeps_credential_without_consuming_token(self) -> None:
        credential = self.create_existing_credential()
        original_token = self.token.read_bytes()

        result = self.run_installer(
            "--enrollment-token-file",
            str(self.token),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            "existing-device-credential\n",
            credential.read_text(encoding="utf-8"),
        )
        self.assertEqual(original_token, self.token.read_bytes())
        self.assertIn("Existing Runtime credential found; keeping it", result.stdout)
        self.assertNotIn("enroll", self.arguments_file.read_text(encoding="utf-8"))
        self.assertNotIn("secret-enrollment-value", result.stdout + result.stderr)

    def test_reenroll_explicitly_replaces_existing_credential(self) -> None:
        self.create_existing_credential()

        result = self.run_installer(
            "--enrollment-token-file",
            str(self.token),
            "--reenroll",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        enrollment_arguments = self.arguments_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("enroll", enrollment_arguments)
        self.assertIn("--replace-existing-credential", enrollment_arguments)
        self.assertIn(str(self.token), enrollment_arguments)
        self.assertNotIn("secret-enrollment-value", result.stdout + result.stderr)

    def test_device_binding_mismatch_fails_before_enrollment(self) -> None:
        self.state.mkdir(parents=True)
        binding = self.state / "bootstrap.device_id"
        binding.write_text("another-device\n", encoding="utf-8")
        binding.chmod(0o600)

        result = self.run_installer(
            "--enrollment-token-file",
            str(self.token),
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("already bound to device another-device", result.stderr)
        self.assertFalse(self.arguments_file.exists())

    def test_base_url_binding_mismatch_fails_before_enrollment(self) -> None:
        self.state.mkdir(parents=True)
        binding = self.state / "bootstrap.base_url"
        binding.write_text("https://other.example\n", encoding="utf-8")
        binding.chmod(0o600)

        result = self.run_installer(
            "--enrollment-token-file",
            str(self.token),
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("bound to a different AgentServer URL", result.stderr)
        self.assertFalse(self.arguments_file.exists())

    def test_require_systemd_fails_before_enrollment(self) -> None:
        result = self.run_installer(
            "--enrollment-token-file",
            str(self.token),
            "--require-systemd",
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("active user systemd instance is required", result.stderr)
        self.assertFalse(self.arguments_file.exists())
        self.assertEqual(
            "secret-enrollment-value\n",
            self.token.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()

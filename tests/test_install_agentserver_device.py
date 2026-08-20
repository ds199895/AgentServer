from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class DeviceInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary_root = Path(self.temporary.name)
        self.root = temporary_root / "device installer with spaces"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "app" / "execution").mkdir(parents=True)
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / "requirements.txt").touch()
        (self.root / "requirements-runtime.lock").touch()
        (self.root / "scripts" / "agentserver_runtime.py").touch()
        (self.root / ".venv" / "bin" / "python").touch(mode=0o700)

        self.installer = self.root / "scripts" / "install_agentserver_device.sh"
        shutil.copy2(
            REPOSITORY_ROOT / "scripts" / "install_agentserver_device.sh",
            self.installer,
        )
        self.installer.chmod(0o700)

        self.log = temporary_root / "calls.log"
        self.fake_bin = temporary_root / "fake-bin"
        self.fake_bin.mkdir()
        self.runtime_installer = self.root / "scripts" / "install_agentserver_runtime.sh"
        self.frp_installer = self.root / "scripts" / "install_frpc_ssh.sh"

        self.enrollment_secret = "one-time-enrollment-secret"
        self.frp_secret = "private-frp-secret"
        self.enrollment_token = temporary_root / "enrollment-token"
        self.frp_token = temporary_root / "frp-token"
        self.enrollment_token.write_text(
            self.enrollment_secret + "\n", encoding="utf-8"
        )
        self.frp_token.write_text(self.frp_secret + "\n", encoding="utf-8")
        self.enrollment_token.chmod(0o600)
        self.frp_token.chmod(0o600)

        self.state = temporary_root / "runtime-state"
        self._write_fake_commands()
        self._write_runtime_installer()
        self._write_frp_installer()

        current_user = subprocess.run(
            ["id", "-un"], check=True, capture_output=True, text=True
        ).stdout.strip()
        self.common_arguments = [
            "--device-id",
            "device-01",
            "--base-url",
            "https://agentserver.example",
            "--runtime-user",
            current_user,
            "--ssh-user",
            current_user,
            "--state-dir",
            str(self.state),
            "--codex-binary",
            str(self.fake_bin / "codex"),
            "--node-binary",
            str(self.fake_bin / "node"),
            "--bubblewrap-binary",
            str(self.fake_bin / "bwrap"),
            "--enrollment-token-file",
            str(self.enrollment_token),
        ]
        self.environment = {
            **os.environ,
            "PATH": f"{self.fake_bin}:/usr/local/bin:/usr/bin:/bin",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o700)

    def _write_fake_commands(self) -> None:
        quoted_log = shlex.quote(str(self.log))
        for binary in ("codex", "node", "bwrap"):
            self._write_executable(self.fake_bin / binary, "#!/bin/sh\nexit 0\n")
        self._write_executable(
            self.fake_bin / "sudo",
            "#!/bin/sh\n"
            f"printf 'sudo:%s\\n' \"$*\" >> {quoted_log}\n"
            "if [ \"${1:-}\" = -- ]; then shift; fi\n"
            "exec \"$@\"\n",
        )
        self._write_executable(
            self.fake_bin / "loginctl",
            "#!/bin/sh\n"
            f"printf 'loginctl:%s\\n' \"$*\" >> {quoted_log}\n"
            "exit 0\n",
        )
        self._write_executable(
            self.fake_bin / "systemctl",
            "#!/bin/sh\n"
            f"printf 'systemctl:%s\\n' \"$*\" >> {quoted_log}\n"
            "exit 0\n",
        )

    def _write_runtime_installer(self) -> None:
        quoted_log = shlex.quote(str(self.log))
        quoted_secret = shlex.quote(self.enrollment_secret)
        self._write_executable(
            self.runtime_installer,
            "#!/bin/sh\n"
            "mode=install\n"
            "token_file=\n"
            "python_binary=\n"
            "previous=\n"
            "require_systemd=0\n"
            "for argument do\n"
            f"  case \"$argument\" in *{quoted_secret}*) exit 42 ;; esac\n"
            "  if [ \"$previous\" = token ]; then token_file=$argument; previous=; fi\n"
            "  if [ \"$previous\" = python ]; then python_binary=$argument; previous=; fi\n"
            "  if [ \"$argument\" = --enrollment-token-file ]; then previous=token; fi\n"
            "  if [ \"$argument\" = --python-binary ]; then previous=python; fi\n"
            "  if [ \"$argument\" = --preflight-only ]; then mode=preflight; fi\n"
            "  if [ \"$argument\" = --require-systemd ]; then require_systemd=1; fi\n"
            "done\n"
            f"printf 'runtime:%s\\n' \"$mode\" >> {quoted_log}\n"
            f"if [ -n \"$python_binary\" ]; then printf 'runtime:python:%s\\n' \"$python_binary\" >> {quoted_log}; fi\n"
            "if [ \"$mode\" = install ] && [ \"$require_systemd\" -ne 1 ]; then exit 43; fi\n"
            "if [ \"$mode\" = install ] && [ -n \"$token_file\" ]; then\n"
            f"  [ \"$(cat \"$token_file\")\" = {quoted_secret} ] || exit 41\n"
            f"  printf 'runtime:token-consumed\\n' >> {quoted_log}\n"
            "fi\n"
            "exit 0\n",
        )

    def _write_frp_installer(self) -> None:
        quoted_log = shlex.quote(str(self.log))
        quoted_secret = shlex.quote(self.frp_secret)
        self._write_executable(
            self.frp_installer,
            "#!/bin/sh\n"
            "token_file=\n"
            "merge_config=\n"
            "previous=\n"
            "for argument do\n"
            f"  case \"$argument\" in *{quoted_secret}*) exit 42 ;; esac\n"
            "  if [ \"$previous\" = token ]; then token_file=$argument; previous=; fi\n"
            "  if [ \"$previous\" = merge ]; then merge_config=$argument; previous=; fi\n"
            "  if [ \"$argument\" = --token-file ]; then previous=token; fi\n"
            "  if [ \"$argument\" = --merge-existing ]; then previous=merge; fi\n"
            "done\n"
            f"if [ -z \"$merge_config\" ]; then [ \"$(cat \"$token_file\")\" = {quoted_secret} ] || exit 41; fi\n"
            f"printf 'frp:start\\n' >> {quoted_log}\n"
            f"if [ -n \"$merge_config\" ]; then printf 'frp:merge:%s\\n' \"$merge_config\" >> {quoted_log}; fi\n"
            "exit \"${FAKE_FRP_EXIT:-0}\"\n",
        )

    def run_installer(
        self, *extra_arguments: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.installer), *self.common_arguments, *extra_arguments],
            env=environment or self.environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def assert_secrets_are_absent(self, result: subprocess.CompletedProcess[str]) -> None:
        visible = result.stdout + result.stderr
        if self.log.exists():
            visible += self.log.read_text(encoding="utf-8")
        self.assertNotIn(self.enrollment_secret, visible)
        self.assertNotIn(self.frp_secret, visible)

    def test_runtime_only_runs_preflight_linger_and_runtime_without_frp(self) -> None:
        result = self.run_installer("--runtime-only")

        self.assertEqual(0, result.returncode, result.stderr)
        calls = self.calls()
        self.assertEqual("runtime:preflight", calls[0])
        self.assertIn("loginctl:enable-linger", "\n".join(calls))
        self.assertIn("systemctl:start user@", "\n".join(calls))
        self.assertNotIn("frp:start", calls)
        self.assertLess(calls.index("runtime:preflight"), calls.index("runtime:install"))
        self.assertLess(calls.index("runtime:install"), calls.index("runtime:token-consumed"))
        self.assert_secrets_are_absent(result)

    def test_runtime_bundle_uses_external_venv_and_pins_python_path(self) -> None:
        external_venv = Path(self.temporary.name) / "runtime-venvs" / "build-01"
        (external_venv / "bin").mkdir(parents=True)
        external_python = external_venv / "bin" / "python"
        external_python.touch(mode=0o700)
        environment = {
            **self.environment,
            "AGENTSERVER_RUNTIME_VENV_DIR": str(external_venv),
        }

        result = self.run_installer("--runtime-only", environment=environment)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.root / ".venv" / "bin" / "python").is_file())
        self.assertEqual([], list(self.root.glob(".venv.install.*")))
        runtime_invocations = [line for line in self.calls() if line == "runtime:preflight"]
        self.assertEqual(["runtime:preflight"], runtime_invocations)
        self.assertIn(
            f"runtime:python:{external_python}",
            self.calls(),
        )

    def test_full_install_finishes_frp_before_consuming_enrollment_token(self) -> None:
        result = self.run_installer(
            "--remote-port",
            "24567",
            "--frp-token-file",
            str(self.frp_token),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        calls = self.calls()
        self.assertLess(calls.index("frp:start"), calls.index("runtime:install"))
        self.assertLess(calls.index("runtime:install"), calls.index("runtime:token-consumed"))
        self.assert_secrets_are_absent(result)

    def test_frp_failure_does_not_call_or_consume_runtime_enrollment(self) -> None:
        environment = {**self.environment, "FAKE_FRP_EXIT": "17"}

        result = self.run_installer(
            "--remote-port",
            "24567",
            "--frp-token-file",
            str(self.frp_token),
            environment=environment,
        )

        self.assertEqual(17, result.returncode)
        calls = self.calls()
        self.assertIn("runtime:preflight", calls)
        self.assertIn("frp:start", calls)
        self.assertNotIn("runtime:install", calls)
        self.assertNotIn("runtime:token-consumed", calls)
        self.assertEqual(
            self.enrollment_secret + "\n",
            self.enrollment_token.read_text(encoding="utf-8"),
        )
        self.assert_secrets_are_absent(result)

    def test_existing_frpc_merge_is_available_through_the_combined_installer(self) -> None:
        existing_config = Path(self.temporary.name) / "existing-frpc.toml"
        existing_config.write_text('serverAddr = "frp.example"\n', encoding="utf-8")

        result = self.run_installer(
            "--remote-port",
            "24567",
            "--merge-existing",
            str(existing_config),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        calls = self.calls()
        self.assertIn(f"frp:merge:{existing_config}", calls)
        self.assertLess(calls.index("frp:start"), calls.index("runtime:install"))
        self.assert_secrets_are_absent(result)

    def test_runtime_only_rejects_merge_existing_before_preflight(self) -> None:
        existing_config = Path(self.temporary.name) / "existing-frpc.toml"
        existing_config.write_text('serverAddr = "frp.example"\n', encoding="utf-8")

        result = self.run_installer(
            "--runtime-only",
            "--merge-existing",
            str(existing_config),
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("cannot be combined", result.stderr)
        self.assertEqual([], self.calls())

    def test_invalid_frp_control_port_is_rejected_before_preflight(self) -> None:
        result = self.run_installer(
            "--remote-port",
            "24567",
            "--frp-server-port",
            "70000",
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("between 1 and 65535", result.stderr)
        self.assertEqual([], self.calls())

    def test_failed_venv_install_keeps_the_previous_environment_and_reruns_cleanly(self) -> None:
        old_venv = self.root / ".venv"
        (old_venv / "pyvenv.cfg").write_text("old-environment\n", encoding="utf-8")
        self._write_executable(old_venv / "bin" / "python", "#!/bin/sh\nexit 1\n")
        failure_marker = Path(self.temporary.name) / "fail-pip"
        failure_marker.touch()
        quoted_failure_marker = shlex.quote(str(failure_marker))
        self._write_executable(
            self.fake_bin / "python3",
            "#!/bin/sh\n"
            "[ \"${1:-}\" = -m ] && [ \"${2:-}\" = venv ] || exit 90\n"
            "destination=$3\n"
            "mkdir -p \"$destination/bin\"\n"
            "printf 'new-environment\\n' > \"$destination/pyvenv.cfg\"\n"
            "printf '#!/bin/sh\\nexit 0\\n' > \"$destination/bin/python\"\n"
            f"printf '#!/bin/sh\\n[ -e {quoted_failure_marker} ] && exit 71\\nexit 0\\n' > \"$destination/bin/pip\"\n"
            "chmod 0700 \"$destination/bin/python\" \"$destination/bin/pip\"\n",
        )

        failed = self.run_installer("--runtime-only")

        self.assertNotEqual(0, failed.returncode)
        self.assertIn("unable to install Runtime Python dependencies", failed.stderr)
        self.assertEqual(
            "old-environment\n",
            (old_venv / "pyvenv.cfg").read_text(encoding="utf-8"),
        )
        self.assertEqual([], list(self.root.glob(".venv.install.*")))
        self.assertEqual([], list(self.root.glob(".venv.invalid.*")))

        failure_marker.unlink()
        recovered = self.run_installer("--runtime-only")

        self.assertEqual(0, recovered.returncode, recovered.stderr)
        self.assertEqual(
            "new-environment\n",
            (old_venv / "pyvenv.cfg").read_text(encoding="utf-8"),
        )
        self.assertEqual([], list(self.root.glob(".venv.install.*")))
        self.assertEqual([], list(self.root.glob(".venv.invalid.*")))


class FrpTokenFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.installer = root / "install_frpc_ssh.sh"
        shutil.copy2(REPOSITORY_ROOT / "scripts" / "install_frpc_ssh.sh", self.installer)
        self.fake_bin = root / "bin"
        self.fake_bin.mkdir()
        fake_pgrep = self.fake_bin / "pgrep"
        fake_pgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_pgrep.chmod(0o700)
        self.environment = {
            **os.environ,
            "PATH": f"{self.fake_bin}:/usr/local/bin:/usr/bin:/bin",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_installer(
        self,
        token: Path | None,
        *,
        device_id: str = "device-01",
        ssh_user: str | None = None,
        server: str | None = None,
        server_port: str | None = None,
        rotate_token: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        arguments = [
            "sh",
            str(self.installer),
            "--device-id",
            device_id,
            "--remote-port",
            "24567",
            "--ssh-user",
            ssh_user
            or subprocess.run(
                ["id", "-un"], check=True, capture_output=True, text=True
            ).stdout.strip(),
        ]
        if server is not None:
            arguments.extend(("--server", server))
        if server_port is not None:
            arguments.extend(("--server-port", server_port))
        if token is not None:
            arguments.extend(("--token-file", str(token)))
        if rotate_token:
            arguments.append("--rotate-token")
        arguments.append("--dry-run")
        return subprocess.run(
            arguments,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def _write_matching_managed_config(self) -> tuple[Path, Path, Path, str]:
        config_dir = Path(self.temporary.name) / "managed-frp"
        config_dir.mkdir(mode=0o700)
        config_dir.chmod(0o750)
        token = config_dir / "token"
        token.write_text("existing-private-token\n", encoding="utf-8")
        token.chmod(0o600)
        user = subprocess.run(
            ["id", "-un"], check=True, capture_output=True, text=True
        ).stdout.strip()
        config = config_dir / "frpc.toml"
        config.write_text(
            "clientID = \"device-01\"\n"
            "user = \"device-01\"\n"
            "serverAddr = \"101.43.103.46\"\n"
            "serverPort = 7000\n"
            "loginFailExit = false\n"
            "\n"
            "auth.method = \"token\"\n"
            "auth.tokenSource.type = \"file\"\n"
            f'auth.tokenSource.file.path = "{token}"\n'
            "\n"
            "transport.tls.enable = true\n"
            "\n"
            "[[proxies]]\n"
            "name = \"ssh\"\n"
            "type = \"tcp\"\n"
            "localIP = \"127.0.0.1\"\n"
            "localPort = 22\n"
            "remotePort = 24567\n"
            "\n"
            "[proxies.annotations]\n"
            "device_id = \"device-01\"\n"
            f'ssh_user = "{user}"\n'
            "service = \"ssh\"\n",
            encoding="utf-8",
        )
        config.chmod(0o600)
        source = self.installer.read_text(encoding="utf-8").replace(
            "Linux) FRP_OS=linux; CONFIG_DIR=/etc/frp ;;",
            f"Linux) FRP_OS=linux; CONFIG_DIR={shlex.quote(str(config_dir))} ;;",
        )
        self.installer.write_text(source, encoding="utf-8")
        self.installer.chmod(0o700)
        return config_dir, token, config, user

    def test_explicit_managed_token_requires_rotation_flag(self) -> None:
        _config_dir, existing_token, config, user = self._write_matching_managed_config()
        new_token = Path(self.temporary.name) / "new-token"
        new_token.write_text("new-private-token\n", encoding="utf-8")
        new_token.chmod(0o600)

        result = self.run_installer(new_token, ssh_user=user)

        self.assertEqual(6, result.returncode)
        self.assertIn("必须显式传入 --rotate-token", result.stderr)
        self.assertEqual("existing-private-token\n", existing_token.read_text(encoding="utf-8"))
        self.assertNotIn("new-private-token", result.stdout + result.stderr)
        self.assertEqual(
            "clientID = \"device-01\"\n",
            config.read_text(encoding="utf-8").splitlines()[0] + "\n",
        )

    def test_explicit_managed_environment_token_requires_rotation_flag(self) -> None:
        _config_dir, existing_token, config, user = self._write_matching_managed_config()
        environment = {
            **self.environment,
            "FRP_TOKEN": "new-private-token",
        }
        result = subprocess.run(
            [
                "sh",
                str(self.installer),
                "--device-id",
                "device-01",
                "--remote-port",
                "24567",
                "--ssh-user",
                user,
                "--dry-run",
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(6, result.returncode)
        self.assertIn("必须显式传入 --rotate-token", result.stderr)
        self.assertEqual("existing-private-token\n", existing_token.read_text(encoding="utf-8"))
        self.assertNotIn("new-private-token", result.stdout + result.stderr)
        self.assertEqual(
            "clientID = \"device-01\"\n",
            config.read_text(encoding="utf-8").splitlines()[0] + "\n",
        )

    def test_explicit_managed_token_rotation_reaches_download(self) -> None:
        _config_dir, existing_token, _config, user = self._write_matching_managed_config()
        new_token = Path(self.temporary.name) / "new-token"
        new_token.write_text("new-private-token\n", encoding="utf-8")
        new_token.chmod(0o600)
        download_marker = Path(self.temporary.name) / "download-called"
        fake_curl = self.fake_bin / "curl"
        fake_curl.write_text(
            "#!/bin/sh\n"
            f"touch {shlex.quote(str(download_marker))}\n"
            "exit 23\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o700)

        result = self.run_installer(new_token, ssh_user=user, rotate_token=True)

        self.assertEqual(23, result.returncode)
        self.assertTrue(download_marker.is_file())
        self.assertEqual("existing-private-token\n", existing_token.read_text(encoding="utf-8"))

    def test_frp_token_file_rejects_loose_mode_before_network_access(self) -> None:
        token = Path(self.temporary.name) / "loose-token"
        token.write_text("must-not-appear\n", encoding="utf-8")
        token.chmod(0o644)

        result = self.run_installer(token)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("0600", result.stderr)
        self.assertNotIn("must-not-appear", result.stdout + result.stderr)
        self.assertNotIn("下载 ", result.stdout)

    def test_frp_token_file_rejects_multiline_secret_before_network_access(self) -> None:
        token = Path(self.temporary.name) / "multiline-token"
        token.write_text("first-secret\nsecond-secret\n", encoding="utf-8")
        token.chmod(0o600)

        result = self.run_installer(token)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("无效空白", result.stderr)
        self.assertNotIn("first-secret", result.stdout + result.stderr)
        self.assertNotIn("下载 ", result.stdout)

    def test_entrypoint_values_reject_embedded_newlines_before_network_access(self) -> None:
        token = Path(self.temporary.name) / "token"
        token.write_text("private-token\n", encoding="utf-8")
        token.chmod(0o600)

        unsafe_values = (
            {"device_id": "device-01\ninjected"},
            {"ssh_user": "root\ninjected"},
            {"server": "frp.example\ninjected"},
        )
        for values in unsafe_values:
            with self.subTest(values=values):
                result = self.run_installer(token, **values)
                self.assertEqual(2, result.returncode)
                self.assertNotIn("下载 ", result.stdout)

    def test_invalid_server_port_is_rejected_before_network_access(self) -> None:
        token = Path(self.temporary.name) / "token"
        token.write_text("private-token\n", encoding="utf-8")
        token.chmod(0o600)

        result = self.run_installer(token, server_port="70000")

        self.assertEqual(2, result.returncode)
        self.assertIn("1-65535", result.stdout)
        self.assertNotIn("下载 ", result.stdout)

    def test_frp_token_environment_rejects_whitespace_before_network_access(self) -> None:
        environment = {**self.environment, "FRP_TOKEN": "first\nsecond"}
        user = subprocess.run(
            ["id", "-un"], check=True, capture_output=True, text=True
        ).stdout.strip()
        result = subprocess.run(
            [
                "sh",
                str(self.installer),
                "--device-id",
                "device-01",
                "--remote-port",
                "24567",
                "--ssh-user",
                user,
                "--dry-run",
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("不能包含空白", result.stderr)
        self.assertNotIn("first", result.stdout + result.stderr)
        self.assertNotIn("下载 ", result.stdout)

    def test_matching_managed_config_reuses_private_token_on_rerun(self) -> None:
        config_dir = Path(self.temporary.name) / "managed-frp"
        config_dir.mkdir(mode=0o700)
        config_dir.chmod(0o750)
        token = config_dir / "token"
        token.write_text("existing-private-token\n", encoding="utf-8")
        token.chmod(0o600)
        user = subprocess.run(
            ["id", "-un"], check=True, capture_output=True, text=True
        ).stdout.strip()
        config = config_dir / "frpc.toml"
        config.write_text(
            "clientID = \"device-01\"\n"
            "user = \"device-01\"\n"
            "serverAddr = \"101.43.103.46\"\n"
            "serverPort = 7000\n"
            "loginFailExit = false\n"
            "\n"
            "auth.method = \"token\"\n"
            "auth.tokenSource.type = \"file\"\n"
            f'auth.tokenSource.file.path = "{token}"\n'
            "\n"
            "transport.tls.enable = true\n"
            "\n"
            "[[proxies]]\n"
            "name = \"ssh\"\n"
            "type = \"tcp\"\n"
            "localIP = \"127.0.0.1\"\n"
            "localPort = 22\n"
            "remotePort = 24567\n"
            "\n"
            "[proxies.annotations]\n"
            "device_id = \"device-01\"\n"
            f'ssh_user = "{user}"\n'
            "service = \"ssh\"\n",
            encoding="utf-8",
        )
        config.chmod(0o600)
        source = self.installer.read_text(encoding="utf-8")
        source = source.replace(
            "Linux) FRP_OS=linux; CONFIG_DIR=/etc/frp ;;",
            f"Linux) FRP_OS=linux; CONFIG_DIR={shlex.quote(str(config_dir))} ;;",
        )
        self.installer.write_text(source, encoding="utf-8")
        self.installer.chmod(0o700)
        download_marker = Path(self.temporary.name) / "download-called"
        fake_curl = self.fake_bin / "curl"
        fake_curl.write_text(
            "#!/bin/sh\n"
            f"touch {shlex.quote(str(download_marker))}\n"
            "exit 23\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o700)

        result = self.run_installer(None, ssh_user=user)

        self.assertEqual(23, result.returncode)
        self.assertTrue(download_marker.is_file())
        self.assertIn("复用现有 token", result.stdout)
        self.assertNotIn("非交互运行时必须", result.stdout + result.stderr)
        self.assertNotIn("existing-private-token", result.stdout + result.stderr)

    def test_managed_config_with_wrong_proxy_field_fails_before_download(self) -> None:
        config_dir = Path(self.temporary.name) / "managed-frp"
        config_dir.mkdir(mode=0o700)
        config_dir.chmod(0o750)
        token = config_dir / "token"
        token.write_text("existing-private-token\n", encoding="utf-8")
        token.chmod(0o600)
        user = subprocess.run(
            ["id", "-un"], check=True, capture_output=True, text=True
        ).stdout.strip()
        config = config_dir / "frpc.toml"
        original = (
            'clientID = "device-01"\n'
            'user = "device-01"\n'
            'serverAddr = "101.43.103.46"\n'
            'serverPort = 7000\n'
            'loginFailExit = false\n\n'
            'auth.method = "token"\n'
            'auth.tokenSource.type = "file"\n'
            f'auth.tokenSource.file.path = "{token}"\n\n'
            'transport.tls.enable = true\n\n'
            '[[proxies]]\n'
            'name = "ssh"\n'
            'type = "tcp"\n'
            'localIP = "0.0.0.0"\n'
            'localPort = 22\n'
            'remotePort = 24567\n\n'
            '[proxies.annotations]\n'
            'device_id = "device-01"\n'
            f'ssh_user = "{user}"\n'
            'service = "ssh"\n'
        )
        config.write_text(original, encoding="utf-8")
        config.chmod(0o600)
        source = self.installer.read_text(encoding="utf-8").replace(
            "Linux) FRP_OS=linux; CONFIG_DIR=/etc/frp ;;",
            f"Linux) FRP_OS=linux; CONFIG_DIR={shlex.quote(str(config_dir))} ;;",
        )
        self.installer.write_text(source, encoding="utf-8")
        download_marker = Path(self.temporary.name) / "download-called"
        fake_curl = self.fake_bin / "curl"
        fake_curl.write_text(
            "#!/bin/sh\n"
            f"touch {shlex.quote(str(download_marker))}\n"
            "exit 23\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o700)

        result = self.run_installer(None, ssh_user=user)

        self.assertEqual(6, result.returncode)
        self.assertIn("参数与本次请求不一致", result.stderr)
        self.assertFalse(download_marker.exists())
        self.assertEqual(original, config.read_text(encoding="utf-8"))

    def test_mismatched_managed_config_fails_closed_before_download(self) -> None:
        config_dir = Path(self.temporary.name) / "managed-frp"
        config_dir.mkdir(mode=0o700)
        config_dir.chmod(0o750)
        token = config_dir / "token"
        token.write_text("existing-private-token\n", encoding="utf-8")
        token.chmod(0o600)
        config = config_dir / "frpc.toml"
        original = (
            'clientID = "another-device"\n'
            'user = "another-device"\n'
            'serverAddr = "101.43.103.46"\n'
            'serverPort = 7000\n'
            f'auth.tokenSource.file.path = "{token}"\n'
            'name = "ssh"\n'
            'remotePort = 24567\n'
            f'ssh_user = "{subprocess.run(["id", "-un"], check=True, capture_output=True, text=True).stdout.strip()}"\n'
        )
        config.write_text(original, encoding="utf-8")
        source = self.installer.read_text(encoding="utf-8").replace(
            "Linux) FRP_OS=linux; CONFIG_DIR=/etc/frp ;;",
            f"Linux) FRP_OS=linux; CONFIG_DIR={shlex.quote(str(config_dir))} ;;",
        )
        self.installer.write_text(source, encoding="utf-8")
        download_marker = Path(self.temporary.name) / "download-called"
        fake_curl = self.fake_bin / "curl"
        fake_curl.write_text(
            "#!/bin/sh\n"
            f"touch {shlex.quote(str(download_marker))}\n"
            "exit 23\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o700)

        result = self.run_installer(None)

        self.assertEqual(6, result.returncode)
        self.assertIn("参数与本次请求不一致", result.stderr)
        self.assertFalse(download_marker.exists())
        self.assertEqual(original, config.read_text(encoding="utf-8"))
        self.assertEqual("existing-private-token\n", token.read_text(encoding="utf-8"))

    def test_dangling_managed_config_link_fails_closed_before_download(self) -> None:
        config_dir = Path(self.temporary.name) / "managed-frp"
        config_dir.mkdir(mode=0o700)
        config_dir.chmod(0o750)
        (config_dir / "frpc.toml").symlink_to(config_dir / "missing-config")
        source = self.installer.read_text(encoding="utf-8").replace(
            "Linux) FRP_OS=linux; CONFIG_DIR=/etc/frp ;;",
            f"Linux) FRP_OS=linux; CONFIG_DIR={shlex.quote(str(config_dir))} ;;",
        )
        self.installer.write_text(source, encoding="utf-8")

        result = self.run_installer(None)

        self.assertEqual(6, result.returncode)
        self.assertIn("参数与本次请求不一致", result.stderr)
        self.assertNotIn("下载 ", result.stdout)

    def test_authorized_key_mutation_is_dropped_to_ssh_user_and_rejects_links(self) -> None:
        source = (REPOSITORY_ROOT / "scripts" / "install_frpc_ssh.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('runuser -u "$SSH_USER_NAME"', source)
        self.assertIn('[ -L "$ssh_dir" ]', source)
        self.assertIn('[ -L "$authorized" ]', source)
        self.assertNotIn(
            'chown -R "$SSH_USER_NAME" "$USER_HOME/.ssh"',
            source,
        )

    def test_normal_frp_install_uses_atomic_private_files_and_service_rollback(self) -> None:
        source = (REPOSITORY_ROOT / "scripts" / "install_frpc_ssh.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("write_private_file_atomic", source)
        self.assertIn("write_private_file_atomic_from_stdin", source)
        self.assertIn("rollback_normal_service", source)
        self.assertIn("prepare_normal_service_transaction", source)
        self.assertIn("[ ! -L \"$TOKEN_PATH\" ] && [ -f \"$TOKEN_PATH\" ]", source)
        self.assertIn("mv -f \"$atomic_temporary\" \"$atomic_target\"", source)


class FrpMergeValidationTests(unittest.TestCase):
    """Reject untrusted paths before the merge installer downloads or writes."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.installer = root / "install_frpc_ssh.sh"
        shutil.copy2(REPOSITORY_ROOT / "scripts" / "install_frpc_ssh.sh", self.installer)
        self.fake_bin = root / "bin"
        self.fake_bin.mkdir()
        self.unit = root / "existing.service"
        self.cwd = root / "existing-cwd"
        self.cwd.mkdir()
        self.frpc = root / "frpc"
        self.frpc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.frpc.chmod(0o700)
        self.config = root / "frpc.toml"
        self.config.write_text("serverAddr = \"example\"\n", encoding="utf-8")
        self.config.chmod(0o600)
        self.token = root / "token"
        self.token.write_text("merge-token\n", encoding="utf-8")
        self.token.chmod(0o600)
        self.user = subprocess.run(
            ["id", "-un"], check=True, capture_output=True, text=True
        ).stdout.strip()

        unit_path = str(self.unit)
        self.unit.write_text(
            "[Service]\n"
            f"User={self.user}\n"
            f"WorkingDirectory={self.cwd}\n"
            f"ExecStart={self.frpc} -c {self.config}\n",
            encoding="utf-8",
        )
        fake_systemctl = self.fake_bin / "systemctl"
        fake_systemctl.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  'cat frpc-agentserver.service') exit 0 ;;\n"
            f"  *'FragmentPath --value') printf '%s\\n' {shlex.quote(unit_path)} ;;\n"
            "  *'MainPID --value') printf '0\\n' ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_systemctl.chmod(0o700)
        fake_pgrep = self.fake_bin / "pgrep"
        fake_pgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_pgrep.chmod(0o700)
        self.environment = {
            **os.environ,
            "PATH": f"{self.fake_bin}:/usr/local/bin:/usr/bin:/bin",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_merge(
        self, config: Path | None = None, *, device_id: str = "device-01"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "sh",
                str(self.installer),
                "--device-id",
                device_id,
                "--remote-port",
                "24567",
                "--ssh-user",
                self.user,
                "--token-file",
                str(self.token),
                "--merge-existing",
                str(config or self.config),
                "--dry-run",
            ],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_merge_rejects_systemd_path_with_whitespace_before_download(self) -> None:
        unsafe_cwd = Path(self.temporary.name) / "cwd with spaces"
        unsafe_cwd.mkdir()
        self.unit.write_text(
            "[Service]\n"
            f"User={self.user}\n"
            f"WorkingDirectory={unsafe_cwd}\n"
            f"ExecStart={self.frpc} -c {self.config}\n",
            encoding="utf-8",
        )

        result = self.run_merge()

        self.assertEqual(6, result.returncode)
        self.assertIn("包含空白", result.stderr)
        self.assertNotIn("下载 ", result.stdout)

    def test_merge_rejects_symlinked_config_before_systemd_inspection(self) -> None:
        target = Path(self.temporary.name) / "real-frpc.toml"
        target.write_text("serverAddr = \"example\"\n", encoding="utf-8")
        target.chmod(0o600)
        link = Path(self.temporary.name) / "linked-frpc.toml"
        link.symlink_to(target)

        result = self.run_merge(link)

        self.assertEqual(2, result.returncode)
        self.assertIn("不能是符号链接", result.stderr)
        self.assertNotIn("下载 ", result.stdout)

    def test_merge_rejects_unsafe_systemd_user_name(self) -> None:
        self.unit.write_text(
            "[Service]\n"
            "User=bad user\n"
            f"WorkingDirectory={self.cwd}\n"
            f"ExecStart={self.frpc} -c {self.config}\n",
            encoding="utf-8",
        )

        result = self.run_merge()

        self.assertEqual(6, result.returncode)
        self.assertIn("用户名无法安全", result.stderr)
        self.assertNotIn("下载 ", result.stdout)

    def test_merge_rejects_systemd_user_starting_with_punctuation(self) -> None:
        self.unit.write_text(
            "[Service]\n"
            "User=-unsafe\n"
            f"WorkingDirectory={self.cwd}\n"
            f"ExecStart={self.frpc} -c {self.config}\n",
            encoding="utf-8",
        )

        result = self.run_merge()

        self.assertEqual(6, result.returncode)
        self.assertIn("用户名无法安全", result.stderr)
        self.assertNotIn("下载 ", result.stdout)

    def test_merge_proxy_validator_compares_exact_name_and_all_fields(self) -> None:
        source = self.installer.read_text(encoding="utf-8")

        self.assertIn('awk -v expected_name="$DEVICE_ID.ssh"', source)
        self.assertIn('remote_port != expected_port', source)
        self.assertIn('ssh_user != "\\\"" expected_user "\\\""', source)
        self.assertIn('device_id != "\\\"" expected_device "\\\""', source)
        self.assertIn('matches != 1', source)
        self.assertNotIn('"$DEVICE_ID\\.ssh"', source)

    def test_darwin_merge_rollback_restores_and_reloads_launchd_job(self) -> None:
        source = self.installer.read_text(encoding="utf-8")

        self.assertIn('EXISTING_LAUNCHD_LABEL=com.agentserver.frpc', source)
        self.assertIn('MERGE_UNIT_LABEL="$EXISTING_LAUNCHD_LABEL"', source)
        self.assertIn('launchctl bootstrap system "$MERGE_UNIT_PATH"', source)
        self.assertIn('launchctl enable "system/$MERGE_UNIT_LABEL"', source)
        self.assertIn('replace_merge_launchd_plist', source)
        self.assertIn('[ "$MERGE_SERVICE_MANAGED" -eq 1 ] && [ "$MERGE_UNIT_MUTATED" -eq 1 ]', source)


class FrpMergeBehaviorTests(unittest.TestCase):
    """Exercise proxy matching and transactional rollback with a fake service manager."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.installer = self.root / "install_frpc_ssh.sh"
        shutil.copy2(REPOSITORY_ROOT / "scripts" / "install_frpc_ssh.sh", self.installer)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.cwd = self.root / "frpc-work"
        self.cwd.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = self.root / "frpc.toml"
        self.unit = self.root / "frpc-agentserver.service"
        self.frpc = self.cwd / "frpc"
        self.downloaded_frpc = self.root / "downloaded-frpc"
        self.systemctl_log = self.root / "systemctl.log"
        self.restart_failed = self.root / "restart-failed"
        self.verify_capture = self.root / "verified.toml"
        self.user = subprocess.run(
            ["/usr/bin/id", "-un"], check=True, capture_output=True, text=True
        ).stdout.strip()
        self.group = subprocess.run(
            ["/usr/bin/id", "-gn"], check=True, capture_output=True, text=True
        ).stdout.strip()
        self.original_binary = b"#!/bin/sh\n[ \"${1:-}\" = --version ] && echo 0.68.0\nexit 0\n"
        self.frpc.write_bytes(self.original_binary)
        self.frpc.chmod(0o700)
        self.original_unit = (
            "[Unit]\nDescription=Existing FRP\n"
            "[Service]\n"
            f"User={self.user}\n"
            f"WorkingDirectory={self.cwd}\n"
            f"ExecStart={self.frpc} -c {self.config}\n"
        )
        self.unit.write_text(self.original_unit, encoding="utf-8")
        self.unit.chmod(0o644)
        self._write_fake_commands()
        self.environment = {
            **os.environ,
            "PATH": f"{self.fake_bin}:/usr/local/bin:/usr/bin:/bin",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _quote(path: Path | str) -> str:
        return shlex.quote(str(path))

    def _write_executable(self, name: str, content: str) -> None:
        path = self.fake_bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o700)

    def _write_fake_commands(self) -> None:
        self.downloaded_frpc.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = --version ]; then echo 0.69.0; exit 0; fi\n"
            f"if [ \"${{1:-}}\" = verify ]; then cp \"$3\" {self._quote(self.verify_capture)}; exit 0; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        self.downloaded_frpc.chmod(0o700)
        self._write_executable(
            "id",
            "#!/bin/sh\n"
            "if [ \"$#\" -eq 1 ] && [ \"$1\" = -u ]; then printf '0\\n'; exit 0; fi\n"
            f"if [ \"${{1:-}}\" = -gn ]; then printf '%s\\n' {shlex.quote(self.group)}; exit 0; fi\n"
            "exec /usr/bin/id \"$@\"\n",
        )
        self._write_executable(
            "systemctl",
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {self._quote(self.systemctl_log)}\n"
            "case \"$*\" in\n"
            "  'cat frpc-agentserver.service') exit 0 ;;\n"
            f"  'show frpc-agentserver.service -p FragmentPath --value') printf '%s\\n' {self._quote(self.unit)}; exit 0 ;;\n"
            "  'show frpc-agentserver.service -p MainPID --value') printf '0\\n'; exit 0 ;;\n"
            "  'is-active --quiet frpc-agentserver.service') exit 0 ;;\n"
            "  'is-enabled --quiet frpc-agentserver.service') exit 0 ;;\n"
            "  'show ssh.service -p LoadState --value') printf 'loaded\\n'; exit 0 ;;\n"
            "  'show ssh.service -p Id --value') printf 'ssh.service\\n'; exit 0 ;;\n"
            "  'restart frpc-agentserver.service')\n"
            f"    if [ ! -e {self._quote(self.restart_failed)} ]; then : > {self._quote(self.restart_failed)}; exit 1; fi\n"
            "    exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
        )
        self._write_executable(
            "getent",
            "#!/bin/sh\n"
            f"if [ \"${{1:-}}\" = passwd ] && [ \"${{2:-}}\" = {shlex.quote(self.user)} ]; then\n"
            f"  printf '%s:x:1000:1000::%s:/bin/sh\\n' {shlex.quote(self.user)} {self._quote(self.home)}\n"
            "  exit 0\n"
            "fi\n"
            "exec /usr/bin/getent \"$@\"\n",
        )
        self._write_executable("runuser", "#!/bin/sh\nexit 0\n")
        self._write_executable(
            "curl",
            "#!/bin/sh\n"
            "output=\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = -o ]; then output=$2; shift 2; else shift; fi\n"
            "done\n"
            ": > \"$output\"\n",
        )
        self._write_executable(
            "sha256sum",
            "#!/bin/sh\n"
            "printf '6b90d1cd28fc661f170c0de90dde03d2c63e4fd7ce0ae2da2ca1c28014b8146e  %s\\n' \"$1\"\n",
        )
        self._write_executable(
            "tar",
            "#!/bin/sh\n"
            "destination=\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = -C ]; then destination=$2; shift 2; else shift; fi\n"
            "done\n"
            "mkdir -p \"$destination/frp_0.69.0_linux_amd64\"\n"
            f"cp {self._quote(self.downloaded_frpc)} \"$destination/frp_0.69.0_linux_amd64/frpc\"\n"
            "chmod 0700 \"$destination/frp_0.69.0_linux_amd64/frpc\"\n",
        )
        self._write_executable("journalctl", "#!/bin/sh\nexit 0\n")

    def write_proxy_config(
        self,
        *,
        name: str,
        port: int,
        device_id: str,
        local_ip: str = "127.0.0.1",
    ) -> bytes:
        content = (
            'clientID = "existing"\n'
            'serverAddr = "frp.example"\n'
            'serverPort = 7000\n\n'
            '[[proxies]]\n'
            f'name = "{name}"\n'
            'type = "tcp"\n'
            f'localIP = "{local_ip}"\n'
            'localPort = 22\n'
            f'remotePort = {port}\n\n'
            '[proxies.annotations]\n'
            f'device_id = "{device_id}"\n'
            f'ssh_user = "{self.user}"\n'
            'service = "ssh"\n'
        ).encode()
        self.config.write_bytes(content)
        self.config.chmod(0o600)
        return content

    def run_merge(self, *, device_id: str, dry_run: bool) -> subprocess.CompletedProcess[str]:
        arguments = [
            "sh",
            str(self.installer),
            "--device-id",
            device_id,
            "--remote-port",
            "24567",
            "--ssh-user",
            self.user,
            "--merge-existing",
            str(self.config),
        ]
        if dry_run:
            arguments.append("--dry-run")
        return subprocess.run(
            arguments,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_device_id_with_dot_does_not_match_a_different_proxy_name(self) -> None:
        self.write_proxy_config(name="labXone.ssh", port=24568, device_id="labXone")

        result = self.run_merge(device_id="lab.one", dry_run=True)

        self.assertEqual(0, result.returncode, result.stderr)
        verified = self.verify_capture.read_text(encoding="utf-8")
        self.assertEqual(1, verified.count('name = "labXone.ssh"'))
        self.assertEqual(1, verified.count('name = "lab.one.ssh"'))
        self.assertNotIn("已存在且配置完全匹配", result.stdout)

    def test_existing_target_proxy_with_wrong_field_is_rejected(self) -> None:
        original = self.write_proxy_config(
            name="lab.one.ssh",
            port=24567,
            device_id="lab.one",
            local_ip="0.0.0.0",
        )

        result = self.run_merge(device_id="lab.one", dry_run=True)

        self.assertEqual(6, result.returncode)
        self.assertIn("与本次请求不一致", result.stderr)
        self.assertEqual(original, self.config.read_bytes())
        self.assertFalse(self.verify_capture.exists())

    def test_matching_target_proxy_is_not_duplicated(self) -> None:
        self.write_proxy_config(name="lab.one.ssh", port=24567, device_id="lab.one")

        result = self.run_merge(device_id="lab.one", dry_run=True)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("已存在且配置完全匹配", result.stdout)
        self.assertEqual(
            1,
            self.verify_capture.read_text(encoding="utf-8").count('name = "lab.one.ssh"'),
        )

    def test_restart_failure_restores_config_binary_unit_and_service_state(self) -> None:
        original_config = self.write_proxy_config(
            name="legacy.ssh", port=24568, device_id="legacy"
        )

        result = self.run_merge(device_id="device-01", dry_run=False)

        self.assertEqual(7, result.returncode, result.stdout + result.stderr)
        self.assertIn("已尽力恢复", result.stderr)
        self.assertEqual(original_config, self.config.read_bytes())
        self.assertEqual(self.original_binary, self.frpc.read_bytes())
        self.assertEqual(self.original_unit, self.unit.read_text(encoding="utf-8"))
        calls = self.systemctl_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, calls.count("restart frpc-agentserver.service"))
        self.assertIn("stop frpc-agentserver.service", calls)
        self.assertIn("enable frpc-agentserver.service", calls)

    def test_snapshot_failure_aborts_before_any_merge_mutation(self) -> None:
        original_config = self.write_proxy_config(
            name="legacy.ssh", port=24568, device_id="legacy"
        )
        self._write_executable(
            "cp",
            "#!/bin/sh\n"
            "destination=\n"
            "for argument do destination=$argument; done\n"
            f"case \"$destination\" in */merge-transaction/config) exit 77 ;; esac\n"
            "exec /bin/cp \"$@\"\n",
        )

        result = self.run_merge(device_id="device-01", dry_run=False)

        self.assertEqual(6, result.returncode)
        self.assertIn("无法创建 merge 事务快照", result.stderr)
        self.assertEqual(original_config, self.config.read_bytes())
        self.assertEqual(self.original_binary, self.frpc.read_bytes())
        self.assertEqual(self.original_unit, self.unit.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

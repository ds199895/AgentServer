from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .runtime_host import AdapterFactory, DeviceRuntimeHost


def _default_state_dir() -> Path:
    return Path(
        os.getenv("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    ) / "agentserver-runtime"


def _codex_binary(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = shutil.which("codex") or "codex"
    if any(character in raw for character in "\0\r\n"):
        raise ValueError("Codex binary must be a safe executable path")
    if raw.startswith("~") or os.path.sep in raw or (
        os.path.altsep is not None and os.path.altsep in raw
    ):
        return str(Path(raw).expanduser().resolve())
    return shutil.which(raw) or raw


def _default_adapter_registry(
    codex_binary: str | None = None,
    *,
    state_dir: str | Path | None = None,
    codex_home: str | Path | None = None,
    bubblewrap_binary: str | None = None,
) -> dict[str, AdapterFactory]:
    """Build provider factories lazily so CLI help/enroll stay lightweight."""

    from .runtime_adapters.codex import CodexRuntimeAdapter

    binary = _codex_binary(codex_binary)
    resolved_state_dir = Path(state_dir or _default_state_dir()).expanduser().resolve()
    resolved_codex_home = Path(
        codex_home or os.getenv("CODEX_HOME") or (Path.home() / ".codex")
    ).expanduser().resolve()
    bubblewrap = _codex_binary(
        bubblewrap_binary or shutil.which("bwrap") or "bwrap"
    )

    def create_codex() -> CodexRuntimeAdapter:
        return CodexRuntimeAdapter(
            binary_path=binary,
            home_path=resolved_codex_home,
            isolation_enabled=True,
            bubblewrap_path=bubblewrap,
            host_state_dir=resolved_state_dir,
        )

    # DeviceRuntimeHost reads this stable metadata without spawning a provider.
    # The preflight starts only bwrap + /bin/true; runtime.probe repeats the
    # authoritative check and a provider is never allowed to fall back bare.
    create_codex.transport = "app-server"  # type: ignore[attr-defined]
    probe_adapter = create_codex()
    create_codex.validate_session = probe_adapter.validate_session  # type: ignore[attr-defined]
    create_codex.available = probe_adapter.probe_sync().available  # type: ignore[attr-defined]
    create_codex.version = ""  # type: ignore[attr-defined]
    create_codex.capabilities = CodexRuntimeAdapter.capabilities  # type: ignore[attr-defined]
    return {"codex": create_codex}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentserver-runtime")
    parser.add_argument(
        "--device-id",
        required=True,
        help="Non-secret AgentServer device identifier",
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="AgentServer HTTPS URL (loopback HTTP is allowed for development)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=_default_state_dir(),
        help="Owner-private persistent device runtime directory",
    )
    parser.add_argument("--heartbeat-interval", type=float, default=10.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--max-backoff", type=float, default=30.0)
    parser.add_argument(
        "--codex-binary",
        default=shutil.which("codex") or "codex",
        help=(
            "Codex executable path for the built-in app-server runtime; "
            "set an absolute path for user-systemd services"
        ),
    )
    parser.add_argument(
        "--bubblewrap-binary",
        default=shutil.which("bwrap") or "bwrap",
        help=(
            "bubblewrap executable used for the mandatory Codex sandbox; "
            "set an absolute path for user-systemd services"
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    enroll = subcommands.add_parser(
        "enroll", help="Pair this device once using an owner-only token file"
    )
    enroll.add_argument(
        "--enrollment-token-file",
        type=Path,
        required=True,
        help="Path to a regular mode-0600 enrollment token file",
    )
    enroll.add_argument(
        "--replace-existing-credential",
        action="store_true",
        help="Consume the token and atomically replace a revoked/stale credential",
    )

    subcommands.add_parser("run", help="Run the persistent device runtime host")
    subcommands.add_parser(
        "rotate-credential",
        help="Rotate the enrolled device credential without printing it",
    )
    return parser


def _host(
    arguments: argparse.Namespace,
    adapter_registry: Mapping[str, AdapterFactory] | None,
) -> DeviceRuntimeHost:
    if adapter_registry is not None:
        resolved_registry = adapter_registry
    elif arguments.command == "run":
        resolved_registry = _default_adapter_registry(
            getattr(arguments, "codex_binary", None),
            state_dir=arguments.state_dir,
            bubblewrap_binary=getattr(arguments, "bubblewrap_binary", None),
        )
    else:
        resolved_registry = {}
    return DeviceRuntimeHost(
        device_id=arguments.device_id,
        base_url=arguments.base_url,
        state_dir=arguments.state_dir,
        adapter_registry=resolved_registry,
        heartbeat_interval=arguments.heartbeat_interval,
        poll_interval=arguments.poll_interval,
        max_backoff=arguments.max_backoff,
    )


async def run_cli(
    arguments: argparse.Namespace,
    *,
    adapter_registry: Mapping[str, AdapterFactory] | None = None,
) -> dict[str, Any]:
    host = _host(arguments, adapter_registry)
    try:
        if arguments.command == "enroll":
            return await host.enroll_from_file(
                arguments.enrollment_token_file,
                replace_existing=arguments.replace_existing_credential,
            )
        if arguments.command == "rotate-credential":
            return await host.rotate_credential()
        if arguments.command != "run":  # protected by argparse
            raise ValueError("unsupported runtime command")

        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_name, stopped.set)
                installed.append(signal_name)
            except (NotImplementedError, RuntimeError):
                pass
        # This line intentionally contains identity and health only. Device and
        # enrollment credentials never enter argv, environment, stdout, or logs.
        print(
            json.dumps(
                {
                    "ready": True,
                    "device_id": host.device_id,
                    "instance_id": host.instance_id,
                    "boot_id": host.boot_id,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        try:
            await host.run(stop_event=stopped)
        finally:
            for signal_name in installed:
                loop.remove_signal_handler(signal_name)
        return {
            "stopped": True,
            "device_id": host.device_id,
            "instance_id": host.instance_id,
        }
    finally:
        await host.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    adapter_registry: Mapping[str, AdapterFactory] | None = None,
) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run_cli(arguments, adapter_registry=adapter_registry))
    except (OSError, RuntimeError, ValueError) as error:
        # Runtime exceptions are designed to avoid secrets; do not include any
        # request payload or enrollment token in this message.
        parser.exit(2, f"agentserver-runtime: {error}\n")
    if arguments.command != "run":
        print(json.dumps(result, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

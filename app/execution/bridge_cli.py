from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any, Sequence

import httpx

from .bridge import AgentBridge, ReloadingTokenFile, _validated_base_url
from .reporter import (
    ReporterContext,
    ReporterSpool,
    RuntimeReporter,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentserver-bridge")
    parser.add_argument("--address", default=os.getenv("AGENTSERVER_CONTROL_SOCKET", ""))
    parser.add_argument("--base-url", default=os.getenv("AGENTSERVER_BASE_URL", ""))
    parser.add_argument(
        "--launch-root-pid",
        type=int,
        default=(
            int(os.environ["AGENTSERVER_LAUNCH_ROOT_PID"])
            if os.getenv("AGENTSERVER_LAUNCH_ROOT_PID")
            else None
        ),
        help="PID of the server-created Agent process root authorized for this Run",
    )
    parser.add_argument(
        "--report-token-file",
        type=Path,
        default=(
            Path(os.environ["AGENTSERVER_REPORT_TOKEN_FILE"])
            if os.getenv("AGENTSERVER_REPORT_TOKEN_FILE")
            else None
        ),
    )
    parser.add_argument(
        "--command-token-file",
        type=Path,
        default=(
            Path(os.environ["AGENTSERVER_COMMAND_TOKEN_FILE"])
            if os.getenv("AGENTSERVER_COMMAND_TOKEN_FILE")
            else None
        ),
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(
            os.getenv("AGENTSERVER_REPORT_STATE_DIR")
            or os.getenv("XDG_STATE_HOME")
            or (Path.home() / ".local" / "state")
        ),
    )
    return parser


async def run_bridge(arguments: argparse.Namespace) -> None:
    if (
        not arguments.address
        or not arguments.base_url
        or not arguments.report_token_file
        or not arguments.command_token_file
        or not arguments.launch_root_pid
    ):
        raise ValueError(
            "bridge requires --address, --base-url, --report-token-file, "
            "--command-token-file, and --launch-root-pid"
        )
    report_token_provider = ReloadingTokenFile(arguments.report_token_file)
    command_token_provider = ReloadingTokenFile(arguments.command_token_file)
    reporter_token = report_token_provider.last_valid_token
    command_token = command_token_provider.last_valid_token
    runtime_context = ReporterContext.from_environment()
    base_url = _validated_base_url(arguments.base_url)
    spool = ReporterSpool(
        arguments.state_dir / "agentserver" / f"bridge-{runtime_context.run_id}.db"
    )
    reporter = RuntimeReporter(
        runtime_context,
        spool,
        producer_id=f"bridge:{runtime_context.device_id or runtime_context.terminal_id}",
        adapter=os.getenv("AGENTSERVER_ADAPTER", "generic"),
        mode="adapter",
    )

    bridge: AgentBridge | None = None

    async def context_provider() -> dict[str, Any]:
        try:
            token = (
                await bridge.reporter_auth_token()
                if bridge is not None
                else report_token_provider()
            )
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                response = await client.get(
                    f"{base_url}/api/runtime/v1/context",
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                value = response.json()
                if isinstance(value, dict):
                    return value
        except (OSError, ValueError, httpx.HTTPError):
            pass
        return {
            "managed": True,
            "origin": "agentserver",
            "terminal_id": runtime_context.terminal_id,
            "launch_id": runtime_context.launch_id,
            "assignment": {
                "task_id": runtime_context.task_id,
                "assignment_id": runtime_context.assignment_id,
                "status": "unknown_offline",
            },
            "active_runs": [runtime_context.run_id],
            "offline": True,
        }

    bridge = AgentBridge(
        reporter,
        address=arguments.address,
        base_url=base_url,
        reporter_token=reporter_token,
        command_token=command_token,
        reporter_token_provider=report_token_provider,
        command_token_provider=command_token_provider,
        launch_root_pid=arguments.launch_root_pid,
        context_provider=context_provider,
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stopped.set)
        except (NotImplementedError, RuntimeError):
            pass
    await bridge.start()
    try:
        print(json.dumps({"ready": True, "address": arguments.address}))
        await stopped.wait()
    finally:
        await bridge.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        asyncio.run(run_bridge(arguments))
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"agentserver-bridge: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

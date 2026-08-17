from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
from multiprocessing.connection import Client as PipeClient
from pathlib import Path
from typing import Any, Sequence

import httpx

from .control import read_linux_process_identity
from .reporter import (
    ADAPTERS,
    ReporterContext,
    ReporterSpool,
    RuntimeReporter,
    load_reporter_token_file,
)


ACTIVITIES = (
    "idle",
    "thinking",
    "planning",
    "coding",
    "tooling",
    "testing",
    "reviewing",
    "waiting",
    "finalizing",
    "unknown",
)
WAIT_REASONS = (
    "user_input",
    "approval",
    "authentication",
    "tool",
    "child_run",
    "network",
    "rate_limit",
    "retry_backoff",
    "dependency",
    "resource",
    "unknown",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentserver-report",
        description="Report public Agent run phases to an AgentServer-managed terminal.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    attach = subparsers.add_parser("attach")
    attach.add_argument("--kind", choices=tuple(ADAPTERS), default="generic")
    attach.add_argument("--version", default="")

    phase = subparsers.add_parser("phase")
    phase.add_argument("activity", choices=ACTIVITIES)
    phase.add_argument("--summary", default="")

    progress = subparsers.add_parser("progress")
    progress.add_argument("--current", type=float, required=True)
    progress.add_argument("--total", type=float, required=True)
    progress.add_argument("--summary", default="")

    wait = subparsers.add_parser("wait")
    wait.add_argument("--reason", choices=WAIT_REASONS, required=True)
    wait.add_argument("--target-run-id", default="")
    wait.add_argument("--summary", default="")

    span = subparsers.add_parser("span")
    span.add_argument("operation", choices=("start", "end"))
    span.add_argument("--id", required=True)
    span.add_argument("--parent-id", default="")
    span.add_argument("--name", default="")
    span.add_argument("--status", choices=("ok", "error", "cancelled"), default="ok")

    artifact = subparsers.add_parser("artifact")
    artifact.add_argument("path")
    artifact.add_argument("--kind", default="file")
    artifact.add_argument("--media-type", default="")

    subparsers.add_parser("heartbeat")

    complete = subparsers.add_parser("complete")
    complete.add_argument("--summary", default="")

    failed = subparsers.add_parser("fail")
    failed.add_argument("--code", default="agent_failed")
    failed.add_argument("--summary", required=True)
    return parser


def event_from_args(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    command = arguments.command
    if command == "attach":
        adapter = ADAPTERS[arguments.kind]
        return "agent.registered", {
            "kind": arguments.kind,
            "version": arguments.version,
            "pid": os.getpid(),
            "cwd": str(Path.cwd()),
            "capabilities": sorted(adapter.capabilities),
        }
    if command == "phase":
        if arguments.activity == "waiting":
            raise ValueError("use `agentserver-report wait --reason ...` for waiting")
        return "run.activity.changed", {
            "activity": arguments.activity,
            "summary": arguments.summary,
        }
    if command == "progress":
        if arguments.total <= 0 or arguments.current < 0 or arguments.current > arguments.total:
            raise ValueError("progress requires 0 <= current <= total and total > 0")
        return "run.progress.updated", {
            "current": arguments.current,
            "total": arguments.total,
            "progress": arguments.current / arguments.total,
            "summary": arguments.summary,
        }
    if command == "wait":
        return "run.activity.changed", {
            "activity": "waiting",
            "wait_reason": arguments.reason,
            "wait_target_run_id": arguments.target_run_id or None,
            "summary": arguments.summary,
        }
    if command == "span":
        outcome = {
            "ok": "succeeded",
            "error": "failed",
            "cancelled": "cancelled",
        }[arguments.status]
        return ("span.started" if arguments.operation == "start" else "span.ended"), {
            "span_id": arguments.id,
            "parent_span_id": arguments.parent_id or None,
            "name": arguments.name,
            "outcome": outcome if arguments.operation == "end" else None,
        }
    if command == "artifact":
        return "artifact.published", {
            "path": arguments.path,
            "kind": arguments.kind,
            "media_type": arguments.media_type or None,
        }
    if command == "heartbeat":
        return "agent.heartbeat", {"pid": os.getpid()}
    if command == "complete":
        return "run.succeeded", {"summary": arguments.summary}
    if command == "fail":
        return "run.failed", {"code": arguments.code, "summary": arguments.summary}
    raise ValueError(f"unsupported command: {command}")


def _expected_control_server(
    environment: dict[str, str] | os._Environ[str],
) -> tuple[int | None, int | None]:
    transport = str(
        environment.get("AGENTSERVER_CONTROL_TRANSPORT") or ""
    ).strip()
    raw_pid = str(environment.get("AGENTSERVER_CONTROL_SERVER_PID") or "").strip()
    raw_start = str(
        environment.get("AGENTSERVER_CONTROL_SERVER_START_TIME") or ""
    ).strip()
    if transport in {"device-bridge", "local-broker-path-compat"}:
        if raw_pid or raw_start:
            raise ValueError("managed control transport has conflicting server identity")
        return None, None
    if transport != "local-broker":
        raise ValueError("managed control transport is missing or unsupported")
    if not raw_pid.isdigit() or not raw_start.isdigit():
        raise ValueError("managed control server identity is incomplete or invalid")
    pid = int(raw_pid)
    start_time = int(raw_start)
    if pid <= 0 or start_time <= 0:
        raise ValueError("managed control server identity is incomplete or invalid")
    return pid, start_time


def _verify_unix_control_server(
    client: socket.socket,
    *,
    expected_pid: int,
    expected_start_time: int,
) -> None:
    """Authenticate the local Broker before disclosing a runtime event."""

    if not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeError("control server identity requires SO_PEERCRED")
    try:
        peer_pid, peer_uid, _peer_gid = struct.unpack(
            "3i",
            client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
        )
    except (OSError, struct.error) as error:
        raise RuntimeError("control server peer identity is unavailable") from error
    if peer_pid != expected_pid or peer_uid != os.geteuid():
        raise RuntimeError("control server process identity does not match the managed launch")
    identity = read_linux_process_identity(peer_pid)
    if identity is None or identity.start_time_ticks != expected_start_time:
        raise RuntimeError("control server process incarnation does not match the managed launch")


def _send_bridge_request(
    address: str,
    request: dict[str, Any],
    *,
    expected_server_pid: int | None = None,
    expected_server_start_time: int | None = None,
) -> dict[str, Any]:
    encoded = json.dumps(request, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("bridge request exceeds 64 KiB")
    if os.name == "nt" or address.startswith("\\\\.\\pipe\\"):
        connection = PipeClient(address, family="AF_PIPE")
        try:
            connection.send_bytes(encoded)
            response = connection.recv_bytes(64 * 1024)
        finally:
            connection.close()
    else:
        if expected_server_pid is None and expected_server_start_time is None:
            expected_server_pid, expected_server_start_time = _expected_control_server(
                os.environ
            )
        if (expected_server_pid is None) != (expected_server_start_time is None):
            raise ValueError("managed control server identity is incomplete or invalid")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(10)
        try:
            client.connect(address)
            if expected_server_pid is not None:
                assert expected_server_start_time is not None
                _verify_unix_control_server(
                    client,
                    expected_pid=expected_server_pid,
                    expected_start_time=expected_server_start_time,
                )
            client.sendall(encoded + b"\n")
            response = b""
            while b"\n" not in response and len(response) <= 64 * 1024:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
        finally:
            client.close()
        response = response.split(b"\n", 1)[0]
    result = json.loads(response)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "bridge rejected the report"))
    return result


def report(arguments: argparse.Namespace, environment: dict[str, str] | None = None) -> dict[str, Any]:
    values = dict(os.environ if environment is None else environment)
    event_type, payload = event_from_args(arguments)
    control_socket = values.get("AGENTSERVER_CONTROL_SOCKET", "").strip()
    if control_socket:
        expected_server_pid, expected_server_start_time = _expected_control_server(
            values
        )
        return _send_bridge_request(
            control_socket,
            {
                "action": (
                    "heartbeat" if event_type == "agent.heartbeat" else "event"
                ),
                "event_type": event_type,
                "payload": payload,
                "adapter": values.get("AGENTSERVER_ADAPTER") or "generic",
                "scope": {
                    "owner_id": values.get("AGENTSERVER_OWNER_ID"),
                    "device_id": values.get("AGENTSERVER_DEVICE_ID"),
                    "terminal_id": values.get("AGENTSERVER_TERMINAL_ID"),
                    "launch_id": values.get("AGENTSERVER_LAUNCH_ID"),
                },
            },
            expected_server_pid=expected_server_pid,
            expected_server_start_time=expected_server_start_time,
        )

    context = ReporterContext.from_environment(values)
    state_directory = Path(
        values.get("AGENTSERVER_REPORT_STATE_DIR")
        or values.get("XDG_STATE_HOME")
        or (Path.home() / ".local" / "state")
    )
    spool = ReporterSpool(state_directory / "agentserver" / f"{context.run_id}.db")
    reporter = RuntimeReporter(
        context,
        spool,
        producer_id=values.get("AGENTSERVER_PRODUCER_ID") or f"agent:{context.run_id}",
        adapter=values.get("AGENTSERVER_ADAPTER") or "generic",
        mode="adapter",
    )
    token_file = values.get("AGENTSERVER_REPORT_TOKEN_FILE", "").strip()
    token = load_reporter_token_file(token_file) if token_file else ""
    base_url = values.get("AGENTSERVER_BASE_URL", "")
    if event_type == "agent.heartbeat":
        if not token or not base_url:
            return {"ok": True, "heartbeat": "offline", "queued": 0}
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/api/runtime/v1/heartbeat",
                headers={"Authorization": f"Bearer {token}"},
                json={"producer_id": reporter.producer_id},
            )
            response.raise_for_status()
            return {"ok": True, "heartbeat": response.json(), "queued": 0}
    event = reporter.emit(event_type, payload)
    if token and base_url:
        reporter.flush(base_url, token)
    return {"ok": True, "event": event, "queued": len(spool)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        result = report(arguments)
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        parser.exit(2, f"agentserver-report: {exc}\n")
    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

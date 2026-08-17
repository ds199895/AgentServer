from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .cli import _send_bridge_request
from .provider_adapters import ADAPTERS


MAX_PROVIDER_EVENT_BYTES = 64 * 1024
BridgeSender = Callable[[str, dict[str, Any]], dict[str, Any]]


def report_provider_event(
    provider: str,
    raw_event: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    sender: BridgeSender = _send_bridge_request,
) -> list[dict[str, Any]]:
    """Normalize one native event and send only public state metadata."""
    values = os.environ if environment is None else environment
    address = str(values.get("AGENTSERVER_CONTROL_SOCKET") or "").strip()
    if not address:
        raise ValueError("AGENTSERVER_CONTROL_SOCKET is required for provider hooks")
    try:
        adapter = ADAPTERS[provider]
    except KeyError as error:
        raise ValueError(f"unsupported provider adapter: {provider}") from error
    scope = {
        "owner_id": values.get("AGENTSERVER_OWNER_ID"),
        "device_id": values.get("AGENTSERVER_DEVICE_ID"),
        "terminal_id": values.get("AGENTSERVER_TERMINAL_ID"),
        "launch_id": values.get("AGENTSERVER_LAUNCH_ID"),
    }
    responses: list[dict[str, Any]] = []
    for event in adapter.normalize_many(raw_event):
        responses.append(
            sender(
                address,
                {
                    "action": "event",
                    "event_type": event.type,
                    "payload": dict(event.payload),
                    "adapter": provider,
                    "scope": scope,
                },
            )
        )
    return responses


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentserver-provider-hook",
        description="Normalize one provider hook/JSONL event into AgentServer runtime facts.",
    )
    parser.add_argument("--provider", choices=tuple(ADAPTERS), required=True)
    parser.add_argument(
        "--jsonl",
        action="store_true",
        help="read a stream such as `codex exec --json`, one JSON object per line",
    )
    return parser


def _decode_provider_event(encoded: bytes, *, label: str = "input") -> Mapping[str, Any]:
    if len(encoded) > MAX_PROVIDER_EVENT_BYTES:
        raise ValueError(f"provider {label} exceeds 64 KiB")
    value = json.loads(encoded or b"{}")
    if not isinstance(value, Mapping):
        raise ValueError(f"provider {label} must be a JSON object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.jsonl:
            for line_number, encoded in enumerate(sys.stdin.buffer, start=1):
                if not encoded.strip():
                    continue
                report_provider_event(
                    arguments.provider,
                    _decode_provider_event(
                        encoded, label=f"JSONL line {line_number}"
                    ),
                )
        else:
            encoded = sys.stdin.buffer.read(MAX_PROVIDER_EVENT_BYTES + 1)
            report_provider_event(
                arguments.provider,
                _decode_provider_event(encoded, label="hook input"),
            )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"agentserver-provider-hook: {error}", file=sys.stderr)
        return 2
    # Hook stdout can steer some providers, so successful telemetry is silent.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

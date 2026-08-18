from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, BinaryIO, TextIO

from .cli import _send_bridge_request
from .provider_adapters import ADAPTERS, NormalizedRuntimeEvent


MAX_PROVIDER_EVENT_BYTES = 64 * 1024
MAX_PROVIDER_STREAM_EVENT_BYTES = 8 * 1024 * 1024
MAX_STREAM_DIAGNOSTICS = 10
BridgeSender = Callable[[str, dict[str, Any]], dict[str, Any]]


class ProviderEventStream:
    """Per-process JSONL state without mutating the global adapter instances."""

    def __init__(self, provider: str) -> None:
        try:
            self.adapter = ADAPTERS[provider]
        except KeyError as error:
            raise ValueError(f"unsupported provider adapter: {provider}") from error
        self.provider = provider
        self.open_spans: dict[str, str] = {}
        self.last_activity: str | None = None
        self.saw_input = False

    def normalize(
        self, raw_event: Mapping[str, Any]
    ) -> tuple[NormalizedRuntimeEvent, ...]:
        self.saw_input = True
        events = self.adapter.normalize_many(raw_event)
        for event in events:
            span_id = str(event.payload.get("span_id") or "")
            if event.type == "span.started" and span_id:
                self.open_spans[span_id] = str(event.payload.get("name") or "other_tool")
            elif event.type == "span.ended" and span_id:
                self.open_spans.pop(span_id, None)
            elif event.type == "run.activity.changed":
                self.last_activity = str(event.payload.get("activity") or "") or None
        return events

    def finish(
        self, *, exit_code: int | None = None, force: bool = False
    ) -> tuple[NormalizedRuntimeEvent, ...]:
        """Close incomplete spans and add a non-authoritative stream boundary."""

        if not self.saw_input and not force:
            return ()
        events: list[NormalizedRuntimeEvent] = []
        incomplete_outcome = "failed" if exit_code not in {None, 0} else "cancelled"
        for span_id, name in self.open_spans.items():
            events.append(
                NormalizedRuntimeEvent(
                    "span.ended",
                    {
                        "span_id": span_id,
                        "name": name,
                        "outcome": incomplete_outcome,
                    },
                )
            )
        self.open_spans.clear()
        if self.last_activity != "finalizing":
            payload: dict[str, Any] = {"activity": "finalizing"}
            if exit_code is not None:
                payload["provider_status"] = (
                    "completed" if exit_code == 0 else "failed"
                )
            events.append(NormalizedRuntimeEvent("run.activity.changed", payload))
            self.last_activity = "finalizing"
        return tuple(events)


def report_provider_event(
    provider: str,
    raw_event: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    sender: BridgeSender = _send_bridge_request,
) -> list[dict[str, Any]]:
    """Normalize one native event and send only public state metadata."""
    try:
        adapter = ADAPTERS[provider]
    except KeyError as error:
        raise ValueError(f"unsupported provider adapter: {provider}") from error
    return report_normalized_events(
        provider,
        adapter.normalize_many(raw_event),
        environment=environment,
        sender=sender,
    )


def report_normalized_events(
    provider: str,
    events: Sequence[NormalizedRuntimeEvent],
    *,
    environment: Mapping[str, str] | None = None,
    sender: BridgeSender = _send_bridge_request,
) -> list[dict[str, Any]]:
    """Send already-normalized facts from one stateful provider stream."""

    if not events:
        return []
    values = os.environ if environment is None else environment
    address = str(values.get("AGENTSERVER_CONTROL_SOCKET") or "").strip()
    if not address:
        raise ValueError("AGENTSERVER_CONTROL_SOCKET is required for provider hooks")
    scope = {
        "owner_id": values.get("AGENTSERVER_OWNER_ID"),
        "device_id": values.get("AGENTSERVER_DEVICE_ID"),
        "terminal_id": values.get("AGENTSERVER_TERMINAL_ID"),
        "launch_id": values.get("AGENTSERVER_LAUNCH_ID"),
    }
    responses: list[dict[str, Any]] = []
    for event in events:
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
    return _decode_bounded_provider_event(
        encoded, label=label, max_bytes=MAX_PROVIDER_EVENT_BYTES
    )


def _decode_bounded_provider_event(
    encoded: bytes, *, label: str, max_bytes: int
) -> Mapping[str, Any]:
    if len(encoded) > max_bytes:
        raise ValueError(f"provider {label} exceeds {max_bytes // 1024} KiB")
    value = json.loads(encoded or b"{}")
    if not isinstance(value, Mapping):
        raise ValueError(f"provider {label} must be a JSON object")
    return value


def _read_bounded_line(
    stream: BinaryIO, *, max_bytes: int
) -> tuple[bytes | None, bool]:
    """Read one logical line while bounding memory and draining oversize input."""

    encoded = stream.readline(max_bytes + 1)
    if not encoded:
        return None, False
    if len(encoded) <= max_bytes:
        return encoded, False
    while encoded and not encoded.endswith(b"\n"):
        encoded = stream.readline(64 * 1024)
    return None, True


def consume_provider_jsonl(
    provider: str,
    *,
    input_stream: BinaryIO,
    error_stream: TextIO,
    environment: Mapping[str, str] | None = None,
    sender: BridgeSender = _send_bridge_request,
) -> int:
    """Consume an observer stream without ever breaking the upstream writer."""

    stream = ProviderEventStream(provider)
    failures = 0
    line_number = 0

    def diagnose(error: Exception | str) -> None:
        nonlocal failures
        failures += 1
        if failures <= MAX_STREAM_DIAGNOSTICS:
            print(f"agentserver-provider-hook: {error}", file=error_stream)

    try:
        while True:
            encoded, oversized = _read_bounded_line(
                input_stream, max_bytes=MAX_PROVIDER_STREAM_EVENT_BYTES
            )
            if encoded is None and not oversized:
                break
            line_number += 1
            if oversized:
                diagnose(
                    f"provider JSONL line {line_number} exceeds "
                    f"{MAX_PROVIDER_STREAM_EVENT_BYTES // (1024 * 1024)} MiB"
                )
                continue
            if not encoded or not encoded.strip():
                continue
            try:
                raw_event = _decode_bounded_provider_event(
                    encoded,
                    label=f"JSONL line {line_number}",
                    max_bytes=MAX_PROVIDER_STREAM_EVENT_BYTES,
                )
                report_normalized_events(
                    provider,
                    stream.normalize(raw_event),
                    environment=environment,
                    sender=sender,
                )
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
                diagnose(error)
    except OSError as error:
        diagnose(error)

    try:
        report_normalized_events(
            provider,
            stream.finish(),
            environment=environment,
            sender=sender,
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        diagnose(error)
    if failures > MAX_STREAM_DIAGNOSTICS:
        print(
            "agentserver-provider-hook: "
            f"{failures - MAX_STREAM_DIAGNOSTICS} additional JSONL errors suppressed",
            file=error_stream,
        )
    # A JSONL observer is commonly the last process in a shell pipeline. It
    # must drain input and exit zero so telemetry cannot mask or break upstream.
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.jsonl:
        return consume_provider_jsonl(
            arguments.provider,
            input_stream=sys.stdin.buffer,
            error_stream=sys.stderr,
        )
    try:
        encoded = sys.stdin.buffer.read(MAX_PROVIDER_EVENT_BYTES + 1)
        report_provider_event(
            arguments.provider,
            _decode_provider_event(encoded, label="hook input"),
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"agentserver-provider-hook: {error}", file=sys.stderr)
        # Telemetry must stay fail-open: exit 2 would *block* the provider's
        # main flow on blockable events (PreToolUse, Stop, UserPromptSubmit),
        # so plain non-zero is used for observer errors instead.
        return 1
    # Hook stdout can steer some providers, so successful telemetry is silent.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import BinaryIO, TextIO

from .cli import _send_bridge_request
from .provider_adapters import ADAPTERS
from .provider_hook import (
    MAX_PROVIDER_STREAM_EVENT_BYTES,
    MAX_STREAM_DIAGNOSTICS,
    BridgeSender,
    ProviderEventStream,
    _decode_bounded_provider_event,
    report_normalized_events,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentserver-provider-exec",
        description=(
            "Run a JSONL provider command while preserving stdout/exit status and "
            "reporting sanitized runtime facts."
        ),
    )
    parser.add_argument("--provider", choices=tuple(ADAPTERS), required=True)
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="provider command, normally separated from wrapper options by `--`",
    )
    return parser


def _shell_exit_status(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 + min(127, -returncode)


def _write_all(stream: BinaryIO, encoded: bytes) -> None:
    """Forward a provider chunk even when a raw stream performs short writes."""

    remaining = memoryview(encoded)
    while remaining:
        written = stream.write(remaining)
        if written is None:
            # Buffered binary streams may document ``None`` as would-block.
            # The CLI uses a blocking stdout, so treating it as an I/O failure
            # is safer than silently dropping provider output.
            raise OSError("provider output stream made no write progress")
        if written <= 0:
            raise OSError("provider output stream made no write progress")
        remaining = remaining[written:]
    stream.flush()


def run_provider_command(
    provider: str,
    command: Sequence[str],
    *,
    output_stream: BinaryIO,
    error_stream: TextIO,
    environment: Mapping[str, str] | None = None,
    sender: BridgeSender = _send_bridge_request,
) -> int:
    """Run one provider without allowing telemetry to affect its data plane."""

    if not command:
        raise ValueError("provider command is required after `--`")
    values = dict(os.environ if environment is None else environment)
    try:
        process = subprocess.Popen(
            list(command),
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=None,
            env=values,
        )
    except OSError as error:
        print(
            f"agentserver-provider-exec: cannot start provider: {error}",
            file=error_stream,
        )
        return 127
    provider_stdout = process.stdout
    if provider_stdout is None:
        process.kill()
        process.wait()
        raise RuntimeError("provider stdout pipe was not created")

    stream = ProviderEventStream(provider)
    failures = 0
    line_number = 0

    def diagnose(error: Exception | str) -> None:
        nonlocal failures
        failures += 1
        if failures <= MAX_STREAM_DIAGNOSTICS:
            print(f"agentserver-provider-exec: telemetry: {error}", file=error_stream)

    try:
        while True:
            encoded = provider_stdout.readline(MAX_PROVIDER_STREAM_EVENT_BYTES + 1)
            if not encoded:
                break
            line_number += 1
            _write_all(output_stream, encoded)
            if len(encoded) > MAX_PROVIDER_STREAM_EVENT_BYTES:
                while encoded and not encoded.endswith(b"\n"):
                    encoded = provider_stdout.readline(64 * 1024)
                    if encoded:
                        _write_all(output_stream, encoded)
                diagnose(
                    f"provider JSONL line {line_number} exceeds "
                    f"{MAX_PROVIDER_STREAM_EVENT_BYTES // (1024 * 1024)} MiB"
                )
                continue
            if not encoded.strip():
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
                    environment=values,
                    sender=sender,
                )
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
                diagnose(error)
    except BrokenPipeError:
        # The wrapper mirrors normal CLI pipe behavior: if its real output
        # consumer goes away, close the provider pipe instead of hanging.
        provider_stdout.close()
    finally:
        try:
            provider_stdout.close()
        finally:
            returncode = process.wait()

    try:
        report_normalized_events(
            provider,
            stream.finish(exit_code=returncode, force=True),
            environment=values,
            sender=sender,
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        diagnose(error)
    if failures > MAX_STREAM_DIAGNOSTICS:
        print(
            "agentserver-provider-exec: telemetry: "
            f"{failures - MAX_STREAM_DIAGNOSTICS} additional errors suppressed",
            file=error_stream,
        )
    return _shell_exit_status(returncode)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    command = list(arguments.command)
    if command and command[0] == "--":
        command.pop(0)
    try:
        return run_provider_command(
            arguments.provider,
            command,
            output_stream=sys.stdout.buffer,
            error_stream=sys.stderr,
        )
    except ValueError as error:
        print(f"agentserver-provider-exec: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

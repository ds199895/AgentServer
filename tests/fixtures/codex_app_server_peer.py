#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--transcript", type=Path, required=True)
    result.add_argument(
        "--scenario",
        choices=(
            "normal",
            "resume-missing",
            "resume-fatal",
            "invalid-initialize",
            "malformed-after-start",
            "eof-on-read",
            "timeout-on-read",
        ),
        default="normal",
    )
    result.add_argument(
        "--approval", choices=("none", "command", "file"), default="none"
    )
    result.add_argument("--user-input", action="store_true")
    result.add_argument(
        "--turn-status",
        choices=("completed", "failed", "interrupted", "hold"),
        default="completed",
    )
    result.add_argument("--burst", type=int, default=0)
    result.add_argument("--stderr-bytes", type=int, default=0)
    return result


class Peer:
    def __init__(self, arguments: argparse.Namespace) -> None:
        self.arguments = arguments
        self.thread_id = "thread-fixture"
        self.turn_number = 0
        self.pending: dict[str, tuple[str, str]] = {}

    def record(self, message: dict[str, Any]) -> None:
        self.arguments.transcript.parent.mkdir(parents=True, exist_ok=True)
        with self.arguments.transcript.open("a", encoding="utf-8") as output:
            output.write(json.dumps(message, separators=(",", ":")) + "\n")

    @staticmethod
    def send(message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def result(self, request: dict[str, Any], value: Any) -> None:
        self.send({"id": request["id"], "result": value})

    def error(self, request: dict[str, Any], code: int, message: str) -> None:
        self.send(
            {"id": request["id"], "error": {"code": code, "message": message}}
        )

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"method": method, "params": params})

    def request(self, request_id: str, method: str, params: dict[str, Any]) -> None:
        self.pending[request_id] = (method, str(params.get("turnId") or ""))
        self.send({"id": request_id, "method": method, "params": params})

    def thread_response(self, request: dict[str, Any]) -> None:
        params = request.get("params") or {}
        self.notify(
            "thread/started",
            {
                "thread": {
                    "id": self.thread_id,
                    "cwd": params.get("cwd"),
                    "model": params.get("model"),
                }
            },
        )
        self.result(
            request,
            {
                "thread": {"id": self.thread_id, "turns": []},
                "cwd": params.get("cwd"),
                "model": params.get("model"),
            },
        )
        if self.arguments.scenario == "malformed-after-start":
            sys.stdout.write("{this-is-not-json}\n")
            sys.stdout.flush()

    def interaction_params(self, turn_id: str) -> dict[str, Any]:
        return {
            "threadId": self.thread_id,
            "turnId": turn_id,
            "itemId": f"item-{turn_id}",
        }

    def emit_sensitive_lifecycle(self, turn_id: str) -> None:
        self.notify(
            "item/started",
            {
                "threadId": self.thread_id,
                "turnId": turn_id,
                "item": {
                    "id": "reasoning-item",
                    "type": "reasoning",
                    "status": "inProgress",
                    "text": "LEAK_HIDDEN_REASONING",
                },
            },
        )
        self.notify(
            "item/reasoning/textDelta",
            {
                "threadId": self.thread_id,
                "turnId": turn_id,
                "itemId": "reasoning-item",
                "delta": "LEAK_HIDDEN_REASONING_DELTA",
            },
        )
        self.notify(
            "item/reasoning/summaryTextDelta",
            {
                "threadId": self.thread_id,
                "turnId": turn_id,
                "itemId": "reasoning-item",
                "delta": "Reviewed the public interface.",
            },
        )
        self.notify(
            "turn/plan/updated",
            {
                "threadId": self.thread_id,
                "turnId": turn_id,
                "plan": [
                    {
                        "step": "Inspect the interface",
                        "status": "completed",
                        "private": "LEAK_PLAN_PRIVATE",
                    }
                ],
            },
        )
        self.notify(
            "item/started",
            {
                "threadId": self.thread_id,
                "turnId": turn_id,
                "item": {
                    "id": "command-item",
                    "type": "commandExecution",
                    "status": "inProgress",
                    "command": "LEAK_COMMAND",
                    "aggregatedOutput": "LEAK_OUTPUT",
                },
            },
        )
        self.notify(
            "item/commandExecution/outputDelta",
            {
                "threadId": self.thread_id,
                "turnId": turn_id,
                "itemId": "command-item",
                "delta": "LEAK_OUTPUT_DELTA",
            },
        )
        self.notify(
            "item/agentMessage/delta",
            {
                "threadId": self.thread_id,
                "turnId": turn_id,
                "itemId": "assistant-item",
                "delta": "LEAK_ASSISTANT_DELTA",
            },
        )
        self.notify(
            "rawResponseItem/completed",
            {"threadId": self.thread_id, "raw": "LEAK_RAW_PROVIDER_PAYLOAD"},
        )
        self.notify(
            "item/completed",
            {
                "threadId": self.thread_id,
                "turnId": turn_id,
                "item": {
                    "id": "command-item",
                    "type": "commandExecution",
                    "status": "completed",
                    "command": "LEAK_COMMAND",
                    "aggregatedOutput": "LEAK_OUTPUT",
                },
            },
        )

    def emit_burst(self, turn_id: str) -> None:
        for index in range(max(0, self.arguments.burst)):
            self.notify(
                "item/started",
                {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "item": {
                        "id": f"burst-{index}",
                        "type": "reasoning",
                        "status": "inProgress",
                        "text": "LEAK_BURST_REASONING",
                    },
                },
            )

    def open_approval(self, turn_id: str) -> None:
        request_id = f"srv-approval-{self.turn_number}"
        params = self.interaction_params(turn_id)
        if self.arguments.approval == "command":
            params.update(
                approvalId=f"approval-{self.turn_number}",
                command="LEAK_APPROVAL_COMMAND",
                reason="LEAK_APPROVAL_REASON",
            )
            method = "item/commandExecution/requestApproval"
        else:
            params.update(reason="LEAK_FILE_REASON", grantRoot="/private/secret")
            method = "item/fileChange/requestApproval"
        self.request(request_id, method, params)

    def open_user_input(self, turn_id: str) -> None:
        self.request(
            f"srv-user-input-{self.turn_number}",
            "item/tool/requestUserInput",
            {
                **self.interaction_params(turn_id),
                "questions": [
                    {
                        "id": "environment",
                        "header": "Environment",
                        "question": "Which environment should be used?",
                        "options": [
                            {
                                "label": "Staging",
                                "description": "Use the staging deployment.",
                            },
                            {
                                "label": "Production",
                                "description": "Use the production deployment.",
                            },
                        ],
                    }
                ],
            },
        )

    def finish_turn(self, turn_id: str) -> None:
        if self.arguments.turn_status == "hold":
            return
        self.notify(
            "turn/completed",
            {
                "threadId": self.thread_id,
                "turn": {
                    "id": turn_id,
                    "status": self.arguments.turn_status,
                    "error": {"message": "LEAK_TURN_ERROR"},
                },
            },
        )

    def start_turn(self, request: dict[str, Any]) -> None:
        self.turn_number += 1
        turn_id = f"turn-{self.turn_number}"
        self.notify(
            "turn/started",
            {
                "threadId": self.thread_id,
                "turn": {"id": turn_id, "status": "inProgress"},
            },
        )
        self.emit_sensitive_lifecycle(turn_id)
        self.emit_burst(turn_id)
        if self.arguments.approval != "none":
            self.open_approval(turn_id)
        elif self.arguments.user_input:
            self.open_user_input(turn_id)
        # Deliberately respond after the server-initiated request. A client
        # whose reader awaits the approval handler will deadlock here.
        self.result(request, {"turn": {"id": turn_id, "status": "inProgress"}})
        if self.arguments.approval == "none" and not self.arguments.user_input:
            self.finish_turn(turn_id)

    def handle_response(self, message: dict[str, Any]) -> None:
        request_id = str(message["id"])
        pending = self.pending.pop(request_id, None)
        if pending is None:
            return
        method, turn_id = pending
        if method.endswith("requestApproval") and self.arguments.user_input:
            self.open_user_input(turn_id)
            return
        self.finish_turn(turn_id)

    def handle_request(self, message: dict[str, Any]) -> bool:
        method = str(message.get("method") or "")
        if method == "initialize":
            if self.arguments.scenario == "invalid-initialize":
                self.result(message, {"userAgent": "fixture/1"})
            else:
                self.result(
                    message,
                    {
                        "userAgent": "codex-fixture/1.0",
                        "platformFamily": "unix",
                        "platformOs": "linux",
                    },
                )
        elif method == "initialized" and "id" not in message:
            pass
        elif method == "thread/resume":
            if self.arguments.scenario == "resume-missing":
                self.error(message, -32000, "Thread not found in rollout store")
            elif self.arguments.scenario == "resume-fatal":
                self.error(message, -32003, "Thread permission denied")
            else:
                self.thread_response(message)
        elif method == "thread/start":
            self.thread_response(message)
        elif method == "turn/start":
            self.start_turn(message)
        elif method == "turn/interrupt":
            self.result(message, {})
            params = message.get("params") or {}
            self.notify(
                "turn/completed",
                {
                    "threadId": self.thread_id,
                    "turn": {
                        "id": params.get("turnId"),
                        "status": "interrupted",
                    },
                },
            )
        elif method in {"thread/read", "thread/rollback"}:
            if method == "thread/read" and self.arguments.scenario == "eof-on-read":
                return False
            if (
                method == "thread/read"
                and self.arguments.scenario == "timeout-on-read"
            ):
                return True
            self.result(
                message,
                {
                    "thread": {
                        "id": self.thread_id,
                        "turns": [
                            {
                                "id": "snapshot-turn",
                                "items": [
                                    {
                                        "id": "snapshot-item",
                                        "type": "commandExecution",
                                        "status": "completed",
                                        "command": "LEAK_SNAPSHOT_COMMAND",
                                        "aggregatedOutput": "LEAK_SNAPSHOT_OUTPUT",
                                    }
                                ],
                            }
                        ],
                    }
                },
            )
        else:
            self.error(message, -32601, "Method not found")
        return True

    def run(self) -> int:
        if self.arguments.stderr_bytes > 0:
            remaining = self.arguments.stderr_bytes
            chunk = "x" * min(16 * 1024, remaining)
            while remaining:
                value = chunk[:remaining]
                sys.stderr.write(value)
                sys.stderr.flush()
                remaining -= len(value)
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                return 2
            if not isinstance(message, dict):
                return 2
            self.record(message)
            if "method" in message:
                if not self.handle_request(message):
                    return 0
            elif "id" in message:
                self.handle_response(message)
        return 0


def main() -> int:
    arguments = parser().parse_args()
    arguments.transcript.write_text("", encoding="utf-8")
    return Peer(arguments).run()


if __name__ == "__main__":
    raise SystemExit(main())

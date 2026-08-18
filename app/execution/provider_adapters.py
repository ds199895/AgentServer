from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from .models import RunActivity, SpanLifecycle, WaitReason


_MACHINE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_PROCESS_TRANSPORT_REFERENCE_KEY = os.urandom(32)
_RUN_ACTIVITIES = frozenset(item.value for item in RunActivity)
_WAIT_REASONS = frozenset(item.value for item in WaitReason)
_SPAN_OUTCOMES = frozenset(
    item.value for item in SpanLifecycle if item is not SpanLifecycle.RUNNING
)
_PROVIDER_KINDS = frozenset({"generic", "codex", "claude", "kimi", "deepseek"})
_STOP_REASONS = frozenset(
    {"clear", "completed", "failed", "logout", "other", "prompt_input_exit", "shutdown"}
)
_CANCEL_REASONS = frozenset(
    {
        "dependency_cancelled",
        "server_requested",
        "shutdown",
        "timeout",
        "unknown",
        "user_requested",
    }
)
_FAILURE_CODES = frozenset(
    {
        "agent_failed",
        "authentication_failed",
        "codex_runtime_error",
        "provider_error",
        "rate_limited",
        "resource_exhausted",
        "runtime_error",
        "timeout",
        "tool_failed",
        "unknown",
    }
)
_TOOL_KIND_ALIASES = {
    "agent": "subagent",
    "agentswarm": "subagent",
    "apply_patch": "file_change",
    "bash": "command_execution",
    "command": "command_execution",
    "command_execution": "command_execution",
    "edit": "file_change",
    "fetchurl": "web_search",
    "file_change": "file_change",
    "glob": "file_read",
    "grep": "file_read",
    "mcp_tool_call": "mcp_tool_call",
    "read": "file_read",
    "shell": "command_execution",
    "spawn_agent": "subagent",
    "task": "subagent",
    "web_fetch": "web_search",
    "web_search": "web_search",
    "webfetch": "web_search",
    "websearch": "web_search",
    "write": "file_change",
}
_ARTIFACT_KINDS = frozenset({"file", "image", "log", "report"})
_DELEGATION_PHASES = frozenset({"started", "completed", "failed", "cancelled"})
_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}$"
)
_UNTYPED_EVENT_TYPES = frozenset(
    {
        "agent.registered",
        "agent.stopping",
        "run.started",
        "run.activity.changed",
        "run.progress.updated",
        "run.input.requested",
        "span.started",
        "span.updated",
        "span.ended",
        "child_run.requested",
        "child_run.observed",
        "artifact.published",
        "run.succeeded",
        "run.failed",
        "run.cancelled",
    }
)


def _identifier(value: object, field: str, *, required: bool = True) -> str | None:
    if value is None or value == "":
        if required:
            raise ValueError(f"provider {field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"provider {field} must be a string machine identifier")
    result = value
    if not _MACHINE_IDENTIFIER.fullmatch(result):
        raise ValueError(f"provider {field} must be a machine identifier")
    return result


def _known_code(
    value: object,
    field: str,
    allowed: frozenset[str],
    *,
    default: str | None = None,
) -> str:
    result = str(value or default or "").strip().lower()
    if result not in allowed:
        raise ValueError(f"provider {field} is not a supported machine code")
    return result


def _provider_reference(
    namespace: str,
    value: object,
    field: str,
    *,
    reference_key: bytes | None = None,
) -> str | None:
    """Remove a raw provider ID while preserving local transport correlation.

    Managed hooks use a launch-domain HMAC; the control Broker rekeys it with a
    private owner/terminal/launch key before persistence. This transport value is
    not an external anonymity boundary, so exports apply separate keyed aliases.
    """

    raw = _identifier(value, field, required=False)
    if raw is None:
        return None
    if reference_key is not None and (
        not isinstance(reference_key, bytes) or len(reference_key) < 32
    ):
        raise ValueError("provider reference_key must be at least 32 bytes")
    if reference_key is None:
        launch_domain = os.getenv("AGENTSERVER_LAUNCH_ID", "").strip()
        reference_key = (
            hashlib.sha256(
                f"agentserver-provider-transport-v1\0{launch_domain}".encode("utf-8")
            ).digest()
            if launch_domain
            else _PROCESS_TRANSPORT_REFERENCE_KEY
        )
    message = f"agentserver-provider-ref-v1\0{namespace}\0{raw}".encode("utf-8")
    digest = hmac.new(reference_key, message, hashlib.sha256).hexdigest()[:32]
    return f"provider-{namespace}-{digest}"


def _tool_kind(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized.startswith("mcp__"):
        # Kimi/Claude expose MCP tools as mcp__<server>__<tool>.
        return "mcp_tool_call"
    return _TOOL_KIND_ALIASES.get(normalized, "other_tool")


def _progress_payload(payload: Mapping[str, Any]) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    if "progress" in payload:
        progress = payload["progress"]
        if (
            not isinstance(progress, (int, float))
            or isinstance(progress, bool)
            or not math.isfinite(progress)
            or not 0 <= progress <= 1
        ):
            raise ValueError("provider progress must be between 0 and 1")
        result["progress"] = float(progress)
    if "current" in payload or "total" in payload:
        current = payload.get("current")
        total = payload.get("total")
        if (
            not isinstance(current, (int, float))
            or isinstance(current, bool)
            or not math.isfinite(current)
            or not isinstance(total, (int, float))
            or isinstance(total, bool)
            or not math.isfinite(total)
            or current < 0
            or total <= 0
            or current > total
        ):
            raise ValueError("provider current/total progress is invalid")
        result.update(current=current, total=total, progress=current / total)
    if not result:
        raise ValueError("provider progress event requires numeric progress")
    return result


def _artifact_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    path = payload.get("path")
    if not isinstance(path, str) or not path or len(path) > 4_096 or "\x00" in path:
        raise ValueError("provider artifact path is invalid")
    normalized = path.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError("provider artifact path must be workspace-relative")
    parts = tuple(part for part in posix.parts if part not in {"", "."})
    if not parts or ".." in parts:
        raise ValueError("provider artifact path must be workspace-relative")
    result: dict[str, Any] = {
        "path": "/".join(parts),
        "kind": _known_code(
            payload.get("kind"), "artifact kind", _ARTIFACT_KINDS, default="file"
        ),
    }
    media_type = payload.get("media_type")
    if media_type is not None and media_type != "":
        if not isinstance(media_type, str):
            raise ValueError("provider artifact media_type must be a string")
        normalized_media_type = media_type.strip().lower()
        if len(normalized_media_type) > 192 or not _MEDIA_TYPE.fullmatch(
            normalized_media_type
        ):
            raise ValueError("provider artifact media_type is invalid")
        result["media_type"] = normalized_media_type
    return result


def sanitize_runtime_payload(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    provider_kind: str,
    reference_key: bytes | None = None,
) -> dict[str, Any]:
    """Rebuild a runtime event from its event-specific public contract.

    ``reference_key`` is required at a persistence boundary. Without it, the
    deterministic references are transport-only correlation values and are not
    an anonymity boundary.
    """

    if event_type not in _UNTYPED_EVENT_TYPES:
        raise ValueError("unsupported untyped provider event type")
    if event_type == "agent.registered":
        return {"kind": provider_kind if provider_kind in _PROVIDER_KINDS else "generic"}
    if event_type == "agent.stopping":
        return {
            "reason": _known_code(
                payload.get("reason"), "reason", _STOP_REASONS, default="other"
            )
        }
    if event_type == "run.started":
        return {
            "activity": _known_code(
                payload.get("activity"), "activity", _RUN_ACTIVITIES, default="unknown"
            )
        }
    if event_type == "run.activity.changed":
        activity = _known_code(payload.get("activity"), "activity", _RUN_ACTIVITIES)
        result: dict[str, Any] = {"activity": activity}
        if activity == RunActivity.WAITING.value:
            reason = _known_code(payload.get("wait_reason"), "wait_reason", _WAIT_REASONS)
            result["wait_reason"] = reason
            target = _identifier(
                payload.get("wait_target_run_id"),
                "wait_target_run_id",
                required=False,
            )
            if reason == WaitReason.CHILD_RUN.value and target is None:
                raise ValueError("child_run waiting requires wait_target_run_id")
            if reason == WaitReason.CHILD_RUN.value and target is not None:
                result["wait_target_run_id"] = target
        return result
    if event_type == "run.progress.updated":
        return _progress_payload(payload)
    if event_type == "artifact.published":
        return _artifact_payload(payload)
    if event_type == "run.input.requested":
        return {
            "wait_reason": _known_code(
                payload.get("wait_reason"),
                "wait_reason",
                _WAIT_REASONS,
                default=WaitReason.USER_INPUT.value,
            )
        }
    if event_type in {"span.started", "span.updated", "span.ended"}:
        span_id = _provider_reference(
            "span",
            payload.get("span_id"),
            "span_id",
            reference_key=reference_key,
        )
        if span_id is None:
            raise ValueError("provider span_id is required")
        result = {"span_id": span_id}
        if event_type == "span.updated":
            result.update(_progress_payload(payload))
            return result
        result.update({"name": _tool_kind(payload.get("name")), "kind": "tool"})
        if event_type == "span.ended":
            result["outcome"] = _known_code(
                payload.get("outcome"), "outcome", _SPAN_OUTCOMES
            )
        return result
    if event_type == "child_run.requested":
        delegation_id = _provider_reference(
            "delegation",
            payload.get("delegation_id"),
            "delegation_id",
            reference_key=reference_key,
        )
        if delegation_id is None:
            raise ValueError("provider delegation_id is required")
        return {
            "delegation_id": delegation_id,
            "agent_kind": _known_code(
                payload.get("agent_kind"),
                "agent_kind",
                _PROVIDER_KINDS,
                default=provider_kind if provider_kind in _PROVIDER_KINDS else "generic",
            ),
            "title": "Observed provider delegation",
        }
    if event_type == "child_run.observed":
        return {
            "agent_kind": _known_code(
                payload.get("agent_kind"),
                "agent_kind",
                _PROVIDER_KINDS,
                default=provider_kind if provider_kind in _PROVIDER_KINDS else "generic",
            ),
            "phase": _known_code(
                payload.get("phase"), "delegation phase", _DELEGATION_PHASES
            ),
        }
    if event_type == "run.failed":
        return {
            "code": _known_code(
                payload.get("code"), "code", _FAILURE_CODES, default="provider_error"
            )
        }
    if event_type == "run.cancelled":
        return {
            "reason": _known_code(
                payload.get("reason"), "reason", _CANCEL_REASONS, default="unknown"
            )
        }
    # run.succeeded carries no provider text. Summaries remain an explicitly
    # authenticated active-reporter concern, not an untyped adapter field.
    return {}


@dataclass(frozen=True)
class NormalizedRuntimeEvent:
    """A public, provider-neutral runtime fact.

    Adapters intentionally discard prompts, tool arguments, tool output and
    assistant text.  Those values can contain secrets or hidden reasoning and
    are not required to render a lifecycle/activity projection.
    """

    type: str
    payload: Mapping[str, Any] = field(default_factory=dict)


class ProviderAdapter:
    """Normalize a provider event without treating transcripts as an API."""

    kind = "generic"
    capabilities = frozenset(
        {"phase_reporting", "progress_reporting", "artifact_reporting"}
    )

    def normalize_many(
        self, event: Mapping[str, Any]
    ) -> tuple[NormalizedRuntimeEvent, ...]:
        event_type = str(event.get("type") or "").strip()
        if not event_type:
            raise ValueError("provider event type is required")
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            raise ValueError("provider payload must be an object")
        return (
            NormalizedRuntimeEvent(
                event_type,
                sanitize_runtime_payload(
                    event_type, payload, provider_kind=self.kind
                ),
            ),
        )

    def normalize(self, event: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Backward-compatible single-event boundary."""
        values = self.normalize_many(event)
        if len(values) != 1:
            raise ValueError("provider event expands to multiple runtime events")
        return values[0].type, dict(values[0].payload)


def _tool_activity(tool_name: str, tool_input: object = None) -> str:
    kind = _tool_kind(tool_name)
    if kind == "file_change":
        return "coding"
    if kind == "subagent":
        return "waiting"
    # Do not inspect or persist the command itself.  A small verb-only check is
    # intentionally avoided because even command text can contain credentials.
    _ = tool_input
    return "tooling"


def _tool_outcome(response: object) -> str:
    if not isinstance(response, Mapping) or not response:
        return "failed"
    for key in ("isError", "is_error", "error"):
        if response.get(key):
            return "failed"
    status = str(response.get("status") or "").lower()
    if status in {"error", "failed", "failure"}:
        return "failed"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status and status not in {"complete", "completed", "ok", "success", "succeeded"}:
        return "failed"
    if "exit_code" in response:
        exit_code = response.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            return "failed"
        return "succeeded" if exit_code == 0 else "failed"
    return "succeeded" if status else "failed"


class CodexAdapter(ProviderAdapter):
    """Normalize stable Codex Hook and ``codex exec --json`` events.

    Hook fields follow the documented command-hook contract.  JSONL events use
    only the documented top-level and item type/status fields; rollout
    transcripts are deliberately unsupported because their format is not a
    stable interface.
    """

    kind = "codex"
    capabilities = ProviderAdapter.capabilities | {
        "tool_events",
        "delegation_observation",
        "cancel",
        "native_hooks",
        "jsonl",
    }

    def normalize_many(
        self, event: Mapping[str, Any]
    ) -> tuple[NormalizedRuntimeEvent, ...]:
        hook_name = str(event.get("hook_event_name") or "")
        if hook_name:
            return self._normalize_hook(hook_name, event)
        return self._normalize_jsonl(event)

    def _normalize_hook(
        self, hook_name: str, event: Mapping[str, Any]
    ) -> tuple[NormalizedRuntimeEvent, ...]:
        metadata: dict[str, Any] = {}
        if hook_name == "SessionStart":
            return (
                NormalizedRuntimeEvent(
                    "agent.registered",
                    {
                        **metadata,
                        "kind": self.kind,
                        "source": "codex_hook",
                    },
                ),
            )
        if hook_name == "SessionEnd":
            reason = str(event.get("reason") or "other").strip().lower()
            if reason not in _STOP_REASONS:
                reason = "other"
            return (
                NormalizedRuntimeEvent(
                    "agent.stopping",
                    {**metadata, "reason": reason},
                ),
            )
        if hook_name == "UserPromptSubmit":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                ),
            )
        if hook_name == "PermissionRequest":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {
                        **metadata,
                        "activity": "waiting",
                        "wait_reason": "approval",
                    },
                ),
            )
        if hook_name == "PreToolUse":
            raw_tool_name = str(event.get("tool_name") or "")
            tool_name = _tool_kind(raw_tool_name)
            tool_id = _provider_reference(
                "tool", event.get("tool_use_id"), "tool_use_id"
            )
            activity = _tool_activity(raw_tool_name, event.get("tool_input"))
            activity_payload: dict[str, Any] = {
                **metadata,
                "activity": activity,
            }
            if activity == "waiting":
                # There is no canonical child Run yet; do not invent a target
                # identity.  The child_run.requested event below is the durable
                # delegation handshake input.
                activity_payload["activity"] = "tooling"
            values = [NormalizedRuntimeEvent("run.activity.changed", activity_payload)]
            if tool_id:
                values.append(
                    NormalizedRuntimeEvent(
                        "span.started",
                        {
                            **metadata,
                            "span_id": tool_id,
                            "name": tool_name,
                            "kind": "tool",
                        },
                    )
                )
            return tuple(values)
        if hook_name == "PostToolUse":
            tool_name = _tool_kind(event.get("tool_name"))
            tool_id = _provider_reference(
                "tool", event.get("tool_use_id"), "tool_use_id"
            )
            values: list[NormalizedRuntimeEvent] = []
            if tool_id:
                values.append(
                    NormalizedRuntimeEvent(
                        "span.ended",
                        {
                            **metadata,
                            "span_id": tool_id,
                            "name": tool_name,
                            "outcome": _tool_outcome(event.get("tool_response")),
                        },
                    )
                )
            values.append(
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                )
            )
            return tuple(values)
        if hook_name == "SubagentStart":
            delegation_id = _provider_reference(
                "delegation", event.get("agent_id"), "agent_id"
            )
            if delegation_id is None:
                raise ValueError("Codex SubagentStart requires agent_id")
            agent_kind = str(event.get("agent_type") or "codex").strip().lower()
            if agent_kind not in _PROVIDER_KINDS:
                agent_kind = "codex"
            return (
                NormalizedRuntimeEvent(
                    "child_run.requested",
                    {
                        **metadata,
                        "delegation_id": delegation_id,
                        "agent_kind": agent_kind,
                        "title": "Observed Codex delegation",
                    },
                ),
            )
        if hook_name == "SubagentStop":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                ),
            )
        if hook_name == "PreCompact":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "planning"},
                ),
            )
        if hook_name == "PostCompact":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                ),
            )
        if hook_name == "Stop":
            # Stop is a turn boundary, not proof that the delegated Task has
            # succeeded.  Keep result authority with explicit completion.
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "finalizing"},
                ),
            )
        raise ValueError("unsupported Codex hook event")

    def _normalize_jsonl(
        self, event: Mapping[str, Any]
    ) -> tuple[NormalizedRuntimeEvent, ...]:
        event_type = str(event.get("type") or "")
        metadata: dict[str, Any] = {}
        if event_type == "thread.started":
            return (
                NormalizedRuntimeEvent(
                    "agent.registered",
                    {**metadata, "kind": self.kind, "source": "codex_jsonl"},
                ),
            )
        if event_type == "turn.started":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed", {**metadata, "activity": "thinking"}
                ),
            )
        if event_type in {"turn.completed"}:
            # A Codex turn can belong to a provider subagent sharing the parent
            # terminal.  It is not proof that the AgentServer Task succeeded.
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "finalizing", "provider_status": "completed"},
                ),
            )
        if event_type in {"turn.failed", "error"}:
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {
                        **metadata,
                        "activity": "finalizing",
                        "provider_status": "failed",
                        "code": "codex_runtime_error",
                    },
                ),
            )
        if event_type.startswith("item."):
            item = event.get("item")
            if not isinstance(item, Mapping):
                raise ValueError("Codex item event requires an item object")
            item_id = _provider_reference("item", item.get("id"), "item.id")
            item_type = str(item.get("type") or "unknown").strip().lower()
            activity = {
                "reasoning": "thinking",
                "plan": "planning",
                "file_change": "coding",
                "command_execution": "tooling",
                "mcp_tool_call": "tooling",
                "web_search": "tooling",
            }.get(item_type, "thinking")
            if event_type == "item.started":
                values = [
                    NormalizedRuntimeEvent(
                        "run.activity.changed",
                        {**metadata, "activity": activity},
                    )
                ]
                if item_id and item_type in {
                    "file_change",
                    "command_execution",
                    "mcp_tool_call",
                    "web_search",
                }:
                    values.append(
                        NormalizedRuntimeEvent(
                            "span.started",
                            {
                                **metadata,
                                "span_id": item_id,
                                "name": item_type,
                                "kind": "tool",
                            },
                        )
                    )
                return tuple(values)
            if event_type == "item.completed" and item_id and item_type in {
                "file_change",
                "command_execution",
                "mcp_tool_call",
                "web_search",
            }:
                status = str(item.get("status") or "").strip().lower()
                if status in {"failed", "error", "failure"}:
                    outcome = "failed"
                elif status in {"cancelled", "canceled"}:
                    outcome = "cancelled"
                elif status in {"complete", "completed", "ok", "success", "succeeded"}:
                    outcome = "succeeded"
                else:
                    raise ValueError("unsupported Codex item completion status")
                return (
                    NormalizedRuntimeEvent(
                        "span.ended",
                        {
                            **metadata,
                            "span_id": item_id,
                            "name": item_type,
                            "outcome": outcome,
                        },
                    ),
                    NormalizedRuntimeEvent(
                        "run.activity.changed",
                        {**metadata, "activity": "thinking"},
                    ),
                )
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                ),
            )
        raise ValueError("unsupported Codex JSONL event")


class ClaudeAdapter(ProviderAdapter):
    """Normalize Claude Code CLI hook and ``--output-format stream-json`` events.

    Hook payloads follow the documented ``hook_event_name`` stdin contract;
    tool calls are keyed by ``tool_use_id``. ``claude -p --output-format
    stream-json`` lines use the Messages-API envelope
    (``system``/``assistant``/``user``/``result``); tool calls are keyed by
    the ``tool_use``/``tool_result`` content block id. Prompt text, tool
    input/output, assistant text and transcripts are dropped; only machine
    identifiers are kept, and only as hashed transport references.
    """

    kind = "claude"
    capabilities = ProviderAdapter.capabilities | {
        "tool_events",
        "delegation_observation",
        "cancel",
        "native_hooks",
        "jsonl",
    }

    def normalize_many(
        self, event: Mapping[str, Any]
    ) -> tuple[NormalizedRuntimeEvent, ...]:
        hook_name = str(event.get("hook_event_name") or "")
        if hook_name:
            return self._normalize_hook(hook_name, event)
        event_type = str(event.get("type") or "")
        if event_type:
            return self._normalize_jsonl(event_type, event)
        raise ValueError("unsupported Claude event")

    def _normalize_hook(
        self, hook_name: str, event: Mapping[str, Any]
    ) -> tuple[NormalizedRuntimeEvent, ...]:
        metadata: dict[str, Any] = {}
        if hook_name == "SessionStart":
            return (
                NormalizedRuntimeEvent(
                    "agent.registered",
                    {**metadata, "kind": self.kind, "source": "claude_hook"},
                ),
            )
        if hook_name == "SessionEnd":
            reason = str(event.get("reason") or "other").strip().lower()
            if reason not in _STOP_REASONS:
                reason = "other"
            return (
                NormalizedRuntimeEvent(
                    "agent.stopping",
                    {**metadata, "reason": reason},
                ),
            )
        if hook_name == "UserPromptSubmit":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                ),
            )
        if hook_name == "Notification":
            # Permission/idle/elicitation pings duplicate the PreToolUse,
            # PermissionRequest and Stop facts below; no lifecycle fact here.
            return ()
        if hook_name == "PermissionRequest":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {
                        **metadata,
                        "activity": "waiting",
                        "wait_reason": "approval",
                    },
                ),
            )
        if hook_name == "PreToolUse":
            raw_tool_name = str(event.get("tool_name") or "")
            tool_name = _tool_kind(raw_tool_name)
            tool_id = _provider_reference(
                "tool", event.get("tool_use_id"), "tool_use_id"
            )
            activity = _tool_activity(raw_tool_name, event.get("tool_input"))
            activity_payload: dict[str, Any] = {**metadata, "activity": activity}
            if activity == "waiting":
                # There is no canonical child Run yet; SubagentStart below is
                # the durable delegation handshake input.
                activity_payload["activity"] = "tooling"
            values = [NormalizedRuntimeEvent("run.activity.changed", activity_payload)]
            if tool_id:
                values.append(
                    NormalizedRuntimeEvent(
                        "span.started",
                        {
                            **metadata,
                            "span_id": tool_id,
                            "name": tool_name,
                            "kind": "tool",
                        },
                    )
                )
            return tuple(values)
        if hook_name in {"PostToolUse", "PostToolUseFailure"}:
            tool_name = _tool_kind(event.get("tool_name"))
            tool_id = _provider_reference(
                "tool", event.get("tool_use_id"), "tool_use_id"
            )
            # PostToolUse only fires for a successful call and
            # PostToolUseFailure only for a failed one, so the outcome is
            # decided by the event name rather than free-text tool_output.
            outcome = "succeeded" if hook_name == "PostToolUse" else "failed"
            values: list[NormalizedRuntimeEvent] = []
            if tool_id:
                values.append(
                    NormalizedRuntimeEvent(
                        "span.ended",
                        {
                            **metadata,
                            "span_id": tool_id,
                            "name": tool_name,
                            "outcome": outcome,
                        },
                    )
                )
            values.append(
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                )
            )
            return tuple(values)
        if hook_name == "PermissionDenied":
            # Auto mode declined the pending call; the span PreToolUse opened
            # never runs, so close it instead of leaving it open forever.
            tool_id = _provider_reference(
                "tool", event.get("tool_use_id"), "tool_use_id"
            )
            values = []
            if tool_id:
                values.append(
                    NormalizedRuntimeEvent(
                        "span.ended",
                        {
                            **metadata,
                            "span_id": tool_id,
                            "name": _tool_kind(event.get("tool_name")),
                            "outcome": "cancelled",
                        },
                    )
                )
            values.append(
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                )
            )
            return tuple(values)
        if hook_name == "SubagentStart":
            delegation_id = _provider_reference(
                "delegation", event.get("agent_id"), "agent_id"
            )
            if delegation_id is None:
                raise ValueError("Claude SubagentStart requires agent_id")
            return (
                NormalizedRuntimeEvent(
                    "child_run.requested",
                    {
                        **metadata,
                        "delegation_id": delegation_id,
                        "agent_kind": self.kind,
                        "title": "Observed Claude delegation",
                    },
                ),
            )
        if hook_name == "SubagentStop":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                ),
            )
        if hook_name == "PreCompact":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "planning"},
                ),
            )
        if hook_name == "PostCompact":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                ),
            )
        if hook_name == "Stop":
            # Stop is a turn boundary, not proof that the delegated Task has
            # succeeded.  Keep result authority with explicit completion.
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "finalizing"},
                ),
            )
        if hook_name == "StopFailure":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {
                        **metadata,
                        "activity": "finalizing",
                        "provider_status": "failed",
                        "code": "provider_error",
                    },
                ),
            )
        raise ValueError("unsupported Claude hook event")

    def _normalize_jsonl(
        self, event_type: str, event: Mapping[str, Any]
    ) -> tuple[NormalizedRuntimeEvent, ...]:
        metadata: dict[str, Any] = {}
        if event_type == "system":
            subtype = str(event.get("subtype") or "").strip().lower()
            if subtype == "init":
                return (
                    NormalizedRuntimeEvent(
                        "agent.registered",
                        {**metadata, "kind": self.kind, "source": "claude_jsonl"},
                    ),
                )
            if subtype == "compact_boundary":
                return (
                    NormalizedRuntimeEvent(
                        "run.activity.changed",
                        {**metadata, "activity": "planning"},
                    ),
                )
            # Other system announcements carry no lifecycle fact.
            return ()
        if event_type == "stream_event":
            # Partial text/usage deltas duplicate the full assistant/user
            # messages handled below; they add no new lifecycle fact.
            return ()
        if event_type == "assistant":
            message = event.get("message")
            if not isinstance(message, Mapping):
                raise ValueError("Claude assistant event requires a message object")
            content = message.get("content")
            if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
                raise ValueError("Claude assistant message content must be a list")
            tool_uses = [
                block
                for block in content
                if isinstance(block, Mapping) and block.get("type") == "tool_use"
            ]
            if not tool_uses:
                return (
                    NormalizedRuntimeEvent(
                        "run.activity.changed",
                        {**metadata, "activity": "thinking"},
                    ),
                )
            values: list[NormalizedRuntimeEvent] = []
            for block in tool_uses:
                raw_name = str(block.get("name") or "")
                activity = _tool_activity(raw_name, block.get("input"))
                if activity == "waiting":
                    activity = "tooling"
                values.append(
                    NormalizedRuntimeEvent(
                        "run.activity.changed", {**metadata, "activity": activity}
                    )
                )
                span_id = _provider_reference("tool", block.get("id"), "content.id")
                if span_id:
                    values.append(
                        NormalizedRuntimeEvent(
                            "span.started",
                            {
                                **metadata,
                                "span_id": span_id,
                                "name": _tool_kind(raw_name),
                                "kind": "tool",
                            },
                        )
                    )
            return tuple(values)
        if event_type == "user":
            message = event.get("message")
            if not isinstance(message, Mapping):
                raise ValueError("Claude user event requires a message object")
            content = message.get("content")
            if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
                raise ValueError("Claude user message content must be a list")
            tool_results = [
                block
                for block in content
                if isinstance(block, Mapping) and block.get("type") == "tool_result"
            ]
            values = []
            for block in tool_results:
                # Tool result blocks carry no tool name; the span id
                # correlates back to the assistant tool_use block.
                span_id = _provider_reference(
                    "tool", block.get("tool_use_id"), "content.tool_use_id"
                )
                if span_id:
                    values.append(
                        NormalizedRuntimeEvent(
                            "span.ended",
                            {
                                **metadata,
                                "span_id": span_id,
                                "name": "other_tool",
                                "outcome": "failed"
                                if block.get("is_error")
                                else "succeeded",
                            },
                        )
                    )
            values.append(
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                )
            )
            return tuple(values)
        if event_type == "result":
            # A Claude turn can belong to a provider subagent sharing the
            # parent terminal.  It is not proof that the AgentServer Task
            # succeeded.
            is_error = bool(event.get("is_error"))
            payload: dict[str, Any] = {
                **metadata,
                "activity": "finalizing",
                "provider_status": "failed" if is_error else "completed",
            }
            if is_error:
                payload["code"] = "provider_error"
            return (NormalizedRuntimeEvent("run.activity.changed", payload),)
        raise ValueError("unsupported Claude JSONL event")


class KimiAdapter(ProviderAdapter):
    """Normalize Kimi Code CLI hook and stream-json events.

    Hook payloads follow the documented ``[[hooks]]`` stdin contract
    (``hook_event_name`` plus event-specific fields; tool calls are keyed by
    ``tool_call_id``).  ``kimi -p --output-format stream-json`` lines use the
    documented ``role`` / ``tool_calls`` message shapes.  Prompt text, tool
    input/output, error messages and subagent responses are dropped; only
    machine identifiers are kept, and only as hashed transport references.
    """

    kind = "kimi"
    capabilities = ProviderAdapter.capabilities | {
        "tool_events",
        "delegation_observation",
        "cancel",
        "native_hooks",
        "jsonl",
    }

    def normalize_many(
        self, event: Mapping[str, Any]
    ) -> tuple[NormalizedRuntimeEvent, ...]:
        hook_name = str(event.get("hook_event_name") or "")
        if hook_name:
            return self._normalize_hook(hook_name, event)
        role = str(event.get("role") or "")
        if role:
            return self._normalize_jsonl(role, event)
        raise ValueError("unsupported Kimi event")

    def _normalize_hook(
        self, hook_name: str, event: Mapping[str, Any]
    ) -> tuple[NormalizedRuntimeEvent, ...]:
        metadata: dict[str, Any] = {}
        if hook_name == "SessionStart":
            return (
                NormalizedRuntimeEvent(
                    "agent.registered",
                    {**metadata, "kind": self.kind, "source": "kimi_hook"},
                ),
            )
        if hook_name == "SessionEnd":
            reason = str(event.get("reason") or "other").strip().lower()
            reason = {"exit": "shutdown", "archive": "other"}.get(reason, reason)
            if reason not in _STOP_REASONS:
                reason = "other"
            return (
                NormalizedRuntimeEvent(
                    "agent.stopping",
                    {**metadata, "reason": reason},
                ),
            )
        if hook_name in {"UserPromptSubmit", "TurnStarted"}:
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                ),
            )
        if hook_name in {"UserPromptQueued", "SessionHeartbeat", "Notification"}:
            # Pure observation pings: the run is busy or nothing changed, so
            # there is no lifecycle fact worth a bridge request.
            return ()
        if hook_name == "PermissionRequest":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {
                        **metadata,
                        "activity": "waiting",
                        "wait_reason": "approval",
                    },
                ),
            )
        if hook_name == "PermissionResult":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                ),
            )
        if hook_name == "PreToolUse":
            raw_tool_name = str(event.get("tool_name") or "")
            tool_name = _tool_kind(raw_tool_name)
            tool_id = _provider_reference(
                "tool",
                event.get("tool_call_id") or event.get("tool_use_id"),
                "tool_call_id",
            )
            activity = _tool_activity(raw_tool_name, event.get("tool_input"))
            activity_payload: dict[str, Any] = {**metadata, "activity": activity}
            if activity == "waiting":
                # Subagent tools share the parent terminal; there is no
                # canonical child Run yet, so do not report a waiting target.
                activity_payload["activity"] = "tooling"
            values = [NormalizedRuntimeEvent("run.activity.changed", activity_payload)]
            if tool_id:
                values.append(
                    NormalizedRuntimeEvent(
                        "span.started",
                        {
                            **metadata,
                            "span_id": tool_id,
                            "name": tool_name,
                            "kind": "tool",
                        },
                    )
                )
            return tuple(values)
        if hook_name in {"PostToolUse", "PostToolUseFailure"}:
            tool_name = _tool_kind(event.get("tool_name"))
            tool_id = _provider_reference(
                "tool",
                event.get("tool_call_id") or event.get("tool_use_id"),
                "tool_call_id",
            )
            # Kimi fires PostToolUse only after a successful tool call and
            # PostToolUseFailure after a failed or blocked one, so the outcome
            # is decided by the event name rather than free-text output.
            outcome = "succeeded" if hook_name == "PostToolUse" else "failed"
            values: list[NormalizedRuntimeEvent] = []
            if tool_id:
                values.append(
                    NormalizedRuntimeEvent(
                        "span.ended",
                        {
                            **metadata,
                            "span_id": tool_id,
                            "name": tool_name,
                            "outcome": outcome,
                        },
                    )
                )
            values.append(
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                )
            )
            return tuple(values)
        if hook_name == "SubagentStart":
            # Kimi currently documents only agent_name, which is a profile name
            # and can repeat concurrently. Never fabricate correlation from it.
            delegation_id = _provider_reference(
                "delegation",
                event.get("agent_id") or event.get("task_id"),
                "agent_id",
            )
            if delegation_id is None:
                return (
                    NormalizedRuntimeEvent(
                        "child_run.observed",
                        {
                            **metadata,
                            "agent_kind": "kimi",
                            "phase": "started",
                        },
                    ),
                )
            return (
                NormalizedRuntimeEvent(
                    "child_run.requested",
                    {
                        **metadata,
                        "delegation_id": delegation_id,
                        "agent_kind": "kimi",
                        "title": "Observed Kimi delegation",
                    },
                ),
            )
        if hook_name == "SubagentStop":
            return (
                NormalizedRuntimeEvent(
                    "child_run.observed",
                    {
                        **metadata,
                        "agent_kind": "kimi",
                        "phase": "completed",
                    },
                ),
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                ),
            )
        if hook_name == "TaskStarted":
            task_kind = str(
                event.get("task_kind") or event.get("kind") or ""
            ).strip().lower()
            task_id = event.get("task_id")
            delegation_id = _provider_reference(
                "delegation",
                str(task_id) if isinstance(task_id, (str, int)) else None,
                "task_id",
            )
            if task_kind != "agent" or delegation_id is None:
                # Background process/question tasks are not agent delegations.
                return ()
            return (
                NormalizedRuntimeEvent(
                    "child_run.requested",
                    {
                        **metadata,
                        "delegation_id": delegation_id,
                        "agent_kind": "kimi",
                        "title": "Observed Kimi background agent",
                    },
                ),
            )
        if hook_name == "PreCompact":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "planning"},
                ),
            )
        if hook_name == "PostCompact":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                ),
            )
        if hook_name == "Stop":
            # Stop is a turn boundary, not proof that the delegated Task has
            # succeeded.  Keep result authority with explicit completion.
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "finalizing"},
                ),
            )
        if hook_name == "StopFailure":
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {
                        **metadata,
                        "activity": "finalizing",
                        "provider_status": "failed",
                        "code": "provider_error",
                    },
                ),
            )
        if hook_name == "Interrupt":
            # The user aborted the current turn; the AgentServer Task keeps its
            # own cancel authority.
            return (
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {
                        **metadata,
                        "activity": "finalizing",
                        "provider_status": "cancelled",
                    },
                ),
            )
        raise ValueError("unsupported Kimi hook event")

    def _normalize_jsonl(
        self, role: str, event: Mapping[str, Any]
    ) -> tuple[NormalizedRuntimeEvent, ...]:
        metadata: dict[str, Any] = {}
        if role == "meta":
            if str(event.get("type") or "") == "system.version":
                return (
                    NormalizedRuntimeEvent(
                        "agent.registered",
                        {**metadata, "kind": self.kind, "source": "kimi_jsonl"},
                    ),
                )
            # Other meta lines (resume hints, notices) carry no lifecycle fact.
            return ()
        if role == "assistant":
            tool_calls = event.get("tool_calls")
            if not tool_calls:
                return (
                    NormalizedRuntimeEvent(
                        "run.activity.changed",
                        {**metadata, "activity": "thinking"},
                    ),
                )
            if not isinstance(tool_calls, Sequence) or isinstance(
                tool_calls, (str, bytes)
            ):
                raise ValueError("Kimi assistant tool_calls must be a list")
            values: list[NormalizedRuntimeEvent] = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, Mapping):
                    raise ValueError("Kimi assistant tool call must be an object")
                function = tool_call.get("function") or {}
                if not isinstance(function, Mapping):
                    raise ValueError("Kimi assistant tool call requires a function")
                raw_name = str(function.get("name") or "")
                activity = _tool_activity(raw_name)
                if activity == "waiting":
                    activity = "tooling"
                values.append(
                    NormalizedRuntimeEvent(
                        "run.activity.changed", {**metadata, "activity": activity}
                    )
                )
                span_id = _provider_reference(
                    "tool", tool_call.get("id"), "tool_calls.id"
                )
                if span_id:
                    values.append(
                        NormalizedRuntimeEvent(
                            "span.started",
                            {
                                **metadata,
                                "span_id": span_id,
                                "name": _tool_kind(raw_name),
                                "kind": "tool",
                            },
                        )
                    )
            return tuple(values)
        if role == "tool":
            # Tool result messages carry no tool name; the span id correlates
            # back to the assistant tool_calls entry.
            span_id = _provider_reference(
                "tool", event.get("tool_call_id"), "tool_call_id"
            )
            outcome = "failed" if event.get("is_error") else "succeeded"
            values = []
            if span_id:
                values.append(
                    NormalizedRuntimeEvent(
                        "span.ended",
                        {
                            **metadata,
                            "span_id": span_id,
                            "name": "other_tool",
                            "outcome": outcome,
                        },
                    )
                )
            values.append(
                NormalizedRuntimeEvent(
                    "run.activity.changed",
                    {**metadata, "activity": "thinking"},
                )
            )
            return tuple(values)
        raise ValueError("unsupported Kimi JSONL event")


ADAPTERS: dict[str, ProviderAdapter] = {
    adapter.kind: adapter
    for adapter in (CodexAdapter(), ClaudeAdapter(), KimiAdapter(), ProviderAdapter())
}

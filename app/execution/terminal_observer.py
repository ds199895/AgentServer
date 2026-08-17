from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .observations import ObservationDraft, ProcessFingerprint


ContextResolver = Callable[[str, str, str], Mapping[str, Any] | None]


@dataclass(frozen=True)
class _ProcessBinding:
    fingerprint: ProcessFingerprint
    agent_instance_id: str
    task_id: str | None
    assignment_id: str | None
    run_id: str | None


class TerminalObservationTranslator:
    """Translate TerminalManager compatibility probes into protocol evidence.

    The translator deliberately keeps process incarnation state outside
    ``TerminalSession.agent``.  This lets an exit use the exact fingerprint that
    was observed at start time and prevents a reused PID from closing a newer
    process.  It performs no I/O other than the small, optional local ``/proc``
    identity read; callers should invoke it from a worker when used by an async
    server.
    """

    def __init__(
        self,
        *,
        default_owner: str,
        context_resolver: ContextResolver | None = None,
        local_device_id: str = "agentserver-local",
        process_ttl_ms: int = 15_000,
        pty_ttl_ms: int = 5_000,
        proc_root: Path = Path("/proc"),
    ) -> None:
        self.default_owner = str(default_owner or "").strip()
        self.context_resolver = context_resolver
        self.local_device_id = str(local_device_id or "").strip()
        if not self.default_owner:
            raise ValueError("default_owner is required")
        if not self.local_device_id:
            raise ValueError("local_device_id is required")
        self.process_ttl_ms = max(0, int(process_ttl_ms))
        self.pty_ttl_ms = max(0, int(pty_ttl_ms))
        self.proc_root = proc_root
        self._lock = threading.RLock()
        self._processes: dict[tuple[str, int], _ProcessBinding] = {}
        self._boot_id = self._read_boot_id()

    @staticmethod
    def _value(source: object | None, name: str, default: Any = None) -> Any:
        if source is None:
            return default
        if isinstance(source, Mapping):
            return source.get(name, default)
        return getattr(source, name, default)

    def _read_boot_id(self) -> str:
        try:
            return (self.proc_root / "sys/kernel/random/boot_id").read_text(
                encoding="utf-8"
            ).strip()[:255]
        except OSError:
            return ""

    def _local_process_identity(
        self, pid: int
    ) -> tuple[str, str, int | None]:
        """Return Linux start ticks, TTY hint and process group when available."""
        try:
            raw = (self.proc_root / str(pid) / "stat").read_text(
                encoding="utf-8", errors="replace"
            )
            closing = raw.rfind(")")
            fields = raw[closing + 2 :].split() if closing >= 0 else []
            # The suffix starts at proc(5) field 3, so field 22 is index 19.
            start_time = fields[19]
        except (OSError, IndexError):
            start_time = ""
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None
        if pgid is not None and pgid <= 0:
            pgid = None
        try:
            tty = os.readlink(self.proc_root / str(pid) / "fd/0")[:255]
        except OSError:
            tty = ""
        return start_time, tty, pgid

    def _new_fingerprint(
        self,
        *,
        device_id: str,
        pid: int,
        local: bool,
        launch_id: str,
    ) -> ProcessFingerprint:
        start_time = tty = ""
        pgid = None
        boot_id = ""
        if local:
            start_time, tty, pgid = self._local_process_identity(pid)
            boot_id = self._boot_id
        if not start_time:
            # Remote legacy probes do not expose process creation time. A random
            # first-seen nonce is safer than treating PID as stable identity.
            start_time = f"observed-{uuid.uuid4().hex}"
        return ProcessFingerprint(
            device_id=device_id,
            pid=pid,
            start_time=start_time,
            boot_id=boot_id,
            pgid=pgid,
            tty=tty,
            launch_nonce=launch_id,
        )

    def _active_context(
        self, owner_id: str, terminal_id: str, launch_id: str
    ) -> Mapping[str, Any]:
        if not self.context_resolver or not terminal_id or not launch_id:
            return {}
        try:
            context = self.context_resolver(owner_id, terminal_id, launch_id)
        except Exception:
            return {}
        if not isinstance(context, Mapping):
            return {}
        active_run_id = context.get("active_run_id")
        recent_run = context.get("recent_run")
        if not active_run_id or not isinstance(recent_run, Mapping):
            return {}
        if str(recent_run.get("id") or "") != str(active_run_id):
            return {}
        attributes = recent_run.get("attributes")
        if not isinstance(attributes, Mapping):
            return {"_active_run_id": str(active_run_id)}
        return {**attributes, "_active_run_id": str(active_run_id)}

    def translate(
        self, session: object | None, payload: Mapping[str, object]
    ) -> ObservationDraft | None:
        event_type = str(payload.get("type") or "")
        if event_type not in {
            "observation.process.started",
            "observation.process.exited",
            "observation.pty.signature",
        }:
            return None
        owner_id = str(self._value(session, "owner") or self.default_owner)
        terminal_id = str(self._value(session, "id") or "") or None
        launch_id = str(self._value(session, "launch_id") or "") or None
        session_device_id = str(self._value(session, "device_id") or "")
        device_id = str(payload.get("device_id") or session_device_id)
        local = not device_id
        if local:
            device_id = self.local_device_id
        confidence = float(payload.get("confidence") or 0.5)
        agent_kind = str(payload.get("agent_kind") or "")[:80]
        cwd = str(payload.get("cwd") or "")[:4096]
        active = self._active_context(
            owner_id, terminal_id or "", launch_id or ""
        )

        if event_type == "observation.pty.signature":
            # A terminal-local banner may identify a preallocated Agent, but it
            # is still low-authority evidence and never reports it as online.
            agent_id = str(active.get("agent_instance_id") or "") or None
            return ObservationDraft(
                type=event_type,
                owner_id=owner_id,
                device_id=device_id,
                terminal_id=terminal_id,
                launch_id=launch_id,
                agent_instance_id=agent_id,
                task_id=str(active.get("task_id") or "") or None,
                assignment_id=str(active.get("assignment_id") or "") or None,
                run_id=str(active.get("_active_run_id") or "") or None,
                payload={
                    "agent_kind": agent_kind,
                    "cwd": cwd,
                    "signature": str(payload.get("source") or "output")[:80],
                },
                confidence=confidence,
                valid_for_ms=self.pty_ttl_ms,
            )

        raw_pid = payload.get("pid")
        if not isinstance(raw_pid, int) or isinstance(raw_pid, bool) or raw_pid <= 0:
            # A banner disappearing is not a process-exit proof.
            return None
        key = (device_id, raw_pid)
        with self._lock:
            if event_type == "observation.process.started":
                binding = self._processes.get(key)
                if binding is None:
                    fingerprint = self._new_fingerprint(
                        device_id=device_id,
                        pid=raw_pid,
                        local=local,
                        launch_id=launch_id or "",
                    )
                    # Only exact/high-confidence terminal ancestry may bind an
                    # OS process directly to the assigned AgentInstance.
                    assigned_agent = (
                        str(active.get("agent_instance_id") or "")
                        if confidence >= 0.95
                        else ""
                    )
                    binding = _ProcessBinding(
                        fingerprint=fingerprint,
                        agent_instance_id=assigned_agent or fingerprint.instance_id,
                        task_id=str(active.get("task_id") or "") or None,
                        assignment_id=(
                            str(active.get("assignment_id") or "") or None
                        ),
                        run_id=str(active.get("_active_run_id") or "") or None,
                    )
                    self._processes[key] = binding
            else:
                binding = self._processes.pop(key, None)
                if binding is None:
                    return None

        values = {
            "type": event_type,
            "owner_id": owner_id,
            "device_id": device_id,
            "terminal_id": terminal_id,
            "launch_id": launch_id,
            "agent_instance_id": binding.agent_instance_id,
            "task_id": binding.task_id,
            "assignment_id": binding.assignment_id,
            "run_id": binding.run_id,
            "payload": {
                "agent_kind": agent_kind,
                "cwd": cwd,
                "source": str(payload.get("source") or "process")[:80],
                "return_code": payload.get("return_code"),
            },
            "fingerprint": binding.fingerprint,
            "confidence": confidence,
            "valid_for_ms": (
                self.process_ttl_ms
                if event_type == "observation.process.started"
                else None
            ),
        }
        return ObservationDraft(**values)

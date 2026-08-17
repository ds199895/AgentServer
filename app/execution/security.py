from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


TOKEN_VERSION = "agentserver.report-token/1"
REPORT_CAPABILITIES = frozenset({"context", "report", "heartbeat"})
COMMAND_CAPABILITIES = frozenset({"context", "heartbeat", "commands", "ack"})
ADAPTER_REPORT_CAPABILITY = "adapter_report"
TOKEN_CAPABILITIES = (
    REPORT_CAPABILITIES | COMMAND_CAPABILITIES | {ADAPTER_REPORT_CAPABILITY}
)


class ReporterTokenError(ValueError):
    """A reporter token is malformed, expired, or outside the requested scope."""


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class ReporterClaims:
    owner_id: str
    run_id: str
    terminal_id: str
    launch_id: str
    device_id: str | None
    agent_instance_id: str | None
    capabilities: tuple[str, ...]
    issued_at: int
    expires_at: int
    token_id: str

    def permits(self, capability: str) -> bool:
        return capability in self.capabilities


class ReporterTokenSigner:
    """Issue short-lived, report-only tokens bound to one run and terminal.

    Reporter tokens intentionally use a different key and payload version from
    browser session cookies. They are authorization credentials, never event or
    trace identifiers.
    """

    def __init__(self, secret: bytes, *, default_ttl: int = 15 * 60) -> None:
        if len(secret) < 32:
            raise ValueError("reporter token secret must contain at least 32 bytes")
        self.secret = secret
        self.default_ttl = max(30, min(int(default_ttl), 24 * 60 * 60))

    def issue(
        self,
        *,
        owner_id: str,
        run_id: str,
        terminal_id: str,
        launch_id: str,
        device_id: str | None = None,
        agent_instance_id: str | None = None,
        capabilities: Iterable[str] = REPORT_CAPABILITIES,
        ttl: int | None = None,
        now: int | None = None,
        token_id: str | None = None,
    ) -> str:
        issued_at = int(time.time() if now is None else now)
        lifetime = self.default_ttl if ttl is None else max(1, min(int(ttl), 24 * 60 * 60))
        requested = tuple(sorted(set(capabilities)))
        unknown = set(requested) - TOKEN_CAPABILITIES
        if unknown:
            raise ValueError(f"unknown reporter capabilities: {sorted(unknown)}")
        required = {
            "owner_id": owner_id,
            "run_id": run_id,
            "terminal_id": terminal_id,
            "launch_id": launch_id,
        }
        if any(not value or len(value) > 255 for value in required.values()):
            raise ValueError("reporter token scope values must be 1..255 characters")
        resolved_token_id = str(token_id or secrets.token_urlsafe(12))
        if (
            not 1 <= len(resolved_token_id) <= 255
            or any(character.isspace() for character in resolved_token_id)
        ):
            raise ValueError("reporter token id must contain 1..255 non-space characters")
        payload = {
            "v": TOKEN_VERSION,
            "owner": owner_id,
            "run": run_id,
            "terminal": terminal_id,
            "launch": launch_id,
            "device": device_id,
            "agent": agent_instance_id,
            "cap": requested,
            "iat": issued_at,
            "exp": issued_at + lifetime,
            "jti": resolved_token_id,
        }
        encoded = _encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = _encode(hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify(
        self,
        token: str,
        *,
        capability: str | None = None,
        owner_id: str | None = None,
        run_id: str | None = None,
        terminal_id: str | None = None,
        launch_id: str | None = None,
        device_id: str | None = None,
        agent_instance_id: str | None = None,
        now: int | None = None,
        clock_skew: int = 5,
    ) -> ReporterClaims:
        try:
            encoded, supplied = token.split(".", 1)
            expected = _encode(
                hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied, expected):
                raise ReporterTokenError("invalid reporter token signature")
            payload = json.loads(_decode(encoded))
            if payload.get("v") != TOKEN_VERSION:
                raise ReporterTokenError("unsupported reporter token version")
            capabilities = tuple(str(item) for item in payload["cap"])
            if set(capabilities) - TOKEN_CAPABILITIES:
                raise ReporterTokenError("reporter token contains unknown capabilities")
            claims = ReporterClaims(
                owner_id=str(payload["owner"]),
                run_id=str(payload["run"]),
                terminal_id=str(payload["terminal"]),
                launch_id=str(payload["launch"]),
                device_id=str(payload["device"]) if payload.get("device") else None,
                agent_instance_id=str(payload["agent"]) if payload.get("agent") else None,
                capabilities=capabilities,
                issued_at=int(payload["iat"]),
                expires_at=int(payload["exp"]),
                token_id=str(payload["jti"]),
            )
        except ReporterTokenError:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ReporterTokenError("malformed reporter token") from exc

        timestamp = int(time.time() if now is None else now)
        skew = max(0, min(int(clock_skew), 60))
        if claims.issued_at > timestamp + skew:
            raise ReporterTokenError("reporter token is not valid yet")
        if claims.expires_at < timestamp - skew:
            raise ReporterTokenError("reporter token has expired")
        expected_scope = {
            "owner": (owner_id, claims.owner_id),
            "run": (run_id, claims.run_id),
            "terminal": (terminal_id, claims.terminal_id),
            "launch": (launch_id, claims.launch_id),
            "device": (device_id, claims.device_id),
            "agent": (agent_instance_id, claims.agent_instance_id),
        }
        for label, (expected_value, actual_value) in expected_scope.items():
            if expected_value is not None and (
                actual_value is None
                or not hmac.compare_digest(expected_value, actual_value)
            ):
                raise ReporterTokenError(f"reporter token {label} scope mismatch")
        if capability and not claims.permits(capability):
            raise ReporterTokenError(f"reporter token does not permit {capability}")
        return claims


class ReporterTokenRegistry:
    """Persistent allow-list and revocation registry for reporter credentials.

    An HMAC signature proves who issued a token; this table additionally proves
    that the token is still admitted.  Keeping both checks means a single Run
    credential can be revoked without rotating every device credential.
    """

    def __init__(self, database_path: Path, signer: ReporterTokenSigner) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.signer = signer
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_reporter_tokens (
                    token_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    terminal_id TEXT NOT NULL,
                    launch_id TEXT NOT NULL,
                    device_id TEXT,
                    agent_instance_id TEXT,
                    capabilities_json TEXT NOT NULL,
                    issued_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    revoked_at INTEGER,
                    refreshed_from_id TEXT
                );
                CREATE INDEX IF NOT EXISTS execution_reporter_tokens_run
                ON execution_reporter_tokens(owner_id, run_id, expires_at);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(execution_reporter_tokens)"
                ).fetchall()
            }
            if "refreshed_from_id" not in columns:
                connection.execute(
                    "ALTER TABLE execution_reporter_tokens "
                    "ADD COLUMN refreshed_from_id TEXT"
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS execution_reporter_token_refresh "
                "ON execution_reporter_tokens(refreshed_from_id) "
                "WHERE refreshed_from_id IS NOT NULL"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def issue(self, **scope: object) -> str:
        token = self.signer.issue(**scope)  # type: ignore[arg-type]
        claims = self.signer.verify(
            token,
            now=int(scope["now"]) if scope.get("now") is not None else None,
            clock_skew=0,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_reporter_tokens(
                    token_id, owner_id, run_id, terminal_id, launch_id,
                    device_id, agent_instance_id, capabilities_json,
                       issued_at, expires_at, revoked_at, refreshed_from_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    claims.token_id,
                    claims.owner_id,
                    claims.run_id,
                    claims.terminal_id,
                    claims.launch_id,
                    claims.device_id,
                    claims.agent_instance_id,
                    json.dumps(claims.capabilities, separators=(",", ":")),
                    claims.issued_at,
                    claims.expires_at,
                ),
            )
        return token

    def refresh(
        self,
        claims: ReporterClaims,
        *,
        ttl: int | None = None,
        now: int | None = None,
    ) -> str:
        """Idempotently issue an overlapping replacement near expiry.

        The old credential deliberately remains valid until its normal expiry.
        This overlap lets a Bridge persist the replacement atomically without a
        crash window in which both its on-disk and in-memory credentials are
        unusable.  Callers must verify the presented token and the active Run
        lease before invoking this method.
        """
        timestamp = int(time.time() if now is None else now)
        if timestamp > claims.expires_at:
            raise ReporterTokenError("reporter token has expired")
        lifetime = claims.expires_at - claims.issued_at
        refresh_window = max(5, min(300, int(lifetime * 0.2)))
        if claims.expires_at - timestamp > refresh_window:
            raise ValueError("reporter token is not yet in its refresh window")
        replacement_lifetime = (
            self.signer.default_ttl
            if ttl is None
            else max(1, min(int(ttl), 24 * 60 * 60))
        )

        def token_from_row(row: sqlite3.Row) -> str:
            capabilities = tuple(json.loads(row["capabilities_json"]))
            return self.signer.issue(
                owner_id=str(row["owner_id"]),
                run_id=str(row["run_id"]),
                terminal_id=str(row["terminal_id"]),
                launch_id=str(row["launch_id"]),
                device_id=row["device_id"],
                agent_instance_id=row["agent_instance_id"],
                capabilities=capabilities,
                ttl=int(row["expires_at"]) - int(row["issued_at"]),
                now=int(row["issued_at"]),
                token_id=str(row["token_id"]),
            )

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parent = connection.execute(
                """
                SELECT * FROM execution_reporter_tokens
                WHERE token_id = ?
                """,
                (claims.token_id,),
            ).fetchone()
            if parent is None or parent["revoked_at"] is not None:
                raise ReporterTokenError("reporter token is not active")
            persisted_parent = (
                str(parent["owner_id"]),
                str(parent["run_id"]),
                str(parent["terminal_id"]),
                str(parent["launch_id"]),
                parent["device_id"],
                parent["agent_instance_id"],
                tuple(json.loads(parent["capabilities_json"])),
                int(parent["issued_at"]),
                int(parent["expires_at"]),
            )
            presented_parent = (
                claims.owner_id,
                claims.run_id,
                claims.terminal_id,
                claims.launch_id,
                claims.device_id,
                claims.agent_instance_id,
                tuple(claims.capabilities),
                claims.issued_at,
                claims.expires_at,
            )
            if persisted_parent != presented_parent:
                raise ReporterTokenError("reporter token registry scope mismatch")
            existing = connection.execute(
                """
                SELECT * FROM execution_reporter_tokens
                WHERE refreshed_from_id = ?
                """,
                (claims.token_id,),
            ).fetchone()
            if existing is not None:
                return token_from_row(existing)

            replacement_id = _encode(
                hmac.new(
                    self.signer.secret,
                    f"refresh\0{claims.token_id}".encode("utf-8"),
                    hashlib.sha256,
                ).digest()[:18]
            )
            token = self.signer.issue(
                owner_id=claims.owner_id,
                run_id=claims.run_id,
                terminal_id=claims.terminal_id,
                launch_id=claims.launch_id,
                device_id=claims.device_id,
                agent_instance_id=claims.agent_instance_id,
                capabilities=claims.capabilities,
                ttl=replacement_lifetime,
                now=timestamp,
                token_id=replacement_id,
            )
            replacement = self.signer.verify(token, now=timestamp, clock_skew=0)
            connection.execute(
                """
                INSERT INTO execution_reporter_tokens(
                    token_id, owner_id, run_id, terminal_id, launch_id,
                    device_id, agent_instance_id, capabilities_json,
                    issued_at, expires_at, revoked_at, refreshed_from_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    replacement.token_id,
                    replacement.owner_id,
                    replacement.run_id,
                    replacement.terminal_id,
                    replacement.launch_id,
                    replacement.device_id,
                    replacement.agent_instance_id,
                    json.dumps(
                        replacement.capabilities, separators=(",", ":")
                    ),
                    replacement.issued_at,
                    replacement.expires_at,
                    claims.token_id,
                ),
            )
            # Retain a bounded audit/replay window, but do not let a long-lived
            # Bridge grow the registry forever through normal rotations.
            connection.execute(
                """
                DELETE FROM execution_reporter_tokens
                WHERE expires_at < ?
                """,
                (timestamp - 24 * 60 * 60,),
            )
            return token

    def verify(self, token: str, **requirements: object) -> ReporterClaims:
        claims = self.signer.verify(token, **requirements)  # type: ignore[arg-type]
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT owner_id, run_id, terminal_id, launch_id, device_id,
                       agent_instance_id, capabilities_json, issued_at,
                       expires_at, revoked_at
                FROM execution_reporter_tokens WHERE token_id = ?
                """,
                (claims.token_id,),
            ).fetchone()
        if row is None:
            raise ReporterTokenError("reporter token is not registered")
        expected = (
            claims.owner_id,
            claims.run_id,
            claims.terminal_id,
            claims.launch_id,
            claims.device_id,
            claims.agent_instance_id,
            tuple(claims.capabilities),
            claims.issued_at,
            claims.expires_at,
        )
        persisted = (
            row["owner_id"],
            row["run_id"],
            row["terminal_id"],
            row["launch_id"],
            row["device_id"],
            row["agent_instance_id"],
            tuple(json.loads(row["capabilities_json"])),
            int(row["issued_at"]),
            int(row["expires_at"]),
        )
        if persisted != expected:
            raise ReporterTokenError("reporter token registry scope mismatch")
        if row["revoked_at"] is not None:
            raise ReporterTokenError("reporter token was revoked")
        return claims

    def revoke(self, token_id: str, *, owner_id: str, now: int | None = None) -> bool:
        timestamp = int(time.time() if now is None else now)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE execution_reporter_tokens SET revoked_at = ?
                WHERE token_id = ? AND owner_id = ? AND revoked_at IS NULL
                """,
                (timestamp, token_id, owner_id),
            )
        return cursor.rowcount == 1

    def revoke_run(self, *, owner_id: str, run_id: str, now: int | None = None) -> int:
        timestamp = int(time.time() if now is None else now)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE execution_reporter_tokens SET revoked_at = ?
                WHERE owner_id = ? AND run_id = ? AND revoked_at IS NULL
                """,
                (timestamp, owner_id, run_id),
            )
        return max(0, cursor.rowcount)

    def revoke_run_capabilities(
        self,
        *,
        owner_id: str,
        run_id: str,
        capabilities: Iterable[str],
        now: int | None = None,
    ) -> int:
        """Revoke active Run tokens that grant any requested capability.

        Run completion uses this to revoke command/ACK authority immediately
        while leaving the report credential alive for exact replay of a final
        event whose HTTP acknowledgement may have been lost.
        """
        requested = set(capabilities)
        unknown = requested - TOKEN_CAPABILITIES
        if unknown:
            raise ValueError(f"unknown reporter capabilities: {sorted(unknown)}")
        if not requested:
            return 0
        timestamp = int(time.time() if now is None else now)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT token_id, capabilities_json
                FROM execution_reporter_tokens
                WHERE owner_id = ? AND run_id = ? AND revoked_at IS NULL
                """,
                (owner_id, run_id),
            ).fetchall()
            token_ids = [
                str(row["token_id"])
                for row in rows
                if requested.intersection(json.loads(row["capabilities_json"]))
            ]
            if not token_ids:
                return 0
            placeholders = ",".join("?" for _ in token_ids)
            cursor = connection.execute(
                f"""
                UPDATE execution_reporter_tokens SET revoked_at = ?
                WHERE owner_id = ? AND run_id = ? AND revoked_at IS NULL
                  AND token_id IN ({placeholders})
                """,
                (timestamp, owner_id, run_id, *token_ids),
            )
        return max(0, cursor.rowcount)


def load_or_create_reporter_secret(data_dir: Path) -> bytes:
    """Load the reporter signing key, intentionally separate from cookies."""
    configured = os.getenv("REPORTER_TOKEN_SECRET")
    if configured:
        secret = configured.encode("utf-8")
        if len(secret) < 32:
            raise ValueError("REPORTER_TOKEN_SECRET must contain at least 32 bytes")
        return secret
    path = Path(data_dir) / "reporter_token_secret"
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory = path.parent.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or stat.S_ISLNK(directory.st_mode)
        or (os.name != "nt" and directory.st_uid != os.geteuid())
    ):
        raise ValueError("reporter token secret directory is not private and owned")

    def read_existing() -> bytes:
        try:
            info = path.lstat()
        except OSError as error:
            raise ValueError("stored reporter token secret is unavailable") from error
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("stored reporter token secret is not a regular file")
        if os.name != "nt" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("stored reporter token secret permissions are unsafe")
        secret_value = path.read_bytes().strip()
        if len(secret_value) < 32:
            raise ValueError("stored reporter token secret is invalid")
        return secret_value

    try:
        return read_existing()
    except ValueError:
        if path.exists() or path.is_symlink():
            return read_existing()

    secret = secrets.token_urlsafe(48).encode("ascii")
    temporary = path.parent / (
        f".{path.name}.create-{os.getpid()}-{secrets.token_hex(8)}"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        offset = 0
        while offset < len(secret):
            offset += os.write(descriptor, secret[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        temporary.chmod(0o600)
        try:
            os.link(temporary, path)
            directory_fd = os.open(
                path.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return secret
        except FileExistsError:
            return read_existing()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

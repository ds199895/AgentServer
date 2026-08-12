from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path


PBKDF2_ITERATIONS = 310_000


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _b64decode(salt),
            int(iterations),
        )
        return hmac.compare_digest(actual, _b64decode(expected))
    except (ValueError, TypeError):
        return False


class UserStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )

    def ensure_user(self, username: str, password: str) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO users(username, password_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (username, hash_password(password), now, now),
            )

    def authenticate(self, username: str, password: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        return bool(row and verify_password(password, row["password_hash"]))

    def update_password(self, username: str, new_password: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?",
                (hash_password(new_password), int(time.time()), username),
            )
        return cursor.rowcount == 1


class SessionSigner:
    def __init__(self, secret: bytes, max_age: int = 7 * 24 * 60 * 60) -> None:
        self.secret = secret
        self.max_age = max_age

    def issue(self, username: str) -> str:
        payload = _b64encode(
            json.dumps(
                {"sub": username, "exp": int(time.time()) + self.max_age},
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{payload}.{signature}"

    def verify(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            payload, supplied_signature = token.split(".", 1)
            expected_signature = _b64encode(
                hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return None
            data = json.loads(_b64decode(payload))
            if int(data["exp"]) < int(time.time()):
                return None
            return str(data["sub"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None


def load_or_create_secret(data_dir: Path) -> bytes:
    configured = os.getenv("SESSION_SECRET")
    if configured:
        return configured.encode("utf-8")

    secret_path = data_dir / "session_secret"
    if secret_path.exists():
        return secret_path.read_bytes().strip()

    data_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48).encode("ascii")
    secret_path.write_bytes(secret)
    secret_path.chmod(0o600)
    return secret

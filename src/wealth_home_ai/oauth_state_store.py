"""Short-lived, one-time storage for OAuth callback state values."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .settings import settings


def _database_path() -> Path:
    return Path(settings.oauth_database_path).expanduser()


def _state_digest(state: str) -> bytes:
    if not state or len(state) > 256:
        raise ValueError("OAuth state must be a non-empty short string.")
    return hashlib.sha256(state.encode("utf-8")).digest()


def _open_database() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path, timeout=settings.database_timeout_seconds
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_states (
            state_digest BLOB PRIMARY KEY,
            expires_at INTEGER NOT NULL,
            encrypted_context BLOB
        )
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(oauth_states)")
    }
    if "encrypted_context" not in columns:
        connection.execute(
            "ALTER TABLE oauth_states ADD COLUMN encrypted_context BLOB"
        )
    return connection


def _credential_cipher() -> Fernet:
    configured_key = settings.oauth_credential_encryption_key.strip()
    if configured_key:
        try:
            key = configured_key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("The OAuth credential-encryption key is invalid.") from exc
    else:
        key_path = _database_path().with_name(_database_path().name + ".key")
        key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            key = key_path.read_bytes()
        except FileNotFoundError:
            generated_key = Fernet.generate_key()
            try:
                descriptor = os.open(
                    key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                key = key_path.read_bytes()
            else:
                with os.fdopen(descriptor, "wb") as key_file:
                    key_file.write(generated_key)
                key = generated_key
        if os.name != "nt":
            os.chmod(key_path, 0o600)
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise ValueError("The OAuth credential-encryption key is invalid.") from exc


def create_oauth_state(
    state: str, credential_context: dict[str, Any] | None = None
) -> None:
    """Persist a state digest and optionally an encrypted, short-lived callback context."""
    digest = _state_digest(state)
    encrypted_context = None
    if credential_context is not None:
        serialized_context = json.dumps(
            credential_context, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        encrypted_context = _credential_cipher().encrypt(serialized_context)
    now = int(time.time())
    connection = _open_database()
    try:
        with connection:
            connection.execute(
                "DELETE FROM oauth_states WHERE expires_at <= ?", (now,)
            )
            connection.execute(
                """
                INSERT INTO oauth_states
                    (state_digest, expires_at, encrypted_context)
                VALUES (?, ?, ?)
                """,
                (
                    digest,
                    now + settings.oauth_state_ttl_seconds,
                    encrypted_context,
                ),
            )
    finally:
        connection.close()


def _consume_oauth_state(
    state: str | None,
) -> tuple[bool, bytes | None]:
    if not state:
        return False, None
    digest = _state_digest(state)
    now = int(time.time())
    connection = _open_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM oauth_states WHERE expires_at <= ?", (now,))
        row = connection.execute(
            """
            SELECT expires_at, encrypted_context FROM oauth_states
            WHERE state_digest = ?
            """,
            (digest,),
        ).fetchone()
        valid = row is not None and row[0] > now
        if valid:
            connection.execute(
                "DELETE FROM oauth_states WHERE state_digest = ?", (digest,)
            )
        connection.commit()
        return valid, row[1] if valid else None
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def consume_oauth_state(state: str | None) -> bool:
    """Atomically validate and consume an unexpired OAuth state exactly once."""
    valid, _ = _consume_oauth_state(state)
    return valid


def consume_oauth_state_context(state: str | None) -> dict[str, Any] | None:
    """Consume an OAuth state and decrypt its one-time callback context, if present."""
    valid, encrypted_context = _consume_oauth_state(state)
    if not valid or encrypted_context is None:
        return None
    try:
        context = json.loads(_credential_cipher().decrypt(encrypted_context))
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("The stored OAuth callback context could not be verified.") from exc
    if not isinstance(context, dict):
        raise ValueError("The stored OAuth callback context is invalid.")
    return context

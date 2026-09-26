"""Short-lived, one-time storage for OAuth callback state values."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

STATE_TTL_SECONDS = 600
DEFAULT_DATABASE = (
    Path(__file__).resolve().parents[2] / ".wealth_home_oauth.sqlite3"
)


def _database_path() -> Path:
    configured_path = os.environ.get("WEALTH_HOME_OAUTH_DB")
    return Path(configured_path).expanduser() if configured_path else DEFAULT_DATABASE


def _state_digest(state: str) -> bytes:
    if not state or len(state) > 256:
        raise ValueError("OAuth state must be a non-empty short string.")
    return hashlib.sha256(state.encode("utf-8")).digest()


def _open_database() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_states (
            state_digest BLOB PRIMARY KEY,
            expires_at INTEGER NOT NULL
        )
        """
    )
    return connection


def create_oauth_state(state: str) -> None:
    """Persist only a digest of an OAuth state until its short expiry."""
    digest = _state_digest(state)
    now = int(time.time())
    connection = _open_database()
    try:
        with connection:
            connection.execute(
                "DELETE FROM oauth_states WHERE expires_at <= ?", (now,)
            )
            connection.execute(
                """
                INSERT INTO oauth_states (state_digest, expires_at)
                VALUES (?, ?)
                """,
                (digest, now + STATE_TTL_SECONDS),
            )
    finally:
        connection.close()


def consume_oauth_state(state: str | None) -> bool:
    """Atomically validate and consume an unexpired OAuth state exactly once."""
    if not state:
        return False
    digest = _state_digest(state)
    now = int(time.time())
    connection = _open_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM oauth_states WHERE expires_at <= ?", (now,))
        row = connection.execute(
            "SELECT expires_at FROM oauth_states WHERE state_digest = ?",
            (digest,),
        ).fetchone()
        valid = row is not None and row[0] > now
        if valid:
            connection.execute(
                "DELETE FROM oauth_states WHERE state_digest = ?", (digest,)
            )
        connection.commit()
        return valid
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()

"""Local, tenant-scoped persistence for user preferences and opt-in secrets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.fernet import InvalidToken

from core.config import settings
from security.credentials import credential_cipher
from services.database import connect_database

MAX_SETTINGS_BYTES = 64_000


def _owner_key(owner_identifier: str) -> str:
    owner = owner_identifier.strip()
    if not owner or len(owner) > 1024:
        raise ValueError("A valid user identifier is required to save settings.")
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


def _database_path() -> Path:
    return Path(settings.oauth_database_path).expanduser()


def _open_database() -> Any:
    path = _database_path()
    connection = connect_database(
        path,
        timeout=settings.database_timeout_seconds,
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            owner_id TEXT PRIMARY KEY,
            preferences_json TEXT NOT NULL,
            encrypted_sensitive BLOB,
            sensitive_consent INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return connection


def _serialize_mapping(value: dict[str, Any], label: str) -> str:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON-compatible object.")
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} contains unsupported values.") from exc
    if len(serialized.encode("utf-8")) > MAX_SETTINGS_BYTES:
        raise ValueError(f"{label} exceeds the {MAX_SETTINGS_BYTES}-byte limit.")
    return serialized


def load_user_settings(owner_identifier: str) -> dict[str, Any]:
    """Load saved preferences and decrypt secrets only when consent was recorded."""
    owner_id = _owner_key(owner_identifier)
    connection = _open_database()
    try:
        row = connection.execute(
            """
            SELECT preferences_json, encrypted_sensitive, sensitive_consent
            FROM user_settings WHERE owner_id = ?
            """,
            (owner_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return {"preferences": {}, "sensitive": {}, "sensitive_consent": False}

    try:
        preferences = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Saved user preferences are invalid.") from exc
    if not isinstance(preferences, dict):
        raise RuntimeError("Saved user preferences are invalid.")

    consented = bool(row[2])
    sensitive: dict[str, Any] = {}
    if consented and row[1] is not None:
        try:
            decoded = credential_cipher(_database_path()).decrypt(row[1])
            sensitive_value = json.loads(decoded)
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "Saved confidential settings could not be decrypted. "
                "Check the credential-encryption key."
            ) from exc
        if not isinstance(sensitive_value, dict):
            raise RuntimeError("Saved confidential settings are invalid.")
        sensitive = sensitive_value
    return {
        "preferences": preferences,
        "sensitive": sensitive,
        "sensitive_consent": consented,
    }


def save_user_settings(
    owner_identifier: str,
    preferences: dict[str, Any],
    sensitive: dict[str, Any],
    *,
    save_sensitive: bool,
) -> None:
    """Save preferences and optionally encrypt personal data and credentials."""
    if not isinstance(save_sensitive, bool):
        raise TypeError("The confidential-storage consent must be boolean.")
    owner_id = _owner_key(owner_identifier)
    preferences_json = _serialize_mapping(preferences, "Preferences")
    sensitive_json = _serialize_mapping(sensitive, "Confidential settings")
    encrypted_sensitive = (
        credential_cipher(_database_path()).encrypt(sensitive_json.encode("utf-8"))
        if save_sensitive
        else None
    )

    connection = _open_database()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO user_settings (
                    owner_id, preferences_json, encrypted_sensitive,
                    sensitive_consent, updated_at
                )
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(owner_id) DO UPDATE SET
                    preferences_json = excluded.preferences_json,
                    encrypted_sensitive = excluded.encrypted_sensitive,
                    sensitive_consent = excluded.sensitive_consent,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    owner_id,
                    preferences_json,
                    encrypted_sensitive,
                    int(save_sensitive),
                ),
            )
    finally:
        connection.close()

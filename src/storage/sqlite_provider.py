"""SQLCipher-backed local storage provider for native and desktop runtimes."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from cryptography.fernet import InvalidToken

from core.config import settings
from security.credentials import credential_cipher
from services.database import connect_database


class SQLiteStorageProvider:
    """SQLCipher provider retaining the existing SQLite user-settings schema."""

    def __init__(self, settings_override=settings) -> None:
        self._settings = settings_override
        self._path = Path(self._settings.oauth_database_path).expanduser()

    def _decrypt_oauth_context(self, encrypted_context: bytes | None) -> str | None:
        if encrypted_context is None:
            return None
        try:
            return credential_cipher(self._path).decrypt(encrypted_context).decode(
                "utf-8"
            )
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise RuntimeError(
                "The stored OAuth callback context could not be verified."
            ) from exc

    def _open_database(self) -> Any:
        connection = connect_database(
            self._path,
            timeout=self._settings.database_timeout_seconds,
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings
            (
                owner_id
                TEXT
                PRIMARY
                KEY,
                preferences_json
                TEXT
                NOT
                NULL,
                encrypted_sensitive
                BLOB,
                sensitive_consent
                INTEGER
                NOT
                NULL
                DEFAULT
                0,
                updated_at
                TEXT
                NOT
                NULL
                DEFAULT
                CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS device_metadata
            (
                metadata_key
                TEXT
                PRIMARY
                KEY,
                metadata_value
                TEXT
                NOT
                NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS storage_records
            (
                collection
                TEXT
                NOT
                NULL,
                record_key
                TEXT
                NOT
                NULL,
                value_json
                TEXT
                NOT
                NULL,
                updated_at
                TEXT
                NOT
                NULL
                DEFAULT
                CURRENT_TIMESTAMP,
                PRIMARY
                KEY
            (
                collection,
                record_key
            )
                )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_states
            (
                state_digest
                BLOB
                PRIMARY
                KEY,
                expires_at
                INTEGER
                NOT
                NULL,
                encrypted_context
                BLOB
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

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        connection = self._open_database()
        try:
            if collection == "oauth_states":
                digest = bytes.fromhex(key)
                row = connection.execute(
                    """
                    SELECT expires_at, encrypted_context
                    FROM oauth_states
                    WHERE state_digest = ?
                    """,
                    (digest,),
                ).fetchone()
                if row is None:
                    return None
                return {
                    "expires_at": int(row[0]),
                    "credential_context_json": self._decrypt_oauth_context(row[1]),
                }
            if collection == "user_settings":
                row = connection.execute(
                    """
                    SELECT preferences_json, encrypted_sensitive, sensitive_consent
                    FROM user_settings
                    WHERE owner_id = ?
                    """,
                    (key,),
                ).fetchone()
                if row is None:
                    return None
                sensitive_json = None
                if bool(row[2]) and row[1] is not None:
                    try:
                        decoded = credential_cipher(self._path).decrypt(row[1])
                    except (InvalidToken, UnicodeDecodeError) as exc:
                        raise RuntimeError(
                            "Saved confidential settings could not be decrypted. "
                            "Check the credential-encryption key."
                        ) from exc
                    try:
                        sensitive = json.loads(decoded)
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise RuntimeError(
                            "Saved confidential settings are invalid."
                        ) from exc
                    if not isinstance(sensitive, dict):
                        raise RuntimeError("Saved confidential settings are invalid.")
                    sensitive_json = json.dumps(
                        sensitive, ensure_ascii=True, separators=(",", ":"), sort_keys=True
                    )
                return {
                    "preferences_json": row[0],
                    "sensitive_json": sensitive_json,
                    "sensitive_consent": bool(row[2]),
                }

            row = connection.execute(
                """
                SELECT value_json
                FROM storage_records
                WHERE collection = ?
                  AND record_key = ?
                """,
                (collection, key),
            ).fetchone()
            if row is None:
                return None
            value = json.loads(row[0])
            if not isinstance(value, dict):
                raise RuntimeError("Stored local data is invalid.")
            return value
        finally:
            connection.close()

    def put(self, collection: str, key: str, value: dict[str, Any]) -> None:
        connection = self._open_database()
        try:
            with connection:
                if collection == "user_settings":
                    preferences_json = value["preferences_json"]
                    sensitive_json = value.get("sensitive_json")
                    sensitive_consent = bool(value.get("sensitive_consent", False))
                    if sensitive_consent and not isinstance(sensitive_json, str):
                        raise TypeError(
                            "Consented settings must include valid confidential data."
                        )
                    encrypted_sensitive = (
                        credential_cipher(self._path).encrypt(
                            sensitive_json.encode("utf-8")
                        )
                        if sensitive_consent and sensitive_json is not None
                        else None
                    )
                    connection.execute(
                        """
                        INSERT INTO user_settings (owner_id, preferences_json, encrypted_sensitive,
                                                   sensitive_consent, updated_at)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(owner_id) DO
                        UPDATE SET
                            preferences_json = excluded.preferences_json,
                            encrypted_sensitive = excluded.encrypted_sensitive,
                            sensitive_consent = excluded.sensitive_consent,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            key,
                            preferences_json,
                            encrypted_sensitive,
                            int(sensitive_consent),
                        ),
                    )
                    return
                if collection == "oauth_states":
                    digest = bytes.fromhex(key)
                    context_json = value.get("credential_context_json")
                    if context_json is not None and not isinstance(context_json, str):
                        raise TypeError("OAuth callback context must be serialized JSON.")
                    connection.execute(
                        "DELETE FROM oauth_states WHERE expires_at <= ?", (int(time.time()),)
                    )
                    encrypted_context = (
                        credential_cipher(self._path).encrypt(
                            context_json.encode("utf-8")
                        )
                        if context_json is not None
                        else None
                    )
                    connection.execute(
                        """
                        INSERT INTO oauth_states
                            (state_digest, expires_at, encrypted_context)
                        VALUES (?, ?, ?)
                        """,
                        (
                            digest,
                            int(value["expires_at"]),
                            encrypted_context,
                        ),
                    )
                    return

                serialized = json.dumps(
                    value,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                )
                connection.execute(
                    """
                    INSERT INTO storage_records (collection, record_key, value_json)
                    VALUES (?, ?, ?) ON CONFLICT(collection, record_key) DO
                    UPDATE SET
                        value_json = excluded.value_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (collection, key, serialized),
                )
        finally:
            connection.close()

    def delete(self, collection: str, key: str) -> None:
        connection = self._open_database()
        try:
            with connection:
                if collection == "user_settings":
                    connection.execute(
                        "DELETE FROM user_settings WHERE owner_id = ?", (key,)
                    )
                elif collection == "oauth_states":
                    connection.execute(
                        "DELETE FROM oauth_states WHERE state_digest = ?",
                        (bytes.fromhex(key),),
                    )
                else:
                    connection.execute(
                        "DELETE FROM storage_records WHERE collection = ? AND record_key = ?",
                        (collection, key),
                    )
        finally:
            connection.close()

    def consume(self, collection: str, key: str) -> dict[str, Any] | None:
        if collection != "oauth_states":
            value = self.get(collection, key)
            if value is not None:
                self.delete(collection, key)
            return value

        connection = self._open_database()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            connection.execute(
                "DELETE FROM oauth_states WHERE expires_at <= ?", (now,)
            )
            row = connection.execute(
                """
                SELECT expires_at, encrypted_context
                FROM oauth_states
                WHERE state_digest = ?
                """,
                (bytes.fromhex(key),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                "DELETE FROM oauth_states WHERE state_digest = ?",
                (bytes.fromhex(key),),
            )
            connection.commit()
            return {
                "expires_at": int(row[0]),
                "credential_context_json": self._decrypt_oauth_context(row[1]),
            }
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    def get_or_create_device_id(self) -> str:
        connection = self._open_database()
        try:
            with connection:
                row = connection.execute(
                    """
                    SELECT metadata_value
                    FROM device_metadata
                    WHERE metadata_key = 'anonymous_device_id'
                    """
                ).fetchone()
                if row is not None:
                    return str(row[0])
                device_id = uuid4().hex
                connection.execute(
                    """
                    INSERT INTO device_metadata (metadata_key, metadata_value)
                    VALUES ('anonymous_device_id', ?)
                    """,
                    (device_id,),
                )
                return device_id
        finally:
            connection.close()

"""SQLCipher connection and database-key access for local application storage."""

from __future__ import annotations

import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any

import sqlcipher3.dbapi2 as sqlcipher

from core.config import settings

KEYRING_SERVICE = "com.gghp.wealth-home.sqlcipher"
KEYRING_ACCOUNT = "primary-database-v1"
_RAW_KEY_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_SQLITE_HEADER = b"SQLite format 3\x00"


class DatabaseSecurityError(RuntimeError):
    """Base class for secure database initialization failures."""


class DatabaseKeyUnavailable(DatabaseSecurityError):
    """Raised when the database key is not available from a trusted vault."""


class PlaintextDatabaseDetected(DatabaseSecurityError):
    """Raised when an existing plaintext database needs explicit migration."""


DatabaseError = sqlcipher.DatabaseError


def _native_keyring_backend() -> Any:
    try:
        import keyring

        backend = keyring.get_keyring()
        module = type(backend).__module__
        expected = {
            "win32": ("keyring.backends.Windows",),
            "darwin": ("keyring.backends.macOS",),
            "linux": (
                "keyring.backends.SecretService",
                "keyring.backends.kwallet",
            ),
        }.get(sys.platform, ())
        if backend.priority <= 0 or not any(
            module.startswith(prefix) for prefix in expected
        ):
            raise DatabaseKeyUnavailable(
                "No supported native credential vault is active. Use Windows "
                "Credential Manager, macOS Keychain, Linux Secret Service/KWallet, "
                "or configure WEALTH_HOME_DB_ENCRYPTION_KEY in a secret manager."
            )
        return keyring, backend
    except DatabaseKeyUnavailable:
        raise
    except (ImportError, RuntimeError, OSError) as exc:
        raise DatabaseKeyUnavailable(
            "The OS credential vault is unavailable. Configure "
            "WEALTH_HOME_DB_ENCRYPTION_KEY in a deployment secret manager."
        ) from exc


def database_key_hex() -> str:
    """Return a validated 256-bit key from deployment secrets or OS keyring."""
    configured = settings.database_encryption_key.strip()
    if configured:
        if not _RAW_KEY_PATTERN.fullmatch(configured):
            raise DatabaseKeyUnavailable(
                "WEALTH_HOME_DB_ENCRYPTION_KEY must be exactly 64 hexadecimal characters."
            )
        return configured.lower()

    keyring, _ = _native_keyring_backend()
    try:
        value = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except (keyring.errors.KeyringError, RuntimeError, OSError) as exc:
        raise DatabaseKeyUnavailable(
            "Could not access the OS credential vault for the database key."
        ) from exc
    if value is None:
        raise DatabaseKeyUnavailable(
            "The SQLCipher database key is not provisioned. Run "
            "`python -m security.database_key provision` once under this user, "
            "or configure WEALTH_HOME_DB_ENCRYPTION_KEY in a secret manager."
        )
    if not _RAW_KEY_PATTERN.fullmatch(value):
        raise DatabaseKeyUnavailable(
            "The database key in the OS credential vault is invalid."
        )
    return value.lower()


def provision_database_key() -> None:
    """Create a random database key in the validated native credential vault."""
    if settings.database_encryption_key.strip():
        raise DatabaseKeyUnavailable(
            "WEALTH_HOME_DB_ENCRYPTION_KEY is configured; OS-keyring provisioning is not needed."
        )
    keyring, _ = _native_keyring_backend()
    try:
        if keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) is not None:
            raise DatabaseKeyUnavailable(
                "A database key is already present in the OS vault; refusing to replace it."
            )
        key = secrets.token_hex(32)
        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, key)
        if keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) != key:
            raise DatabaseKeyUnavailable(
                "The OS credential vault did not verify the stored database key."
            )
    except DatabaseKeyUnavailable:
        raise
    except (keyring.errors.KeyringError, RuntimeError, OSError):
        raise DatabaseKeyUnavailable(
            "Could not provision the database key in the OS credential vault."
        ) from None


def connect_database(
    database_path: str | Path | None = None,
    *,
    timeout: float | None = None,
) -> Any:
    """Open SQLCipher and key it before inspecting or accessing database pages."""
    path = Path(database_path or settings.oauth_database_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.is_file():
        with path.open("rb") as database_file:
            if database_file.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER:
                raise PlaintextDatabaseDetected(
                    "The existing database is plaintext. Stop the app and migrate it "
                    "with `python -m security.database_migration --confirm-plaintext-removal`."
                )

    key_hex = database_key_hex()
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR,
            0o600,
        )
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    if os.name != "nt":
        os.chmod(path, 0o600)

    connection = sqlcipher.connect(
        str(path),
        timeout=timeout
        if timeout is not None
        else settings.database_timeout_seconds,
    )
    succeeded = False
    try:
        # This must be the first database operation on every new connection.
        connection.execute(f"PRAGMA key = \"x'{key_hex}'\"")
        cipher_version = connection.execute("PRAGMA cipher_version").fetchone()
        if not cipher_version or not cipher_version[0]:
            raise DatabaseSecurityError(
                "The active SQLite driver does not provide SQLCipher."
            )
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        succeeded = True
        return connection
    except sqlcipher.DatabaseError as exc:
        raise DatabaseSecurityError(
            "The encrypted database could not be opened. Its key may be wrong "
            "or the database may be damaged."
        ) from exc
    finally:
        if not succeeded:
            connection.close()

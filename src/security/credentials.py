"""Encryption helpers for credentials persisted by the application."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet

from core.config import settings


def credential_cipher(database_path: Path | None = None) -> Fernet:
    """Load the configured Fernet key or create a protected local key file."""
    configured_key = settings.oauth_credential_encryption_key.strip()
    if configured_key:
        try:
            key = configured_key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("The credential-encryption key is invalid.") from exc
    else:
        database = database_path or Path(settings.oauth_database_path).expanduser()
        key_path = database.with_name(database.name + ".key")
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
        raise ValueError("The credential-encryption key is invalid.") from exc

"""Local, tenant-scoped persistence for user preferences and opt-in secrets."""

from __future__ import annotations

import json
from typing import Any

from core.config import settings
from storage import UserSettingsRepository, get_storage_provider

MAX_SETTINGS_BYTES = 64_000


def _repository() -> UserSettingsRepository:
    return UserSettingsRepository(get_storage_provider(settings))


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
    row = _repository().load(owner_identifier)
    if row is None:
        return {"preferences": {}, "sensitive": {}, "sensitive_consent": False}

    try:
        preferences = json.loads(row["preferences_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Saved user preferences are invalid.") from exc
    if not isinstance(preferences, dict):
        raise RuntimeError("Saved user preferences are invalid.")

    consented = bool(row["sensitive_consent"])
    sensitive: dict[str, Any] = {}
    if consented and row.get("sensitive_json") is not None:
        try:
            sensitive_value = json.loads(row["sensitive_json"])
        except (TypeError, json.JSONDecodeError) as exc:
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
    preferences_json = _serialize_mapping(preferences, "Preferences")
    sensitive_json = _serialize_mapping(sensitive, "Confidential settings")
    _repository().save(
        owner_identifier,
        preferences_json,
        sensitive_json,
        save_sensitive=save_sensitive,
    )

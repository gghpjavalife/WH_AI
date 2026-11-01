"""Repository mapping application settings to provider records."""

from __future__ import annotations

import hashlib
from typing import Any

from .provider import StorageProvider


class UserSettingsRepository:
    """Persist user settings without exposing database details to services."""

    def __init__(self, provider: StorageProvider) -> None:
        self._provider = provider

    @staticmethod
    def _owner_key(owner_identifier: str) -> str:
        owner = owner_identifier.strip()
        if not owner or len(owner) > 1024:
            raise ValueError("A valid user identifier is required to save settings.")
        return hashlib.sha256(owner.encode("utf-8")).hexdigest()

    def load(self, owner_identifier: str) -> dict[str, Any] | None:
        return self._provider.get("user_settings", self._owner_key(owner_identifier))

    def save(
            self,
            owner_identifier: str,
            preferences_json: str,
            sensitive_json: str,
            *,
            save_sensitive: bool,
    ) -> None:
        self._provider.put(
            "user_settings",
            self._owner_key(owner_identifier),
            {
                "preferences_json": preferences_json,
                "sensitive_json": sensitive_json if save_sensitive else None,
                "sensitive_consent": save_sensitive,
            },
        )

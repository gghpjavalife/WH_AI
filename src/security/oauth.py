"""Short-lived, one-time storage for OAuth callback state values."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.config import settings
from storage import get_storage_provider


def _database_path() -> Path:
    """Keep the configured native SQLite path and its existing schema."""
    return Path(settings.oauth_database_path).expanduser()


def _storage_provider():
    configured = replace(settings, oauth_database_path=str(_database_path()))
    return get_storage_provider(configured)


def _state_digest(state: str) -> bytes:
    if not state or len(state) > 256:
        raise ValueError("OAuth state must be a non-empty short string.")
    return hashlib.sha256(state.encode("utf-8")).digest()


def create_oauth_state(
        state: str, credential_context: dict[str, Any] | None = None
) -> None:
    """Persist only a short-lived digest and an optional encrypted callback context."""
    digest = _state_digest(state).hex()
    context_json = None
    if credential_context is not None:
        context_json = json.dumps(
            credential_context, separators=(",", ":"), sort_keys=True
        )
    _storage_provider().put(
        "oauth_states",
        digest,
        {
            "expires_at": int(time.time()) + settings.oauth_state_ttl_seconds,
            "credential_context_json": context_json,
        },
    )


def _consume_oauth_state(state: str | None) -> tuple[bool, str | None]:
    if not state:
        return False, None
    stored = _storage_provider().consume("oauth_states", _state_digest(state).hex())
    if stored is None or int(stored["expires_at"]) <= int(time.time()):
        return False, None
    return True, stored.get("credential_context_json")


def consume_oauth_state(state: str | None) -> bool:
    """Atomically validate and consume an unexpired OAuth state exactly once."""
    valid, _ = _consume_oauth_state(state)
    return valid


def consume_oauth_state_context(state: str | None) -> dict[str, Any] | None:
    """Consume an OAuth state and return its verified callback context."""
    valid, context_json = _consume_oauth_state(state)
    if not valid or context_json is None:
        return None
    try:
        context = json.loads(context_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("The stored OAuth callback context could not be verified.") from exc
    if not isinstance(context, dict):
        raise ValueError("The stored OAuth callback context is invalid.")
    return context

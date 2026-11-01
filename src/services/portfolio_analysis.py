"""Persistence for the latest AI analysis associated with a broker account."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.config import settings
from storage import StorageProvider, get_storage_provider

MAX_ANALYSIS_BYTES = 512_000


def _record_key(
    owner_identifier: str,
    broker_name: str,
    account_identifier: str,
    scope: str,
) -> str:
    identity = [
        owner_identifier.strip().lower(),
        broker_name.strip().lower(),
        account_identifier.strip(),
        scope.strip().lower(),
    ]
    if any(not value for value in identity):
        raise ValueError("A user, broker, account, and analysis scope are required.")
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def save_portfolio_analysis(
    owner_identifier: str,
    broker_name: str,
    account_identifier: str,
    scope: str,
    analysis: dict[str, Any],
    analyzed_at: str,
    *,
    provider: str = "",
    model: str = "",
    storage_provider: StorageProvider | None = None,
) -> None:
    """Save a validated analysis without persisting broker or AI credentials."""
    if not isinstance(analysis, dict):
        raise TypeError("Portfolio analysis must be an object.")
    if not isinstance(analyzed_at, str) or not analyzed_at.strip():
        raise ValueError("An analysis timestamp is required.")
    try:
        analysis_json = json.dumps(
            analysis,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Portfolio analysis contains unsupported values.") from error
    if len(analysis_json.encode("utf-8")) > MAX_ANALYSIS_BYTES:
        raise ValueError("Portfolio analysis exceeds the storage size limit.")

    active_provider = storage_provider or get_storage_provider(settings)
    active_provider.put(
        "portfolio_analysis",
        _record_key(owner_identifier, broker_name, account_identifier, scope),
        {
            "analysis_json": analysis_json,
            "analyzed_at": analyzed_at,
            "provider": provider,
            "model": model,
        },
    )


def load_portfolio_analysis(
    owner_identifier: str,
    broker_name: str,
    account_identifier: str,
    scope: str,
    *,
    storage_provider: StorageProvider | None = None,
) -> dict[str, Any] | None:
    """Load the latest saved analysis for one user, broker account, and scope."""
    active_provider = storage_provider or get_storage_provider(settings)
    record = active_provider.get(
        "portfolio_analysis",
        _record_key(owner_identifier, broker_name, account_identifier, scope),
    )
    if record is None:
        return None

    try:
        analysis = json.loads(record["analysis_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("The saved portfolio analysis is invalid.") from error
    analyzed_at = record.get("analyzed_at")
    if not isinstance(analysis, dict) or not isinstance(analyzed_at, str):
        raise RuntimeError("The saved portfolio analysis is invalid.")
    return {
        "analysis": analysis,
        "analyzed_at": analyzed_at,
        "provider": str(record.get("provider", "")),
        "model": str(record.get("model", "")),
    }

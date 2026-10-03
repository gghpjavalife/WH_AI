"""Optional Turso and Resend integrations with session-safe configuration."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from .broker_factory import GenericDynamicAdapter
from .settings import configured_value, settings


def turso_is_configured() -> bool:
    return bool(
        _service_value("TURSO_PRIMARY_DB_URL", settings.turso_primary_db_url)
        and _service_value("TURSO_AUTH_TOKEN", settings.turso_auth_token)
    )


def _service_value(name: str, configured: str) -> str:
    return str(configured_value(name, configured)).strip()


def _turso_endpoint() -> str:
    database_url = _service_value(
        "TURSO_PRIMARY_DB_URL", settings.turso_primary_db_url
    )
    if database_url.startswith("libsql://"):
        database_url = "https://" + database_url.removeprefix("libsql://")
    parts = urlsplit(database_url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Turso must use a valid HTTPS database URL.")
    path = parts.path.rstrip("/")
    if path.endswith("/v2/pipeline"):
        return urlunsplit(("https", parts.netloc, path, "", ""))
    return urlunsplit(("https", parts.netloc, f"{path}/v2/pipeline", "", ""))


def _hrana_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": int(value)}
    if isinstance(value, int):
        return {"type": "integer", "value": value}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, bytes):
        return {
            "type": "blob",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, str):
        return {"type": "text", "value": value}
    raise TypeError(f"Unsupported Turso bind argument: {type(value).__name__}.")


def execute_turso_query(
    sql_statement: str, arguments: Sequence[Any] = ()
) -> list[dict[str, Any]]:
    """Execute one parameterized statement over Turso's Hrana HTTP pipeline."""
    statement = sql_statement.strip()
    if not statement or len(statement) > 20_000:
        raise ValueError("Provide a non-empty Turso statement under 20,000 characters.")
    token = _service_value("TURSO_AUTH_TOKEN", settings.turso_auth_token)
    if not token:
        raise RuntimeError("Configure TURSO_AUTH_TOKEN before using Turso storage.")
    if not _service_value("TURSO_PRIMARY_DB_URL", settings.turso_primary_db_url):
        raise RuntimeError(
            "Configure TURSO_PRIMARY_DB_URL before using Turso storage."
        )
    encoded_arguments = [_hrana_value(value) for value in arguments]
    response = requests.post(
        _turso_endpoint(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "requests": [
                {
                    "type": "execute",
                    "stmt": {"sql": statement, "args": encoded_arguments},
                },
                {"type": "close"},
            ]
        },
        timeout=(
            settings.http_connect_timeout_seconds,
            settings.database_timeout_seconds,
        ),
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results") if isinstance(payload, dict) else None
    first = results[0] if isinstance(results, list) and results else None
    if not isinstance(first, dict) or first.get("type") == "error":
        raise RuntimeError("Turso rejected the database statement.")
    statement_result = (first.get("response") or {}).get("result") or {}
    columns = statement_result.get("cols") or []
    rows = statement_result.get("rows") or []
    names = [str(column.get("name", "")) for column in columns]
    output = []
    for row in rows:
        values = []
        for cell in row:
            if cell.get("type") == "null":
                values.append(None)
            elif cell.get("type") == "blob":
                values.append(base64.b64decode(cell.get("base64", "")))
            else:
                values.append(cell.get("value"))
        output.append(dict(zip(names, values, strict=False)))
    return output


def send_resend_email(
    subject: str,
    html_content: str,
    user_email: str,
    *,
    api_key: str | None = None,
    sender: str | None = None,
) -> str:
    """Send HTML email through Resend without logging credentials or payloads."""
    key = (
        api_key
        or _service_value("RESEND_API_KEY", settings.resend_api_key)
    ).strip()
    source = (
        sender or _service_value("RESEND_SENDER", settings.resend_sender)
    ).strip()
    recipient = user_email.strip()
    if not key:
        raise RuntimeError("Configure a Resend API key before sending email.")
    if not source or not recipient:
        raise ValueError("Configure a verified Resend sender and recipient email.")
    if not subject.strip() or len(subject) > 200:
        raise ValueError("Email subject must contain 1–200 characters.")
    if len(html_content) > 1_000_000:
        raise ValueError("Email report exceeds the 1 MB limit.")
    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "from": source,
            "to": [recipient],
            "subject": subject.strip(),
            "html": html_content,
        },
        timeout=(
            settings.http_connect_timeout_seconds,
            settings.http_read_timeout_seconds,
        ),
    )
    response.raise_for_status()
    payload = response.json()
    message_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(message_id, str) or not message_id:
        raise RuntimeError("Resend did not acknowledge the email.")
    return message_id


def _tenant_key(user_identifier: str) -> str:
    value = user_identifier.strip()
    if not value:
        raise ValueError("An authenticated user identifier is required.")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ensure_dynamic_broker_table() -> None:
    execute_turso_query(
        """
        CREATE TABLE IF NOT EXISTS dynamic_broker_registry (
            owner_id TEXT NOT NULL,
            name TEXT NOT NULL,
            configuration_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (owner_id, name)
        )
        """
    )


def list_dynamic_brokers(user_identifier: str) -> list[str]:
    """List only the current tenant's custom broker definitions."""
    _ensure_dynamic_broker_table()
    rows = execute_turso_query(
        "SELECT name FROM dynamic_broker_registry WHERE owner_id = ? ORDER BY name",
        (_tenant_key(user_identifier),),
    )
    return [str(row["name"]) for row in rows if row.get("name")]


def register_dynamic_broker(
    user_identifier: str,
    name: str,
    configuration: dict[str, Any],
) -> None:
    """Validate and store a tenant-scoped, read-only broker endpoint definition."""
    normalized_name = name.strip()
    if not normalized_name or len(normalized_name) > 80:
        raise ValueError("Broker name must contain 1–80 characters.")
    validated = GenericDynamicAdapter(
        {**configuration, "name": normalized_name}
    )
    safe_configuration = {
        "name": validated.name,
        "api_base": validated.api_base,
        "endpoints": validated.endpoints,
    }
    _ensure_dynamic_broker_table()
    execute_turso_query(
        """
        INSERT INTO dynamic_broker_registry (owner_id, name, configuration_json)
        VALUES (?, ?, ?)
        ON CONFLICT(owner_id, name) DO UPDATE SET
            configuration_json = excluded.configuration_json
        """,
        (
            _tenant_key(user_identifier),
            normalized_name,
            json.dumps(safe_configuration, separators=(",", ":"), sort_keys=True),
        ),
    )


def load_dynamic_broker(
    user_identifier: str, name: str
) -> dict[str, Any] | None:
    """Load one broker definition while enforcing tenant ownership."""
    _ensure_dynamic_broker_table()
    rows = execute_turso_query(
        """
        SELECT configuration_json FROM dynamic_broker_registry
        WHERE owner_id = ? AND name = ?
        """,
        (_tenant_key(user_identifier), name.strip()),
    )
    if not rows:
        return None
    try:
        configuration = json.loads(str(rows[0]["configuration_json"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Stored dynamic broker definition is invalid.") from exc
    if not isinstance(configuration, dict):
        raise RuntimeError("Stored dynamic broker definition is invalid.")
    GenericDynamicAdapter(configuration)
    return configuration

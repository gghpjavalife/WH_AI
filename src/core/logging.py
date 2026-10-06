"""Privacy-conscious diagnostics for external service failures."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from http import HTTPStatus

logger = logging.getLogger("wealth_home_ai")
logger.setLevel(logging.INFO)

_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.: -]")
_REQUEST_ID_HEADERS = (
    "x-request-id",
    "request-id",
    "x-correlation-id",
    "correlation-id",
)


def _cause_with_status(error: BaseException) -> BaseException:
    current = error
    while current.__cause__ is not None:
        current = current.__cause__
    return current


def diagnostic_summary(operation: str, error: BaseException) -> str:
    """Build a UI-safe error summary without response bodies or credentials."""
    operation = _SAFE_LABEL.sub("_", operation.strip())[:80]
    source = _cause_with_status(error)
    details = [type(error).__name__]
    status = getattr(source, "status", None)
    response = getattr(source, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if isinstance(status, int):
        details.append(f"HTTP {status}")
        try:
            details.append(HTTPStatus(status).phrase)
        except ValueError:
            pass

    headers = getattr(source, "headers", None)
    if not isinstance(headers, Mapping) and response is not None:
        headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        normalized_headers = {
            str(name).lower(): value for name, value in headers.items()
        }
        for header_name in _REQUEST_ID_HEADERS:
            request_id = normalized_headers.get(header_name)
            if isinstance(request_id, str) and request_id:
                safe_id = _SAFE_LABEL.sub("_", request_id.strip())[:80]
                if safe_id:
                    details.append(f"request_id={safe_id}")
                    break

    return f"{operation} failed ({'; '.join(details)}). See server logs for details."


def log_failure(
    operation: str, error: BaseException, *, broker: str | None = None
) -> str:
    """Log safe exception metadata and return a UI-ready diagnostic summary."""
    summary = diagnostic_summary(operation, error)
    safe_broker = _SAFE_LABEL.sub("_", broker)[:40] if broker else ""
    broker_context = f" broker={safe_broker}" if safe_broker else ""
    logger.error("%s%s", summary, broker_context)
    return summary


def log_success(operation: str, *, broker: str, item_count: int) -> None:
    """Log an external feed refresh without logging account or holding data."""
    logger.info(
        "%s completed broker=%s item_count=%d",
        operation,
        _SAFE_LABEL.sub("_", broker)[:40],
        item_count,
    )


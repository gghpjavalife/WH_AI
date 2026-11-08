"""Database-neutral persistence contracts for application repositories."""

from __future__ import annotations

from typing import Any, Protocol


class StorageRequestPending(RuntimeError):
    """Raised when a browser storage request is waiting for its client response."""


class StorageProvider(Protocol):
    """CRUD interface implemented by device-local storage backends."""

    def get(self, collection: str, key: str) -> dict[str, Any] | None: ...

    def put(self, collection: str, key: str, value: dict[str, Any]) -> None: ...

    def delete(self, collection: str, key: str) -> None: ...

    def delete_collection(self, collection: str) -> None:
        """Delete all records in a collection, idempotently.

        Browser callers must retry after StorageRequestPending until acknowledged.
        Browser collection names must not contain ':' because keys use that delimiter.
        """
        ...

    def consume(self, collection: str, key: str) -> dict[str, Any] | None: ...

    def get_or_create_device_id(self) -> str: ...

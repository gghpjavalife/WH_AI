"""IndexedDB-backed storage provider for Streamlit and browser runtimes."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import streamlit as st
import streamlit.components.v1 as components

from .provider import StorageRequestPending

_COMPONENT_PATH = (
        Path(__file__).resolve().parents[1] / "ui" / "components" / "indexeddb_storage"
)
_storage_component = components.declare_component(
    "wh_ai_indexeddb_storage",
    path=str(_COMPONENT_PATH),
)


def _state() -> Any:
    return st.session_state


def _cache_key(collection: str, key: str) -> str:
    return f"{collection}:{key}"


def _failure_key(action: str, collection: str, key: str) -> str | tuple[str, str]:
    if action == "delete_collection":
        return (action, collection)
    return _cache_key(collection, key)


def _clear_collection_cache(session: Any, collection: str) -> None:
    prefix = f"{collection}:"
    for name in ("_local_storage_cache", "_local_storage_consume_results"):
        cache = session.setdefault(name, {})
        for key in list(cache):
            if key.startswith(prefix):
                del cache[key]


def _enqueue(
        action: str,
        collection: str,
        key: str,
        value: dict[str, Any] | None = None,
) -> None:
    session = _state()
    request_key = _failure_key(action, collection, key)
    failures = session.setdefault("_local_storage_failures", {})
    if request_key in failures:
        raise RuntimeError(failures[request_key])

    queue = session.setdefault("_local_storage_queue", [])
    for existing in reversed(queue):
        if existing["collection"] == collection and (
            existing["action"] == "delete_collection" or action == "delete_collection"
        ):
            if existing["action"] == action:
                return
            break
        if (
                existing["collection"] == collection
                and existing["key"] == key
                and existing["action"] == action
        ):
            existing["value"] = value
            return
    queue.append(
        {
            "request_id": uuid4().hex,
            "action": action,
            "collection": collection,
            "key": key,
            "value": value,
        }
    )


class IndexedDBStorageProvider:
    """Browser storage whose requests are executed only by the user's browser."""

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        session = _state()
        cache_key = _cache_key(collection, key)
        cache = session.setdefault("_local_storage_cache", {})
        if cache_key in cache:
            return cache[cache_key]

        _enqueue("get", collection, key)
        raise StorageRequestPending("Waiting for browser storage.")

    def put(self, collection: str, key: str, value: dict[str, Any]) -> None:
        session = _state()
        session.setdefault("_local_storage_cache", {})[
            _cache_key(collection, key)
        ] = value
        _enqueue("put", collection, key, value)

    def delete(self, collection: str, key: str) -> None:
        _state().setdefault("_local_storage_cache", {}).pop(
            _cache_key(collection, key), None
        )
        _enqueue("delete", collection, key)

    def delete_collection(self, collection: str) -> None:
        if not isinstance(collection, str) or not collection or ":" in collection:
            raise ValueError("A nonempty collection name without ':' is required.")
        session = _state()
        results = session.setdefault("_local_storage_collection_delete_results", set())
        if collection in results:
            results.remove(collection)
            return
        _enqueue("delete_collection", collection, "")
        _clear_collection_cache(session, collection)
        raise StorageRequestPending("Waiting for browser storage.")

    def consume(self, collection: str, key: str) -> dict[str, Any] | None:
        session = _state()
        result_key = _cache_key(collection, key)
        results = session.setdefault("_local_storage_consume_results", {})
        if result_key in results:
            return results.pop(result_key)
        _enqueue("consume", collection, key)
        raise StorageRequestPending("Waiting for browser storage.")

    def get_or_create_device_id(self) -> str:
        session = _state()
        if "anonymous_device_id" in session:
            return str(session["anonymous_device_id"])
        _enqueue("get_device_id", "device_metadata", "anonymous_device_id")
        raise StorageRequestPending("Waiting for browser storage.")


def process_browser_storage_bridge() -> None:
    """Render the browser bridge and transfer only request results to Python."""
    session = _state()
    queue = session.setdefault("_local_storage_queue", [])
    active = session.get("_local_storage_active")
    if active is None and queue:
        active = queue[0]
        session["_local_storage_active"] = active

    if session.get("_local_storage_error"):
        st.error(f"Browser storage failed: {session['_local_storage_error']}")
    response = _storage_component(
        request=active,
        default=None,
        key="wh_ai_indexeddb_storage_bridge",
        height=1,
    )
    if not isinstance(response, dict) or not active:
        return
    if response.get("request_id") != active.get("request_id"):
        return

    if response.get("error"):
        message = str(response["error"])
        session.setdefault("_local_storage_failures", {})[
            _failure_key(active["action"], active["collection"], active["key"])
        ] = message
        session["_local_storage_error"] = message
    elif active["action"] == "get_device_id":
        device_id = str(response.get("value", ""))
        if not device_id:
            session.setdefault("_local_storage_failures", {})[
                _cache_key(active["collection"], active["key"])
            ] = "Browser storage did not return a device ID."
        else:
            session["anonymous_device_id"] = device_id
    elif active["action"] == "get":
        session.setdefault("_local_storage_cache", {})[
            _cache_key(active["collection"], active["key"])
        ] = response.get("value")
    elif active["action"] == "consume":
        session.setdefault("_local_storage_consume_results", {})[
            _cache_key(active["collection"], active["key"])
        ] = response.get("value")
    elif active["action"] == "delete_collection":
        if response.get("value") is True:
            _clear_collection_cache(session, active["collection"])
            session.setdefault("_local_storage_collection_delete_results", set()).add(
                active["collection"]
            )
        else:
            message = "Browser storage did not confirm collection deletion."
            session.setdefault("_local_storage_failures", {})[
                _failure_key(active["action"], active["collection"], active["key"])
            ] = message
            session["_local_storage_error"] = message

    if queue and queue[0].get("request_id") == active.get("request_id"):
        queue.pop(0)
    session.pop("_local_storage_active", None)
    st.rerun()


def has_pending_browser_write(collection: str) -> bool:
    queue = _state().get("_local_storage_queue", [])
    return any(
        item.get("collection") == collection and item.get("action") == "put"
        for item in queue
    )

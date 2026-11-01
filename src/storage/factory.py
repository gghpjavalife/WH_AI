"""Runtime selection of device-local persistence providers."""

from __future__ import annotations

from core.config import settings as runtime_settings

from .provider import StorageProvider


def is_browser_runtime() -> bool:
    """Streamlit scripts execute on a server; their durable store is the browser."""
    try:
        import streamlit as st
    except ImportError:
        return False
    return st.runtime.exists()


def get_storage_provider(settings_override=None) -> StorageProvider:
    if is_browser_runtime():
        from .indexeddb_provider import IndexedDBStorageProvider

        return IndexedDBStorageProvider()
    from .sqlite_provider import SQLiteStorageProvider

    return SQLiteStorageProvider(settings_override or runtime_settings)

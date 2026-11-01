"""Local storage providers and repositories."""

from .provider import StorageProvider, StorageRequestPending
from .repository import UserSettingsRepository


def get_storage_provider(settings_override=None):
    from .factory import get_storage_provider as resolve_storage_provider

    return resolve_storage_provider(settings_override)


__all__ = [
    "StorageProvider",
    "StorageRequestPending",
    "UserSettingsRepository",
    "get_storage_provider",
]

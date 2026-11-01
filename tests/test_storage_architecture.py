import secrets
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import services.database as secure_database
from core.config import settings
from storage.factory import get_storage_provider
from storage.indexeddb_provider import IndexedDBStorageProvider
from storage.sqlite_provider import SQLiteStorageProvider


class StorageArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "local.sqlite3"
        self.configured_settings = replace(
            settings,
            oauth_database_path=str(self.database),
            database_encryption_key=secrets.token_hex(32),
            database_timeout_seconds=2,
        )
        self.database_settings_patch = patch.object(
            secure_database, "settings", self.configured_settings
        )
        self.database_settings_patch.start()
        self.addCleanup(self.database_settings_patch.stop)
        self.addCleanup(self.temporary_directory.cleanup)

    def test_platform_factory_selects_browser_or_sqlite_provider(self):
        with patch("storage.factory.is_browser_runtime", return_value=True):
            browser_provider = get_storage_provider(self.configured_settings)
        self.assertIsInstance(browser_provider, IndexedDBStorageProvider)

        with patch("storage.factory.is_browser_runtime", return_value=False):
            sqlite_provider = get_storage_provider(self.configured_settings)
        self.assertIsInstance(sqlite_provider, SQLiteStorageProvider)

    def test_browser_component_uses_streamlit_message_protocol(self):
        component_path = (
            Path(__file__).parents[1]
            / "src"
            / "ui"
            / "components"
            / "indexeddb_storage"
            / "index.html"
        )
        component = component_path.read_text(encoding="utf-8")
        self.assertIn(
            "{ isStreamlitMessage: true, type, ...data }",
            component,
        )

    def test_sqlite_provider_preserves_settings_schema_and_device_identity(self):
        provider = SQLiteStorageProvider(self.configured_settings)
        provider.put("drafts", "draft-1", {"title": "Local draft"})
        self.assertEqual(
            provider.get("drafts", "draft-1"), {"title": "Local draft"}
        )
        provider.delete("drafts", "draft-1")
        self.assertIsNone(provider.get("drafts", "draft-1"))

        provider.put(
            "user_settings",
            "owner-hash",
            {
                "preferences_json": '{"theme":"dark"}',
                "sensitive_json": '{"api_key":"local-secret"}',
                "sensitive_consent": True,
            },
        )

        stored = provider.get("user_settings", "owner-hash")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored["preferences_json"], '{"theme":"dark"}')
        self.assertEqual(stored["sensitive_json"], '{"api_key":"local-secret"}')
        self.assertTrue(stored["sensitive_consent"])
        self.assertEqual(
            provider.get_or_create_device_id(),
            provider.get_or_create_device_id(),
        )

        connection = secure_database.connect_database(self.database)
        try:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(user_settings)")
            }
        finally:
            connection.close()
        self.assertEqual(
            columns,
            {
                "owner_id",
                "preferences_json",
                "encrypted_sensitive",
                "sensitive_consent",
                "updated_at",
            },
        )


if __name__ == "__main__":
    unittest.main()

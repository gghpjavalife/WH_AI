import secrets
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import services.database as secure_database
from core.config import settings
from storage.factory import get_storage_provider
from storage.indexeddb_provider import IndexedDBStorageProvider, process_browser_storage_bridge
from storage.provider import StorageRequestPending
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

    def test_sqlite_delete_collection_is_scoped_and_idempotent(self):
        provider = SQLiteStorageProvider(self.configured_settings)
        settings_value = {
            "preferences_json": '{"theme":"dark"}',
            "sensitive_json": '{"api_key":"secret"}',
            "sensitive_consent": True,
        }
        provider.put("user_settings", "owner", settings_value)
        device_id = provider.get_or_create_device_id()
        provider.put("portfolio_analysis", "first", {"analysis": 1})
        provider.put("portfolio_analysis", "second", {"analysis": 2})
        provider.put("portfolio_analysis_extra", "first", {"keep": True})
        provider.put("drafts", "first", {"keep": True})
        provider.put("oauth_states", "ab" * 32, {"expires_at": 9999999999})

        provider.delete_collection("portfolio_analysis")
        provider.delete_collection("portfolio_analysis")

        self.assertIsNone(provider.get("portfolio_analysis", "first"))
        self.assertIsNone(provider.get("portfolio_analysis", "second"))
        self.assertEqual(provider.get("portfolio_analysis_extra", "first"), {"keep": True})
        self.assertEqual(provider.get("drafts", "first"), {"keep": True})
        self.assertEqual(provider.get("user_settings", "owner"), settings_value)
        self.assertEqual(provider.get_or_create_device_id(), device_id)
        self.assertEqual(provider.get("oauth_states", "ab" * 32)["expires_at"], 9999999999)
        for invalid in ("", "portfolio_analysis:other"):
            with self.assertRaises(ValueError):
                provider.delete_collection(invalid)

    def test_browser_delete_collection_waits_for_ack_and_invalidates_cache(self):
        session = {
            "_local_storage_cache": {
                "portfolio_analysis:first": {"old": True},
                "portfolio_analysis:second": {"old": True},
                "portfolio_analysis_extra:first": {"keep": True},
                "user_settings:owner": {"keep": True},
            }
        }
        provider = IndexedDBStorageProvider()
        with patch("storage.indexeddb_provider._state", return_value=session), \
                patch("storage.indexeddb_provider.st.rerun"):
            with self.assertRaises(StorageRequestPending):
                provider.delete_collection("portfolio_analysis")
            with self.assertRaises(StorageRequestPending):
                provider.delete_collection("portfolio_analysis")
            self.assertEqual(len(session["_local_storage_queue"]), 1)
            self.assertEqual(
                session["_local_storage_cache"],
                {"portfolio_analysis_extra:first": {"keep": True},
                 "user_settings:owner": {"keep": True}},
            )
            request = session["_local_storage_queue"][0]
            session["_local_storage_cache"]["portfolio_analysis:first"] = {"stale": True}
            with patch("storage.indexeddb_provider._storage_component", return_value={
                "request_id": request["request_id"], "value": True,
            }):
                process_browser_storage_bridge()
            provider.delete_collection("portfolio_analysis")
            self.assertNotIn("portfolio_analysis:first", session["_local_storage_cache"])
            self.assertFalse(session["_local_storage_queue"])
            with self.assertRaises(StorageRequestPending):
                provider.delete_collection("portfolio_analysis")

    def test_browser_delete_collection_reports_failure(self):
        session = {}
        provider = IndexedDBStorageProvider()
        with patch("storage.indexeddb_provider._state", return_value=session), \
                patch("storage.indexeddb_provider.st.rerun"):
            with self.assertRaises(StorageRequestPending):
                provider.delete_collection("portfolio_analysis")
            request = session["_local_storage_queue"][0]
            with patch("storage.indexeddb_provider._storage_component", return_value={
                "request_id": request["request_id"], "error": "transaction aborted",
            }):
                process_browser_storage_bridge()
            with self.assertRaisesRegex(RuntimeError, "transaction aborted"):
                provider.delete_collection("portfolio_analysis")
            for invalid in ("", "portfolio_analysis:other"):
                with self.assertRaises(ValueError):
                    provider.delete_collection(invalid)

    def test_browser_delete_collection_is_a_queue_ordering_barrier(self):
        session = {}
        provider = IndexedDBStorageProvider()
        with patch("storage.indexeddb_provider._state", return_value=session):
            provider.put("portfolio_analysis", "key", {"version": 1})
            with self.assertRaises(StorageRequestPending):
                provider.delete_collection("portfolio_analysis")
            provider.put("portfolio_analysis", "key", {"version": 2})
            queue = session["_local_storage_queue"]
            self.assertEqual([item["action"] for item in queue],
                             ["put", "delete_collection", "put"])
            self.assertEqual(queue[0]["value"], {"version": 1})
            self.assertEqual(queue[2]["value"], {"version": 2})

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the browser bridge test")
    def test_browser_collection_delete_transaction(self):
        subprocess.run(
            ["node", str(Path(__file__).with_name("test_indexeddb_cleanup.cjs"))],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()

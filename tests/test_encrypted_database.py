import os
import secrets
import sqlite3
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import services.database as secure_database
from core.config import settings
from security.database_migration import migrate_plaintext_database


class EncryptedDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "private.sqlite3"
        self.configured_settings = replace(
            settings,
            oauth_database_path=str(self.database),
            database_encryption_key=secrets.token_hex(32),
        )
        self.settings_patch = patch.object(
            secure_database,
            "settings",
            self.configured_settings,
        )
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.addCleanup(self.directory.cleanup)

    def test_database_pages_are_encrypted_and_open_only_with_correct_key(self):
        connection = secure_database.connect_database(self.database)
        try:
            connection.execute("CREATE TABLE confidential(value TEXT)")
            connection.execute(
                "INSERT INTO confidential(value) VALUES (?)",
                ("strictly-confidential-value",),
            )
            connection.commit()
            self.assertTrue(
                connection.execute("PRAGMA cipher_version").fetchone()[0]
            )
        finally:
            connection.close()

        database_bytes = self.database.read_bytes()
        self.assertNotEqual(database_bytes[:16], b"SQLite format 3\x00")
        self.assertNotIn(b"strictly-confidential-value", database_bytes)
        if os.name != "nt":
            self.assertEqual(
                stat.S_IMODE(self.database.stat().st_mode),
                0o600,
            )
        with self.assertRaises(sqlite3.DatabaseError):
            plain_connection = sqlite3.connect(self.database)
            try:
                plain_connection.execute(
                    "SELECT * FROM sqlite_master"
                ).fetchall()
            finally:
                plain_connection.close()

        wrong_key_settings = replace(
            self.configured_settings,
            database_encryption_key=secrets.token_hex(32),
        )
        with patch.object(secure_database, "settings", wrong_key_settings):
            with self.assertRaisesRegex(
                secure_database.DatabaseSecurityError,
                "could not be opened",
            ):
                secure_database.connect_database(self.database)

    def test_plaintext_database_is_rejected_until_explicit_migration(self):
        plain_connection = sqlite3.connect(self.database)
        plain_connection.execute("CREATE TABLE legacy(value TEXT)")
        plain_connection.execute(
            "INSERT INTO legacy(value) VALUES (?)",
            ("preserved-during-migration",),
        )
        plain_connection.commit()
        plain_connection.close()

        with self.assertRaises(secure_database.PlaintextDatabaseDetected):
            secure_database.connect_database(self.database)

        migrate_plaintext_database(self.database)
        self.assertNotEqual(
            self.database.read_bytes()[:16],
            b"SQLite format 3\x00",
        )
        encrypted = secure_database.connect_database(self.database)
        try:
            self.assertEqual(
                encrypted.execute("SELECT value FROM legacy").fetchone()[0],
                "preserved-during-migration",
            )
            self.assertEqual(
                encrypted.execute("PRAGMA quick_check").fetchone()[0],
                "ok",
            )
        finally:
            encrypted.close()

    def test_keyring_key_is_loaded_and_provisioning_refuses_overwrite(self):
        key = secrets.token_hex(32)
        keyring = MagicMock()
        keyring.get_password.return_value = key
        with patch.object(
            secure_database,
            "_native_keyring_backend",
            return_value=(keyring, object()),
        ), patch.object(
            secure_database,
            "settings",
            replace(self.configured_settings, database_encryption_key=""),
        ):
            self.assertEqual(secure_database.database_key_hex(), key)
            with self.assertRaisesRegex(
                secure_database.DatabaseKeyUnavailable,
                "already present",
            ):
                secure_database.provision_database_key()
        keyring.set_password.assert_not_called()

    def test_environment_key_requires_exact_raw_key_length(self):
        invalid_settings = replace(
            self.configured_settings,
            database_encryption_key="too-short",
        )
        with patch.object(secure_database, "settings", invalid_settings):
            with self.assertRaisesRegex(
                secure_database.DatabaseKeyUnavailable,
                "64 hexadecimal characters",
            ):
                secure_database.database_key_hex()

    def test_keyring_provisioning_creates_random_key_without_printing_it(self):
        keyring = MagicMock()
        stored_values = {}

        def get_stored_value(service: str, account: str) -> str | None:
            return stored_values.get((service, account))

        def set_stored_value(service: str, account: str, value: str) -> None:
            stored_values[(service, account)] = value

        keyring.get_password.side_effect = get_stored_value
        keyring.set_password.side_effect = set_stored_value
        with patch.object(
            secure_database,
            "_native_keyring_backend",
            return_value=(keyring, object()),
        ), patch.object(
            secure_database,
            "settings",
            replace(self.configured_settings, database_encryption_key=""),
        ):
            secure_database.provision_database_key()
        keyring.set_password.assert_called_once()
        saved_key = keyring.set_password.call_args.args[2]
        self.assertEqual(len(saved_key), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in saved_key))


if __name__ == "__main__":
    unittest.main()

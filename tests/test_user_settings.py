import secrets
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import services.database as secure_database
import services.user_settings as user_settings
from core.config import settings
from ui.user_settings import _validate_contact, _validate_email, _validate_phone


class UserSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "settings.sqlite3"
        self.configured_settings = replace(
            settings,
            oauth_database_path=str(self.database),
            database_encryption_key=secrets.token_hex(32),
            database_timeout_seconds=2,
        )
        self.database_settings_patch = patch.object(
            secure_database,
            "settings",
            self.configured_settings,
        )
        self.database_settings_patch.start()
        self.addCleanup(self.database_settings_patch.stop)
        self.settings_patch = patch.object(
            user_settings, "settings", self.configured_settings
        )
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.addCleanup(self.temporary_directory.cleanup)

    def test_preferences_save_without_persisting_sensitive_values_by_default(self):
        user_settings.save_user_settings(
            "person@example.com",
            {"default_broker": "Upstox"},
            {"profile": {"email": "person@example.com"}, "api_key": "private"},
            save_sensitive=False,
        )

        loaded = user_settings.load_user_settings("person@example.com")
        self.assertEqual(loaded["preferences"], {"default_broker": "Upstox"})
        self.assertEqual(loaded["sensitive"], {})
        self.assertFalse(loaded["sensitive_consent"])

        connection = secure_database.connect_database(self.database)
        try:
            stored = connection.execute(
                "SELECT preferences_json, encrypted_sensitive, sensitive_consent "
                "FROM user_settings"
            ).fetchone()
        finally:
            connection.close()
        self.assertIn("Upstox", stored[0])
        self.assertIsNone(stored[1])
        self.assertEqual(stored[2], 0)
        self.assertNotIn(b"private", self.database.read_bytes())
        self.assertNotIn(b"person@example.com", self.database.read_bytes())

    def test_sensitive_data_is_encrypted_and_scoped_to_its_owner(self):
        confidential = {
            "profile": {"email": "person@example.com", "phone": "+919876543210"},
            "broker_credentials": {"Upstox": {"api_secret": "broker-secret"}},
        }
        user_settings.save_user_settings(
            "person@example.com",
            {"default_broker": "Upstox"},
            confidential,
            save_sensitive=True,
        )

        loaded = user_settings.load_user_settings("person@example.com")
        self.assertEqual(loaded["sensitive"], confidential)
        self.assertTrue(loaded["sensitive_consent"])
        self.assertEqual(
            user_settings.load_user_settings("another@example.com")["sensitive"],
            {},
        )
        self.assertNotIn(b"broker-secret", self.database.read_bytes())
        self.assertNotIn(b"person@example.com", self.database.read_bytes())

    def test_withdrawing_consent_removes_existing_encrypted_values(self):
        user_settings.save_user_settings(
            "person@example.com",
            {},
            {"api_key": "existing-secret"},
            save_sensitive=True,
        )
        user_settings.save_user_settings(
            "person@example.com",
            {"default_broker": "Dhan"},
            {"api_key": "new-session-secret"},
            save_sensitive=False,
        )

        loaded = user_settings.load_user_settings("person@example.com")
        self.assertEqual(loaded["sensitive"], {})
        self.assertFalse(loaded["sensitive_consent"])

    def test_saving_again_updates_existing_preferences_and_profile(self):
        user_settings.save_user_settings(
            "person@example.com",
            {"risk_limit": 20},
            {"profile": {"email": "old@example.com"}},
            save_sensitive=True,
        )
        user_settings.save_user_settings(
            "person@example.com",
            {"risk_limit": 35},
            {"profile": {"email": "new@example.com", "phone": "+15551234567"}},
            save_sensitive=True,
        )

        loaded = user_settings.load_user_settings("person@example.com")
        self.assertEqual(loaded["preferences"], {"risk_limit": 35})
        self.assertEqual(
            loaded["sensitive"]["profile"],
            {"email": "new@example.com", "phone": "+15551234567"},
        )

    def test_contact_validation_requires_valid_email_and_country_code_phone(self):
        self.assertIsNone(_validate_email("person.name+tag@example.co.uk"))
        for invalid_email in (
            "not-an-email",
            "person@localhost",
            ".person@example.com",
            "person..name@example.com",
            "person@-example.com",
        ):
            with self.subTest(email=invalid_email):
                self.assertIsNotNone(_validate_email(invalid_email))

        self.assertIsNone(_validate_phone("+91 98765 43210"))
        self.assertIsNone(_validate_phone("+1-234-567-8901"))
        for invalid_phone in (
            "9876543210",
            "+91 98765 4321",
            "+91 98765 432100",
            "+0 98765 43210",
            "+91 abcde fghij",
        ):
            with self.subTest(phone=invalid_phone):
                self.assertIsNotNone(_validate_phone(invalid_phone))
        self.assertIsNotNone(
            _validate_contact(
                {"email": "bad@", "phone": "+91 98765 43210"}
            )
        )
        self.assertIsNotNone(
            _validate_contact(
                {"email": "person@example.com", "phone": "9876543210"}
            )
        )

    def test_invalid_owner_and_non_json_preferences_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "user identifier"):
            user_settings.load_user_settings(" ")
        with self.assertRaisesRegex(ValueError, "unsupported values"):
            user_settings.save_user_settings(
                "person@example.com",
                {"invalid": object()},
                {},
                save_sensitive=False,
            )


if __name__ == "__main__":
    unittest.main()

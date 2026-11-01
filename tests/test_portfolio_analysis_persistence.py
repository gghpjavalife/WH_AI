import unittest

from services.portfolio_analysis import (
    load_portfolio_analysis,
    save_portfolio_analysis,
)


class InMemoryStorage:
    def __init__(self):
        self.records = {}

    def get(self, collection, key):
        return self.records.get((collection, key))

    def put(self, collection, key, value):
        self.records[(collection, key)] = value

    def delete(self, collection, key):
        self.records.pop((collection, key), None)

    def consume(self, collection, key):
        return self.records.pop((collection, key), None)

    @staticmethod
    def get_or_create_device_id():
        return "test-device"


class PortfolioAnalysisPersistenceTests(unittest.TestCase):
    def test_analysis_round_trips_for_the_same_broker_account(self):
        storage = InMemoryStorage()
        analysis = {
            "analysis": "Diversification is improving.",
            "assessment": {"sentiment": "positive"},
            "cash_deployment_list": [],
        }

        save_portfolio_analysis(
            "owner@example.com",
            "Zerodha",
            "account-123",
            "all",
            analysis,
            "2026-11-01T10:00:00+05:30",
            provider="Gemini",
            model="fast-model",
            storage_provider=storage,
        )

        self.assertEqual(
            load_portfolio_analysis(
                "owner@example.com",
                "Zerodha",
                "account-123",
                "all",
                storage_provider=storage,
            ),
            {
                "analysis": analysis,
                "analyzed_at": "2026-11-01T10:00:00+05:30",
                "provider": "Gemini",
                "model": "fast-model",
            },
        )

    def test_analysis_is_isolated_by_owner_broker_account_and_scope(self):
        storage = InMemoryStorage()
        save_portfolio_analysis(
            "owner@example.com",
            "Zerodha",
            "account-123",
            "all",
            {"analysis": "Saved result"},
            "2026-11-01T10:00:00+05:30",
            storage_provider=storage,
        )

        other_account = load_portfolio_analysis(
            "owner@example.com",
            "Zerodha",
            "account-456",
            "all",
            storage_provider=storage,
        )
        other_owner = load_portfolio_analysis(
            "other@example.com",
            "Zerodha",
            "account-123",
            "all",
            storage_provider=storage,
        )
        other_scope = load_portfolio_analysis(
            "owner@example.com",
            "Zerodha",
            "account-123",
            "equity",
            storage_provider=storage,
        )

        self.assertIsNone(other_account)
        self.assertIsNone(other_owner)
        self.assertIsNone(other_scope)

    def test_new_run_replaces_the_saved_analysis_for_that_account(self):
        storage = InMemoryStorage()
        for summary, analyzed_at in (
            ("Earlier result", "2026-10-31T10:00:00+05:30"),
            ("Refreshed result", "2026-11-01T10:00:00+05:30"),
        ):
            save_portfolio_analysis(
                "owner@example.com",
                "Zerodha",
                "account-123",
                "all",
                {"analysis": summary},
                analyzed_at,
                storage_provider=storage,
            )

        saved = load_portfolio_analysis(
            "owner@example.com",
            "Zerodha",
            "account-123",
            "all",
            storage_provider=storage,
        )

        self.assertIsNotNone(saved)
        self.assertEqual(saved["analysis"]["analysis"], "Refreshed result")
        self.assertEqual(saved["analyzed_at"], "2026-11-01T10:00:00+05:30")

    def test_analysis_requires_a_stable_account_identity(self):
        with self.assertRaises(ValueError):
            save_portfolio_analysis(
                "owner@example.com",
                "Zerodha",
                "",
                "all",
                {"analysis": "Not saved"},
                "2026-11-01T10:00:00+05:30",
                storage_provider=InMemoryStorage(),
            )


if __name__ == "__main__":
    unittest.main()

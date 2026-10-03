from datetime import datetime, timedelta
from unittest import TestCase
from zoneinfo import ZoneInfo

from wealth_home_ai.jev_rules import JevRuleEngine, buy_execution_block_reason


class JevRuleEngineTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 5, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.targets = [
            {
                "Ticker": "TCS",
                "Entry_Price": 1000,
                "Target_Price": 1200,
                "Confidence_Score": 85,
                "Sector": "Information Technology",
            },
            {
                "Ticker": "HDFCBANK",
                "Entry_Price": 2000,
                "Target_Price": 2300,
                "Confidence_Score": 92,
                "Sector": "Financials",
            },
        ]

    def _engine(self, **facts):
        return JevRuleEngine(
            {
                "now": self.now,
                "cash_balance": 50_000,
                "active_positions_count": 0,
                "realized_daily_loss_pct": 0,
                "max_allocation_pct": 100,
                "llm_targets": self.targets,
                **facts,
            }
        )

    def test_circuit_breaker_sets_persistent_24_hour_buy_lock(self):
        engine = self._engine(realized_daily_loss_pct=-3.1)

        trades, audit = engine.run()

        self.assertEqual(trades, [])
        self.assertEqual(
            engine.facts["buy_lock_until"], self.now + timedelta(hours=24)
        )
        self.assertTrue(any("Rule A" in line for line in audit))

    def test_circuit_breaker_keeps_existing_lock_until_its_expiry(self):
        expiry = self.now + timedelta(hours=5)
        engine = self._engine(buy_lock_until=expiry)

        trades, audit = engine.run()

        self.assertEqual(trades, [])
        self.assertEqual(engine.facts["buy_lock_until"], expiry)
        self.assertTrue(any("existing BUY lock" in line for line in audit))

    def test_execution_guard_rechecks_circuit_breaker_and_lock_expiry(self):
        reason, lock_until = buy_execution_block_reason(
            -3.0, None, 0, self.now
        )

        self.assertIn("locked for 24 hours", reason)
        self.assertEqual(lock_until, self.now + timedelta(hours=24))

        reason, retained_lock = buy_execution_block_reason(
            0.0, lock_until, 0, self.now + timedelta(hours=1)
        )
        self.assertIn("locked until", reason)
        self.assertEqual(retained_lock, lock_until)

        reason, cleared_lock = buy_execution_block_reason(
            0.0, lock_until, 0, lock_until + timedelta(minutes=1)
        )
        self.assertIsNone(reason)
        self.assertIsNone(cleared_lock)

    def test_execution_guard_rechecks_trading_window_and_focus_cap(self):
        weekend = self.now.replace(day=10)
        reason, lock_until = buy_execution_block_reason(
            0.0, None, 0, weekend
        )
        self.assertIn("Monday–Friday", reason)
        self.assertIsNone(lock_until)

        reason, lock_until = buy_execution_block_reason(
            0.0, None, 8, self.now
        )
        self.assertIn("eight active positions", reason)
        self.assertIsNone(lock_until)

    def test_trading_window_blocks_weekends_and_times_after_three(self):
        engine = self._engine(
            now=self.now.replace(hour=15, minute=1)
        )

        trades, audit = engine.run()

        self.assertEqual(trades, [])
        self.assertTrue(any("Rule B" in line for line in audit))

    def test_focus_cap_blocks_at_eight_active_positions(self):
        engine = self._engine(active_positions_count=8)

        trades, audit = engine.run()

        self.assertEqual(trades, [])
        self.assertTrue(any("Rule C" in line for line in audit))

    def test_confidence_boundary_and_allocation_cap_limit_abundant_cash(self):
        targets = [
            *self.targets,
            {
                "Ticker": "INFY",
                "Entry_Price": 1000,
                "Target_Price": 1200,
                "Confidence_Score": 69.9,
                "Sector": "Information Technology",
            },
        ]
        engine = self._engine(
            llm_targets=targets,
            max_allocation_pct=40,
        )

        trades, audit = engine.run()

        self.assertEqual({trade["Ticker"] for trade in trades}, {"TCS", "HDFCBANK"})
        self.assertLessEqual(sum(t["Qty"] * t["Entry_Price"] for t in trades), 20_000)
        self.assertTrue(any("Rule D" in line for line in audit))
        self.assertTrue(any("Rule E" in line for line in audit))

    def test_scarce_cash_prioritizes_unheld_sector_before_confidence(self):
        targets = [
            {
                **self.targets[1],
                "Confidence_Score": 98,
            },
            {
                **self.targets[0],
                "Confidence_Score": 75,
            },
        ]
        engine = self._engine(
            cash_balance=10_000,
            llm_targets=targets,
            sector_holdings=["Financials"],
        )

        trades, audit = engine.run()

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["Ticker"], "TCS")
        self.assertTrue(any("Rule F" in line for line in audit))

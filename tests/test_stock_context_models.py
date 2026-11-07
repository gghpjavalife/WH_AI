import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pydantic import ValidationError

from services.stock_context_models import (
    InvestmentHorizon, ShareholderDistributionMetrics, StockSignalContext,
    TechnicalIndicators, TradeSignalResponse, local_gatekeeper, run_triggered_analysis,
)


def context(**changes):
    data = {
        "horizon": "LONG_TERM_EQUITY",
        "profile": {"ticker": "TEST", "full_summary": "x" * 10000},
        "current_price": 100,
        "fundamentals": {"market_cap": 1000, "float_market_cap": 100,
                         "altman_z_score": 1.81, "altman_model_applicable": True, "dividend_yield": .05,
                         "free_cash_flow": -1},
        "technicals": {"sma_200": 100, "rsi_14": 70},
        "shareholders": {"promoter_pledged_shares_pct": .25},
    }
    data.update(changes)
    return StockSignalContext.model_validate(data)


class ModelsTests(unittest.TestCase):
    def test_circuit_threshold_and_action_shield(self):
        for distance, eligible in ((-1, False), (0, False), (1.499, False),
                                   (1.5, True), (2, True), (None, True)):
            c = context(intraday={"distance_to_lower_circuit_pct": distance})
            result = local_gatekeeper(c)
            self.assertEqual(result.is_eligible_for_llm, eligible)
            if distance is None:
                self.assertTrue(any("circuit proximity unavailable" in item.lower()
                                    for item in result.local_warnings))
        for event, expected in (("SPLIT", False), ("BONUS", False), ("DIVIDEND_EX", True),
                                ("NONE", True), (None, True)):
            c = context(current_price=99, technicals={"sma_200": 100, "rsi_14": 71},
                        news={"upcoming_corporate_action_event": event},
                        intraday={"distance_to_lower_circuit_pct": 1})
            result = local_gatekeeper(c)
            self.assertEqual(result.technical_divergence_detected, expected)
            self.assertFalse(result.is_eligible_for_llm)
            with self.assertRaises(ValueError):
                run_triggered_analysis(c, triggered=True, llm_callable=lambda _: self.fail("LLM invoked"))
        with self.assertRaises(ValidationError):
            context(intraday={"lower_circuit_limit": 100, "upper_circuit_limit": 99})
        with self.assertRaises(ValidationError):
            context(news={"upcoming_corporate_action_event": "RIGHTS"})

    def test_intraday_gate_recomputes_distance_from_live_price(self):
        data = {"vwap": 100, "distance_from_vwap_pct": 0, "opening_range_breakout": "NONE",
                "time_of_day_segment": "POWER_HOUR", "relative_volume_intraday": 1,
                "bid_ask_spread": 1, "lower_circuit_limit": 100,
                "distance_to_lower_circuit_pct": 20}
        c = context(horizon=InvestmentHorizon.INTRADAY_TRADING, current_price=101, intraday=data)
        self.assertFalse(local_gatekeeper(c).is_eligible_for_llm)

    def test_allocation_and_ranges(self):
        ShareholderDistributionMetrics(pct_held_by_institutions=.3, pct_held_by_insiders=.2,
                                       pct_held_by_retail=.52)
        with self.assertRaises(ValidationError):
            ShareholderDistributionMetrics(pct_held_by_institutions=.3, pct_held_by_insiders=.2,
                                           pct_held_by_retail=.521)
        for value in (float("nan"), float("inf"), True, -0.01, 1.01):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ShareholderDistributionMetrics(promoter_pledged_shares_pct=value)

    def test_intraday_requires_every_metric_and_enums(self):
        data = {"vwap": 100, "distance_from_vwap_pct": 0, "opening_range_breakout": "NONE",
                "time_of_day_segment": "POWER_HOUR", "relative_volume_intraday": 0,
                "bid_ask_spread": 0}
        context(horizon=InvestmentHorizon.INTRADAY_TRADING, intraday=data)
        for name in data:
            with self.subTest(name=name), self.assertRaises(ValidationError):
                context(horizon=InvestmentHorizon.INTRADAY_TRADING,
                        intraday={**data, name: None})
        with self.assertRaises(ValidationError):
            context(intraday={**data, "opening_range_breakout": "unknown"})

    def test_thresholds_and_divergence(self):
        c = context()
        self.assertTrue(local_gatekeeper(c).is_eligible_for_llm)
        c.fundamentals.altman_z_score = 1.809
        self.assertFalse(local_gatekeeper(c).is_eligible_for_llm)
        c.fundamentals.altman_z_score = 1.81
        c.fundamentals.float_market_cap = 99
        self.assertFalse(local_gatekeeper(c).is_eligible_for_llm)
        c.fundamentals.float_market_cap = 100
        c.fundamentals.float_market_cap = 1001
        self.assertFalse(local_gatekeeper(c).is_eligible_for_llm)
        c.fundamentals.float_market_cap = 100
        c.shareholders.promoter_pledged_shares_pct = .5
        result = local_gatekeeper(c)
        self.assertTrue(result.is_eligible_for_llm)
        self.assertTrue(result.local_warnings)
        c.shareholders.promoter_pledged_shares_pct = .501
        self.assertFalse(local_gatekeeper(c).is_eligible_for_llm)
        c.shareholders.promoter_pledged_shares_pct = .25
        c.fundamentals.dividend_yield = .051
        c.current_price = 99
        c.technicals.rsi_14 = 71
        result = local_gatekeeper(c)
        self.assertTrue(result.technical_divergence_detected)
        self.assertGreater(result.macro_risk_penalty, 0)

    def test_missing_gate_inputs_fail_closed(self):
        for section, name in (("fundamentals", "altman_z_score"),
                              ("fundamentals", "altman_model_applicable"),
                              ("fundamentals", "market_cap"),
                              ("fundamentals", "float_market_cap"),
                              ("fundamentals", "dividend_yield"),
                              ("fundamentals", "free_cash_flow"),
                              ("shareholders", "promoter_pledged_shares_pct")):
            c = context()
            setattr(getattr(c, section), name, None)
            self.assertFalse(local_gatekeeper(c).is_eligible_for_llm, name)
        c = context()
        c.fundamentals.dividend_yield = .1
        c.fundamentals.free_cash_flow = None
        self.assertFalse(local_gatekeeper(c).is_eligible_for_llm)
        c = context()
        c.profile.sector = "Financial Services"
        c.fundamentals.altman_model_applicable = False
        self.assertFalse(local_gatekeeper(c).is_eligible_for_llm)
        c = context()
        c.shareholders.promoter_pledged_shares_pct = 0
        c.shareholders.promoter_pledge_verified = False
        self.assertFalse(local_gatekeeper(c).is_eligible_for_llm)

    def test_optional_technical_fields_and_skipped_divergence(self):
        technicals = TechnicalIndicators.model_validate(
            {name: None for name in TechnicalIndicators.model_fields}
        )
        self.assertTrue(all(value is None for value in technicals.model_dump().values()))
        for field in ("sma_200", "rsi_14", "current_price"):
            with self.subTest(field=field):
                c = context()
                if field == "current_price":
                    c.current_price = None
                else:
                    setattr(c.technicals, field, None)
                with self.assertLogs("wealth_home_ai", level="WARNING") as logs:
                    result = local_gatekeeper(c)
                self.assertTrue(result.is_eligible_for_llm)
                self.assertFalse(result.technical_divergence_detected)
                self.assertTrue(any("divergence check skipped" in item for item in result.local_warnings))
                self.assertTrue(any("divergence check skipped" in item for item in logs.output))
                calls = []
                with self.assertLogs("wealth_home_ai", level="WARNING"):
                    run_triggered_analysis(c, triggered=True, llm_callable=lambda payload: (
                        calls.append(json.loads(payload)) or
                        {"recommendation": "HOLD", "confidence_score": .5, "structural_rationale": "Missing data."}
                    ))
                self.assertIn("divergence check skipped", calls[0]["local_gatekeeper"]["local_warnings"][0])
                c.fundamentals.altman_z_score = 1.0
                with self.assertLogs("wealth_home_ai", level="WARNING"):
                    self.assertFalse(local_gatekeeper(c).is_eligible_for_llm)

        with self.assertRaises(ValidationError):
            TechnicalIndicators(rsi_14=float("inf"))
        with self.assertRaises(ValidationError):
            TechnicalIndicators(rsi_14=101)

    def test_analysis_trigger_rechecks_gate_and_preserves_full_payload(self):
        calls = []

        def llm(payload):
            calls.append(json.loads(payload))
            return {"recommendation": "BUY", "confidence_score": .8,
                    "structural_rationale": "Reason"}

        c = context(source_data={"rows": list(range(1000))})
        for trigger in (False, None, 1, "true"):
            with self.subTest(trigger=trigger), self.assertRaises(ValueError):
                run_triggered_analysis(c, triggered=trigger, llm_callable=llm)
        c.fundamentals.altman_z_score = 1
        with self.assertRaises(ValueError):
            run_triggered_analysis(c, triggered=True, llm_callable=llm)
        self.assertEqual(calls, [])
        c.fundamentals.altman_z_score = 2
        signal = run_triggered_analysis(c, triggered=True, llm_callable=llm)
        self.assertIsInstance(signal, TradeSignalResponse)
        self.assertEqual(len(calls[0]["source_data"]["rows"]), 1000)
        self.assertTrue(calls[0]["local_gatekeeper"]["is_eligible_for_llm"])
        self.assertEqual(len(calls[0]["profile"]["full_summary"]), 10000)
        self.assertIsNone(calls[0]["fundamentals"]["trailing_pe"])
        with self.assertRaises(ValidationError):
            run_triggered_analysis(c, triggered=True, llm_callable=lambda _: {"recommendation": "MAYBE"})

    def test_source_data_and_warnings_default_independently(self):
        first, second = context(), context()
        first.source_data["rows"] = [1]
        first.data_warnings.append("source missing")
        self.assertEqual(second.source_data, {})
        self.assertEqual(second.data_warnings, [])

    def test_intraday_freshness_checked_at_invocation(self):
        now = datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc)
        intraday = {"vwap": 100, "distance_from_vwap_pct": 0, "opening_range_breakout": "NONE",
                    "time_of_day_segment": "POWER_HOUR", "relative_volume_intraday": 1,
                    "bid_ask_spread": 1}
        c = context(horizon=InvestmentHorizon.INTRADAY_TRADING, intraday=intraday)
        calls = []

        def llm(payload):
            calls.append(payload)
            return {"recommendation": "BUY", "confidence_score": .8, "structural_rationale": "Reason"}

        with patch("services.stock_context_models.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            for timestamp in (None, "invalid", "2026-01-05T04:00:00",
                              (now + timedelta(microseconds=1)).isoformat(),
                              (now - timedelta(seconds=30, microseconds=1)).isoformat()):
                c.source_data["upstox_stream_timestamp"] = {"traded_at": timestamp}
                with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                    run_triggered_analysis(c, triggered=True, llm_callable=llm)
            self.assertEqual(calls, [])
            for timestamp in (now.isoformat(), (now - timedelta(seconds=30)).isoformat()):
                c.source_data["upstox_stream_timestamp"] = {"traded_at": timestamp}
                run_triggered_analysis(c, triggered=True, llm_callable=llm)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

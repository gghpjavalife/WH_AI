import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from services.stock_context import (
    _daily_history,
    _quote_matches_stream_session,
    fetch_and_build_market_context,
    main,
)
from services.stock_context_models import InvestmentHorizon, local_gatekeeper, run_triggered_analysis
from services.stock_context_stream import UpstoxMicrostructureStream


def _history():
    close = pd.Series([100 + i * .1 + (i % 5) * .2 for i in range(260)])
    return pd.DataFrame({
        "high": close + 2, "low": close - 2, "close": close,
        "volume": [1000 + i for i in range(260)],
    }, index=pd.date_range("2025-01-01", periods=260)).assign(
        high=(close + 2).to_numpy(), low=(close - 2).to_numpy(),
        close=close.to_numpy(),
    )


class FakeTicker:
    def __init__(self, symbol, **kwargs):
        self.symbol = symbol

    def get_modules(self, modules):
        return {self.symbol: {
            "price": {"longName": "Example", "marketCap": 1_000_000, "currency": "USD"},
            "summaryProfile": {"sector": "Tech", "industry": "Software",
                               "longBusinessSummary": "A full summary.", "country": "US"},
            "defaultKeyStatistics": {"floatShares": 5000, "sharesOutstanding": 10000},
            "financialData": {"currentPrice": 100, "freeCashflow": 500, "financialCurrency": "USD"},
            "summaryDetail": {"dividendYield": .01},
            "majorHoldersBreakdown": {"institutionsPercentHeld": .3, "insidersPercentHeld": .2},
        }}

    def history(self, **kwargs):
        return _history()

    def balance_sheet(self, **kwargs):
        return pd.DataFrame()

    def income_statement(self, **kwargs):
        return pd.DataFrame()

    def cash_flow(self, **kwargs):
        return pd.DataFrame()

    @property
    def option_chain(self):
        return "No option chain data found"


class ContextIntegrationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("services.stock_context.fetch_upstox_corporate_actions", return_value={"data": []})
        self.actions = patcher.start()
        self.addCleanup(patcher.stop)

    def test_quote_session_timestamp_formats_and_missing_values(self):
        tick = "2026-01-05T09:30:00+05:30"
        timestamp = int(datetime.fromisoformat(tick).timestamp() * 1000)
        for value in (tick, timestamp, str(timestamp)):
            self.assertTrue(_quote_matches_stream_session({"timestamp": value}, tick))
        for value in (None, True, "bad", "2026-01-05T09:30:00",
                      "2026-01-04T09:30:00+05:30"):
            self.assertFalse(_quote_matches_stream_session({"timestamp": value}, tick))

    @patch("services.stock_context_financials.calculate_financial_metrics")
    def test_mixed_date_and_datetime_yahoo_indexes_are_normalized(self, financial):
        financial.return_value = {
            "piotroski_f_score": 5, "altman_z_score": 3,
            "revenue_growth_yoy": None, "earnings_growth_yoy": None,
            "earnings_quality_score": None,
        }

        class MixedDateTicker(FakeTicker):
            def history(self, **kwargs):
                frame = super().history(**kwargs)
                frame.index = pd.Index(
                    [
                        timestamp.date()
                        if index % 2
                        else timestamp.to_pydatetime().replace(tzinfo=timezone.utc)
                        for index, timestamp in enumerate(frame.index)
                    ],
                    dtype=object,
                )
                return frame

        raw = MixedDateTicker("TEST").history()
        history = _daily_history(raw, "TEST")
        self.assertIsInstance(history.index, pd.DatetimeIndex)
        context = fetch_and_build_market_context(
            "TEST", InvestmentHorizon.LONG_TERM_EQUITY,
            yahoo_factory=MixedDateTicker,
        )
        self.assertIsNotNone(context.technicals.sma_200)

    @patch("services.stock_context_financials.calculate_financial_metrics")
    def test_short_history_allows_llm_with_skipped_divergence_warning(self, financial):
        financial.return_value = {
            "piotroski_f_score": 5, "altman_z_score": 3,
            "revenue_growth_yoy": None, "earnings_growth_yoy": None,
            "earnings_quality_score": None,
        }
        short = _history().iloc[:40]
        with patch.object(FakeTicker, "history", return_value=short):
            context = fetch_and_build_market_context(
                "TEST", InvestmentHorizon.LONG_TERM_EQUITY, yahoo_factory=FakeTicker,
                supplemental={"fundamentals": {"altman_model_applicable": True},
                              "shareholders": {"promoter_pledged_shares_pct": .2}},
            )
        self.assertIsNone(context.technicals.sma_200)
        calls = []
        with self.assertLogs("wealth_home_ai", level="WARNING"):
            signal = run_triggered_analysis(
                context, triggered=True,
                llm_callable=lambda payload: (
                    calls.append(json.loads(payload))
                    or {"recommendation": "HOLD", "confidence_score": .5, "structural_rationale": "Limited history."}
                ),
            )
        self.assertEqual(signal.recommendation, "HOLD")
        self.assertTrue(any("divergence check skipped" in warning
                            for warning in calls[0]["local_gatekeeper"]["local_warnings"]))

    @patch("services.stock_context_financials.calculate_financial_metrics")
    def test_missing_candle_is_preserved_and_technical_values_repaired(self, financial):
        financial.return_value = {
            "piotroski_f_score": 5, "altman_z_score": 3,
            "revenue_growth_yoy": None, "earnings_growth_yoy": None,
            "earnings_quality_score": None,
        }
        broken = _history()
        broken.iloc[-1, broken.columns.get_loc("volume")] = float("nan")
        with patch.object(FakeTicker, "history", return_value=broken):
            context = fetch_and_build_market_context("TEST", InvestmentHorizon.LONG_TERM_EQUITY,
                                                       yahoo_factory=FakeTicker)
        self.assertIsNotNone(context.technicals.sma_200)
        self.assertFalse(local_gatekeeper(context).is_eligible_for_llm)
        self.assertTrue(any("forward/back-filled" in warning for warning in context.data_warnings))
        columns = context.source_data["yahoo_daily_history"]["columns"]
        self.assertIsNone(context.source_data["yahoo_daily_history"]["rows"][-1][columns.index("volume")])

    @patch("services.stock_context_financials.calculate_financial_metrics")
    def test_build_context_without_llm_and_fail_closed_when_pledging_missing(self, financial):
        financial.return_value = {
            "piotroski_f_score": 5, "altman_z_score": 3,
            "revenue_growth_yoy": None, "earnings_growth_yoy": None,
            "earnings_quality_score": None,
        }
        context = fetch_and_build_market_context(
            "TEST", InvestmentHorizon.LONG_TERM_EQUITY,
            yahoo_factory=FakeTicker,
        )
        self.assertEqual(context.profile.ticker, "TEST")
        self.assertIsNone(context.shareholders.promoter_pledged_shares_pct)
        self.assertFalse(local_gatekeeper(context).is_eligible_for_llm)
        self.assertEqual(len(context.source_data["yahoo_daily_history"]["rows"]), 260)
        self.assertIn('"promoter_pledged_shares_pct":null', context.model_dump_json())

    def test_intraday_without_stream_fails_before_network(self):
        with self.assertRaises(ValueError):
            fetch_and_build_market_context("TEST", InvestmentHorizon.INTRADAY_TRADING,
                                           yahoo_factory=FakeTicker)

    @patch("services.stock_context.fetch_upstox_shareholdings")
    @patch("services.stock_context.fetch_upstox_news")
    @patch("services.stock_context.fetch_upstox_quote")
    @patch("services.stock_context_financials.calculate_financial_metrics")
    def test_ingestion_gate_then_explicit_llm_call(self, financial, quote, news, holdings):
        financial.return_value = {
            "piotroski_f_score": 5, "altman_z_score": 3,
            "revenue_growth_yoy": .1, "earnings_growth_yoy": .2,
            "earnings_quality_score": 1.5,
        }
        quote.return_value = {"last_price": 100, "lower_circuit_limit": 90,
                              "upper_circuit_limit": 110}
        news.return_value = ([{"headline": "News", "timestamp": None, "source": "Upstox",
                               "url": None, "raw_sentiment_score": None}], [{"data": {}}])
        holdings.return_value = {"data": [{"category": "promoters", "history": []}]}
        context = fetch_and_build_market_context(
            "TEST", InvestmentHorizon.LONG_TERM_EQUITY,
            instrument_key="NSE_EQ|INE002A01018", yahoo_factory=FakeTicker,
            supplemental={"shareholders": {"promoter_pledged_shares_pct": .2},
                          "fundamentals": {"altman_model_applicable": True}},
        )
        self.assertEqual(context.profile.isin, "INE002A01018")
        self.assertEqual(context.intraday.lower_circuit_limit, 90)
        self.assertTrue(local_gatekeeper(context).is_eligible_for_llm)
        self.assertEqual(context.news.headlines[0].headline, "News")
        self.assertIn("upstox_shareholdings", context.source_data)
        self.assertEqual(context.news.upcoming_corporate_action_event, "NONE")
        self.assertIn("upstox_corporate_actions", context.source_data)
        seen = []
        def llm(payload):
            seen.append(payload)
            return {"recommendation": "HOLD", "confidence_score": .6, "structural_rationale": "Review."}
        with self.assertRaises(ValueError):
            run_triggered_analysis(context, triggered=False, llm_callable=llm)
        self.assertEqual(seen, [])
        self.assertEqual(run_triggered_analysis(context, triggered=True, llm_callable=llm).recommendation, "HOLD")
        self.assertEqual(len(seen), 1)

    @patch("services.stock_context.fetch_upstox_shareholdings", return_value={"data": []})
    @patch("services.stock_context.fetch_upstox_news", return_value=([], []))
    @patch("services.stock_context.fetch_upstox_quote", return_value={
        "last_price": 101, "lower_circuit_limit": 100, "upper_circuit_limit": 120})
    @patch("services.stock_context_financials.calculate_financial_metrics")
    def test_action_and_circuit_ingestion_block_llm(self, financial, quote, news, holdings):
        financial.return_value = {"piotroski_f_score": 5, "altman_z_score": 3,
                                  "revenue_growth_yoy": None, "earnings_growth_yoy": None,
                                  "earnings_quality_score": None}
        self.actions.return_value = {"data": [{
            "name": "Bonus", "expiry_date": datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        }]}
        context = fetch_and_build_market_context(
            "TEST", InvestmentHorizon.LONG_TERM_EQUITY,
            instrument_key="NSE_EQ|INE002A01018", yahoo_factory=FakeTicker,
            supplemental={"fundamentals": {"altman_model_applicable": True},
                          "shareholders": {"promoter_pledged_shares_pct": .2}})
        context.current_price = 99
        context.technicals.sma_200 = 100
        context.technicals.rsi_14 = 71
        gate = local_gatekeeper(context)
        self.assertFalse(gate.is_eligible_for_llm)
        self.assertFalse(gate.technical_divergence_detected)
        self.assertEqual(context.news.upcoming_corporate_action_event, "BONUS")
        self.assertEqual(context.source_data["upstox_corporate_actions"]["data"][0]["name"], "Bonus")
        with self.assertRaises(ValueError):
            run_triggered_analysis(context, triggered=True, llm_callable=lambda _: self.fail("LLM invoked"))
        overridden = fetch_and_build_market_context(
            "TEST", InvestmentHorizon.LONG_TERM_EQUITY, instrument_key="NSE_EQ|INE002A01018",
            yahoo_factory=FakeTicker, supplemental={"news": {"upcoming_corporate_action_event": "SPLIT"}})
        self.assertEqual(overridden.news.upcoming_corporate_action_event, "SPLIT")
        self.assertEqual(overridden.source_data["supplemental"]["news"]["upcoming_corporate_action_event"], "SPLIT")

    @patch("services.stock_context.fetch_upstox_shareholdings", return_value={"data": []})
    @patch("services.stock_context.fetch_upstox_news", return_value=([], []))
    @patch("services.stock_context.fetch_upstox_quote", return_value={"last_price": 100})
    def test_unavailable_corporate_actions_warn_and_remain_unknown(self, quote, news, holdings):
        self.actions.side_effect = requests.HTTPError("unavailable")
        with self.assertLogs("wealth_home_ai", level="WARNING"):
            context = fetch_and_build_market_context(
                "TEST", InvestmentHorizon.LONG_TERM_EQUITY,
                instrument_key="NSE_EQ|INE002A01018", yahoo_factory=FakeTicker)
        self.assertIsNone(context.news.upcoming_corporate_action_event)
        self.assertTrue(any("corporate actions unavailable" in item.lower() for item in context.data_warnings))

    @patch("services.stock_context.fetch_upstox_shareholdings", side_effect=requests.HTTPError("unavailable"))
    @patch("services.stock_context.fetch_upstox_news", side_effect=requests.HTTPError("unavailable"))
    @patch("services.stock_context.fetch_upstox_quote", side_effect=requests.HTTPError("unavailable"))
    def test_upstox_failures_keep_public_context_with_explicit_warnings(self, quote, news, holdings):
        self.actions.side_effect = requests.HTTPError("unavailable")
        with self.assertLogs("wealth_home_ai", level="WARNING"):
            context = fetch_and_build_market_context(
                "TEST", InvestmentHorizon.LONG_TERM_EQUITY,
                instrument_key="NSE_EQ|INE002A01018", yahoo_factory=FakeTicker)
        self.assertEqual(context.profile.isin, "INE002A01018")
        self.assertIsNotNone(context.technicals.sma_200)
        self.assertIsNone(context.shareholders.promoter_pledged_shares_pct)
        self.assertIsNone(context.news.upcoming_corporate_action_event)
        self.assertIsNone(context.intraday)
        self.assertFalse(local_gatekeeper(context).is_eligible_for_llm)
        for phrase in ("full quote unavailable", "news unavailable", "shareholdings unavailable",
                       "corporate actions unavailable"):
            self.assertTrue(any(phrase in warning.lower() for warning in context.data_warnings))

    @patch("services.stock_context.fetch_upstox_shareholdings")
    @patch("services.stock_context.fetch_upstox_news", return_value=([], []))
    @patch("services.stock_context.fetch_upstox_quote", return_value={"last_price": 100})
    @patch("services.stock_context_financials.calculate_financial_metrics")
    def test_bad_upstox_pledge_zero_is_unverified_and_blocks(self, financial, quote, news, holdings):
        financial.return_value = {"piotroski_f_score": 5, "altman_z_score": 3,
                                  "revenue_growth_yoy": None, "earnings_growth_yoy": None,
                                  "earnings_quality_score": None}
        holdings.return_value = {"data": [], "promoter_pledged_shares_pct": "bad"}
        with self.assertLogs("wealth_home_ai", level="WARNING"):
            context = fetch_and_build_market_context(
                "TEST", InvestmentHorizon.LONG_TERM_EQUITY,
                instrument_key="NSE_EQ|INE002A01018", yahoo_factory=FakeTicker,
                supplemental={"fundamentals": {"altman_model_applicable": True}},
            )
        self.assertEqual(context.shareholders.promoter_pledged_shares_pct, 0)
        self.assertFalse(context.shareholders.promoter_pledge_verified)
        self.assertFalse(local_gatekeeper(context).is_eligible_for_llm)
        with self.assertLogs("wealth_home_ai", level="WARNING"):
            context = fetch_and_build_market_context(
                "TEST", InvestmentHorizon.LONG_TERM_EQUITY,
                instrument_key="NSE_EQ|INE002A01018", yahoo_factory=FakeTicker,
                supplemental={"fundamentals": {"altman_model_applicable": True},
                              "shareholders": {"promoter_pledged_shares_pct": .3}},
            )
        self.assertTrue(context.shareholders.promoter_pledge_verified)
        self.assertTrue(local_gatekeeper(context).is_eligible_for_llm)

    @patch("services.stock_context.fetch_and_build_market_context")
    @patch("sys.argv", ["stock_context", "TEST"])
    def test_command_line_emits_context_and_local_gate(self, fetch):
        from services.stock_context_models import StockSignalContext
        fetch.return_value = StockSignalContext.model_validate({
            "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "TEST"},
        })
        output = io.StringIO()
        with redirect_stdout(output):
            main()
        result = json.loads(output.getvalue())
        self.assertEqual(result["context"]["profile"]["ticker"], "TEST")
        self.assertFalse(result["gatekeeper"]["is_eligible_for_llm"])

    @patch("services.stock_context.fetch_upstox_shareholdings", return_value={"data": []})
    @patch("services.stock_context.fetch_upstox_news", return_value=([], []))
    @patch("services.stock_context.fetch_upstox_quote", return_value={
        "last_price": 110, "lower_circuit_limit": 100, "upper_circuit_limit": 120,
        "timestamp": "2026-01-05T09:30:00+05:30"})
    @patch("services.stock_context_financials.calculate_financial_metrics")
    def test_intraday_snapshot_retains_stream_and_baseline_provenance(self, financial, quote, news, holdings):
        financial.return_value = {"piotroski_f_score": 5, "altman_z_score": 3,
                                  "revenue_growth_yoy": None, "earnings_growth_yoy": None,
                                  "earnings_quality_score": None}
        key = "NSE_EQ|INE002A01018"
        stream = UpstoxMicrostructureStream(key, {"09:30": 100}, baseline_source={"candles": [1, 2]})
        with patch.object(stream, "snapshot", return_value={
            "current_price": 100, "observed_at": "2026-01-05T09:30:00+05:30",
            "traded_at": "2026-01-05T09:30:00+05:30", "raw_message": {"depth": list(range(30))},
            "completed_minute_bars": [], "vwap": 99, "distance_from_vwap_pct": 1,
            "opening_range_breakout": "INSIDE_BAR", "time_of_day_segment": "MORNING_RUSH",
            "relative_volume_intraday": 1.5, "bid_ask_spread": .5,
        }):
            context = fetch_and_build_market_context(
                "TEST", InvestmentHorizon.INTRADAY_TRADING, instrument_key=key,
                intraday_stream=stream, yahoo_factory=FakeTicker,
            )
        self.assertEqual(len(context.source_data["upstox_latest_stream_message"]["depth"]), 30)
        self.assertEqual(context.source_data["intraday_volume_baseline_source"]["candles"], [1, 2])
        self.assertEqual(context.intraday.vwap, 99)
        self.assertEqual(context.intraday.distance_to_lower_circuit_pct, 0)
        self.assertEqual(context.intraday.upper_circuit_limit, 120)
        self.assertFalse(local_gatekeeper(context).is_eligible_for_llm)
        quote.return_value["timestamp"] = "2026-01-04T09:30:00+05:30"
        with patch.object(stream, "snapshot", return_value={
            "current_price": 100, "observed_at": "2026-01-05T09:30:00+05:30",
            "traded_at": "2026-01-05T09:30:00+05:30", "raw_message": {},
            "completed_minute_bars": [], "vwap": 99, "distance_from_vwap_pct": 1,
            "opening_range_breakout": "INSIDE_BAR", "time_of_day_segment": "MORNING_RUSH",
            "relative_volume_intraday": 1.5, "bid_ask_spread": .5,
        }):
            stale = fetch_and_build_market_context("TEST", InvestmentHorizon.INTRADAY_TRADING,
                instrument_key=key, intraday_stream=stream, yahoo_factory=FakeTicker)
        self.assertIsNone(stale.intraday.lower_circuit_limit)
        self.assertTrue(any("session does not match" in item for item in stale.data_warnings))


if __name__ == "__main__":
    unittest.main()

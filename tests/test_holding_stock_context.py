import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

from features.analysis.stock_context import (
    CONTEXT_TTL, _CONTEXT_INFO, _OVERVIEW_INFO, append_context_to_row,
    context_filter_fields, holding_identity, load_holding_context, refresh_holding_contexts,
)
from features.analysis.holdings import _build_holding_scan_row
from features.equities.scanner import build_scan_table_payload
from services.stock_context_models import (
    InvestmentHorizon, LocalGatekeeperResponse, NewsHeadline, StockSignalContext, TechnicalIndicators,
)
from brokers.factory import UpstoxAdapter
from streamlit.testing.v1 import AppTest


class HoldingStockContextTests(unittest.TestCase):
    def test_context_descriptions_cover_schema_and_raw_columns_even_when_unavailable(self):
        row, config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 100}), {})
        row["contextInfo"] = {"existing": "Existing help."}
        config["rawInfo"] = {"existing": "Existing column help."}
        append_context_to_row(row, config, None)
        info, raw_info, field_info = row["contextInfo"], config["rawInfo"], config["fieldInfo"]
        self.assertEqual(info["existing"], "Existing help.")
        self.assertEqual(raw_info["existing"], "Existing column help.")
        for field in StockSignalContext.model_fields:
            with self.subTest(field=field):
                self.assertIn(f"context_{field}", _OVERVIEW_INFO)
        for field in LocalGatekeeperResponse.model_fields:
            with self.subTest(gate=field):
                self.assertIn(f"context_gate_{field}", field_info)
        for section, field, label, _ in context_filter_fields():
            with self.subTest(section=section, field=field):
                self.assertIn(field, _CONTEXT_INFO[section])
                self.assertEqual(field_info[f"context_{section}_{field}"], raw_info[label])
                self.assertGreater(len(raw_info[label]), 25)
                self.assertNotIn("source, units and interpretation may vary", raw_info[label])
        for field in NewsHeadline.model_fields:
            self.assertIn(f"context_news_{field}", field_info)
        for label in ("Context · horizon", "Context · current price", "Gate · eligibility",
                      "Gate · technical divergence", "Gate · macro risk penalty"):
            self.assertIn(label, raw_info)
        self.assertIn("skipped", field_info["context_divergence"])
        self.assertIn("unknown", field_info["context_news_upcoming_corporate_action_event"])
        self.assertIn("unverified", field_info["context_shareholders_promoter_pledged_shares_pct"])

    def test_merged_rows_receive_context_help_without_losing_existing_help_or_links(self):
        research = {
            "technical": {"raw_indicators": {"rsi14": 61}},
            "fundamentals": {"metrics": [{"Fundamental": "Trailing P/E", "Value": "12.00"}]},
        }
        row, config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 100}), research)
        config["info"]["fundamental_0"] = ["Legacy P/E help.", "https://example.com/pe"]
        technical_help = list(config["info"]["RSI_14"])
        row["contextInfo"] = {"fundamental_0": "Existing row help."}
        context = StockSignalContext.model_validate({
            "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "ABC.NS"},
            "technicals": {"rsi_14": 62}, "fundamentals": {"trailing_pe": 13},
        }).model_dump(mode="json")
        append_context_to_row(row, config, {"context": context, "gate": {}})
        self.assertEqual(config["info"]["RSI_14"], technical_help)
        self.assertEqual(config["info"]["fundamental_0"], ["Legacy P/E help.", "https://example.com/pe"])
        self.assertIn("relative strength", row["contextInfo"]["RSI_14"])
        self.assertIn("Existing row help.", row["contextInfo"]["fundamental_0"])
        self.assertIn("trailing earnings", row["contextInfo"]["fundamental_0"])
        self.assertEqual(row["ind"]["RSI_14"][0], "62.0")
        self.assertEqual(row["ind"]["fundamental_0"][0], "13.0")
        self.assertEqual(row["contextModel"], {k: v for k, v in context.items() if k != "source_data"})

    def test_raw_info_and_merged_help_exist_before_context_is_available(self):
        research = {"fundamentals": {"metrics": [
            {"Fundamental": "Trailing P/E", "Value": "12.00"},
        ]}}
        row, config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 100}), research)
        row["contextInfo"] = {"RSI_14": "Existing row help."}
        config["rawInfo"] = {"Technicals · rsi 14": "Existing column help."}
        technical_help = list(config["info"]["RSI_14"])
        append_context_to_row(row, config, None)
        self.assertEqual(config["info"]["RSI_14"], technical_help)
        self.assertIn("Existing row help.", row["contextInfo"]["RSI_14"])
        self.assertIn("relative strength", row["contextInfo"]["RSI_14"])
        self.assertIn("trailing earnings", row["contextInfo"]["fundamental_0"])
        self.assertIn("Existing column help.", config["rawInfo"]["Technicals · rsi 14"])
        self.assertIn("relative strength", config["rawInfo"]["Technicals · rsi 14"])
        for label in config["rawCols"]:
            if label.startswith(("Context · ", "Gate · ")):
                self.assertIn(label, config["rawInfo"])

    def test_unknown_context_fields_have_safe_fallback_and_keep_values(self):
        row, config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 100}), {})
        context = {
            "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "ABC.NS", "future_metric": 42},
        }
        append_context_to_row(row, config, {"context": context, "gate": {}})
        self.assertEqual(row["ind"]["context_profile_future_metric"], ["42", "neu"])
        self.assertEqual(row["contextModel"]["profile"]["future_metric"], 42)
        self.assertIn("source, units and interpretation may vary",
                      row["contextInfo"]["context_profile_future_metric"])

    def test_upstox_frames_preserve_each_broker_instrument_key(self):
        adapter = UpstoxAdapter()
        sdk = MagicMock()
        sdk.get_positions.return_value.data = [
            {"trading_symbol": "ABC", "instrument_token": "NSE_EQ|INE002A01018",
             "quantity": 1, "average_price": 99, "last_price": 100},
            {"trading_symbol": "XYZ", "instrument_token": "NSE_FO|123",
             "quantity": 2, "average_price": 19, "last_price": 20},
        ]
        sdk.get_holdings.return_value.data = [
            {"trading_symbol": "ABC", "instrument_token": "BSE_EQ|INE002A01018",
             "quantity": 1, "average_price": 99, "last_price": 100},
        ]
        quotes = MagicMock()
        quotes.get_ltp.return_value.data = {}
        with patch.object(adapter, "_api_client", return_value=object()), \
                patch("brokers.factory.PortfolioApi", return_value=sdk), \
                patch("brokers.factory.MarketQuoteV3Api", return_value=quotes):
            positions = adapter.fetch_positions("test-token")
            holdings = adapter.fetch_holdings("test-token")
        self.assertEqual(list(positions["Instrument_Key"]), ["NSE_EQ|INE002A01018", "NSE_FO|123"])
        self.assertEqual(list(holdings["Instrument_Key"]), ["BSE_EQ|INE002A01018"])

    def test_connected_holding_populates_context_in_rendered_table(self):
        app = AppTest.from_string("""
import pandas as pd
import streamlit as st
from unittest.mock import patch
from services.stock_context_models import StockSignalContext
from features.analysis.holdings import render_stock_holdings_table
from features.analysis.stock_context import load_holding_context

st.session_state["token"] = "broker-test-token"
st.session_state["broker_state"] = "connected"
st.session_state["broker_name"] = "Upstox"
st.session_state["stock_research_cache"] = {
    "selected_stock_research_ABC": {"signal": "Hold", "reason": "Local", "errors": {}}
}
ctx = StockSignalContext.model_validate({
    "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "ABC.NS"},
    "intraday": {"lower_circuit_limit": 100, "distance_to_lower_circuit_pct": 1},
    "source_data": {"rows": [123, 456]},
})
with patch("features.analysis.holdings.configured_value", return_value="token"), \\
     patch("features.analysis.stock_context.fetch_and_build_market_context", return_value=ctx):
    render_stock_holdings_table(pd.DataFrame([{
        "Ticker": "ABC", "Instrument_Key": "NSE_EQ|INE002A01018", "Qty": 1,
        "Avg_Price": 100, "LTP": 101, "Investment": 100, "Market_Value": 101,
        "P&L": 1, "Returns_%": 1, "Allocation_%": 100,
    }]), key="test_context", theme="dark")
    for future in st.session_state.get("holding_research_jobs", {}).values():
        future.result(timeout=20)
""")
        with patch("features.equities.scan_table.show_scan_table") as table:
            app.run(timeout=20)
            for future in app.session_state["holding_research_jobs"].values():
                future.result(timeout=20)
            app.run(timeout=20)
        self.assertFalse(app.exception)
        self.assertFalse(any(exp.label == "Full source data and context" for exp in app.expander))
        self.assertEqual(len(app.get("json")), 0)
        self.assertEqual(len(app.get("download_button")), 0)
        cache = app.session_state["stock_context_cache"]
        self.assertEqual(len(cache), 1)
        result = next(iter(cache.values()))[1]
        self.assertFalse(result["gate"]["is_eligible_for_llm"])
        self.assertEqual(result["context"]["intraday"]["lower_circuit_limit"], 100)
        self.assertEqual(result["context"]["source_data"]["rows"], [123, 456])
        payload, config = table.call_args.args
        self.assertEqual(config["tableKey"], "test_context")
        self.assertEqual(payload[0]["ind"]["context_intraday_lower_circuit_limit"][0], "100.0")

    def test_identity_respects_broker_instrument_type_and_exchange(self):
        equity = pd.Series({"Ticker": "ABC", "Instrument_Key": "NSE_EQ|INE002A01018"})
        self.assertEqual(holding_identity(equity, "home_equity", "Upstox"),
                         ("ABC.NS", "NSE_EQ|INE002A01018", InvestmentHorizon.LONG_TERM_EQUITY))
        equity["Instrument_Key"] = "BSE_EQ|INE002A01018"
        self.assertEqual(holding_identity(equity, "trading", "Upstox")[0], "ABC.BO")
        equity["Instrument_Key"] = "NSE_FO|123"
        self.assertIsNone(holding_identity(equity, "trading", "Upstox"))
        equity["Instrument_Key"] = None
        self.assertIsNone(holding_identity(equity, "trading", "Upstox"))
        self.assertIsNone(holding_identity(equity, "home_equity", "Upstox"))
        self.assertEqual(holding_identity(equity, "home_equity", "Zerodha")[0], "ABC.NS")

    @patch("features.analysis.stock_context.fetch_and_build_market_context")
    def test_local_gate_and_display_preserve_missing_values(self, fetch):
        fetch.return_value = StockSignalContext.model_validate({
            "profile": {"ticker": "ABC.NS", "isin": "INE002A01018"},
            "horizon": "LONG_TERM_EQUITY",
            "intraday": {"lower_circuit_limit": 100, "distance_to_lower_circuit_pct": 1},
            "news": {"upcoming_corporate_action_event": "BONUS"},
            "source_data": {"rows": [1, 2, 3]},
        })
        identity = ("ABC.NS", "NSE_EQ|INE002A01018", InvestmentHorizon.LONG_TERM_EQUITY)
        result = load_holding_context(identity, analytics_available=True)
        self.assertFalse(result["gate"]["is_eligible_for_llm"])
        self.assertEqual(result["context"]["source_data"]["rows"], [1, 2, 3])
        fetch.assert_called_once_with("ABC.NS", InvestmentHorizon.LONG_TERM_EQUITY,
                                      instrument_key="NSE_EQ|INE002A01018")
        row, config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 101}), {})
        append_context_to_row(row, config, result)
        self.assertEqual(len(row["raw"]), len(config["rawCols"]))
        index = config["rawCols"].index("Intraday · distance to lower circuit pct")
        self.assertEqual(row["raw"][index], 1)
        index = config["rawCols"].index("Fundamentals · altman z score")
        self.assertEqual(row["raw"][index], "")
        index = config["rawCols"].index("News · upcoming corporate action event")
        self.assertEqual(row["raw"][index], "BONUS")
        self.assertEqual(config["rawTypes"][index], "text")
        index = config["rawCols"].index("Gate · eligibility")
        self.assertEqual(row["raw"][index], "Blocked")
        self.assertEqual(config["rawTypes"][index], "text")
        self.assertTrue(any("Phase 1: Blocked" in section.get("note", "") for section in row["extraSections"]))

    def test_context_unifies_corresponding_rows_and_keeps_distinct_periods(self):
        research = {
            "technical": {
                "raw_indicators": {"sma20": 100.0, "rsi14": 61.0, "volatility20_annualized_pct": 12.0},
                "indicators": [{"Indicator": "14-day RSI", "Value": "61", "Signal": "Positive"}],
            },
            "fundamentals": {"metrics": [
                {"Fundamental": "Trailing P/E", "Value": "12.00"},
                {"Fundamental": "Debt / equity", "Value": "150.00"},
                {"Fundamental": "Free cash flow", "Value": "INR 100.00"},
            ]},
            "fundamental_checks": [{"Fundamental": "Trailing P/E", "Assessment": "Positive"}],
        }
        row, config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 110}), research)
        context = StockSignalContext.model_validate({
            "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "ABC.NS"},
            "technicals": {"sma_20": 100, "rsi_14": 62, "historical_volatility_30": 15},
            "fundamentals": {"trailing_pe": 13, "debt_to_equity": 1.5, "free_cash_flow": 110},
        }).model_dump(mode="json")
        append_context_to_row(row, config, {"context": context, "gate": {}})
        self.assertEqual(config["rawTypes"][config["rawCols"].index("As of")], "text")
        self.assertEqual(row["ind"]["SMA_20"][0], "100.0")
        self.assertEqual(row["ind"]["RSI_14"][0], "62.0")
        self.assertEqual(row["ind"]["RSI_14"][1], "neu")
        self.assertEqual(row["ind"]["fundamental_0"][0], "13.0")
        self.assertEqual(row["ind"]["fundamental_1"][0], "1.5")
        self.assertEqual(row["ind"]["fundamental_2"][0], "110.0")
        self.assertEqual(row["rsi"], 62)
        self.assertEqual(row["raw"][config["rawCols"].index("RSI 14")], 62)
        self.assertIn("Technicals · rsi 14", config["hiddenRawCols"])
        self.assertNotIn("context_technicals_sma_20", row["ind"])
        self.assertNotIn("context_fundamentals_trailing_pe", row["ind"])
        risk = dict(config["groups"])["Risk and range"]
        self.assertIn(["Volatility_20d_annualized_%", "20-day annualized volatility"], risk)
        self.assertIn(["context_technicals_historical_volatility_30", "30-day annualized volatility"], risk)
        self.assertEqual(row["ind"]["context_technicals_historical_volatility_30"][0], "15%")
        self.assertEqual(row["groupRatings"]["Momentum"]["positive"], 0)
        self.assertEqual(row["groupRatings"]["Momentum"]["rated"], 0)
        self.assertEqual(row["groupRatings"]["Fundamentals"]["rated"], 0)

    def test_context_units_risk_flags_unknowns_and_full_source(self):
        long_text = "A" * 10000
        headlines = [{"headline": long_text + str(index), "source": "Source",
                      "url": "https://example.com", "raw_sentiment_score": 0.1}
                     for index in range(25)]
        context = StockSignalContext.model_validate({
            "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "ABC.NS", "full_summary": long_text},
            "fundamentals": {"dividend_yield": 0.025, "altman_z_score": 1.5,
                             "altman_model_applicable": False},
            "shareholders": {"pct_held_by_institutions": 0.35, "promoter_pledged_shares_pct": 0.4,
                             "promoter_pledge_verified": False, "short_interest_pct_float": 1.5},
            "intraday": {"distance_to_lower_circuit_pct": 1.0},
            "news": {"headlines": headlines},
            "source_data": {"history": list(range(3000)), "provider_only_marker": long_text},
        }).model_dump(mode="json")
        row, config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 100}), {})
        append_context_to_row(row, config, {"context": context, "gate": {}, "token": "secret"})
        self.assertEqual(row["ind"]["context_fundamentals_dividend_yield"], ["2.5%", "neu"])
        self.assertEqual(row["ind"]["context_shareholders_pct_held_by_institutions"], ["35%", "neu"])
        self.assertIn("unverified", row["ind"]["context_shareholders_short_interest_pct_float"][0])
        self.assertEqual(row["ind"]["context_fundamentals_altman_z_score"][1], "neu")
        self.assertEqual(row["ind"]["context_shareholders_promoter_pledged_shares_pct"][1], "neu")
        self.assertEqual(row["ind"]["context_intraday_distance_to_lower_circuit_pct"], ["1%", "neg"])
        self.assertEqual(row["ind"]["context_fundamentals_roe"], ["Unavailable", "unknown"])
        self.assertEqual(row["raw"][config["rawCols"].index("Fundamentals · dividend yield")], 0.025)
        self.assertEqual(config["rawUnits"]["Fundamentals · dividend yield"], "fraction")
        self.assertEqual(config["rawUnits"]["Intraday · distance to lower circuit pct"], "percentage points")
        self.assertEqual(row["raw"][config["rawCols"].index("Shareholders · Institutional ownership")], 0.35)
        self.assertEqual(row["ind"]["context_profile_full_summary"][0], long_text)
        self.assertEqual(row["contextModel"], {k: v for k, v in context.items() if k != "source_data"})
        self.assertNotIn("contextSourceData", row)
        self.assertNotIn("source_data", row["contextModel"])
        self.assertNotIn("secret", str(row))
        self.assertNotIn("provider_only_marker", json.dumps(row))
        self.assertEqual(context["source_data"]["history"], list(range(3000)))
        section = next(s for s in row["extraSections"] if s["title"] == "Context headlines")
        self.assertEqual(len(section["items"]), 25)
        self.assertIn(long_text + "24", section["items"][-1]["title"])
        self.assertEqual(section["items"][-1]["url"], "https://example.com")
        self.assertEqual(row["groupRatings"]["Intraday market"], {
            "positive": 0, "rated": 1, "label": "0 / 1 positive / directional checks",
        })
        self.assertEqual(row["groupRatings"]["Company profile"]["label"], "Not rated")
        missing = next(s["note"] for s in row["extraSections"] if s["title"] == "Unavailable context sections")
        self.assertIn("Futures", missing)
        self.assertIn("Options", missing)

    def test_fundamental_availability_message_reflects_context_and_signal_gap(self):
        research = {"errors": {
            "fundamentals": "Yahoo Finance fundamentals are temporarily unavailable for BSE.",
            "news": "News provider unavailable.",
        }}
        holding = pd.Series({"Ticker": "BSE", "LTP": 100})
        for fundamentals, covered in [({"trailing_pe": 12}, True),
                                      ({"altman_model_applicable": False}, False),
                                      ({}, False)]:
            row, config = _build_holding_scan_row(holding, research)
            context = StockSignalContext.model_validate({
                "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "BSE.NS"},
                "fundamentals": fundamentals,
            }).model_dump(mode="json")
            append_context_to_row(row, config, {"context": context, "gate": {}})
            note = next(s["note"] for s in row["extraSections"] if s["title"] == "Data availability")
            self.assertIn("News provider unavailable.", note)
            if covered:
                self.assertNotIn("Yahoo Finance fundamentals", note)
                self.assertIn("Research signal excludes unavailable research fundamentals.", note)
            else:
                self.assertIn("Yahoo Finance fundamentals", note)

    def test_duplicate_headlines_are_combined_without_losing_context_metadata(self):
        research = {"news": {"headlines": [
            {"title": "Company update", "url": "https://example.com/a", "publisher": "Legacy"}
        ]}}
        row, config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 100}), research)
        context = StockSignalContext.model_validate({
            "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "ABC.NS"},
            "news": {"headlines": [
                {"headline": "Company update", "url": "https://example.com/a",
                 "source": "Context provider", "raw_sentiment_score": 0.4},
                {"headline": "Different news", "url": "https://example.com/b",
                 "source": "Context provider"},
            ]},
        }).model_dump(mode="json")
        append_context_to_row(row, config, {"context": context, "gate": {}})
        old = next(s for s in row["extraSections"] if s["title"] == "Recent company headlines")
        new = next(s for s in row["extraSections"] if s["title"] == "Context headlines")
        self.assertIn("Context provider", old["items"][0]["sentimentLabel"])
        self.assertIn("0.4", old["items"][0]["sentimentLabel"])
        self.assertEqual(len(new["items"]), 1)
        self.assertEqual(new["items"][0]["title"], "Different news")
        self.assertEqual(new["items"][0]["url"], "https://example.com/b")
        self.assertEqual(len(row["contextModel"]["news"]["headlines"]), 2)

    def test_ratings_require_explicit_checks_and_configs_are_stable_across_rows(self):
        research = {"technical": {
            "raw_indicators": {"sma20": 100, "rsi14": 60},
            "indicators": [
                {"Indicator": "14-day RSI", "Value": "60", "Signal": "Positive"},
                {"Indicator": "Price vs 20-day average", "Value": "Unknown", "Signal": "Neutral"},
            ],
        }}
        holding = pd.Series({"Ticker": "ABC", "LTP": 110, "P&L": 5})
        row, config = _build_holding_scan_row(holding, research)
        append_context_to_row(row, config, None)
        self.assertEqual(row["groupRatings"]["Trend"]["label"], "Not rated")
        self.assertEqual(row["groupRatings"]["Momentum"]["label"], "1 / 1 positive / directional checks")
        self.assertEqual(row["ind"]["SMA_20"][1], "neu")
        self.assertEqual(row["ind"]["RSI_14"][1], "pos")
        self.assertEqual(row["groupRatings"]["Holding details"]["label"], "Not rated")
        context = StockSignalContext(horizon="LONG_TERM_EQUITY", profile={"ticker": "ABC.NS"}).model_dump(mode="json")
        other, other_config = _build_holding_scan_row(holding, research)
        append_context_to_row(other, other_config, {"context": context, "gate": {}})
        self.assertEqual(config["groups"], other_config["groups"])
        self.assertEqual(other["ind"]["SMA_20"][0], "₹100.00")
        self.assertEqual(other["ind"]["RSI_14"][0], "60.0")
        self.assertIn("Context value unavailable", other["contextInfo"]["RSI_14"])

    def test_equal_assessment_and_rounded_technical_display(self):
        research = {"technical": {
            "raw_indicators": {"rsi14": 60, "adx14": 24.8},
            "indicators": [{"Indicator": "14-day RSI", "Value": "60", "Signal": "Positive"}],
        }}
        row, config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 110}), research)
        context = StockSignalContext.model_validate({
            "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "ABC.NS"},
            "technicals": {"rsi_14": 60, "adx_14": 24.800511645520128},
        }).model_dump(mode="json")
        append_context_to_row(row, config, {"context": context, "gate": {}})
        self.assertEqual(row["ind"]["RSI_14"], ["60.0", "pos"])
        self.assertEqual(row["groupRatings"]["Momentum"]["positive"], 1)
        self.assertEqual(row["ind"]["ADX_14"][0], "24.8")
        self.assertEqual(row["raw"][config["rawCols"].index("ADX 14")], 24.800511645520128)

    def test_context_technical_populates_existing_group_without_legacy_values(self):
        row, config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 100}), {})
        context = StockSignalContext.model_validate({
            "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "ABC.NS"},
            "technicals": {"sma_20": 90, "rsi_14": 55},
            "fundamentals": {"altman_z_score": 1.0, "altman_model_applicable": True},
            "shareholders": {"promoter_pledged_shares_pct": 0.3, "promoter_pledge_verified": True},
        }).model_dump(mode="json")
        append_context_to_row(row, config, {"context": context, "gate": {}})
        self.assertEqual(row["ind"]["SMA_20"], ["90.0", "neu"])
        self.assertEqual(row["ind"]["RSI_14"], ["55.0", "neu"])
        self.assertEqual(row["groupRatings"]["Fundamentals"]["rated"], 1)
        self.assertEqual(row["groupRatings"]["Ownership and trading activity"]["rated"], 1)
        self.assertFalse(any(title == "Context: technicals" for title, _ in row["extraGroups"]))

    def test_all_context_technical_fields_are_visible_in_named_groups_even_when_unavailable(self):
        technical_fields = TechnicalIndicators.model_fields
        self.assertEqual(len(technical_fields), 22)
        values = {
            "sma_20": 20, "sma_50": 50, "sma_200": 200,
            "ema_20": 21, "ema_50": 51, "ema_200": 201, "rsi_14": 55,
            "macd_line": 1, "macd_signal": 2, "macd_hist": -1,
            "bb_upper": 110, "bb_middle": 100, "bb_lower": 90, "adx_14": 25,
            "current_volume": 1000, "avg_volume_3m": 800, "money_flow_index_14": 60,
            "atr_percentile_14": 70, "chop_index_14": 40, "squeeze_alert": False,
            "volume_price_trend": 500, "historical_volatility_30": 15,
        }
        from features.analysis.stock_context import _TECHNICAL_ROWS, _TECHNICAL_GROUPS

        for context_values in (values, {name: None for name in values}, None):
            row, config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 100}), {})
            result = None if context_values is None else {"context": StockSignalContext.model_validate({
                "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "ABC.NS"},
                "technicals": context_values,
            }).model_dump(mode="json"), "gate": {}}
            append_context_to_row(row, config, result)
            groups = dict(config["groups"])
            for name in technical_fields:
                key = _TECHNICAL_ROWS.get(name, f"context_technicals_{name}")
                with self.subTest(context=context_values is not None, field=name):
                    self.assertIn(key, row["ind"])
                    self.assertTrue(any(key == k for items in groups.values() for k, _ in items))
                    if context_values is None or context_values[name] is None:
                        self.assertEqual(row["ind"][key], ["Unavailable", "unknown"])
                    else:
                        self.assertNotEqual(row["ind"][key][0], "Unavailable")
                        if name in _TECHNICAL_GROUPS:
                            self.assertIn(key, [k for k, _ in groups[_TECHNICAL_GROUPS[name]]])
            self.assertEqual(row["technicalCoverage"]["total"], 22)
            self.assertEqual(row["technicalCoverage"]["available"], 22 if context_values == values else 0)
            self.assertEqual(row["technicalCoverage"]["contextAvailable"], context_values is not None)

    def test_missing_legacy_indicator_does_not_make_context_a_legacy_check(self):
        research = {"technical": {
            "raw_indicators": {"rsi14": None, "sma20": None},
            "indicators": [{"Indicator": "14-day RSI", "Value": "Unavailable", "Signal": "Positive"}],
        }}
        row, config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 100}), research)
        context = StockSignalContext.model_validate({
            "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "ABC.NS"},
            "technicals": {"rsi_14": 55},
        }).model_dump(mode="json")
        append_context_to_row(row, config, {"context": context, "gate": {}})
        self.assertEqual(row["ind"]["RSI_14"], ["55.0", "neu"])
        self.assertEqual(row["ind"]["SMA_20"], ["Unavailable", "unknown"])
        self.assertEqual(row["groupRatings"]["Momentum"]["label"], "Not rated")

    def test_scanner_payload_can_receive_context_without_holding_extras(self):
        rows, config = build_scan_table_payload([{
            "Ticker": "ABC", "Recommendation": "HOLD", "RSI_14": 56.75,
            "Daily_change_%": -2.25,
        }], {})
        original_columns = list(config["rawCols"])
        original_raw = list(rows[0]["raw"])
        context = StockSignalContext.model_validate({
            "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "ABC.NS"},
            "source_data": {"history": list(range(1000))},
        }).model_dump(mode="json")
        append_context_to_row(rows[0], config, {"context": context, "gate": {}})
        self.assertEqual(len(rows[0]["raw"]), len(config["rawCols"]))
        self.assertEqual(config["rawCols"][:len(original_columns)], original_columns)
        self.assertEqual(rows[0]["raw"][:len(original_raw)], original_raw)
        self.assertIn("groupRatings", rows[0])
        self.assertIn("extraSections", rows[0])
        self.assertIn("extraGroups", rows[0])
        self.assertNotIn("source_data", rows[0]["contextModel"])
        self.assertNotIn("contextSourceData", rows[0])
        self.assertEqual(context["source_data"]["history"], list(range(1000)))
        self.assertNotIn("history", str(rows[0]["extraSections"]))

    @patch("features.analysis.stock_context.fetch_and_build_market_context")
    def test_token_unavailable_uses_public_data_without_upstox(self, fetch):
        fetch.return_value = StockSignalContext(horizon="LONG_TERM_EQUITY", profile={"ticker": "ABC.NS"})
        identity = ("ABC.NS", "NSE_EQ|INE002A01018", InvestmentHorizon.LONG_TERM_EQUITY)
        result = load_holding_context(identity, analytics_available=False)
        fetch.assert_called_once_with("ABC.NS", InvestmentHorizon.LONG_TERM_EQUITY, instrument_key=None)
        self.assertIn("analytics token", result["notice"])

    @patch("features.analysis.stock_context.load_holding_context")
    def test_cache_is_session_local_expires_and_is_identity_specific(self, load):
        load.return_value = {"error": "unavailable"}
        first = ("ABC.NS", "NSE_EQ|INE002A01018", InvestmentHorizon.LONG_TERM_EQUITY)
        second = ("ABC.BO", "BSE_EQ|INE002A01018", InvestmentHorizon.SHORT_TERM_TRADING)
        now = datetime.now(timezone.utc)
        cache = {}
        refresh_holding_contexts(cache, {first, second}, analytics_available=True, now=now)
        self.assertEqual(load.call_count, 2)
        refresh_holding_contexts(cache, {first, second}, analytics_available=True, now=now + timedelta(seconds=1))
        self.assertEqual(load.call_count, 2)
        refresh_holding_contexts(cache, {first}, analytics_available=True, now=now + CONTEXT_TTL)
        self.assertEqual(load.call_count, 3)
        self.assertEqual(len(cache), 1)

    @patch("features.analysis.stock_context.load_holding_context")
    def test_refresh_isolates_provider_failures_without_leaking_details(self, load):
        failed = ("ABC.NS", None, InvestmentHorizon.LONG_TERM_EQUITY)
        succeeded = ("XYZ.NS", None, InvestmentHorizon.LONG_TERM_EQUITY)
        now = datetime.now(timezone.utc)
        cache = {}

        def fetch(identity, *, analytics_available):
            if identity == failed:
                raise KeyError("provider response secret-token")
            return {"context": {"profile": {"ticker": identity[0]}}}

        load.side_effect = fetch
        with self.assertLogs("wealth_home_ai", level="ERROR") as logs:
            refresh_holding_contexts(cache, {failed, succeeded}, analytics_available=True, now=now)
        error = cache[(*failed, True)][1]["error"]
        self.assertIn("ABC.NS", error)
        self.assertIn("KeyError", error)
        self.assertNotIn("secret-token", error)
        self.assertNotIn("secret-token", " ".join(logs.output))
        self.assertEqual(cache[(*succeeded, True)][1]["context"]["profile"]["ticker"], "XYZ.NS")

    @patch("features.analysis.stock_context.load_holding_context", side_effect=AssertionError("programming error"))
    def test_refresh_does_not_hide_unexpected_programming_errors(self, load):
        identity = ("ABC.NS", None, InvestmentHorizon.LONG_TERM_EQUITY)
        with self.assertRaises(AssertionError):
            refresh_holding_contexts({}, {identity}, analytics_available=False)

    @patch("features.analysis.stock_context.fetch_and_build_market_context", side_effect=requests.HTTPError("bad"))
    def test_provider_failure_is_reported_not_safe(self, fetch):
        identity = ("ABC.NS", None, InvestmentHorizon.LONG_TERM_EQUITY)
        with self.assertLogs("wealth_home_ai", level="ERROR"):
            result = load_holding_context(identity, analytics_available=False)
        self.assertIn("failed", result["error"])
        self.assertNotIn("gate", result)


if __name__ == "__main__":
    unittest.main()

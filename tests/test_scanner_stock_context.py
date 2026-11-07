import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from features.analysis.stock_context import CONTEXT_TTL
from features.equities.scanner import (
    _append_scan_context, _scanner_identities, build_scan_table_payload,
)
from services.stock_context_models import InvestmentHorizon, StockSignalContext


def _row(ticker):
    return {
        "Ticker": ticker, "Price": 100, "Recommendation": "HOLD",
        "Passes_filters": True, "Signal_score": 0,
    }


def _master():
    return pd.DataFrame([
        {"Symbol": "RELIANCE", "Ticker": "RELIANCE", "Company": "Reliance",
         "ISIN": "INE002A01018", "Exchange": "NSE"},
        {"Symbol": "ABC.BO", "Ticker": "ABC", "Company": "ABC",
         "ISIN": "INE002A01018", "Exchange": "BSE"},
    ])


def _finish_scan(app, prefix):
    job = app.session_state["_market_scan_jobs"].get(prefix)
    if job is not None:
        job["future"].result(timeout=10)
        app.run(timeout=20)


def _universe_mock(load):
    async def scan(stocks, token, *, on_progress, cancel):
        records = []
        for stock in stocks:
            identity = (stock["symbol"], stock["instrument_key"], stock["horizon"])
            result = load(identity, analytics_available=bool(token))
            records.append({"symbol": identity[0], "status": "ok" if result.get("context") else "error",
                            "context": result.get("context"), "local_gatekeeper": result.get("gate"),
                            "error": result.get("error")})
            on_progress(len(records), len(stocks))
        return records
    return scan


class ScannerStockContextTests(unittest.TestCase):
    def test_identity_verifies_master_mapping_and_uses_public_fallback(self):
        master = pd.concat([_master(), pd.DataFrame([
            {"Symbol": "BAD", "Ticker": "BAD", "ISIN": "INE002A01019", "Exchange": "NSE"},
            {"Symbol": "MISMATCH.BO", "Ticker": "OTHER", "ISIN": "INE002A01018", "Exchange": "BSE"},
            {"Symbol": "BROKER", "Ticker": "BROKER", "ISIN": "INE002A01018", "Exchange": "Broker map"},
            {"Symbol": "AMBIG", "Ticker": "AMBIG", "ISIN": "INE002A01018", "Exchange": "NSE"},
            {"Symbol": "AMBIG", "Ticker": "AMBIG", "ISIN": "INE009A01021", "Exchange": "NSE"},
        ])], ignore_index=True)
        rows = [_row(x) for x in ("RELIANCE", "ABC.BO", "BAD", "MISMATCH.BO", "BROKER", "AMBIG", "UNKNOWN")]
        identities = _scanner_identities(rows, master, "Trading")
        h = InvestmentHorizon.SHORT_TERM_TRADING
        self.assertEqual(identities["RELIANCE"], ("RELIANCE.NS", "NSE_EQ|INE002A01018", h))
        self.assertEqual(identities["ABC.BO"], ("ABC.BO", "BSE_EQ|INE002A01018", h))
        for ticker in ("BAD", "BROKER", "AMBIG", "UNKNOWN"):
            self.assertEqual(identities[ticker], (ticker + ".NS", None, h))
        self.assertEqual(identities["MISMATCH.BO"], ("MISMATCH.BO", None, h))
        self.assertEqual(_scanner_identities(rows, master, "Equities")["RELIANCE"][2],
                         InvestmentHorizon.LONG_TERM_EQUITY)

    def test_payload_context_fields_and_missing_rows_remain_aligned(self):
        first = {**_row("RELIANCE"), "SMA_20": 100, "RSI_14": 45, "As_of": "2026-01-01"}
        payload, config = build_scan_table_payload([first, _row("ABC.BO")], {})
        context = {
            "context": StockSignalContext.model_validate({
                "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "RELIANCE.NS"},
                "technicals": {"sma_20": 101, "rsi_14": 45},
                "intraday": {"lower_circuit_limit": 90},
                "news": {"upcoming_corporate_action_event": "BONUS"},
            }).model_dump(mode="json"),
            "gate": {"is_eligible_for_llm": False},
        }
        config = _append_scan_context(payload, config, {"RELIANCE": context})
        self.assertEqual(len(config["rawCols"]), len(config["rawTypes"]))
        for row in payload:
            self.assertEqual(len(row["raw"]), len(config["rawCols"]))
        idx = config["rawCols"].index("Intraday · lower circuit limit")
        self.assertEqual(payload[0]["raw"][idx], 90)
        self.assertEqual(payload[1]["raw"][idx], "")
        self.assertEqual(config["rawTypes"][config["rawCols"].index("As of")], "text")
        self.assertEqual(payload[0]["ind"]["SMA_20"][0], "101.0")
        self.assertEqual(payload[0]["raw"][config["rawCols"].index("SMA 20")], 101)
        self.assertTrue(payload[0]["extraGroups"])
        self.assertTrue(payload[1]["extraSections"])

    def test_explicit_scans_refresh_once_and_preserve_scope_snapshots_on_reruns(self):
        app = AppTest.from_string("""
from features.equities.scanner import _render_scanner
_render_scanner(scope="Equities", suggested_symbols=["RELIANCE", "ABC.BO"])
_render_scanner(scope="Trading", suggested_symbols=["RELIANCE", "ABC.BO"])
""")
        rows = [_row("RELIANCE"), _row("ABC.BO")]
        calls = []

        def refresh(identity, *, analytics_available):
            calls.append((identity, analytics_available))
            return ({"context": {"horizon": identity[2].value,
                                 "intraday": {"lower_circuit_limit": 90}},
                     "gate": {"is_eligible_for_llm": True}}
                    if identity[0] == "RELIANCE.NS" else {"error": "Provider unavailable"})

        with patch("features.equities.scanner._load_indian_equity_master", return_value=_master()), \
             patch("features.equities.scanner._scan_stocks", return_value=(rows, {})), \
             patch("features.equities.scanner.configured_value", return_value=""), \
             patch("features.equities.scanner._scan_universe", side_effect=_universe_mock(refresh)), \
             patch("features.equities.scanner.show_scan_table") as table:
            app.run(timeout=20)
            self.assertFalse(app.exception)
            self.assertFalse(calls)
            self.assertEqual(sum("several provider requests per stock" in w.value
                                 and "rate limits" in w.value for w in app.warning), 2)
            app.button(key="market_equities_scan_button").click().run(timeout=20)
            _finish_scan(app, "equities")
            self.assertFalse(app.exception)
            self.assertEqual(len(calls), 2)
            self.assertFalse(calls[0][1])
            self.assertTrue(any("unavailable for 1" in w.value for w in app.warning))
            payload, config = table.call_args.args
            self.assertEqual(len(payload), 2)
            self.assertTrue(config["showColumnFilters"])
            self.assertEqual(payload[1]["extraSections"][0]["note"], "Provider unavailable")
            self.assertEqual({identity[0][0] for identity in calls}, {"RELIANCE.NS", "ABC.BO"})
            app.run(timeout=20)
            self.assertEqual(len(calls), 2)
            app.button(key="market_trading_scan_button").click().run(timeout=20)
            _finish_scan(app, "trading")
            self.assertFalse(app.exception)
            self.assertEqual(len(calls), 4)
            self.assertNotEqual(calls[0][0][2], calls[2][0][2])
            self.assertEqual(len(app.session_state["stock_context_cache"]), 4)
            self.assertEqual(len(app.session_state["market_equities_scan_contexts"]), 2)
            self.assertEqual(len(app.session_state["market_trading_scan_contexts"]), 2)
            table.reset_mock()
            app.run(timeout=20)
            self.assertFalse(app.exception)
            self.assertEqual(len(calls), 4)
            equity_config, trading_config = [call.args[1] for call in table.call_args_list]
            self.assertNotEqual(equity_config["tableKey"], trading_config["tableKey"])
            self.assertEqual(len(app.session_state["market_equities_scan_results"]), 2)

    def test_scans_use_session_cache_and_token_availability(self):
        source = """
from features.equities.scanner import _render_scanner
_render_scanner(scope="Equities", suggested_symbols=["RELIANCE"])
"""
        first = AppTest.from_string(source)
        second = AppTest.from_string(source)
        with patch("features.equities.scanner._load_indian_equity_master", return_value=_master()), \
             patch("features.equities.scanner._scan_stocks", return_value=([_row("RELIANCE")], {})), \
             patch("features.equities.scanner.configured_value", return_value="analytics-token"), \
             patch("features.equities.scanner._scan_universe",
                   side_effect=_universe_mock(lambda identity, **kwargs: {"error": "Provider unavailable"})) as load, \
             patch("features.equities.scanner.show_scan_table"):
            first.run(timeout=20)
            first.button(key="market_equities_scan_button").click().run(timeout=20)
            _finish_scan(first, "equities")
            first.button(key="market_equities_scan_button").click().run(timeout=20)
            _finish_scan(first, "equities")
            self.assertEqual(load.call_count, 1)
            self.assertEqual(load.call_args.args[1], "analytics-token")
            self.assertEqual(load.call_args.args[0][0]["instrument_key"], "NSE_EQ|INE002A01018")
            second.run(timeout=20)
            second.button(key="market_equities_scan_button").click().run(timeout=20)
            _finish_scan(second, "equities")
            self.assertEqual(load.call_count, 2)
            self.assertFalse(first.exception)
            self.assertFalse(second.exception)

    def test_context_retained_without_viewer_and_respects_cache_expiry(self):
        source = """
from features.equities.scanner import _render_scanner
_render_scanner(scope="Equities", suggested_symbols=["RELIANCE"])
_render_scanner(scope="Trading", suggested_symbols=["RELIANCE"])
"""
        app = AppTest.from_string(source)
        result = {
            "context": {"source_data": {"rows": [123, 456]}, "profile": {"ticker": "RELIANCE.NS"}},
            "gate": {"is_eligible_for_llm": True},
        }
        with patch("features.equities.scanner._load_indian_equity_master", return_value=_master()), \
             patch("features.equities.scanner._scan_stocks", return_value=([_row("RELIANCE")], {})), \
             patch("features.equities.scanner.configured_value", return_value="token"), \
             patch("features.equities.scanner._scan_universe",
                   side_effect=_universe_mock(lambda identity, **kwargs: result)) as load, \
             patch("features.equities.scanner.show_scan_table") as table:
            app.run(timeout=20)
            app.button(key="market_equities_scan_button").click().run(timeout=20)
            _finish_scan(app, "equities")
            self.assertFalse(app.exception)
            self.assertEqual(len(app.get("json")), 0)
            self.assertNotIn("source_data", str(table.call_args.args[0]))
            app.button(key="market_trading_scan_button").click().run(timeout=20)
            _finish_scan(app, "trading")
            self.assertEqual(load.call_count, 2)
            app.run(timeout=20)
            self.assertFalse(app.exception)
            self.assertEqual(len(app.get("json")), 0)
            self.assertEqual(len(app.get("download_button")), 0)
            self.assertFalse(any(exp.label == "Full source data and context" for exp in app.expander))
            self.assertEqual(load.call_count, 2)
            equity_key = app.session_state["market_equities_scan_contexts"]["RELIANCE"]
            cache = app.session_state["stock_context_cache"]
            self.assertEqual(cache[equity_key][1]["context"]["source_data"]["rows"], [123, 456])
            cache[equity_key] = (datetime.now(timezone.utc) - CONTEXT_TTL - timedelta(seconds=1), result)
            app.session_state["market_equities_scan_context_snapshot"][equity_key] = cache[equity_key]
            table.reset_mock()
            app.run(timeout=20)
            self.assertFalse(app.exception)
            self.assertEqual(len(app.get("json")), 0)
            self.assertEqual(load.call_count, 2)
            self.assertIn("stale", table.call_args_list[0].args[0][0]["extraSections"][0]["note"])
            self.assertEqual(len(app.session_state["market_equities_scan_results"]), 1)

    def test_returning_to_scanner_ignores_saved_download_button_state(self):
        app = AppTest.from_string("""
import streamlit as st
from features.equities.scanner import _restore_scanner_state, _save_scanner_state
st.session_state["_run_seq"] = st.session_state.get("_run_seq", 0) + 1
show = st.checkbox("Show scanner", value=True)
if show:
    for prefix in ("equities", "trading"):
        _restore_scanner_state(prefix)
        st.text_input("Saved setting", key=f"market_{prefix}_setting")
        _save_scanner_state(prefix)
""")
        app.run(timeout=20)
        for prefix in ("equities", "trading"):
            app.session_state[f"market_{prefix}_setting"] = f"{prefix} value"
        app.run(timeout=20)
        self.assertFalse(app.exception)
        for prefix in ("equities", "trading"):
            download_key = f"market_{prefix}_scanner_context_viewer_download"
            self.assertNotIn(download_key, app.session_state["_scanner_saved"][prefix])
            # Emulate an entry retained by a session running the previous code.
            app.session_state["_scanner_saved"][prefix][download_key] = False

        app.checkbox[0].uncheck().run(timeout=20)
        app.checkbox[0].check().run(timeout=20)
        self.assertFalse(app.exception)
        for prefix in ("equities", "trading"):
            self.assertEqual(app.session_state[f"market_{prefix}_setting"], f"{prefix} value")
            self.assertNotIn(f"market_{prefix}_scanner_context_viewer_download",
                             app.session_state["_scanner_saved"][prefix])
        self.assertEqual(len(app.get("json")), 0)

    def test_long_scan_retains_stale_context_and_gate_prefers_newer_cache(self):
        app = AppTest.from_string("""
from features.equities.scanner import _render_scanner
_render_scanner(scope="Equities", suggested_symbols=["RELIANCE", "ABC.BO"])
""")
        rows = [_row("RELIANCE"), _row("ABC.BO")]

        def load(identity, *, analytics_available):
            return {"context": {"profile": {"ticker": identity[0]},
                                "intraday": {"lower_circuit_limit": 90}},
                    "gate": {"is_eligible_for_llm": True}}

        with patch("features.equities.scanner._load_indian_equity_master", return_value=_master()), \
             patch("features.equities.scanner._scan_stocks", return_value=(rows, {})), \
             patch("features.equities.scanner.configured_value", return_value=""), \
             patch("features.equities.scanner._scan_universe", side_effect=_universe_mock(load)), \
             patch("features.equities.scanner.show_scan_table") as table:
            app.run(timeout=20)
            app.button(key="market_equities_scan_button").click().run(timeout=20)
            _finish_scan(app, "equities")
            keys = app.session_state["market_equities_scan_contexts"]
            snapshot = app.session_state["market_equities_scan_context_snapshot"]
            cache = app.session_state["stock_context_cache"]
            old = datetime.now(timezone.utc) - CONTEXT_TTL - timedelta(minutes=5)
            for key in keys.values():
                snapshot[key] = (old, snapshot[key][1])
                cache[key] = snapshot[key]
            newer = {"context": {"profile": {"ticker": "ABC.BO"},
                                  "intraday": {"lower_circuit_limit": 95}},
                     "gate": {"is_eligible_for_llm": False}}
            cache[keys["ABC.BO"]] = (datetime.now(timezone.utc), newer)
            table.reset_mock()
            app.run(timeout=20)
            payload, config = table.call_args.args
            lower = config["rawCols"].index("Intraday · lower circuit limit")
            gate = config["rawCols"].index("Gate · eligibility")
            self.assertEqual(payload[0]["raw"][lower], 90)
            self.assertEqual(payload[0]["raw"][gate], "Eligible (stale)")
            self.assertIn("gate are stale", payload[0]["extraSections"][0]["note"])
            self.assertEqual(payload[1]["raw"][lower], 95)
            self.assertEqual(payload[1]["raw"][gate], "Blocked")
            self.assertTrue(any("stale for 1" in warning.value for warning in app.warning))
            self.assertFalse(any("unavailable for" in warning.value for warning in app.warning))


if __name__ == "__main__":
    unittest.main()

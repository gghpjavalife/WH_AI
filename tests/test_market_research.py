import math
import unittest
from concurrent.futures import Future
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests
from streamlit.testing.v1 import AppTest

from application.dashboard import (
    _close_analysis_setup_dialog,
    _pending_analysis_setup_scope,
)
from features.equities.scanner import (
    RECOMMENDED_FILTERS,
    _ai_recommendation_candidates,
    _scan_stocks,
)
from features.analysis.panel import analysis_status_icon
from features.equities.market_data import (
    MarketDataError,
    assess_stock,
    calculate_indicators,
    fetch_bse_equity_master,
    fetch_daily_history,
    fetch_indian_equity_master,
    fetch_nse_equity_master,
    matches_scanner_filters,
    normalize_symbol,
)
from features.mutual_funds.research import _fund_frame
from ui.helpers import brand_lockup_html


def _equity_master(*symbols: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Symbol": symbol,
                "Company": symbol,
                "ISIN": f"INE{index:09d}",
                "Exchange": "NSE",
            }
            for index, symbol in enumerate(symbols, start=1)
        ]
    )


class MarketResearchTests(unittest.TestCase):
    def test_analysis_setup_waits_for_existing_settings_dialog(self):
        state = {
            "analysis_setup_dialog_scope": "portfolio_overview",
            "user_settings_dialog_open": True,
        }
        with patch("application.dashboard.st.session_state", state):
            self.assertIsNone(_pending_analysis_setup_scope())

        state["user_settings_dialog_open"] = False
        state["user_settings_feedback"] = {"kind": "success"}
        with patch("application.dashboard.st.session_state", state):
            self.assertIsNone(_pending_analysis_setup_scope())

        state.pop("user_settings_feedback")
        with patch("application.dashboard.st.session_state", state):
            self.assertEqual(
                _pending_analysis_setup_scope(), "portfolio_overview"
            )

    def test_dismissing_analysis_setup_clears_its_pending_scope(self):
        state = {"analysis_setup_dialog_scope": "portfolio_overview"}
        with patch("application.dashboard.st.session_state", state):
            _close_analysis_setup_dialog()
            self.assertIsNone(_pending_analysis_setup_scope())

    def test_analysis_status_indicators_distinguish_caution_from_strength(self):
        self.assertEqual(
            analysis_status_icon({"assessment": {"sentiment": "positive"}}),
            "🟢",
        )
        self.assertEqual(
            analysis_status_icon({"assessment": {"sentiment": "negative"}}),
            "🔴",
        )
        self.assertEqual(analysis_status_icon({"assessment": {}}), None)

    def test_home_shows_categorized_analysis_without_sentiment_tab_labels(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        current_time = pd.Timestamp.now(tz="Asia/Kolkata").to_pydatetime()
        analysis = {
            "analysis": "The portfolio is concentrated and should retain cash.",
            "assessment": {
                "sentiment": "cautious",
                "summary": "Concentration is elevated; avoid unverified deployment.",
                "strengths": ["No unverified deployment was made."],
                "warnings": ["One holding dominates portfolio value."],
                "suggestions": ["Review position sizing before adding capital."],
                "scenarios": [
                    {
                        "title": "Downside case",
                        "condition": "The largest holding weakens further.",
                        "implication": "Reassess concentration and risk limits.",
                    }
                ],
            },
            "cash_deployment_list": [],
        }
        app.session_state["token"] = "test-token"
        app.session_state["broker_state"] = Mock()
        app.session_state["broker_name"] = "Upstox"
        app.session_state["last_sync_attempt"] = current_time
        app.session_state["profile_fetch_attempted"] = True
        equity_analysis = {
            **analysis,
            "assessment": {
                **analysis["assessment"],
                "sentiment": "positive",
            },
        }
        app.session_state["analysis_results"] = {
            "all": analysis,
            "equity": equity_analysis,
            "options": equity_analysis,
        }
        app.session_state["app_navigation"] = "Home"
        app.run(timeout=20)

        self.assertFalse(app.exception)
        tab_labels = [tab.label for tab in app.tabs]
        self.assertTrue(any("Home" in label for label in tab_labels))
        self.assertTrue(any("Equities" in label for label in tab_labels))
        self.assertTrue(any("F&O" in label for label in tab_labels))
        self.assertFalse(
            any(
                status in label
                for label in tab_labels
                for status in ("Review", "Constructive", "Risk")
            )
        )
        self.assertTrue(
            any("Concentration is elevated" in item.value for item in app.markdown)
        )
        self.assertTrue(
            any("One holding dominates" in item.value for item in app.markdown)
        )
        self.assertTrue(any("Suggestions" in item.value for item in app.markdown))
        self.assertFalse(any("Downside case" in item.value for item in app.markdown))
        app.session_state["app_navigation"] = ":material/show_chart: 🔴 Equities"
        app.session_state["analysis_results"]["equity"]["assessment"][
            "sentiment"
        ] = "cautious"
        app.run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(
            app.session_state["app_navigation"],
            ":material/show_chart: Equities",
        )
        app.session_state["analysis_results"]["all"]["assessment"][
            "sentiment"
        ] = "positive"
        app.session_state["analysis_results"]["equity"]["assessment"][
            "sentiment"
        ] = "positive"
        app.run(timeout=20)
        self.assertFalse(app.exception)
        self.assertFalse(
            any(
                status in tab.label
                for tab in app.tabs
                for status in ("Review", "Constructive", "Risk")
            )
        )
        self.assertEqual(
            app.session_state["app_navigation"],
            ":material/show_chart: Equities",
        )

    def test_equities_tab_shows_its_scoped_ai_brief(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["analysis_results"] = {
            "equity": {
                "analysis": "Equity positions show mixed performance.",
                "assessment": {
                    "sentiment": "cautious",
                    "summary": "Concentration and drawdowns need attention.",
                    "strengths": ["Several positions remain profitable."],
                    "warnings": ["Loss-making holdings remain in the portfolio."],
                    "suggestions": ["Review each position against its thesis."],
                    "scenarios": [],
                },
                "cash_deployment_list": [],
            }
        }
        app.session_state["app_navigation"] = "Equities"
        app.run(timeout=20)

        self.assertFalse(app.exception)
        self.assertFalse(
            any(
                status in tab.label
                for tab in app.tabs
                for status in ("Review", "Constructive", "Risk")
            )
        )
        self.assertEqual(
            app.session_state["app_navigation"],
            ":material/show_chart: Equities",
        )
        self.assertTrue(
            any("Concentration and drawdowns" in item.value for item in app.markdown)
        )
        self.assertTrue(
            any("Loss-making holdings" in item.value for item in app.markdown)
        )


    def test_public_chart_response_is_normalized_to_daily_history(self):
        timestamps = [1_700_000_000 + day * 86_400 for day in range(60)]
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": timestamps,
                        "indicators": {
                            "quote": [
                                {
                                    "open": [100.0] * 60,
                                    "high": [101.0] * 60,
                                    "low": [99.0] * 60,
                                    "close": [100.0] * 60,
                                    "volume": [120_000] * 60,
                                }
                            ]
                        },
                    }
                ],
                "error": None,
            }
        }
        response = Mock()
        response.status_code = 200
        response.json.return_value = payload
        with patch(
            "requests.Session.get",
            return_value=response,
        ) as get:
            fetch_daily_history.clear()
            history = fetch_daily_history("RELIANCE")
            fetch_daily_history.clear()

        self.assertEqual(len(history), 60)
        self.assertEqual(history.iloc[-1]["Close"], 100.0)
        self.assertEqual(history.iloc[-1]["Volume"], 120_000)
        self.assertIn("/RELIANCE.NS", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["params"]["interval"], "1d")

    def test_symbol_normalization_accepts_nse_suffix_and_rejects_bad_input(self):
        self.assertEqual(normalize_symbol("  reliance.ns "), "RELIANCE")
        with self.assertRaises(ValueError):
            normalize_symbol("../private")
        with self.assertRaises(ValueError):
            normalize_symbol("^NSEI")

    def test_nse_equity_master_loads_all_valid_active_eq_symbols(self):
        response = Mock()
        response.content = (
            b"\xef\xbb\xbfSYMBOL,NAME OF COMPANY,SERIES,ISIN NUMBER\n"
            b"ALPHA,Alpha Limited,EQ,INE000A01001\n"
            b"BETA,Beta Limited,BE,INE000A01002\n"
            b"GAMMA,Gamma Limited, EQ ,INE000A01003\n"
            b"../BAD,Invalid Limited,EQ,INE000A01004\n"
            b"ALPHA,Alpha Limited,EQ,INE000A01001\n"
        )
        response.status_code = 200
        response.raise_for_status.return_value = None
        with patch(
            "features.equities.market_data.requests.get",
            return_value=response,
        ) as get:
            fetch_nse_equity_master.clear()
            master = fetch_nse_equity_master()
            fetch_nse_equity_master.clear()

        self.assertEqual(master["Symbol"].tolist(), ["ALPHA", "GAMMA"])
        self.assertEqual(master.loc[0, "Company"], "Alpha Limited")
        self.assertEqual(master.loc[1, "ISIN"], "INE000A01003")
        self.assertIn("EQUITY_L.csv", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["timeout"], (5, 12))

    def test_nse_equity_master_surfaces_feed_failures(self):
        with patch(
            "features.equities.market_data.requests.get",
            side_effect=requests.RequestException("unavailable"),
        ):
            fetch_nse_equity_master.clear()
            with self.assertRaisesRegex(MarketDataError, "NSE equity list"):
                fetch_nse_equity_master()
            fetch_nse_equity_master.clear()

    def test_bse_equity_master_maps_active_equities_to_yahoo_codes(self):
        response = Mock()
        response.json.return_value = [
            {
                "SCRIP_CD": "500002",
                "Scrip_Name": "ABB India Ltd",
                "Status": "Active",
                "ISIN_NUMBER": "INE117A01022",
                "scrip_id": "ABB",
                "Segment": "Equity",
            },
            {
                "SCRIP_CD": "500003",
                "Scrip_Name": "Aegis Logistics Ltd",
                "Status": "Active",
                "ISIN_NUMBER": "INE208C01025",
                "scrip_id": "AEGISLOG",
                "Segment": "Equity",
            },
            {
                "SCRIP_CD": "500004",
                "Scrip_Name": "Inactive Ltd",
                "Status": "Suspended",
                "ISIN_NUMBER": "INE000A01000",
                "scrip_id": "INACTIVE",
                "Segment": "Equity",
            },
            {
                "SCRIP_CD": "500005",
                "Scrip_Name": "Preference Ltd",
                "Status": "Active",
                "ISIN_NUMBER": "INE000A01005",
                "scrip_id": "PREF",
                "Segment": "PreferenceShares",
            },
        ]
        response.status_code = 200
        response.raise_for_status.return_value = None
        with patch(
            "features.equities.market_data.requests.get",
            return_value=response,
        ) as get:
            fetch_bse_equity_master.clear()
            master = fetch_bse_equity_master()
            fetch_bse_equity_master.clear()

        self.assertEqual(master["Symbol"].tolist(), ["ABB.BO", "AEGISLOG.BO"])
        self.assertEqual(master["Exchange"].tolist(), ["BSE", "BSE"])
        self.assertEqual(master.loc[0, "Ticker"], "ABB")
        self.assertEqual(get.call_args.kwargs["params"]["segment"], "Equity")
        self.assertEqual(get.call_args.kwargs["params"]["status"], "Active")

    def test_indian_equity_master_combines_exchanges_without_duplicate_isins(self):
        bse = pd.DataFrame(
            [
                {
                    "Symbol": "ABB.BO",
                    "Company": "ABB India Ltd",
                    "ISIN": "INE117A01022",
                    "Exchange": "BSE",
                    "Ticker": "ABB",
                },
                {
                    "Symbol": "AEGISLOG.BO",
                    "Company": "Aegis Logistics Ltd",
                    "ISIN": "INE208C01025",
                    "Exchange": "BSE",
                    "Ticker": "AEGISLOG",
                },
            ]
        )
        nse = pd.DataFrame(
            [
                {
                    "Symbol": "ABB",
                    "Company": "ABB India Limited",
                    "ISIN": "INE117A01022",
                    "Exchange": "NSE",
                },
                {
                    "Symbol": "RELIANCE",
                    "Company": "Reliance Industries Limited",
                    "ISIN": "INE002A01018",
                    "Exchange": "NSE",
                },
            ]
        )
        with (
            patch(
                "features.equities.market_data.fetch_bse_equity_master",
                return_value=bse,
            ),
            patch(
                "features.equities.market_data.fetch_nse_equity_master",
                return_value=nse,
            ),
        ):
            fetch_indian_equity_master.clear()
            master = fetch_indian_equity_master()
            fetch_indian_equity_master.clear()

        self.assertEqual(
            set(master["Symbol"]),
            {"ABB", "RELIANCE", "AEGISLOG.BO"},
        )
        self.assertEqual(
            master.loc[master["ISIN"].eq("INE117A01022"), "Exchange"].iloc[0],
            "NSE",
        )

    def test_nse_bse_chart_symbols_keep_their_exchange_suffix(self):
        response = Mock()
        response.json.return_value = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1_700_000_000],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [100.0],
                                    "high": [101.0],
                                    "low": [99.0],
                                    "close": [100.0],
                                    "volume": [120_000],
                                }
                            ]
                        },
                    }
                ],
                "error": None,
            }
        }
        response.status_code = 200
        response.raise_for_status.return_value = None
        with patch(
            "requests.Session.get",
            return_value=response,
        ) as get:
            fetch_daily_history.clear()
            fetch_daily_history("ABB.BO")
            self.assertIn("/ABB.BO", get.call_args.args[0])
            fetch_daily_history.clear()

    def test_indicators_calculate_consistent_trend_momentum_and_volume(self):
        history = pd.DataFrame(
            {
                "Close": [100.0 + day * 0.2 for day in range(260)],
                "High": [102.0 + day * 0.2 for day in range(260)],
                "Low": [98.0 + day * 0.2 for day in range(260)],
                "Volume": [150_000.0] * 260,
            }
        )
        indicators = calculate_indicators(history)

        self.assertEqual(indicators["price"], 151.8)
        self.assertGreater(indicators["sma20"], indicators["sma50"])
        self.assertGreater(indicators["sma50"], indicators["sma200"])
        self.assertEqual(indicators["rsi14"], 100.0)
        self.assertEqual(indicators["average_volume_20"], 150_000.0)
        self.assertEqual(indicators["relative_volume"], 1.0)
        self.assertAlmostEqual(indicators["atr14"], 4.0)
        self.assertTrue(60.0 < indicators["stochastic_k"] < 80.0)
        self.assertGreater(indicators["bollinger_upper"], indicators["sma20"])
        self.assertIn("macd_histogram", indicators)
        self.assertIn("volatility20_annualized_pct", indicators)
        self.assertGreaterEqual(indicators["adx14"], 25.0)
        self.assertGreater(indicators["plus_di14"], indicators["minus_di14"])
        self.assertTrue(math.isfinite(indicators["cci20"]))
        self.assertEqual(indicators["mfi14"], 100.0)
        self.assertGreater(indicators["obv_change20"], 0)
        self.assertEqual(indicators["high_52w"], 153.8)
        self.assertAlmostEqual(indicators["low_52w"], 99.6)

    def test_short_or_invalid_price_history_is_rejected(self):
        with self.assertRaises(MarketDataError):
            calculate_indicators(pd.DataFrame({"Close": [100.0] * 49}))
        with self.assertRaises(MarketDataError):
            calculate_indicators(pd.DataFrame({"Close": [0.0] * 50}))

    def test_fund_research_calculates_asset_specific_values(self):
        funds = _fund_frame(
            pd.DataFrame(
                [
                    {
                        "Fund": "Example fund",
                        "Units": 10.0,
                        "Avg_NAV": 100.0,
                        "Latest_NAV": 120.0,
                        "NAV_Date": date.today().isoformat(),
                        "ISIN": "INF000000000",
                    }
                ]
            )
        )
        self.assertEqual(funds.iloc[0]["Current_Value"], 1200.0)
        self.assertEqual(funds.iloc[0]["Returns_%"], 20.0)
        self.assertEqual(funds.iloc[0]["NAV_age_days"], 0)

    def test_recommendation_has_explicit_bullish_and_bearish_reasons(self):
        buy = assess_stock(
            {
                "price": 110.0,
                "sma20": 105.0,
                "sma50": 100.0,
                "sma200": 90.0,
                "ema12": 108.0,
                "ema26": 104.0,
                "rsi14": 58.0,
                "macd": 2.0,
                "macd_signal": 1.0,
                "relative_volume": 1.3,
                "adx14": 30.0,
                "plus_di14": 22.0,
                "minus_di14": 10.0,
                "stochastic_k": 65.0,
                "stochastic_d": 55.0,
                "cci20": 30.0,
                "mfi14": 60.0,
                "obv_change20": 1000.0,
                "bollinger_middle": 105.0,
                "return_1m_pct": 3.0,
                "return_3m_pct": 7.0,
                "price_52w_position_pct": 80.0,
            }
        )
        sell = assess_stock(
            {
                "price": 90.0,
                "sma20": 100.0,
                "sma50": 105.0,
                "sma200": 120.0,
                "ema12": 98.0,
                "ema26": 102.0,
                "rsi14": 30.0,
                "macd": -2.0,
                "macd_signal": -1.0,
                "relative_volume": 0.7,
                "adx14": 30.0,
                "plus_di14": 8.0,
                "minus_di14": 20.0,
                "stochastic_k": 20.0,
                "stochastic_d": 30.0,
                "cci20": -30.0,
                "mfi14": 30.0,
                "obv_change20": -1000.0,
                "bollinger_middle": 100.0,
                "return_1m_pct": -3.0,
                "return_3m_pct": -7.0,
                "price_52w_position_pct": 20.0,
            }
        )
        partial = assess_stock(
            {
                "price": 110.0,
                "sma20": 105.0,
                "sma50": 100.0,
                "rsi14": 58.0,
                "macd": 2.0,
                "macd_signal": 1.0,
            }
        )
        self.assertEqual(buy["recommendation"], "STRONG BUY")
        self.assertIn("positive", buy["reason"])
        self.assertGreaterEqual(len(buy["indicators"]), 15)
        self.assertTrue(
            any(
                item["Indicator"] == "50-day vs 200-day trend"
                for item in partial["indicators"]
            )
        )
        unavailable_names = {
            item["Indicator"]
            for item in partial["indicators"]
            if item["Signal"] == "Unavailable"
        }
        self.assertIn("ADX / directional index (14)", unavailable_names)
        self.assertIn("CCI (20)", unavailable_names)
        self.assertIn("Money flow index (14)", unavailable_names)
        self.assertIn("20-day OBV change", unavailable_names)
        self.assertEqual(sell["recommendation"], "STRONG SELL")
        self.assertIn("negative", sell["reason"])

    def test_assessment_scores_all_available_indicator_groups(self):
        assessment = assess_stock(
            {
                "price": 120.0,
                "sma20": 115.0,
                "sma50": 110.0,
                "sma200": 100.0,
                "ema12": 117.0,
                "ema26": 112.0,
                "rsi14": 60.0,
                "macd": 3.0,
                "macd_signal": 2.0,
                "adx14": 30.0,
                "plus_di14": 25.0,
                "minus_di14": 10.0,
                "stochastic_k": 70.0,
                "stochastic_d": 60.0,
                "cci20": 25.0,
                "mfi14": 60.0,
                "obv_change20": 1000.0,
                "bollinger_middle": 115.0,
                "relative_volume": 1.2,
                "return_1m_pct": 4.0,
                "return_3m_pct": 8.0,
                "price_52w_position_pct": 75.0,
            }
        )

        self.assertEqual(assessment["recommendation"], "STRONG BUY")
        self.assertEqual(assessment["evaluated_checks"], 17)
        self.assertEqual(assessment["bullish_checks"], 17)

    def test_unavailable_indicator_data_is_not_counted_as_positive(self):
        assessment = assess_stock(
            {
                "price": 110.0,
                "sma20": 105.0,
                "sma50": 100.0,
                "rsi14": 60.0,
                "macd": 2.0,
                "macd_signal": 1.0,
            }
        )

        self.assertEqual(assessment["evaluated_checks"], 4)
        self.assertEqual(assessment["bullish_checks"], 4)

    def test_ai_candidate_filter_keeps_only_nse_buys_and_honors_custom_filters(self):
        rows = [
            {"Ticker": "AAA", "Recommendation": "STRONG BUY", "Passes_filters": False},
            {"Ticker": "BBB", "Recommendation": "BUY", "Passes_filters": True},
            {"Ticker": "CCC", "Recommendation": "HOLD", "Passes_filters": True},
            {"Ticker": "DDD.BO", "Recommendation": "STRONG BUY", "Passes_filters": True},
        ]

        self.assertEqual(
            [row["Ticker"] for row in _ai_recommendation_candidates(rows)],
            ["AAA", "BBB"],
        )
        self.assertEqual(
            [
                row["Ticker"]
                for row in _ai_recommendation_candidates(
                    rows, use_custom_filters=True
                )
            ],
            ["BBB"],
        )

    def test_scanner_filters_enforce_all_selected_limits(self):
        indicators = {
            "price": 100.0,
            "sma50": 90.0,
            "rsi14": 60.0,
            "average_volume_20": 200_000.0,
        }
        self.assertTrue(
            matches_scanner_filters(
                indicators,
                minimum_price=20.0,
                maximum_price=5000.0,
                minimum_rsi=35.0,
                maximum_rsi=70.0,
                minimum_average_volume=100_000.0,
                require_above_sma50=True,
            )
        )
        self.assertFalse(
            matches_scanner_filters(
                indicators,
                minimum_price=101.0,
                maximum_price=5000.0,
                minimum_rsi=35.0,
                maximum_rsi=70.0,
                minimum_average_volume=100_000.0,
                require_above_sma50=True,
            )
        )
        trade_indicators = {
            **indicators,
            "relative_volume": 1.4,
            "atr_pct": 1.8,
        }
        self.assertTrue(
            matches_scanner_filters(
                trade_indicators,
                minimum_price=20.0,
                maximum_price=5000.0,
                minimum_rsi=35.0,
                maximum_rsi=70.0,
                minimum_average_volume=100_000.0,
                require_above_sma50=True,
                minimum_relative_volume=1.0,
                minimum_atr_pct=1.0,
            )
        )
        self.assertFalse(
            matches_scanner_filters(
                trade_indicators,
                minimum_price=20.0,
                maximum_price=5000.0,
                minimum_rsi=35.0,
                maximum_rsi=70.0,
                minimum_average_volume=100_000.0,
                require_above_sma50=True,
                minimum_relative_volume=1.5,
                minimum_atr_pct=1.0,
            )
        )

    def test_scanner_returns_values_and_explanations_for_the_full_indicator_set(self):
        history = pd.DataFrame(
            {
                "Date": pd.date_range("2025-01-01", periods=260).date,
                "Close": [100 + day * 0.1 for day in range(260)],
                "High": [102 + day * 0.1 for day in range(260)],
                "Low": [98 + day * 0.1 for day in range(260)],
                "Volume": [250_000] * 260,
            }
        )
        with patch(
            "features.equities.scanner.fetch_daily_history",
            return_value=history,
        ):
            rows, failures = _scan_stocks(
                ["RELIANCE"],
                {
                    "minimum_price": RECOMMENDED_FILTERS["minimum_price"],
                    "maximum_price": RECOMMENDED_FILTERS["maximum_price"],
                    "minimum_rsi": RECOMMENDED_FILTERS["minimum_rsi"],
                    "maximum_rsi": RECOMMENDED_FILTERS["maximum_rsi"],
                    "minimum_average_volume": RECOMMENDED_FILTERS[
                        "minimum_average_volume"
                    ],
                    "require_above_sma50": RECOMMENDED_FILTERS[
                        "require_above_sma50"
                    ],
                },
            )

        self.assertFalse(failures)
        row = rows[0]
        self.assertEqual(row["As_of"], str(history["Date"].iloc[-1]))
        self.assertIn("Daily_change_%", row)
        self.assertIn("SMA_20", row)
        self.assertIn("SMA_50", row)
        self.assertIn("SMA_200", row)
        self.assertIn("EMA_12", row)
        self.assertIn("EMA_26", row)
        self.assertIn("RSI_14", row)
        self.assertIn("MACD", row)
        self.assertIn("MACD_signal", row)
        self.assertIn("MACD_histogram", row)
        self.assertIn("ATR_14", row)
        self.assertIn("ATR_%", row)
        self.assertIn("Bollinger_lower", row)
        self.assertIn("Bollinger_middle", row)
        self.assertIn("Bollinger_upper", row)
        self.assertIn("Bollinger_width_%", row)
        self.assertIn("Stochastic_%K", row)
        self.assertIn("Stochastic_%D", row)
        self.assertIn("ADX_14", row)
        self.assertIn("Plus_DI_14", row)
        self.assertIn("Minus_DI_14", row)
        self.assertIn("CCI_20", row)
        self.assertIn("MFI_14", row)
        self.assertIn("OBV_change_20d", row)
        self.assertIn("Volatility_20d_annualized_%", row)
        self.assertIn("Return_1m_%", row)
        self.assertIn("Return_3m_%", row)
        self.assertIn("Average_volume_20d", row)
        self.assertIn("Volume_vs_average", row)
        self.assertIn("52w_high", row)
        self.assertIn("52w_low", row)
        self.assertIn("52w_range_position_%", row)
        self.assertIn("50-day vs 200-day trend", row["Indicator_signals"])

    def test_home_is_default_and_market_is_available_without_a_broker(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Equities"
        master = pd.DataFrame(
            [
                {
                    "Symbol": "ALPHA",
                    "Company": "Alpha Limited",
                    "ISIN": "INE000A01001",
                    "Exchange": "NSE",
                },
                {
                    "Symbol": "BETA",
                    "Company": "Beta Limited",
                    "ISIN": "INE000A01002",
                    "Exchange": "BSE",
                },
            ]
        )
        with patch(
            "features.equities.scanner.fetch_indian_equity_master",
            return_value=master,
        ):
            app.run()

        self.assertFalse(app.exception)
        self.assertEqual(
            [tab.label for tab in app.tabs],
            [
                ":material/home: Home",
                ":material/show_chart: Equities",
                "Holdings · 0.0%",
                "Market Scanner",
                ":material/swap_horiz: Trades",
                ":material/query_stats: F&O",
                ":material/savings: Mutual Funds",
                ":material/shopping_cart: Cart (0)",
            ],
        )
        self.assertGreaterEqual(len(app.get("popover")), 2)
        self.assertFalse(app.exception)
        self.assertTrue(
            any(
                "GGHP" in item.value and "gghp-name" in item.value
                for item in app.get("html")
            )
        )
        brand_html = brand_lockup_html(
            "GGHP",
            "Governed Growth & Hedged Portfolios",
            "Governed multi-broker portfolio companion",
        )
        self.assertEqual(brand_html.count('class="gghp-initial"'), 4)
        self.assertIn("Governed multi-broker portfolio companion", brand_html)
        self.assertTrue(
            any("JEV guardrails" in item.value for item in app.markdown)
        )
        self.assertTrue(
            any("AI insights" in item.value for item in app.markdown)
        )
        feature_popover_help = {
            item.proto.popover.help
            for item in app.get("popover")
            if item.proto.HasField("popover")
        }
        self.assertIn(
            "What AI insights do and when to use them.",
            feature_popover_help,
        )
        self.assertIn(
            "How the deterministic JEV checks protect app-issued buys.",
            feature_popover_help,
        )
        self.assertEqual(
            app.session_state["app_navigation"],
            ":material/show_chart: Equities",
        )
        self.assertTrue(
            any(
                tab.label.endswith("Equities")
                for tab in app.tabs
            )
        )
        self.assertTrue(
            any("No broker is connected" in item.value for item in app.info)
        )
        self.assertTrue(
            any(widget.label == "Choose which stocks to scan" for widget in app.selectbox)
        )
        self.assertFalse(
            any(tab.label == "Market research" for tab in app.tabs)
        )
        self.assertIn(
            "Market Scanner",
            [tab.label for tab in app.tabs],
        )

    def test_broker_connection_home_uses_the_compact_account_title(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.run()

        self.assertFalse(app.exception)
        self.assertTrue(
            any(
                "Connect a broker to load your portfolio" in item.value
                for item in app.caption
            )
        )
        self.assertFalse(
            any(
                title.value == "Welcome to Wealth Home AI"
                for title in app.title
            )
        )

    def test_opening_user_settings_keeps_workspace_rendered(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        ).run(timeout=20)

        app.button(key="open_user_settings").click().run(timeout=20)

        self.assertFalse(app.exception)
        self.assertTrue(any(item.type == "dialog" for item in app))
        self.assertTrue(
            any(item.key == "settings_profile_name" for item in app.text_input)
        )
        self.assertIn("run_analysis_all", {item.key for item in app.button})
        self.assertTrue(
            any(item.key == "selected_broker" for item in app.selectbox)
        )

    def test_user_settings_remains_available_during_analysis(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        current_time = pd.Timestamp.now(tz="Asia/Kolkata").to_pydatetime()
        broker = Mock()
        broker.fetch_live_prices.return_value = {}
        app.session_state["token"] = "test-token"
        app.session_state["broker_state"] = broker
        app.session_state["broker_name"] = "Upstox"
        app.session_state["last_sync_attempt"] = current_time
        app.session_state["profile_fetch_attempted"] = True
        app.session_state["user_gemini_api_key"] = "test-api-key"
        app.session_state["llm_model_selection_gemini"] = "gemini-2.5-flash"
        quote_future = Future()
        quote_future.set_result({})
        analysis_future = Future()
        with patch(
            "services.analysis_jobs.submit_analysis_task",
            return_value=quote_future,
        ) as quote_submit, patch(
            "services.analysis_jobs.submit_portfolio_analysis",
            return_value=analysis_future,
        ) as analysis_submit:
            app.run(timeout=20)
            app.button(key="run_analysis_home").click().run(timeout=20)
            app.button(key="open_user_settings").click().run(timeout=20)

        quote_submit.assert_called_once()
        analysis_submit.assert_called_once()
        self.assertFalse(app.exception)
        self.assertTrue(any(item.type == "dialog" for item in app))
        self.assertTrue(
            any(item.key == "settings_profile_name" for item in app.text_input)
        )
        self.assertFalse(
            any("AI analysis is running" in item.value for item in app.info)
        )
        self.assertTrue(
            any(
                item.key == "run_analysis_home"
                and item.label == "Analyzing portfolio..."
                and item.disabled
                for item in app.button
            )
        )

    def test_connected_account_context_is_available_in_home_and_equities_tabs(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        current_time = pd.Timestamp.now(tz="Asia/Kolkata").to_pydatetime()
        app.session_state["token"] = "test-token"
        app.session_state["broker_state"] = Mock()
        app.session_state["broker_name"] = "Upstox"
        app.session_state["last_sync_attempt"] = current_time
        app.session_state["last_sync"] = current_time
        app.session_state["profile_fetch_attempted"] = True
        app.session_state["broker_profile_name"] = "Test Investor"
        app.run(timeout=10)

        self.assertFalse(app.exception)
        self.assertEqual(
            [tab.label for tab in app.tabs],
            [
                ":material/home: Home",
                "Equities · 0.0%",
                "Trading · 0.0%",
                "Mutual Funds · 0.0%",
                "F&O · 0.0%",
                ":material/show_chart: Equities",
                ":material/swap_horiz: Trades",
                ":material/query_stats: F&O",
                ":material/savings: Mutual Funds",
                ":material/shopping_cart: Cart (0)",
            ],
        )
        self.assertTrue(
            any("Signed in as **Test Investor**" in item.value for item in app.caption)
        )
        self.assertFalse(
            any("Last broker sync:" in item.value for item in app.caption)
        )
        self.assertIn("workspace_disconnect_home", {item.key for item in app.button})
        self.assertIn("run_analysis_home", {item.key for item in app.button})
        self.assertIn("run_analysis_all", {item.key for item in app.button})
        app.session_state["app_navigation"] = ":material/show_chart: Equities"
        app.run(timeout=10)

        self.assertFalse(app.exception)
        self.assertIn("Holdings · 0.0%", [tab.label for tab in app.tabs])
        self.assertIn("Market Scanner", [tab.label for tab in app.tabs])
        self.assertTrue(
            any("Signed in as **Test Investor**" in item.value for item in app.caption)
        )
        self.assertFalse(
            any("Last broker sync:" in item.value for item in app.caption)
        )
        self.assertIn(
            "workspace_disconnect_equities", {item.key for item in app.button}
        )
        self.assertNotIn("run_analysis_home", {item.key for item in app.button})
        self.assertNotIn("run_analysis_all", {item.key for item in app.button})

    def test_completed_broker_refresh_does_not_leave_success_banner(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        broker = Mock()
        broker.fetch_balance.return_value = 100.0
        broker.fetch_positions.return_value = pd.DataFrame(
            columns=["Ticker", "Qty", "Avg_Price", "LTP"]
        )
        broker.fetch_holdings.return_value = pd.DataFrame(
            columns=["Ticker", "Qty", "Avg_Price", "LTP"]
        )
        broker.fetch_mutual_fund_holdings.return_value = pd.DataFrame(
            columns=["Fund", "Folio", "ISIN", "Units", "Avg_NAV", "Latest_NAV", "NAV_Date"]
        )
        app.session_state["token"] = "test-token"
        app.session_state["broker_state"] = broker
        app.session_state["broker_name"] = "Upstox"
        app.session_state["last_sync_attempt"] = None
        app.session_state["profile_fetch_attempted"] = True
        app.run(timeout=20)

        self.assertFalse(app.exception)
        self.assertIsNotNone(app.session_state["last_sync"])
        self.assertFalse(app.status)
        self.assertTrue(
            any(
                item.value == "Upstox portfolio data refreshed."
                for item in app.get("toast")
            )
        )

    def test_portfolio_analysis_opens_setup_dialog_when_provider_key_is_missing(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        current_time = pd.Timestamp.now(tz="Asia/Kolkata").to_pydatetime()
        app.session_state["token"] = "test-token"
        app.session_state["broker_state"] = Mock()
        app.session_state["broker_name"] = "Upstox"
        app.session_state["last_sync_attempt"] = current_time
        app.session_state["last_sync"] = current_time
        app.session_state["profile_fetch_attempted"] = True
        app.session_state["broker_profile_name"] = "Test Investor"
        app.session_state["llm_model_selection_gemini"] = ""
        app.run(timeout=10)

        analysis_buttons = [
            item
            for item in app.button
            if item.key in {"run_analysis_home", "run_analysis_all"}
        ]
        self.assertEqual(len(analysis_buttons), 2)
        self.assertTrue(all(not item.disabled for item in analysis_buttons))

        next(
            item for item in analysis_buttons if item.key == "run_analysis_all"
        ).click().run(timeout=10)

        self.assertFalse(app.exception)
        self.assertTrue(any(item.type == "dialog" for item in app))
        self.assertTrue(
            any(item.label == "Gemini API key" for item in app.text_input)
        )
        self.assertTrue(any(item.label == "Model ID" for item in app.text_input))
        app.selectbox(key="analysis_setup_provider_all").select(
            "Custom OpenAI-compatible"
        ).run(timeout=10)
        self.assertTrue(
            any(item.label == "Provider name" for item in app.text_input)
        )
        self.assertTrue(
            any(item.label == "API base URL" for item in app.text_input)
        )

    def test_analysis_setup_waits_until_user_settings_dialog_is_closed(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        ).run(timeout=20)
        app.session_state["analysis_setup_dialog_scope"] = "all"
        app.session_state["user_settings_dialog_open"] = True
        app.run(timeout=20)

        self.assertFalse(app.exception)
        self.assertTrue(any(item.type == "dialog" for item in app))
        self.assertTrue(
            any(item.key == "settings_profile_name" for item in app.text_input)
        )
        self.assertFalse(
            any(item.key == "analysis_setup_api_key_all_gemini" for item in app.text_input)
        )

        app.session_state["user_settings_dialog_open"] = False
        app.run(timeout=20)
        self.assertFalse(app.exception)
        self.assertTrue(
            any(item.key == "analysis_setup_api_key_all_gemini" for item in app.text_input)
        )

    def test_analysis_setup_lists_saved_provider_configurations(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        current_time = pd.Timestamp.now(tz="Asia/Kolkata").to_pydatetime()
        app.session_state["token"] = "test-token"
        app.session_state["broker_state"] = Mock()
        app.session_state["broker_name"] = "Upstox"
        app.session_state["last_sync_attempt"] = current_time
        app.session_state["last_sync"] = current_time
        app.session_state["profile_fetch_attempted"] = True
        app.session_state["broker_profile_name"] = "Test Investor"
        app.session_state["llm_provider"] = "Gemini"
        app.session_state["settings_ai_provider_configs"] = [
            {
                "id": "openai-profile",
                "provider": "OpenAI",
                "name": "OpenAI primary",
                "model": "gpt-4o-mini",
                "base_url": "",
            },
            {
                "id": "groq-profile",
                "provider": "Groq",
                "name": "Groq backup",
                "model": "llama-3.3-70b-versatile",
                "base_url": "",
            },
        ]
        app.session_state["settings_ai_provider_config_keys"] = {
            "openai-profile": "",
            "groq-profile": "",
        }
        app.session_state["settings_ai_active_config_id"] = "openai-profile"
        app.run(timeout=10)
        app.session_state["user_llm_api_key_openai"] = ""

        next(
            item for item in app.button if item.key == "run_analysis_home"
        ).click().run(timeout=10)

        provider_select = next(
            item
            for item in app.selectbox
            if item.label == "Configured AI provider"
        )
        self.assertTrue(
            any("OpenAI primary" in option for option in provider_select.options)
        )
        self.assertTrue(
            any("Groq backup" in option for option in provider_select.options)
        )
        self.assertTrue(
            any(item.label == "OpenAI primary API key" for item in app.text_input)
        )

    def test_user_settings_deletes_provider_configuration_in_one_action(self):
        config = {
            "id": "openai-profile",
            "provider": "OpenAI",
            "name": "OpenAI primary",
            "model": "gpt-4o-mini",
            "base_url": "",
        }
        saved = {
            "preferences": {
                "ai_provider_configs": [config],
                "ai_active_config_id": config["id"],
                "ai_provider": "OpenAI",
                "ai_models": {"OpenAI": config["model"]},
            },
            "sensitive": {
                "ai_provider_config_keys": {config["id"]: "test-api-key"},
                "ai_api_keys": {"OpenAI": "test-api-key"},
            },
            "sensitive_consent": True,
        }
        app_source = f"""
import streamlit as st
from ui.user_settings import apply_settings_to_session, _render_ai_provider_configurations
if "delete_test_initialized" not in st.session_state:
    apply_settings_to_session({saved!r})
    st.session_state.delete_test_initialized = True
_render_ai_provider_configurations("delete-test-user")
"""
        from hashlib import sha256

        safe_id = sha256(config["id"].encode("utf-8")).hexdigest()[:12]
        with (
            patch("ui.user_settings.load_user_settings", return_value=saved),
            patch("ui.user_settings.save_user_settings") as save_settings,
        ):
            app = AppTest.from_string(app_source).run(timeout=10)
            self.assertFalse(app.exception)
            app.button(key=f"delete_ai_config_{safe_id}").click().run(timeout=10)

        self.assertFalse(app.exception)
        self.assertEqual(app.session_state.settings_ai_provider_configs, [])
        save_settings.assert_called_once()
        persisted_preferences = save_settings.call_args.args[1]
        self.assertEqual(persisted_preferences["ai_provider_configs"], [])
        self.assertNotIn("OpenAI", persisted_preferences["ai_models"])

    def test_workspace_names_are_not_repeated_as_large_page_titles(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Equities"
        with patch(
            "features.equities.scanner.fetch_indian_equity_master",
            return_value=_equity_master("ALPHA"),
        ):
            app.run(timeout=10)

        self.assertFalse(app.exception)
        self.assertFalse(
            any(
                title.value in {"Equities", "Connect your investment account"}
                for title in app.title
            )
        )

    def test_disconnected_combined_workspace_shows_research_and_screeners(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Equities"
        with patch(
            "features.equities.scanner.fetch_indian_equity_master",
            return_value=_equity_master("RELIANCE"),
        ):
            app.run()

        self.assertFalse(app.exception)
        self.assertFalse(any(title.value == "Equities" for title in app.title))
        self.assertTrue(
            any(
                "No broker is connected" in item.value
                for item in app.info
            )
        )
        self.assertEqual(len(app.segmented_control), 0)
        self.assertTrue(
            any(widget.label == "Choose which stocks to scan" for widget in app.selectbox)
        )

    def test_portfolio_scan_universe_uses_equity_holding_tickers(self):
        from features.equities import scanner as market

        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Trades"
        app.session_state["portfolio"] = pd.DataFrame(
            {
                "Ticker": ["ALPHA", "BETA"],
                "Qty": [1, 2],
                "Avg_Price": [10.0, 20.0],
                "LTP": [11.0, 21.0],
            }
        )
        app.session_state["market_trading_scan_universe"] = "Portfolio holdings"
        with patch(
            "features.equities.scanner.fetch_indian_equity_master",
            return_value=_equity_master("ALPHA", "BETA", "GAMMA"),
        ), patch.object(market, "_scan_stocks", return_value=([], [])):
            app.run(timeout=10)

        self.assertFalse(app.exception)
        self.assertTrue(
            any("2 portfolio holdings available to scan" in item.value for item in app.caption)
        )

    def test_equities_workspace_has_no_equities_or_equity_analysis_headings(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Equities"
        with patch(
            "features.equities.scanner.fetch_indian_equity_master",
            return_value=_equity_master("ALPHA"),
        ):
            app.run(timeout=10)

        self.assertFalse(app.exception)
        self.assertFalse(any(item.value in {"Equities", "Equity analysis"} for item in app.subheader))

    def test_scan_table_embeds_rows_with_color_coded_indicators(self):
        import json

        from features.equities.scanner import _scan_table_payload
        from features.equities.scan_table import render_scan_table

        row = {
            "Ticker": "ABC.BO",
            "As_of": "2026-01-01",
            "Price": 120.0,
            "Recommendation": "STRONG BUY",
            "Signal_score": 7,
            "Reason": "Trend is strong.",
            "Indicator_signals": "RSI: 60 (Positive); MACD: 1 (Negative)",
            "Daily_change_%": 1.5,
            "RSI_14": 60.0,
            "ADX_14": 31.2,
            "SMA_20": 100.0,
            "Passes_filters": True,
        }
        payload, config = _scan_table_payload([row], {"ABC.BO": "ABC \u00b7 Alpha"})
        html = render_scan_table(payload, config)

        self.assertEqual(payload[0]["label"], "ABC.BO")
        self.assertEqual(payload[0]["ind"]["SMA_20"], ["\u20b9100.00", "pos"])
        self.assertEqual(payload[0]["ind"]["ADX_14"][0], "31.2")
        self.assertEqual(
            payload[0]["checks"], [["RSI", "60", "p"], ["MACD", "1", "n"]]
        )
        self.assertEqual(config["signals"], ["STRONG BUY"])
        self.assertIn("Download CSV", html)
        self.assertIn("Records per page", html)
        self.assertIn(json.dumps("ABC.BO", ensure_ascii=False), html)


    def test_trades_workspace_does_not_render_custom_scanner_filters(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Trades"
        app.run(timeout=10)

        self.assertFalse(app.exception)
        self.assertFalse(
            any(widget.label == "Use custom filters" for widget in app.toggle)
        )
        self.assertFalse(
            any(
                widget.label
                in {
                    "Minimum price (₹)",
                    "Maximum price (₹)",
                    "Minimum 20-day average volume",
                    "Minimum RSI",
                    "Maximum RSI",
                    "Minimum volume / 20-day average",
                    "Minimum ATR / price (%)",
                }
                for widget in app.number_input
            )
        )

    def test_mutual_fund_workspace_uses_nav_and_return_filters(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Mutual Funds"
        app.run()

        self.assertFalse(app.exception)
        self.assertTrue(
            any(widget.label == "Find a mutual fund" for widget in app.text_input)
        )
        self.assertTrue(
            any(widget.label == "Minimum return (%)" for widget in app.number_input)
        )
        self.assertTrue(
            any(widget.label == "Maximum NAV age (days)" for widget in app.number_input)
        )

    def test_derivatives_workspace_discloses_missing_quote_feed(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "F&O"
        app.run()

        self.assertFalse(app.exception)
        self.assertTrue(
            any(tab.label.endswith("F&O") for tab in app.tabs)
        )
        self.assertTrue(
            any("option-chain and futures-contract quotes" in item.value for item in app.info)
        )
        self.assertTrue(
            any(widget.label == "Minimum open interest" for widget in app.number_input)
        )


if __name__ == "__main__":
    unittest.main()

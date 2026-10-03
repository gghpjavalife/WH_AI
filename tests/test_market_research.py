import math
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests
from streamlit.testing.v1 import AppTest

from wealth_home_ai.features.market import (
    RECOMMENDED_FILTERS,
    _ai_recommendation_candidates,
    _scan_stocks,
)
from wealth_home_ai.market_research import (
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
from wealth_home_ai.features.workspace_research import _fund_frame


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
            "wealth_home_ai.market_research.requests.get",
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
            "wealth_home_ai.market_research.requests.get",
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
            "wealth_home_ai.market_research.requests.get",
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
                "wealth_home_ai.market_research.fetch_bse_equity_master",
                return_value=bse,
            ),
            patch(
                "wealth_home_ai.market_research.fetch_nse_equity_master",
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
        self.assertEqual(sell["recommendation"], "SELL")
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
            "wealth_home_ai.features.market.fetch_daily_history",
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
        app.session_state["app_navigation"] = "Market research"
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
            "wealth_home_ai.features.market.fetch_indian_equity_master",
            return_value=master,
        ):
            app.run()

        self.assertFalse(app.exception)
        self.assertEqual(
            [tab.label for tab in app.tabs],
            [
                "Home",
                "Equities",
                "Trades",
                "F&O",
                "Mutual Funds",
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
        self.assertTrue(
            any(
                "Governed Growth &amp; Hedged Portfolios" in item.value
                for item in app.get("html")
            )
        )
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
            "Equities",
        )
        self.assertTrue(
            any(
                tab.label == "Equities"
                for tab in app.tabs
            )
        )
        self.assertTrue(
            any("No broker is connected" in item.value for item in app.info)
        )
        watchlist = next(
            widget
            for widget in app.multiselect
            if widget.label == "Indian stock watchlist"
        )
        self.assertTrue(
            any(str(option).startswith("ALPHA") for option in watchlist.options)
        )
        self.assertTrue(
            any(str(option).startswith("BETA") for option in watchlist.options)
        )
        all_stocks = next(
            widget
            for widget in app.checkbox
            if widget.label.startswith("Select all ")
        )
        self.assertFalse(all_stocks.value)
        all_stocks.set_value(True).run()
        self.assertFalse(app.exception)
        self.assertFalse(
            any(widget.label == "Indian stock watchlist" for widget in app.multiselect)
        )
        self.assertTrue(
            any(
                "All " in item.value and " stocks selected" in item.value
                for item in app.caption
            )
        )
        self.assertFalse(
            any(tab.label in {"Market research", "Market scanner"} for tab in app.tabs)
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

    def test_workspace_names_are_not_repeated_as_large_page_titles(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Equities"
        app.run()

        self.assertFalse(app.exception)
        self.assertFalse(
            any(
                title.value in {"Equities", "Connect your investment account"}
                for title in app.title
            )
        )

    def test_market_scanner_uses_recommended_defaults_and_warns_on_changes(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Market scanner"
        with patch(
            "wealth_home_ai.features.market.fetch_indian_equity_master",
            return_value=_equity_master(
                "RELIANCE",
                "HDFCBANK",
                "ICICIBANK",
                "SBIN",
                "TCS",
                "INFY",
                "BHARTIARTL",
                "ITC",
            ),
        ):
            app.run()
            app.toggle(key="market_equities_use_custom").set_value(True).run()
        self.assertFalse(app.exception)
        self.assertTrue(
            any(widget.label == "Minimum price (₹)" for widget in app.number_input)
        )
        self.assertEqual(
            app.number_input(key="market_equities_minimum_rsi").value,
            35.0,
        )
        with patch(
            "wealth_home_ai.features.market.fetch_indian_equity_master",
            return_value=_equity_master(
                "RELIANCE",
                "HDFCBANK",
                "ICICIBANK",
                "SBIN",
                "TCS",
                "INFY",
                "BHARTIARTL",
                "ITC",
            ),
        ):
            app.number_input(key="market_equities_minimum_rsi").set_value(40).run()
        self.assertFalse(app.exception)
        self.assertTrue(
            any(
                "differ from the suggested defaults" in item.value
                for item in app.warning
            )
        )

    def test_disconnected_combined_workspace_shows_research_and_screeners(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Equities"
        with patch(
            "wealth_home_ai.features.market.fetch_indian_equity_master",
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
            any(widget.label == "Indian stock watchlist" for widget in app.multiselect)
        )

    def test_scanner_select_all_lists_buy_signals_by_strength_with_pagination(self):
        from wealth_home_ai.features import market

        def row(ticker, signal, score, passes):
            return {
                "Ticker": ticker,
                "As_of": "2026-01-01",
                "Price": 100.0,
                "Recommendation": signal,
                "Signal_score": score,
                "Reason": "r",
                "Indicator_signals": "",
                "Daily_change_%": 1.0,
                "RSI_14": 55.0,
                "Passes_filters": passes,
            }

        rows = [row("HOLDER", "HOLD", 0, True)] + [
            row(f"B{index:02d}", "BUY", index, False) for index in range(30)
        ] + [row("TOP", "STRONG BUY", 11, False)]
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Equities"
        with patch(
            "wealth_home_ai.features.market.fetch_indian_equity_master",
            return_value=_equity_master("RELIANCE"),
        ), patch.object(market, "_scan_stocks", return_value=(rows, [])):
            app.run()
            app.checkbox(key="market_equities_select_all").check().run()
            app.button(key="market_equities_scan_button").click().run()

        self.assertFalse(app.exception)
        metric = next(
            item for item in app.metric if item.label == "Buy / Strong Buy signals"
        )
        self.assertEqual(metric.value, "31")

    def test_scanner_specific_selection_lists_every_scanned_stock(self):
        from wealth_home_ai.features import market

        rows = [
            {
                "Ticker": "RELIANCE.NS",
                "As_of": "2026-01-01",
                "Price": 100.0,
                "Recommendation": "SELL",
                "Signal_score": -4,
                "Reason": "r",
                "Indicator_signals": "",
                "Daily_change_%": -1.0,
                "RSI_14": 30.0,
                "Passes_filters": False,
            }
        ]
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Equities"
        with patch(
            "wealth_home_ai.features.market.fetch_indian_equity_master",
            return_value=_equity_master("RELIANCE"),
        ), patch.object(market, "_scan_stocks", return_value=(rows, [])):
            app.run()
            app.button(key="market_equities_scan_button").click().run()

        self.assertFalse(app.exception)
        metric = next(item for item in app.metric if item.label == "Stocks scanned")
        self.assertEqual(metric.value, "1")
    def test_scan_table_embeds_rows_with_color_coded_indicators(self):
        import json

        from wealth_home_ai.features.market import _scan_table_payload
        from wealth_home_ai.features.scan_table import render_scan_table

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

        self.assertEqual(payload[0]["label"], "ABC \u00b7 Alpha")
        self.assertEqual(payload[0]["ind"]["SMA_20"], ["\u20b9100.00", "pos"])
        self.assertEqual(payload[0]["ind"]["ADX_14"][0], "31.2")
        self.assertEqual(
            payload[0]["checks"], [["RSI", "60", "p"], ["MACD", "1", "n"]]
        )
        self.assertEqual(config["signals"], ["STRONG BUY"])
        self.assertIn("Download CSV", html)
        self.assertIn("Records per page", html)
        self.assertIn(json.dumps("ABC \u00b7 Alpha", ensure_ascii=False), html)


    def test_trades_workspace_uses_trading_specific_scanner_filters(self):
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        )
        app.session_state["app_navigation"] = "Trades"
        app.run()
        app.toggle(key="market_trading_use_custom").set_value(True).run()

        self.assertFalse(app.exception)
        self.assertEqual(
            app.number_input(key="market_trading_minimum_relative_volume").value,
            1.0,
        )
        self.assertEqual(
            app.number_input(key="market_trading_minimum_atr_pct").value,
            1.0,
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
            any(tab.label == "F&O" for tab in app.tabs)
        )
        self.assertTrue(
            any("option-chain and futures-contract quotes" in item.value for item in app.info)
        )
        self.assertTrue(
            any(widget.label == "Minimum open interest" for widget in app.number_input)
        )


if __name__ == "__main__":
    unittest.main()

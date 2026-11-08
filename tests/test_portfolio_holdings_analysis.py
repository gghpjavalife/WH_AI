import json
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from features.analysis.fundamentals import fetch_company_fundamentals
from features.analysis.holdings import (
    _build_holding_scan_row,
    _holding_table_columns,
    build_portfolio_holding_rows,
    combine_stock_signal,
)
from features.equities.scan_table import render_scan_table, show_scan_table
from services.ai import ask_llm_agent
from streamlit.testing.v1 import AppTest


class PortfolioHoldingsAnalysisTests(unittest.TestCase):
    def test_build_portfolio_rows_includes_each_holding_type(self):
        equities = pd.DataFrame(
            [
                {
                    "Ticker": "ABC",
                    "Company": "ABC Ltd",
                    "Qty": 2,
                    "Avg_Price": 100,
                    "LTP": 125,
                }
            ]
        )
        positions = pd.DataFrame(
            [
                {
                    "Ticker": "XYZ",
                    "Qty": 3,
                    "Avg_Price": 20,
                    "LTP": 18,
                }
            ]
        )
        funds = pd.DataFrame(
            [
                {
                    "Fund": "Index Fund",
                    "Units": 10,
                    "Avg_NAV": 50,
                    "Latest_NAV": 60,
                    "ISIN": "INF000000001",
                }
            ]
        )

        rows = build_portfolio_holding_rows(equities, positions, funds)

        self.assertEqual([row["type"] for row in rows], ["Equity", "Trading", "Mutual fund"])
        self.assertEqual(rows[0]["current_value"], 250)
        self.assertEqual(rows[0]["return_pct"], 25)
        self.assertEqual(rows[1]["return_pct"], -10)
        self.assertEqual(rows[2]["name"], "Index Fund")
        self.assertEqual(rows[2]["isin"], "INF000000001")
        self.assertEqual(rows[2]["return_pct"], 20)

    def test_combined_signal_uses_technical_fundamental_and_news_inputs(self):
        technical = {
            "evaluated_checks": 12,
            "bullish_checks": 10,
            "bearish_checks": 2,
        }
        fundamentals = {
            "values": {
                "trailingPE": 18,
                "forwardPE": 20,
                "priceToBook": 2,
                "pegRatio": 1.2,
                "returnOnEquity": 0.2,
                "profitMargins": 0.15,
                "revenueGrowth": 0.1,
                "earningsGrowth": 0.1,
                "debtToEquity": 80,
                "freeCashflow": 500,
            }
        }
        news = {
            "headlines": [
                {"sentiment": 1},
                {"sentiment": 1},
                {"sentiment": 0},
            ]
        }

        result = combine_stock_signal(technical, fundamentals, news)

        self.assertEqual(result["signal"], "Strong Buy")
        self.assertGreater(result["score"], 0.6)
        self.assertIn("Technical", result["reason"])
        self.assertIn("Fundamentals", result["reason"])
        self.assertIn("News headlines", result["reason"])

    def test_combined_signal_uses_available_components_and_not_rates_without_data(self):
        technical = {
            "evaluated_checks": 12,
            "bullish_checks": 1,
            "bearish_checks": 11,
        }

        result = combine_stock_signal(technical, None, None)

        self.assertEqual(result["signal"], "Strong Sell")
        self.assertIsNone(combine_stock_signal(None, None, None)["score"])

    def test_holdings_use_the_shared_scanner_table_with_expandable_research(self):
        holding = pd.Series(
            {
                "Ticker": "ABC",
                "Company": "ABC Ltd",
                "Qty": 2,
                "Avg_Price": 100.0,
                "LTP": 110.0,
                "Investment": 200.0,
                "Market_Value": 220.0,
                "P&L": 20.0,
                "Returns_%": 10.0,
                "Allocation_%": 100.0,
            }
        )
        research = {
            "signal": "Buy",
            "score": 0.3,
            "reason": "Buy based on combined evidence.",
            "errors": {},
            "technical": {
                "bullish_checks": 2,
                "bearish_checks": 1,
                "evaluated_checks": 3,
                "raw_indicators": {
                    "price": 110.0,
                    "daily_change_pct": 2.5,
                    "return_1m_pct": 8.0,
                    "rsi14": 61.0,
                    "relative_volume": 1.4,
                    "sma20": 105.0,
                    "sma50": 102.0,
                    "sma200": 95.0,
                    "ema12": 107.0,
                    "ema26": 104.0,
                    "macd": 2.0,
                    "macd_signal": 1.0,
                    "macd_histogram": 1.0,
                    "atr14": 2.0,
                    "atr_pct": 1.8,
                    "adx14": 30.0,
                    "plus_di14": 25.0,
                    "minus_di14": 10.0,
                    "cci20": 100.0,
                    "mfi14": 65.0,
                    "obv_change20": 1000.0,
                    "bollinger_lower": 100.0,
                    "bollinger_middle": 105.0,
                    "bollinger_upper": 115.0,
                    "bollinger_width_pct": 14.0,
                    "stochastic_k": 60.0,
                    "stochastic_d": 55.0,
                    "volatility20_annualized_pct": 20.0,
                    "return_3m_pct": 12.0,
                    "average_volume_20": 50000.0,
                    "high_52w": 120.0,
                    "low_52w": 70.0,
                    "price_52w_position_pct": 80.0,
                },
                "indicators": [
                    {
                        "Indicator": "Price vs 20-day average",
                        "Value": "₹110.00 vs ₹105.00",
                        "Signal": "Positive",
                    },
                    {
                        "Indicator": "14-day RSI",
                        "Value": "61.0",
                        "Signal": "Positive",
                    },
                ],
            },
            "fundamentals": {
                "source": "Public quote summary",
                "available_count": 1,
                "expected_count": 12,
                "metrics": [
                    {
                        "Fundamental": "Trailing P/E",
                        "Value": "20.00",
                        "raw": 20.0,
                    }
                ],
            },
            "fundamental_checks": [
                {
                    "Fundamental": "Trailing P/E",
                    "Assessment": "Positive",
                }
            ],
            "news": {
                "source": "Public headlines",
                "headlines": [
                    {
                        "title": "ABC reports strong growth",
                        "url": "https://finance.yahoo.com/quote/ABC",
                        "publisher": "Yahoo Finance",
                        "published": "Today",
                        "sentiment": 1,
                    }
                ],
            },
        }

        payload, config = _build_holding_scan_row(holding, research)
        html = render_scan_table([payload], config)

        self.assertEqual(payload["signal"], "BUY")
        self.assertEqual(payload["pos"], 2)
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["price"], 110.0)
        self.assertEqual(payload["quantity"], 2.0)
        self.assertEqual(payload["average"], 100.0)
        self.assertEqual(payload["investment"], 200.0)
        self.assertEqual(payload["holdingValue"], 220.0)
        self.assertEqual(payload["pnl"], 20.0)
        self.assertEqual(payload["returnPct"], 10.0)
        self.assertEqual(payload["allocationPct"], 100.0)
        self.assertEqual(payload["ind"]["fundamental_0"][0], "20.00")
        self.assertIn(
            ["price", "LTP", 1, "currency"], _holding_table_columns("equity")
        )
        self.assertIn(["signal", "Signal", 0], _holding_table_columns("equity"))
        self.assertIn('"Ticker"', html)
        self.assertIn('"Signal"', html)
        self.assertIn(
            ["holding_value", "Market value"], payload["extraGroups"][0][1]
        )
        self.assertIn("Fundamentals", html)
        self.assertIn("Recent company headlines", html)
        self.assertIn("ABC reports strong growth", html)

    def test_holdings_table_columns_are_configurable_by_workspace(self):
        equity_columns = _holding_table_columns("equity")
        home_columns = _holding_table_columns("home_equity")
        trading_columns = _holding_table_columns("trading")
        scanner_html = render_scan_table(
            [],
            {
                "signals": ["BUY"],
                "groups": [],
                "info": {},
                "checkInfo": {},
                "rawCols": [],
            },
        )

        self.assertEqual(equity_columns, home_columns)
        self.assertEqual(
            [column[0] for column in equity_columns],
            [
                "ticker",
                "signal",
                "pnl",
                "returnPct",
                "quantity",
                "average",
                "price",
                "investment",
                "holdingValue",
                "allocationPct",
            ],
        )
        self.assertNotEqual(trading_columns, equity_columns)
        self.assertEqual([column[0] for column in trading_columns[:4]],
                         ["stock", "signal", "pnl", "returnPct"])
        self.assertIn("Gross exposure", [column[1] for column in trading_columns])
        self.assertIn("Open P&L", [column[1] for column in trading_columns])
        self.assertNotIn('"columns":', scanner_html)
        self.assertIn("Day change", scanner_html)

    @patch("features.equities.scan_table._COMPONENT")
    def test_scan_table_serializes_non_finite_numbers_as_missing(self, component):
        payload = [
            {
                "ticker": "ABC",
                "chg": float("nan"),
                "rsi": float("inf"),
                "raw": [float("-inf")],
            }
        ]
        config = {"tableKey": "test-table", "metadata": {"value": float("nan")}}

        show_scan_table(payload, config)

        component_data = component.call_args.kwargs["data"]
        json.dumps(component_data, allow_nan=False)
        self.assertIsNone(component_data["payload"][0]["chg"])
        self.assertIsNone(component_data["payload"][0]["rsi"])
        self.assertIsNone(component_data["payload"][0]["raw"][0])
        self.assertIsNone(component_data["config"]["metadata"]["value"])

        html = render_scan_table(payload, config)
        self.assertIn('"chg": null', html)
        self.assertNotIn("NaN", html)
        self.assertNotIn("Infinity", html)

    def test_empty_news_and_missing_fundamentals_do_not_create_empty_panels(self):
        row, _ = _build_holding_scan_row(
            pd.Series({"Ticker": "ABC.BO", "LTP": 100}),
            {"news": {"source": "Yahoo", "headlines": []},
             "errors": {"fundamentals": "Yahoo fundamentals unavailable for BSE."}},
        )
        titles = [section["title"] for section in row["extraSections"]]
        self.assertNotIn("Recent company headlines", titles)
        self.assertNotIn("Fundamentals", titles)
        availability = next(section for section in row["extraSections"]
                            if section["title"] == "Data availability")
        self.assertIn("Yahoo fundamentals unavailable for BSE.", availability["note"])

    def test_holdings_render_the_shared_interactive_table(self):
        app = AppTest.from_string(
            """
import pandas as pd
import streamlit as st
from features.analysis.holdings import render_stock_holdings_table

st.session_state["stock_research_cache"] = {
    "selected_stock_research_ABC": {
        "signal": "Buy",
        "score": 0.3,
        "reason": "combined evidence",
        "errors": {},
        "technical": {
            "bullish_checks": 1,
            "bearish_checks": 0,
            "evaluated_checks": 1,
            "raw_indicators": {
                "price": 110.0,
                "daily_change_pct": 1.0,
                "return_1m_pct": 2.0,
                "rsi14": 55.0,
                "relative_volume": 1.1,
            },
            "indicators": [
                {"Indicator": "RSI", "Value": "55", "Signal": "Positive"}
            ],
        },
        "fundamentals": {
            "source": "Yahoo",
            "available_count": 1,
            "expected_count": 12,
            "metrics": [
                {"Fundamental": "P/E", "Value": "20.0", "raw": 20.0}
            ],
        },
        "fundamental_checks": [],
        "news": {"source": "Yahoo", "headlines": []},
    }
}
render_stock_holdings_table(
    pd.DataFrame([{
        "Ticker": "ABC",
        "Company": "ABC Ltd",
        "Qty": 2,
        "Avg_Price": 100.0,
        "LTP": 110.0,
        "Market_Value": 220.0,
        "P&L": 20.0,
        "Returns_%": 10.0,
        "Allocation_%": 100.0,
    }]),
    key="test_holdings",
    theme="dark",
)
"""
        )

        with patch("features.equities.scan_table.show_scan_table") as table:
            app.run()
        self.assertFalse(app.exception)
        self.assertEqual(table.call_args.args[1]["tableKey"], "test_holdings")
        self.assertTrue(callable(table.call_args.kwargs["on_refresh"]))

    @patch("features.analysis.fundamentals.requests.get")
    def test_company_fundamentals_are_normalized_and_coverage_is_reported(self, get):
        response = Mock()
        response.json.return_value = {
            "quoteSummary": {
                "result": [
                    {
                        "price": {
                            "marketCap": {"raw": 1234567},
                            "currency": "INR",
                        },
                        "summaryDetail": {
                            "trailingPE": {"raw": 20.0},
                            "dividendYield": {"raw": 0.02},
                        },
                        "defaultKeyStatistics": {},
                        "financialData": {
                            "returnOnEquity": {"raw": 0.15},
                        },
                    }
                ]
            }
        }
        get.return_value = response

        fundamentals = fetch_company_fundamentals("ABC")

        self.assertEqual(fundamentals["symbol"], "ABC")
        self.assertEqual(fundamentals["available_count"], 4)
        self.assertEqual(fundamentals["expected_count"], 12)
        values = {
            item["Fundamental"]: item["Value"]
            for item in fundamentals["metrics"]
        }
        self.assertEqual(values["Market capitalization"], "INR 1,234,567.00")
        self.assertEqual(values["Dividend yield"], "2.00%")
        self.assertEqual(
            get.call_args.kwargs["params"]["modules"],
            "price,summaryDetail,defaultKeyStatistics,financialData",
        )

    @patch("services.ai.requests.post")
    def test_portfolio_only_ai_mode_never_requests_deployment_candidates(self, post):
        response = Mock()
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"analysis":"Portfolio concentration is high.",'
                            '"assessment":{"sentiment":"cautious",'
                            '"summary":"The portfolio is concentrated.",'
                            '"strengths":[],"warnings":["Concentration risk."],'
                            '"suggestions":[]}}'
                        )
                    }
                }
            ]
        }
        post.return_value = response

        result = ask_llm_agent(
            portfolio_summary='{"equity": []}',
            api_key="test-key",
            model="test-model",
            provider="Custom OpenAI-compatible",
            base_url="https://llm.example/v1",
            portfolio_only=True,
        )

        self.assertEqual(result["cash_deployment_list"], [])
        prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("Do not make", prompt)
        self.assertIn("Do not refer to JEV rules", prompt)
        self.assertNotIn("cash_deployment_list", prompt)


if __name__ == "__main__":
    unittest.main()

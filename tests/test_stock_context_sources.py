import unittest

import pandas as pd

from services.stock_context_sources import json_value, map_yahoo_modules, option_ratios, returns_from_history


class SourceNormalizationTests(unittest.TestCase):
    def test_yahoo_mapping_uses_fractional_ownership_and_consistent_units(self):
        mapped = map_yahoo_modules({
            "price": {"longName": "Example", "marketCap": 1000},
            "summaryProfile": {"sector": "Technology"},
            "defaultKeyStatistics": {"floatShares": 50, "sharesOutstanding": 100},
            "financialData": {"currentPrice": 10, "debtToEquity": 125},
            "majorHoldersBreakdown": {"institutionsPercentHeld": .3, "insidersPercentHeld": .2},
            "summaryDetail": {"dividendYield": .06},
        }, "TEST")
        self.assertEqual(mapped["fundamentals"]["float_market_cap"], 500)
        self.assertEqual(mapped["fundamentals"]["debt_to_equity"], 1.25)
        self.assertAlmostEqual(mapped["shareholders"]["pct_held_by_retail"], .5)
        self.assertIsNone(mapped["shareholders"].get("promoter_pledged_shares_pct"))
        self.assertEqual(mapped["fundamentals"]["dividend_yield"], .06)

    def test_preserves_entire_frame_including_index(self):
        frame = pd.DataFrame(
            {"volume": [1.0, float("nan")]},
            index=pd.MultiIndex.from_tuples([("A", "calls"), ("A", "puts")], names=["symbol", "optionType"]),
        )
        self.assertEqual(json_value(frame), {
            "index_names": ["symbol", "optionType"],
            "index": [["A", "calls"], ["A", "puts"]],
            "columns": ["volume"], "rows": [[1.0], [None]],
        })
        self.assertIsNone(json_value(pd.NaT))

    def test_option_ratios_do_not_drop_missing_contracts(self):
        frame = pd.DataFrame(
            {"volume": [100, 50], "openInterest": [200, 300]},
            index=pd.Index(["calls", "puts"], name="optionType"),
        )
        self.assertEqual(option_ratios(frame), {
            "put_call_volume_ratio": 0.5, "put_call_oi_ratio": 1.5,
        })
        frame.loc["puts", "volume"] = float("nan")
        self.assertIsNone(option_ratios(frame)["put_call_volume_ratio"])
        frame.loc["puts", "volume"] = 0
        self.assertIsNone(option_ratios(frame)["put_call_volume_ratio"])

    def test_macro_return_and_beta(self):
        index = pd.date_range("2024-01-01", periods=220)
        benchmark = pd.DataFrame({"close": [100 + i + i % 3 for i in range(220)]}, index=index)
        macro = returns_from_history(benchmark, benchmark, benchmark)
        self.assertAlmostEqual(macro["beta_to_sp500"], 1)
        self.assertEqual(macro["market_regime_bias"], "Risk-On")
        self.assertAlmostEqual(macro["sector_return_5d"], (benchmark["close"].iloc[-1] / benchmark["close"].iloc[-6] - 1) * 100)


if __name__ == "__main__":
    unittest.main()

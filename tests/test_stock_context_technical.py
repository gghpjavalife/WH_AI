import unittest
from unittest.mock import patch

import pandas as pd

from services.stock_context_models import TechnicalIndicators
from services.stock_context_technical import (
    calculate_technical_indicators, exponential_moving_average,
    moving_average_convergence_divergence, relative_strength_index,
    simple_moving_average,
)


class TechnicalIndicatorsTests(unittest.TestCase):
    def test_fill_missing_cells_without_modifying_input(self):
        frame = pd.DataFrame({"high": [102.0] * 30, "low": [98.0] * 30,
                              "close": [100.0] * 30, "volume": [10.0] * 30})
        frame.loc[0, "volume"] = float("nan")
        frame.loc[12, "close"] = float("nan")
        warnings = []
        with self.assertLogs("wealth_home_ai", level="WARNING"):
            result = calculate_technical_indicators(frame, warnings=warnings)
        self.assertIsNotNone(result["sma_20"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("2 missing", warnings[0])
        self.assertTrue(pd.isna(frame.loc[0, "volume"]))
        frame["volume"] = float("nan")
        with self.assertRaises(ValueError):
            calculate_technical_indicators(frame)

    def test_full_history_and_short_history(self):
        close = pd.Series([100 + i * 0.1 + (i % 5) * 0.2 for i in range(260)])
        history = pd.DataFrame({
            "high": close + 2, "low": close - 2, "close": close,
            "volume": [1000 + i for i in range(260)],
        })
        result = calculate_technical_indicators(history)
        self.assertAlmostEqual(result["sma_200"], close.tail(200).mean())
        self.assertEqual(result["current_volume"], 1259)
        self.assertIsNotNone(result["rsi_14"])
        self.assertGreaterEqual(result["atr_percentile_14"], 0)
        self.assertLessEqual(result["atr_percentile_14"], 100)
        short = calculate_technical_indicators(history.iloc[:30])
        self.assertIsNone(short["sma_200"])
        self.assertIsNone(short["avg_volume_3m"])

    def test_rejects_bad_candles(self):
        with self.assertRaises(ValueError):
            calculate_technical_indicators(pd.DataFrame({
                "high": [1], "low": [2], "close": [3], "volume": [-1],
            }))
        with self.assertRaises(ValueError):
            calculate_technical_indicators(pd.DataFrame({
                "high": [float("inf")], "low": [1], "close": [2], "volume": [1],
            }))

    def test_vector_math_and_model_mapping_without_disk_io(self):
        close = pd.Series([100 + i * .1 + (i % 5 - 2) * .5 for i in range(260)])
        history = pd.DataFrame({"high": close + 2, "low": close - 2,
                                "close": close, "volume": range(1000, 1260)})
        with patch.object(pd.DataFrame, "to_csv", side_effect=AssertionError("disk write")), \
                patch.object(pd.DataFrame, "to_sql", side_effect=AssertionError("disk write")):
            result = calculate_technical_indicators(history)
        indicators = TechnicalIndicators.model_validate(result)
        self.assertEqual(set(result), set(TechnicalIndicators.model_fields))
        self.assertTrue(all(value is not None for value in indicators.model_dump().values()))
        self.assertAlmostEqual(result["sma_20"], close.iloc[-20:].mean())
        self.assertAlmostEqual(result["ema_50"], close.ewm(span=50, adjust=False).mean().iloc[-1])
        self.assertAlmostEqual(result["bb_upper"], result["sma_20"] + 2 * close.iloc[-20:].std(ddof=0))
        self.assertAlmostEqual(result["macd_hist"], result["macd_line"] - result["macd_signal"])

    def test_known_sma_ema_rsi_macd_values_and_warmup(self):
        close = pd.Series([float(i) for i in range(1, 41)])
        self.assertEqual(simple_moving_average(close, 3).iloc[2], 2)
        self.assertTrue(pd.isna(simple_moving_average(close, 3).iloc[1]))
        self.assertAlmostEqual(exponential_moving_average(close, 3).iloc[2], 2.25)
        self.assertEqual(relative_strength_index(close).iloc[-1], 100)
        self.assertEqual(relative_strength_index(pd.Series([5.0] * 40)).iloc[-1], 50)
        self.assertEqual(relative_strength_index(-close).iloc[-1], 0)
        line, signal, hist = moving_average_convergence_divergence(close)
        self.assertTrue(pd.isna(line.iloc[24]))
        self.assertTrue(pd.isna(signal.iloc[32]))
        self.assertAlmostEqual(hist.iloc[-1], line.iloc[-1] - signal.iloc[-1])

    def test_existing_ai_atr_uses_shared_vector_calculation(self):
        from services.ai import calculate_atr
        close = pd.Series([100.0] * 30)
        self.assertAlmostEqual(calculate_atr(close + 2, close - 2, close), 4.0)
        with self.assertRaises(ValueError):
            calculate_atr(close.iloc[:5] + 2, close.iloc[:5] - 2, close.iloc[:5])


if __name__ == "__main__":
    unittest.main()

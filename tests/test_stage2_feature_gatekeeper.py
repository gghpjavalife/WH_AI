import unittest

import numpy as np
import pandas as pd

from services.stage2_feature_gatekeeper import (
    GatekeeperConfig,
    compute_22_technical_features,
    evaluate_jev_gatekeeper,
    run_stage2_batch,
    select_liquid_universe,
)


def _history(rows=260):
    close = pd.Series(
        [100 + index * 0.3 + np.sin(index / 4) for index in range(rows)],
        dtype="float64",
    )
    return pd.DataFrame(
        {
            "Open": (close - 0.2).to_numpy(),
            "High": (close + 1).to_numpy(),
            "Low": (close - 1).to_numpy(),
            "Close": close.to_numpy(),
            "Volume": [100_000 + index * 10 for index in range(rows)],
        },
        index=pd.date_range("2025-01-01", periods=rows, freq="B"),
    )


class Stage2FeatureGatekeeperTests(unittest.TestCase):
    def test_gatekeeper_defaults_preserve_existing_enabled_thresholds(self):
        config = GatekeeperConfig()

        self.assertEqual(config.min_last_price, 20.0)
        self.assertEqual(config.max_spread_pct, 0.75)
        self.assertEqual(config.min_daily_volume, 50_000)
        self.assertEqual(config.min_adx_trend, 18.0)
        self.assertEqual(config.max_choppiness_index, 61.8)
        self.assertEqual(config.min_piotroski_score, 4)
        self.assertEqual(config.min_altman_z_score, 1.81)
        self.assertEqual(config.max_promoter_pledge_pct, 25.0)
        self.assertTrue(
            all(
                getattr(config, field)
                for field in (
                    "enable_min_last_price",
                    "enable_max_spread_pct",
                    "enable_min_daily_volume",
                    "enable_min_adx_trend",
                    "enable_max_choppiness_index",
                    "enable_min_piotroski_score",
                    "enable_min_altman_z_score",
                    "enable_max_promoter_pledge_pct",
                )
            )
        )

    def test_select_universe_prefers_turnover_and_deduplicates_issuer(self):
        universe = [
            {
                "symbol": "ABC.BO",
                "instrument_key": "BSE_EQ|ABC",
                "segment": "BSE_EQ",
                "isin": "INABC",
            },
            {
                "symbol": "ABC",
                "instrument_key": "NSE_EQ|ABC",
                "segment": "NSE_EQ",
                "isin": "INABC",
            },
            {
                "symbol": "XYZ",
                "instrument_key": "NSE_EQ|XYZ",
                "segment": "NSE_EQ",
                "isin": "INXYZ",
            },
            {
                "symbol": "NOPE",
                "instrument_key": "NSE_FO|NOPE",
                "segment": "NSE_FO",
                "isin": "INNOPE",
            },
        ]

        selected = select_liquid_universe(
            universe,
            {
                "BSE_EQ|ABC": {"avg_20d_turnover": 20_000_000},
                "NSE_EQ|ABC": {"avg_20d_turnover": 10_000_000},
                "NSE_EQ|XYZ": {"avg_20d_turnover": 4_000_000},
            },
        )

        self.assertEqual(selected, ["BSE_EQ|ABC"])

    def test_unranked_candidates_are_deterministic_and_nse_first(self):
        universe = [
            {"instrument_key": "BSE_EQ|B", "segment": "BSE_EQ"},
            {"instrument_key": "NSE_EQ|Z", "segment": "NSE_EQ"},
            {"instrument_key": "NSE_EQ|A", "segment": "NSE_EQ"},
        ]

        self.assertEqual(
            select_liquid_universe(universe),
            ["NSE_EQ|A", "NSE_EQ|Z", "BSE_EQ|B"],
        )

    def test_compute_features_returns_22_derived_indicators_and_handles_live_data(self):
        history = _history(260)
        quote = {
            "last_price": 200.0,
            "open": 199.0,
            "high": 202.0,
            "low": 198.0,
            "volume": 500_000,
            "vwap": 199.5,
            "timestamp": "2025-12-30T12:00:00+05:30",
        }

        features = compute_22_technical_features(history, quote)

        self.assertIsNotNone(features)
        self.assertEqual(features["last_price"], 200.0)
        self.assertEqual(features["vwap"], 199.5)
        self.assertTrue(features["above_vwap"])
        self.assertEqual(
            len(
                set(features)
                & {
                    "sma_20", "sma_50", "sma_200", "ema_21", "macd_line",
                    "macd_signal", "macd_hist", "rsi_14", "bb_upper", "bb_lower",
                    "bb_width_pct", "atr_14", "atr_percentile", "squeeze_alert",
                    "adx_14", "choppiness_14", "mfi_14", "vpt", "rvol_20",
                    "stoch_k", "roc_10", "dist_52w_high_pct",
                }
            ),
            22,
        )
        self.assertTrue(all(
            not isinstance(value, float) or np.isfinite(value)
            for value in features.values()
        ))

    def test_same_day_live_quote_updates_last_daily_candle_instead_of_appending(self):
        history = _history(60)
        history.index = pd.date_range("2025-01-01", periods=60, freq="B")
        quote = {
            "last_price": 123.0,
            "open": 120,
            "high": 125,
            "low": 119,
            "volume": 333,
            "timestamp": history.index[-1].strftime("%Y-%m-%dT12:00:00+05:30"),
        }

        features = compute_22_technical_features(history, quote)
        replaced_candle_check = compute_22_technical_features(
            history, quote, min_history_bars=61
        )

        self.assertEqual(features["last_price"], 123.0)
        self.assertLess(features["rvol_20"], 0.01)
        self.assertIsNone(replaced_candle_check)

    def test_insufficient_or_malformed_history_is_safe(self):
        self.assertIsNone(compute_22_technical_features(_history(49)))
        with self.assertRaisesRegex(ValueError, "missing required columns"):
            compute_22_technical_features(pd.DataFrame({"close": [1]}))

    def test_gatekeeper_rejects_bad_microstructure_and_choppy_market(self):
        quote = {
            "last_price": 10,
            "volume": 100,
            "bid_ask_spread_pct": 1.0,
            "is_circuit_locked": True,
        }
        features = {"adx_14": 12, "choppiness_14": 70}

        decision = evaluate_jev_gatekeeper("NSE_EQ|A", "A", quote, features)

        self.assertFalse(decision.passed)
        self.assertIn("CIRCUIT_LOCKED", decision.reject_reasons)
        self.assertIn("LOW_VOLUME<50000", decision.reject_reasons)
        self.assertIn("CHOPPY_REGIME>61.8", decision.reject_reasons)

    def test_gatekeeper_can_disable_individual_rejection_rules(self):
        decision = evaluate_jev_gatekeeper(
            "NSE_EQ|A",
            "A",
            {
                "last_price": 10,
                "volume": 100,
                "bid_ask_spread_pct": 1.0,
                "is_circuit_locked": False,
            },
            {"adx_14": 12, "choppiness_14": 70},
            config=GatekeeperConfig(
                enable_min_last_price=False,
                enable_max_spread_pct=False,
                enable_min_daily_volume=False,
                enable_min_adx_trend=False,
                enable_max_choppiness_index=False,
            ),
        )

        self.assertTrue(decision.passed)
        self.assertEqual(decision.reject_reasons, [])

    def test_pending_fundamentals_do_not_reject_otherwise_eligible_stock(self):
        quote = {
            "last_price": 100,
            "volume": 100_000,
            "bid_ask_spread_pct": 0.2,
            "is_circuit_locked": False,
        }
        features = {
            "last_price": 100,
            "sma_50": 90,
            "sma_200": 80,
            "macd_hist": 1,
            "rsi_14": 60,
            "above_vwap": True,
            "rvol_20": 1.3,
            "adx_14": 25,
            "choppiness_14": 40,
        }

        decision = evaluate_jev_gatekeeper(
            "NSE_EQ|A",
            "A",
            quote,
            features,
            {"piotroski_f_score": None, "altman_z_score": None},
        )

        self.assertTrue(decision.passed)
        self.assertEqual(decision.signal_tier, "STRONG_BUY")
        self.assertEqual(decision.score, 100.0)

    def test_populated_fundamental_risk_fields_are_enforced(self):
        quote = {
            "last_price": 100,
            "volume": 100_000,
            "bid_ask_spread_pct": 0.2,
            "is_circuit_locked": False,
        }
        features = {"adx_14": 25, "choppiness_14": 40}

        decision = evaluate_jev_gatekeeper(
            "NSE_EQ|A",
            "A",
            quote,
            features,
            {
                "piotroski_f_score": 2,
                "altman_z_score": 1.2,
                "promoter_pledge_pct": 30,
            },
        )

        self.assertEqual(
            decision.reject_reasons,
            ["WEAK_PIOTROSKI<4", "DISTRESS_ALTMAN_Z<1.81", "HIGH_PLEDGE>25.0%"],
        )

    def test_batch_runner_uses_only_provided_snapshots(self):
        universe = [
            {
                "symbol": "ABC",
                "instrument_key": "NSE_EQ|ABC",
                "segment": "NSE_EQ",
                "isin": "INABC",
            }
        ]
        quote = {
            "last_price": 150,
            "volume": 200_000,
            "bid_ask_spread_pct": 0.1,
            "is_circuit_locked": False,
            "vwap": 149,
        }

        session_state = {}
        result = run_stage2_batch(
            universe,
            {"NSE_EQ|ABC": quote},
            {"NSE_EQ|ABC": _history()},
            max_instruments=10,
            min_avg_turnover_inr=0,
            session_state=session_state,
        )

        self.assertEqual(result.liquid_instrument_keys, ["NSE_EQ|ABC"])
        self.assertEqual(len(result.decisions), 1)
        self.assertEqual(result.decisions[0].instrument_key, "NSE_EQ|ABC")
        self.assertIs(
            session_state["_stage2_gatekeeper_decisions"]["NSE_EQ|ABC"],
            result.decisions[0],
        )
        self.assertNotIn("MISSING_LIVE_QUOTE", result.decisions[0].reject_reasons)


if __name__ == "__main__":
    unittest.main()

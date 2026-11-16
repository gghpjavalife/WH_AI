import unittest

import numpy as np
import pandas as pd

from services.stage2_feature_gatekeeper import (
    GatekeeperConfig,
    compute_22_technical_features,
    evaluate_investment_classification,
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


def _investment_fundamentals():
    return {
        "sector": "Technology",
        "industry": "Software",
        "quarterly_yoy_sales_growth_pct": 15,
        "quarterly_yoy_net_profit_growth_pct": 22,
        "quarterly_yoy_operating_profit_growth_pct": 12,
        "peg_ratio": 1.2,
        "promoter_pledge_pct": 4,
        "sales_cagr_3y_pct": 11,
        "profit_cagr_3y_pct": 13,
        "roce_pct": 20,
        "debt_to_equity": 0.5,
        "ocf_to_pat_3y": 1,
        "trailing_pe": 15,
        "five_year_median_pe": 15,
    }


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

    def test_investment_classification_assigns_both_horizons_independently(self):
        result = evaluate_investment_classification(
            {"last_price": 120, "volume": 100_000},
            {"sma_200": 100},
            _investment_fundamentals(),
        )

        self.assertTrue(result["pass_short"])
        self.assertTrue(result["pass_long"])
        self.assertEqual(result["investment_tag"], "SHORT + LONG")
        self.assertEqual(result["failed_short"], [])
        self.assertEqual(result["failed_long"], [])
        self.assertFalse(result["partial_data_warning"])

    def test_investment_classification_uses_available_checks_and_80_percent_gate(self):
        fundamentals = _investment_fundamentals()
        fundamentals["quarterly_yoy_operating_profit_growth_pct"] = 0
        fundamentals.pop("quarterly_yoy_net_profit_growth_pct")
        fundamentals.pop("peg_ratio")
        fundamentals["forward_pe"] = 20
        result = evaluate_investment_classification(
            {"last_price": 120, "volume": 100_000},
            {"sma_200": 100},
            fundamentals,
        )

        self.assertTrue(result["pass_short"])
        self.assertEqual(result["short_checks_passed"], 4)
        self.assertEqual(result["short_checks_available"], 5)
        self.assertEqual(result["failed_short"], ["quarterly_operating_profit_growth"])
        self.assertTrue(result["partial_data_warning"])

        fundamentals["quarterly_yoy_sales_growth_pct"] = 0
        result = evaluate_investment_classification(
            {"last_price": 120, "volume": 100_000},
            {"sma_200": 100},
            fundamentals,
        )
        self.assertFalse(result["pass_short"])
        self.assertEqual(result["short_checks_passed"], 3)

    def test_lender_rules_ignore_altman_and_piotroski_while_nonlender_finance_is_not_lender(self):
        fundamentals = _investment_fundamentals()
        fundamentals.update(
            {
                "sector": "Financial Services",
                "industry": "Commercial Banks",
                "piotroski_f_score": 0,
                "altman_z_score": 0,
                "roa_pct": 1.5,
                "net_npa_pct": 0.8,
                "capital_adequacy_ratio_pct": 16,
                "price_to_book": 2,
                "five_year_median_pb": 2,
            }
        )
        decision = evaluate_jev_gatekeeper(
            "NSE_EQ|BANK",
            "BANK",
            {"last_price": 120, "volume": 100_000, "bid_ask_spread_pct": 0.1},
            {"last_price": 120, "sma_50": 110, "sma_200": 100, "adx_14": 25, "choppiness_14": 50},
            fundamentals,
        )
        self.assertTrue(decision.investment_classification["is_lender"])
        self.assertTrue(decision.is_lender)
        self.assertEqual(decision.investment_tag, "SHORT + LONG")
        self.assertEqual(decision.failed_short, [])
        self.assertNotIn("WEAK_PIOTROSKI<4", decision.reject_reasons)
        self.assertNotIn("DISTRESS_ALTMAN_Z<1.81", decision.reject_reasons)

        fundamentals["industry"] = "Asset Management"
        nonlender = evaluate_investment_classification(
            {"last_price": 120, "volume": 100_000},
            {"sma_200": 100},
            fundamentals,
        )
        self.assertFalse(nonlender["is_lender"])

    def test_investment_classification_handles_missing_nan_and_all_missing_metrics(self):
        result = evaluate_investment_classification(
            {"last_price": float("nan"), "volume": None},
            {"sma_200": float("nan")},
            {
                "industry": "Housing Finance",
                "returnOnAssets": 0.015,
                "net_npa_pct": 0.9,
                "capitalAdequacyRatio": 0.16,
                "priceToBook": float("nan"),
            },
        )

        self.assertTrue(result["is_lender"])
        self.assertFalse(result["pass_short"])
        self.assertTrue(result["pass_long"])
        self.assertEqual(result["investment_tag"], "LONG-TERM")
        self.assertTrue(result["partial_data_warning"])
        self.assertNotIn("capital_adequacy_ratio", result["missing_metrics"])

    def test_investment_classification_reports_short_term_only(self):
        fundamentals = _investment_fundamentals()
        fundamentals["sales_cagr_3y_pct"] = 0
        fundamentals["profit_cagr_3y_pct"] = 0
        fundamentals["debt_to_equity"] = 2
        fundamentals["ocf_to_pat_3y"] = 0
        result = evaluate_investment_classification(
            {"last_price": 120, "volume": 100_000},
            {"sma_200": 100},
            fundamentals,
        )

        self.assertTrue(result["pass_short"])
        self.assertFalse(result["pass_long"])
        self.assertEqual(result["investment_tag"], "SHORT-TERM")

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

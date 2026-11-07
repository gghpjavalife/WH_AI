import unittest

import pandas as pd

from services.stock_context_financials import (
    calculate_altman_z_score,
    calculate_financial_metrics,
    calculate_growth_and_quality,
    calculate_piotroski_f_score,
    verify_altman_inputs,
)


def statements():
    balance = pd.DataFrame([
        {"symbol": "XYZ", "asOfDate": "2022-12-31", "periodType": "12M",
         "TotalAssets": 100},
        {"symbol": "XYZ", "asOfDate": "2023-12-31", "periodType": "12M",
         "TotalAssets": 120, "LongTermDebt": 40, "CurrentAssets": 45,
         "CurrentLiabilities": 30, "OrdinarySharesNumber": 10},
        {"symbol": "XYZ", "asOfDate": "2024-12-31", "periodType": "12M",
         "TotalAssets": 150, "LongTermDebt": 30, "CurrentAssets": 80,
         "CurrentLiabilities": 40, "OrdinarySharesNumber": 10,
         "RetainedEarnings": 30, "TotalLiabilitiesNetMinorityInterest": 80},
    ])
    income = pd.DataFrame([
        {"symbol": "XYZ", "asOfDate": "2023-12-31", "periodType": "12M",
         "TotalRevenue": 100, "NetIncome": 10, "GrossProfit": 30},
        {"symbol": "XYZ", "asOfDate": "2024-12-31", "periodType": "12M",
         "TotalRevenue": 150, "NetIncome": 20, "GrossProfit": 60, "EBIT": 25},
    ])
    cash = pd.DataFrame([
        {"symbol": "XYZ", "asOfDate": "2024-12-31", "periodType": "12M",
         "OperatingCashFlow": 25},
    ])
    return balance, income, cash


class FinancialMetricsTests(unittest.TestCase):
    def setUp(self):
        self.balance, self.income, self.cash = statements()

    def calculate(self, market_equity=200):
        return calculate_financial_metrics(self.balance, self.income, self.cash, market_equity)

    def test_complete_scores_and_ratios(self):
        result = self.calculate()
        self.assertAlmostEqual(result["altman_z_score"], 3.65)
        self.assertEqual(result["piotroski_f_score"], 9)
        self.assertEqual(result["revenue_growth_yoy"], 0.5)
        self.assertEqual(result["earnings_growth_yoy"], 1)
        self.assertEqual(result["earnings_quality_score"], 1.25)

    def test_altman_verification_matrix_tracks_each_required_ratio(self):
        matrix = verify_altman_inputs(self.balance, self.income, self.cash, 200)
        self.assertEqual(matrix["period"], "2024-12-31")
        self.assertTrue(all(entry["valid"] for entry in matrix["metrics"].values()))
        self.balance.loc[2, "TotalAssets"] = 0
        matrix = verify_altman_inputs(self.balance, self.income, self.cash, 200)
        self.assertFalse(matrix["metrics"]["working_capital_to_assets"]["valid"])
        self.assertIsNone(self.calculate()["altman_z_score"])

    def test_individual_helpers(self):
        self.assertAlmostEqual(calculate_altman_z_score(self.balance, self.income, 200), 3.65)
        self.assertEqual(calculate_piotroski_f_score(self.balance, self.income, self.cash), 9)
        self.assertEqual(calculate_growth_and_quality(self.income, self.cash), {
            "revenue_growth_yoy": 0.5,
            "earnings_growth_yoy": 1.0,
            "earnings_quality_score": 1.25,
        })

    def test_each_of_nine_criteria_contributes_one_point(self):
        # Choose values that independently flip one criterion without affecting others.
        cases = [
            ("income", 1, "NetIncome", -1, 7),  # positive ROA and change in ROA
            ("cash", 0, "OperatingCashFlow", -1, 7),  # OCF positive and > NI
            ("income", 0, "NetIncome", 17, 8),
            ("cash", 0, "OperatingCashFlow", 19, 8),
            ("balance", 2, "LongTermDebt", 60, 8),
            ("balance", 2, "CurrentAssets", 60, 8),  # equality is not improvement
            ("balance", 2, "OrdinarySharesNumber", 11, 8),
            ("income", 1, "GrossProfit", 45, 8),  # equality is not improvement
            ("income", 0, "AverageAssets", 80, 8),
        ]
        for frame_name, row, field, value, expected in cases:
            with self.subTest(field=field, value=value):
                self.balance, self.income, self.cash = statements()
                getattr(self, frame_name).loc[row, field] = value
                self.assertEqual(self.calculate()["piotroski_f_score"], expected)

    def test_missing_inputs_never_yield_partial_score(self):
        for frame_name, row, field in (
            ("balance", 2, "LongTermDebt"), ("balance", 1, "CurrentLiabilities"),
            ("balance", 1, "OrdinarySharesNumber"), ("income", 0, "GrossProfit"),
            ("income", 1, "NetIncome"), ("cash", 0, "OperatingCashFlow"),
            ("balance", 0, "TotalAssets"),
        ):
            with self.subTest(field=field):
                self.balance, self.income, self.cash = statements()
                getattr(self, frame_name).loc[row, field] = float("nan")
                self.assertIsNone(self.calculate()["piotroski_f_score"])

    def test_altman_missing_components_and_zero_denominators(self):
        for frame_name, row, field in (
            ("balance", 2, "RetainedEarnings"), ("balance", 2, "CurrentAssets"),
            ("balance", 2, "TotalLiabilitiesNetMinorityInterest"),
            ("income", 1, "EBIT"), ("income", 1, "TotalRevenue"),
        ):
            with self.subTest(field=field):
                self.balance, self.income, self.cash = statements()
                getattr(self, frame_name).loc[row, field] = None
                self.assertIsNone(self.calculate()["altman_z_score"])
        self.balance, self.income, self.cash = statements()
        self.assertIsNone(self.calculate(None)["altman_z_score"])
        self.assertIsNone(self.calculate(-1)["altman_z_score"])
        for field in ("TotalAssets", "TotalLiabilitiesNetMinorityInterest"):
            self.balance, self.income, self.cash = statements()
            self.balance.loc[2, field] = 0
            self.assertIsNone(self.calculate()["altman_z_score"])

    def test_zero_denominators_and_signed_growth(self):
        for frame_name, row, field in (
            ("balance", 1, "CurrentLiabilities"), ("balance", 2, "TotalAssets"),
            ("income", 0, "TotalRevenue"), ("income", 1, "TotalRevenue"),
        ):
            with self.subTest(field=field, row=row):
                self.balance, self.income, self.cash = statements()
                getattr(self, frame_name).loc[row, field] = 0
                self.assertIsNone(self.calculate()["piotroski_f_score"])
        self.balance, self.income, self.cash = statements()
        self.income.loc[0, "NetIncome"] = -10
        self.income.loc[1, "NetIncome"] = -5
        self.assertEqual(self.calculate()["earnings_growth_yoy"], 0.5)
        self.assertEqual(self.calculate()["earnings_quality_score"], -5)
        self.income.loc[0, "NetIncome"] = 0
        self.income.loc[1, "NetIncome"] = 0
        self.assertIsNone(self.calculate()["earnings_growth_yoy"])
        self.assertIsNone(self.calculate()["earnings_quality_score"])

    def test_reported_average_assets_and_field_aliases(self):
        self.balance = self.balance.iloc[1:].copy()
        self.income["AverageAssets"] = [110, 135]
        self.balance.rename(columns={
            "CurrentAssets": "TotalCurrentAssets",
            "CurrentLiabilities": "TotalCurrentLiabilities",
            "LongTermDebt": "LongTermDebtAndCapitalLeaseObligation",
            "TotalLiabilitiesNetMinorityInterest": "TotalLiabilities",
            "OrdinarySharesNumber": "ShareIssued",
        }, inplace=True)
        self.income.rename(columns={"TotalRevenue": "OperatingRevenue",
                                    "NetIncome": "NetIncomeCommonStockholders"}, inplace=True)
        self.cash.rename(columns={"OperatingCashFlow": "CashFlowFromContinuingOperatingActivities"}, inplace=True)
        result = self.calculate()
        self.assertEqual(result["piotroski_f_score"], 9)
        self.assertAlmostEqual(result["altman_z_score"], 3.65)
        self.assertEqual(result["earnings_growth_yoy"], 1)

    def test_leverage_uses_average_not_year_end_assets(self):
        self.income["AverageAssets"] = [80, 135]
        self.balance.loc[2, "LongTermDebt"] = 65
        # Leverage improves using average assets; turnover does not.
        self.assertEqual(self.calculate()["piotroski_f_score"], 8)
        self.balance.loc[2, "LongTermDebt"] = -1
        self.assertIsNone(self.calculate()["piotroski_f_score"])

    def test_52_53_week_periods_and_unmatched_aliases(self):
        self.balance["asOfDate"] = ["2022-12-31", "2023-12-30", "2025-01-04"]
        self.income["asOfDate"] = ["2023-12-30", "2025-01-04"]
        self.cash["asOfDate"] = ["2025-01-04"]
        self.assertEqual(self.calculate()["piotroski_f_score"], 9)
        self.income.loc[1, "OperatingRevenue"] = 150
        self.income.loc[1, "TotalRevenue"] = None
        self.assertIsNone(self.calculate()["revenue_growth_yoy"])
        self.assertIsNone(self.calculate()["piotroski_f_score"])
        self.assertIsNotNone(self.calculate()["altman_z_score"])

    def test_quarterly_and_nonconsecutive_periods_are_not_used(self):
        quarter = self.income.iloc[[1]].copy()
        quarter["asOfDate"], quarter["periodType"], quarter["TotalRevenue"] = "2025-03-31", "3M", 999
        self.income = pd.concat([self.income, quarter], ignore_index=True)
        self.assertEqual(self.calculate()["revenue_growth_yoy"], 0.5)
        self.income.loc[0, "asOfDate"] = "2022-12-31"
        result = self.calculate()
        self.assertIsNone(result["revenue_growth_yoy"])
        self.assertIsNone(result["earnings_growth_yoy"])
        self.assertIsNone(result["piotroski_f_score"])
        self.assertAlmostEqual(result["altman_z_score"], 3.65)

    def test_misaligned_dates_do_not_use_future_or_stale_statements(self):
        self.cash.loc[0, "asOfDate"] = "2025-12-31"
        result = self.calculate()
        self.assertTrue(all(value is None for value in result.values()))
        self.cash.loc[0, "asOfDate"] = "2024-12-30"
        result = self.calculate()
        self.assertIsNone(result["piotroski_f_score"])
        self.assertIsNone(result["earnings_quality_score"])
        self.assertAlmostEqual(result["altman_z_score"], 3.65)

    def test_multiindex_duplicate_and_multiple_symbols_fail_closed(self):
        self.balance = self.balance.set_index("symbol")
        self.income = self.income.set_index("symbol")
        self.cash = self.cash.set_index("symbol")
        self.assertEqual(self.calculate()["piotroski_f_score"], 9)
        self.income = pd.concat([self.income, self.income.iloc[[-1]]])
        self.assertTrue(all(value is None for value in self.calculate().values()))
        self.balance, self.income, self.cash = statements()
        self.cash.loc[0, "symbol"] = "OTHER"
        self.assertTrue(all(value is None for value in self.calculate().values()))

    def test_empty_and_invalid_values(self):
        self.assertTrue(all(v is None for v in calculate_financial_metrics(None, None, None).values()))
        self.income.loc[1, "EBIT"] = float("inf")
        self.assertIsNone(self.calculate()["altman_z_score"])
        self.cash.loc[0, "OperatingCashFlow"] = pd.NA
        self.assertIsNone(self.calculate()["earnings_quality_score"])
        self.assertIsNone(self.calculate()["piotroski_f_score"])


if __name__ == "__main__":
    unittest.main()

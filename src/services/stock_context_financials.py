"""Financial metrics from annual, long-form Yahoo-style statement data.

All monetary fields (including market equity) must use the same currency and
units. Growth and quality are dimensionless ratios, not percentages; scores
are dimensionless. The caller must ensure market equity is appropriate for
the statement date and currency. Dates here are fiscal period ends, not filing
dates: these helpers cannot establish point-in-time availability.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


_EMPTY = {
    "altman_z_score": None,
    "piotroski_f_score": None,
    "revenue_growth_yoy": None,
    "earnings_growth_yoy": None,
    "earnings_quality_score": None,
}


def _number(value: Any) -> float | None:
    if value is None or pd.api.types.is_bool(value):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _value(row: dict | None, *names: str) -> float | None:
    if row is not None:
        for name in names:
            value = _number(row.get(name))
            if value is not None:
                return value
    return None


def _ratio(numerator: float | None, denominator: float | None,
           *, positive: bool = False) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    if positive and denominator < 0:
        return None
    result = numerator / denominator
    return result if math.isfinite(result) else None


def _annual_rows(frame: Any) -> tuple[dict, set]:
    """Return unique annual period-end rows and their explicit symbols."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return {}, set()
    data = frame.copy()
    # Yahooquery commonly puts symbol in the index.
    for name in ("symbol", "asOfDate", "periodType"):
        if name not in data.columns and name in data.index.names:
            data[name] = data.index.get_level_values(name)
    if not {"asOfDate", "periodType"}.issubset(data.columns):
        return {}, set()
    data = data[data["periodType"].astype(str).str.strip().str.upper().isin(
        ("12M", "FY", "ANNUAL", "A")
    )]
    symbols = set()
    if "symbol" in data.columns:
        symbols = {str(s).strip().upper() for s in data["symbol"]
                   if pd.notna(s) and str(s).strip()}
        # An unidentified row cannot safely be associated with an identified ticker.
        data = data[data["symbol"].notna() & data["symbol"].astype(str).str.strip().ne("")]
    rows: dict = {}
    duplicates = set()
    for _, row in data.iterrows():
        try:
            date = pd.Timestamp(row["asOfDate"])
            if pd.isna(date):
                continue
            date = date.date()
        except (ValueError, TypeError, OverflowError):
            continue
        if date in rows:
            duplicates.add(date)
        else:
            rows[date] = row.to_dict()
    for date in duplicates:
        rows[date] = None
    return rows, symbols


def _prior_date(date, dates):
    candidates = [d for d in dates if 330 <= (date - d).days <= 400]
    # Ambiguous fiscal periods must not be resolved arbitrarily.
    return candidates[0] if len(candidates) == 1 else None


def _assets(row):
    return _value(row, "TotalAssets")


def _average_assets(date, balance, income):
    average = _value(income.get(date), "AverageAssets")
    if average is None:
        average = _value(balance.get(date), "AverageAssets")
    if average is not None:
        return average if average > 0 else None
    prior = _prior_date(date, balance)
    current_assets = _assets(balance.get(date))
    prior_assets = _assets(balance.get(prior))
    if (current_assets is None or prior_assets is None or
            current_assets <= 0 or prior_assets <= 0):
        return None
    average = (current_assets / 2) + (prior_assets / 2)
    return average if math.isfinite(average) else None


def _revenue(row):
    return _value(row, "TotalRevenue", "OperatingRevenue", "Revenue")


def _net_income(row):
    return _value(row, "NetIncome", "NetIncomeCommonStockholders")


def _ocf(row):
    return _value(row, "OperatingCashFlow", "CashFlowFromContinuingOperatingActivities",
                  "TotalCashFromOperatingActivities")


def _working_capital(row):
    reported = _value(row, "WorkingCapital")
    if reported is not None:
        return reported
    current_assets = _value(row, "CurrentAssets", "TotalCurrentAssets")
    current_liabilities = _value(row, "CurrentLiabilities", "TotalCurrentLiabilities")
    if current_assets is None or current_liabilities is None:
        return None
    return current_assets - current_liabilities


def _altman_matrix(date, balance, income, market_equity):
    b, i = balance.get(date), income.get(date)
    assets = _assets(b)
    equity = _number(market_equity)
    return {
        "working_capital_to_assets": _ratio(_working_capital(b), assets, positive=True),
        "retained_earnings_to_assets": _ratio(_value(b, "RetainedEarnings"), assets, positive=True),
        "ebit_to_assets": _ratio(_value(i, "EBIT"), assets, positive=True),
        "market_equity_to_liabilities": _ratio(
            equity if equity is not None and equity >= 0 else None,
            _value(b, "TotalLiabilitiesNetMinorityInterest", "TotalLiabilities"), positive=True),
        "sales_to_assets": _ratio(_revenue(i), assets, positive=True),
    }


def _altman(date, balance, income, market_equity):
    matrix = _altman_matrix(date, balance, income, market_equity)
    if any(value is None for value in matrix.values()):
        return None
    result = sum(c * w for c, w in zip(matrix.values(), (1.2, 1.4, 3.3, 0.6, 1.0)))
    return result if math.isfinite(result) else None


def verify_altman_inputs(balance_sheet, income_statement, cash_flow, market_equity):
    """Return each required ratio and its verification status for the exact latest annual period."""
    balance, bs_symbols = _annual_rows(balance_sheet)
    income, is_symbols = _annual_rows(income_statement)
    cash, cf_symbols = _annual_rows(cash_flow)
    dates = set(balance) | set(income) | set(cash)
    aligned = bool(dates) and len(bs_symbols | is_symbols | cf_symbols) <= 1
    date = max(dates) if aligned else None
    matrix = _altman_matrix(date, balance, income, market_equity)
    return {
        "period": date.isoformat() if date is not None else None,
        "same_ticker": len(bs_symbols | is_symbols | cf_symbols) <= 1,
        "metrics": {name: {"value": value, "valid": value is not None}
                    for name, value in matrix.items()},
    }


def _matched_shares(current_balance, prior_balance, current_income, prior_income):
    # Use the same share definition in both periods; never compare issued with
    # outstanding or point-in-time with weighted-average shares.
    for rows, names in (
        ((current_balance, prior_balance), ("OrdinarySharesNumber", "ShareIssued")),
        ((current_income, prior_income), ("BasicAverageShares", "DilutedAverageShares")),
    ):
        for name in names:
            current, prior = (_value(row, name) for row in rows)
            if current is not None and prior is not None and current > 0 and prior > 0:
                return current, prior
    return None, None


def _matched_values(current, prior, *names):
    for name in names:
        value, previous = _value(current, name), _value(prior, name)
        if value is not None and previous is not None:
            return value, previous
    return None, None


def _piotroski(date, balance, income, cash):
    prior = _prior_date(date, income)
    if prior is None:
        return None
    b, pb = balance.get(date), balance.get(prior)
    i, pi, c = income.get(date), income.get(prior), cash.get(date)
    avg, prev_avg = _average_assets(date, balance, income), _average_assets(prior, balance, income)
    ni, prev_ni = _matched_values(i, pi, "NetIncome", "NetIncomeCommonStockholders")
    ocf = _ocf(c)
    roa, prev_roa = _ratio(ni, avg, positive=True), _ratio(prev_ni, prev_avg, positive=True)
    current_debt, prior_debt = _matched_values(
        b, pb, "LongTermDebt", "LongTermDebtAndCapitalLeaseObligation")
    if current_debt is not None and current_debt < 0:
        current_debt = None
    if prior_debt is not None and prior_debt < 0:
        prior_debt = None
    debt = _ratio(current_debt, avg, positive=True)
    prev_debt = _ratio(prior_debt, prev_avg, positive=True)
    current_ratio = _ratio(_value(b, "CurrentAssets", "TotalCurrentAssets"),
                           _value(b, "CurrentLiabilities", "TotalCurrentLiabilities"), positive=True)
    prev_current_ratio = _ratio(_value(pb, "CurrentAssets", "TotalCurrentAssets"),
                                _value(pb, "CurrentLiabilities", "TotalCurrentLiabilities"), positive=True)
    revenue, prev_revenue = _matched_values(i, pi, "TotalRevenue", "OperatingRevenue", "Revenue")
    margin = _ratio(_value(i, "GrossProfit"), revenue, positive=True)
    prev_margin = _ratio(_value(pi, "GrossProfit"), prev_revenue, positive=True)
    turnover = _ratio(revenue, avg, positive=True)
    prev_turnover = _ratio(prev_revenue, prev_avg, positive=True)
    shares, prev_shares = _matched_shares(b, pb, i, pi)
    values = (ni, ocf, roa, prev_roa, debt, prev_debt, current_ratio,
              prev_current_ratio, shares, prev_shares, margin, prev_margin,
              turnover, prev_turnover)
    if any(v is None for v in values):
        return None
    return sum((roa > 0, ocf > 0, roa > prev_roa, ocf > ni,
                debt < prev_debt, current_ratio > prev_current_ratio,
                shares <= prev_shares, margin > prev_margin, turnover > prev_turnover))


def _growth(current, prior):
    if current is None or prior is None or prior == 0:
        return None
    # Absolute prior value preserves the direction of change for losses.
    return _ratio(current - prior, abs(prior), positive=True)


def calculate_financial_metrics(balance_sheet: pd.DataFrame | None, income_statement: pd.DataFrame | None,
                                cash_flow: pd.DataFrame | None, market_equity: float | None = None) -> dict:
    """Compute latest annual metrics, returning None for unavailable inputs.

    Only annual rows are considered; all statements must refer to one ticker.
    Metrics use the latest fiscal end present in any statement and exact-date
    alignment. Prior fiscal ends must be uniquely 330–400 days earlier. ROA
    and asset turnover use reported AverageAssets, or the mean of consecutive
    year-end assets; the latter needs three balance sheets for F-score. Debt
    leverage uses long-term debt / average assets. No dilution compares the
    same reported share field; unchanged shares earn a point. Strict increases
    or decreases are required for other change criteria.

    Growth is (current - prior) / abs(prior), including negative earnings.
    Earnings quality is operating cash flow / net income; zero denominators
    are unavailable. Altman is the public manufacturing Z-score and should
    not be interpreted as a general model for other company types.
    """
    balance, bs_symbols = _annual_rows(balance_sheet)
    income, is_symbols = _annual_rows(income_statement)
    cash, cf_symbols = _annual_rows(cash_flow)
    if len(bs_symbols | is_symbols | cf_symbols) > 1:
        return _EMPTY.copy()
    dates = set(balance) | set(income) | set(cash)
    if not dates:
        return _EMPTY.copy()
    date = max(dates)
    i, c = income.get(date), cash.get(date)
    prior = _prior_date(date, income)
    previous = income.get(prior)
    revenue, prev_revenue = _matched_values(i, previous, "TotalRevenue", "OperatingRevenue", "Revenue")
    ni, prev_ni = _matched_values(i, previous, "NetIncome", "NetIncomeCommonStockholders")
    return {
        "altman_z_score": _altman(date, balance, income, market_equity),
        "piotroski_f_score": _piotroski(date, balance, income, cash),
        "revenue_growth_yoy": _growth(revenue, prev_revenue),
        "earnings_growth_yoy": _growth(ni, prev_ni),
        "earnings_quality_score": _ratio(_ocf(c), _net_income(i)),
    }


def calculate_altman_z_score(balance_sheet: pd.DataFrame, income_statement: pd.DataFrame,
                             market_equity: float | None) -> float | None:
    """Return the public-manufacturing Altman score from the two statements."""
    return calculate_financial_metrics(balance_sheet, income_statement, None, market_equity)["altman_z_score"]


def calculate_piotroski_f_score(balance_sheet: pd.DataFrame, income_statement: pd.DataFrame,
                                cash_flow: pd.DataFrame) -> int | None:
    """Return the nine-point Piotroski score, or None if any criterion is unavailable."""
    return calculate_financial_metrics(balance_sheet, income_statement, cash_flow)["piotroski_f_score"]


def calculate_growth_and_quality(income_statement: pd.DataFrame, cash_flow: pd.DataFrame) -> dict:
    """Return annual revenue and earnings growth and the OCF/net-income ratio."""
    result = calculate_financial_metrics(None, income_statement, cash_flow)
    return {key: result[key] for key in
            ("revenue_growth_yoy", "earnings_growth_yoy", "earnings_quality_score")}

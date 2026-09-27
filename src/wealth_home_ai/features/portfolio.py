"""Portfolio totals and manual debt-holding validation."""

from __future__ import annotations

from typing import Any

import pandas as pd

DEBT_COLUMNS = [
    "Instrument",
    "Principal",
    "Current_Value",
    "Annual_Rate_%",
    "Maturity_Date",
]


def empty_debt_holdings() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Instrument": pd.Series(dtype="string"),
            "Principal": pd.Series(dtype="float64"),
            "Current_Value": pd.Series(dtype="float64"),
            "Annual_Rate_%": pd.Series(dtype="float64"),
            "Maturity_Date": pd.Series(dtype="string"),
        },
        columns=DEBT_COLUMNS,
    )


def validate_debt_holdings(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize user-entered debt assets before storing them in session state."""
    if not set(DEBT_COLUMNS).issubset(frame.columns):
        raise ValueError("Debt rows are missing required fields.")
    normalized = frame[DEBT_COLUMNS].copy()
    normalized["Instrument"] = (
        normalized["Instrument"].fillna("").astype(str).str.strip()
    )
    normalized = normalized[normalized["Instrument"].ne("")]
    for column in ("Principal", "Current_Value", "Annual_Rate_%"):
        normalized[column] = pd.to_numeric(normalized[column], errors="raise")
    if (
        not normalized[["Principal", "Current_Value", "Annual_Rate_%"]]
        .map(
            lambda value: pd.notna(value)
            and value < float("inf")
            and value > float("-inf")
        )
        .all()
        .all()
        or normalized[["Principal", "Current_Value", "Annual_Rate_%"]].lt(0).any().any()
    ):
        raise ValueError(
            "Debt values and interest rates must be finite and non-negative."
        )
    normalized["Maturity_Date"] = (
        normalized["Maturity_Date"].fillna("").astype(str).str.strip()
    )
    has_maturity_date = normalized["Maturity_Date"].ne("")
    parsed_maturity_dates = pd.to_datetime(
        normalized.loc[has_maturity_date, "Maturity_Date"],
        format="%Y-%m-%d",
        errors="coerce",
    )
    if parsed_maturity_dates.isna().any():
        raise ValueError("Maturity dates must use YYYY-MM-DD format.")
    return normalized.reset_index(drop=True)


def calculate_allocation(
    balance: float,
    equity_holdings: pd.DataFrame,
    trading_positions: pd.DataFrame,
    mutual_funds: pd.DataFrame,
    debt_holdings: pd.DataFrame,
) -> dict[str, Any]:
    """Calculate gross portfolio allocation and P&L by supported asset class."""
    equity_value = (
        float((equity_holdings["Qty"].abs() * equity_holdings["LTP"]).sum())
        if not equity_holdings.empty
        else 0.0
    )
    trading_value = (
        float((trading_positions["Qty"].abs() * trading_positions["LTP"]).sum())
        if not trading_positions.empty
        else 0.0
    )
    mutual_fund_value = (
        float((mutual_funds["Units"] * mutual_funds["Latest_NAV"]).sum())
        if not mutual_funds.empty
        else 0.0
    )
    debt_value = (
        float(debt_holdings["Current_Value"].sum())
        if not debt_holdings.empty
        else 0.0
    )
    equity_invested = (
        float((equity_holdings["Qty"].abs() * equity_holdings["Avg_Price"]).sum())
        if not equity_holdings.empty
        else 0.0
    )
    trading_invested = (
        float((trading_positions["Qty"].abs() * trading_positions["Avg_Price"]).sum())
        if not trading_positions.empty
        else 0.0
    )
    mutual_fund_invested = (
        float((mutual_funds["Units"] * mutual_funds["Avg_NAV"]).sum())
        if not mutual_funds.empty
        else 0.0
    )
    debt_invested = (
        float(debt_holdings["Principal"].sum())
        if not debt_holdings.empty
        else 0.0
    )
    equity_pnl = (
        float(
            (
                equity_holdings["Qty"]
                * (equity_holdings["LTP"] - equity_holdings["Avg_Price"])
            ).sum()
        )
        if not equity_holdings.empty
        else 0.0
    )
    trading_pnl = (
        float(
            (
                trading_positions["Qty"]
                * (trading_positions["LTP"] - trading_positions["Avg_Price"])
            ).sum()
        )
        if not trading_positions.empty
        else 0.0
    )
    mutual_fund_pnl = (
        float(
            (
                mutual_funds["Units"]
                * (mutual_funds["Latest_NAV"] - mutual_funds["Avg_NAV"])
            ).sum()
        )
        if not mutual_funds.empty
        else 0.0
    )
    debt_pnl = (
        float((debt_holdings["Current_Value"] - debt_holdings["Principal"]).sum())
        if not debt_holdings.empty
        else 0.0
    )
    values = {
        "Equity": equity_value,
        "Trading": trading_value,
        "Debt": debt_value,
        "Mutual funds": mutual_fund_value,
        "Options": 0.0,
        "Futures": 0.0,
    }
    total_investments = sum(values.values())
    return {
        "allocation": values,
        "total_investments": total_investments,
        "invested_amount": (
            equity_invested
            + trading_invested
            + mutual_fund_invested
            + debt_invested
        ),
        "total_portfolio_value": total_investments + float(balance),
        "cash": float(balance),
        "total_pnl": equity_pnl + trading_pnl + mutual_fund_pnl + debt_pnl,
        "pnl_by_asset": {
            "Equity": equity_pnl,
            "Trading": trading_pnl,
            "Debt": debt_pnl,
            "Mutual funds": mutual_fund_pnl,
            "Options": 0.0,
            "Futures": 0.0,
        },
        "asset_counts": {
            "Equity": len(equity_holdings),
            "Trading": len(trading_positions),
            "Debt": len(debt_holdings),
            "Mutual funds": len(mutual_funds),
            "Options": 0,
            "Futures": 0,
        },
    }

"""Portfolio totals and allocation."""

from __future__ import annotations

from typing import Any

import pandas as pd

def calculate_allocation(
    balance: float,
    equity_holdings: pd.DataFrame,
    trading_positions: pd.DataFrame,
    mutual_funds: pd.DataFrame,
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
    values = {
        "Equity": equity_value,
        "Trading": trading_value,
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
        ),
        "total_portfolio_value": total_investments + float(balance),
        "cash": float(balance),
        "total_pnl": equity_pnl + trading_pnl + mutual_fund_pnl,
        "pnl_by_asset": {
            "Equity": equity_pnl,
            "Trading": trading_pnl,
            "Mutual funds": mutual_fund_pnl,
            "Options": 0.0,
            "Futures": 0.0,
        },
        "asset_counts": {
            "Equity": len(equity_holdings),
            "Trading": len(trading_positions),
            "Mutual funds": len(mutual_funds),
            "Options": 0,
            "Futures": 0,
        },
    }

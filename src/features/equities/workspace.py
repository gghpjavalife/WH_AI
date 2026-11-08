"""Equity holdings view."""

from collections.abc import Callable

import pandas as pd
import streamlit as st

from ..analysis.panel import render_analysis_panel
from ..analysis.holdings import render_stock_holdings_table
from .scanner import render_market_scanner
from ui.helpers import workspace_subtabs_css


def build_equity_holdings_display(
    holdings: pd.DataFrame, total_investments: float | None = None
) -> pd.DataFrame:
    display = holdings.copy()
    display["Investment"] = display["Qty"].abs() * display["Avg_Price"]
    display["Market_Value"] = display["Qty"] * display["LTP"]
    display["P&L"] = display["Qty"] * (display["LTP"] - display["Avg_Price"])
    display["Returns_%"] = display.apply(
        lambda row: row["P&L"] / row["Investment"] * 100
        if row["Investment"]
        else 0.0,
        axis=1,
    )
    allocation_base = total_investments
    if allocation_base is None:
        allocation_base = float(display["Market_Value"].abs().sum())
    display["Allocation_%"] = (
        display["Market_Value"].abs().div(allocation_base).fillna(0.0) * 100
        if allocation_base > 0
        else 0.0
    )
    return display


def render_equity(
    holdings: pd.DataFrame, total_investments: float | None = None
) -> None:
    if holdings.empty:
        st.info("No equity holdings were returned by the connected broker.")
        return
    display = build_equity_holdings_display(holdings, total_investments)
    theme = getattr(st.context.theme, "type", "light")
    render_stock_holdings_table(
        display,
        key="equities_holdings_table",
        theme=theme,
        layout="equity",
    )


def render_equities_workspace(
    holdings: pd.DataFrame,
    data_error: str | None,
    *,
    broker_connected: bool,
    symbols: list[str],
    run_analysis: Callable[..., None],
    show_approved_trades: Callable[[str], None],
    total_investments: float | None = None,
) -> None:
    if data_error:
        st.warning(data_error)
    portfolio_value = total_investments
    if portfolio_value is None:
        portfolio_value = float(
            (holdings["Qty"].abs() * holdings["LTP"]).sum()
        ) if not holdings.empty else 0.0
    equity_value = float(
        (holdings["Qty"].abs() * holdings["LTP"]).sum()
    ) if not holdings.empty else 0.0
    share = equity_value / portfolio_value * 100 if portfolio_value > 0 else 0.0
    with st.container(key="equity_workspace_tabs"):
        st.html(workspace_subtabs_css("equity_workspace_tabs"))
        holdings_tab, scanner_tab = st.tabs(
            [f"Holdings · {share:.1f}%", "Market Scanner"]
        )
        with holdings_tab:
            render_equity(holdings, total_investments)
            render_analysis_panel(
                "equity", "equity", run_analysis, show_approved_trades
            )
        with scanner_tab:
            render_market_scanner(
                broker_connected=broker_connected,
                scope="Equities",
                symbols=symbols,
            )

"""Equity holdings view."""

from collections.abc import Callable

import pandas as pd
import streamlit as st

from ..analysis.panel import render_analysis_panel
from .scanner import render_market_scanner
from ..shared.performance import style_returns


def render_equity(holdings: pd.DataFrame) -> None:
    if holdings.empty:
        st.info("No equity holdings were returned by the connected broker.")
        return
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
    theme = getattr(st.context.theme, "type", "light")
    st.dataframe(style_returns(display, theme=theme), hide_index=True)


def render_equities_workspace(
    holdings: pd.DataFrame,
    data_error: str | None,
    *,
    broker_connected: bool,
    symbols: list[str],
    run_analysis: Callable[..., None],
    show_approved_trades: Callable[[str], None],
) -> None:
    if data_error:
        st.warning(data_error)
    render_equity(holdings)
    render_analysis_panel(
        "equity", "equity", run_analysis, show_approved_trades
    )
    with st.expander("Market scanner", icon=":material/filter_list:"):
        render_market_scanner(
            broker_connected=broker_connected,
            scope="Equities",
            symbols=symbols,
        )

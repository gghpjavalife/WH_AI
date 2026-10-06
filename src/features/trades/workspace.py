"""Open broker trading-position view."""

from collections.abc import Callable

import pandas as pd
import streamlit as st

from ..analysis.panel import render_analysis_panel
from ..equities.scanner import render_market_scanner
from ..shared.performance import style_returns


def render_trading(positions: pd.DataFrame) -> None:
    st.caption("Values and P&L are derived from the connected broker's current positions.")
    if positions.empty:
        st.info("The broker returned no open trading positions.")
        return
    display = positions.copy()
    display["Gross_Exposure"] = display["Qty"].abs() * display["LTP"]
    display["P&L"] = display["Qty"] * (display["LTP"] - display["Avg_Price"])
    investment = display["Qty"].abs() * display["Avg_Price"]
    display["Returns_%"] = (
        display["P&L"].div(investment.where(investment.ne(0))).fillna(0.0) * 100
    )
    theme = getattr(st.context.theme, "type", "light")
    st.dataframe(style_returns(display, theme=theme), hide_index=True)


def render_trades_workspace(
    positions: pd.DataFrame,
    data_error: str | None,
    *,
    broker_connected: bool,
    symbols: list[str],
    run_analysis: Callable[..., None],
    show_approved_trades: Callable[[str], None],
) -> None:
    if data_error:
        st.warning(data_error)
    render_trading(positions)
    st.subheader("Trades analysis")
    render_analysis_panel(
        "trading", "trading", run_analysis, show_approved_trades
    )
    with st.expander("Market scanner", icon=":material/filter_list:"):
        render_market_scanner(
            broker_connected=broker_connected,
            scope="Trading",
            symbols=symbols,
        )

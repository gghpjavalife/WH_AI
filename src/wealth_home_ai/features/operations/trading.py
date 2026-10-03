"""Open broker trading-position view."""

import pandas as pd
import streamlit as st

from .performance import style_returns


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

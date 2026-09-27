"""Open broker trading-position view."""

import pandas as pd
import streamlit as st


def render_trading(positions: pd.DataFrame) -> None:
    st.caption("Values and P&L are derived from the connected broker's current positions.")
    if positions.empty:
        st.info("The broker returned no open trading positions.")
        return
    display = positions.copy()
    display["Gross_Exposure"] = display["Qty"].abs() * display["LTP"]
    display["P&L"] = display["Qty"] * (display["LTP"] - display["Avg_Price"])
    st.dataframe(display, hide_index=True)

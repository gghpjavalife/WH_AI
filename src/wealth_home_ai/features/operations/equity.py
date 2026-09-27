"""Equity holdings view."""

import pandas as pd
import streamlit as st


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
    st.dataframe(display, hide_index=True)

"""Asset-specific research and screening for holdings without a quote feed."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st


def _fund_frame(holdings: pd.DataFrame) -> pd.DataFrame:
    frame = holdings.copy()
    units = pd.to_numeric(
        frame.get("Units", pd.Series(index=frame.index, dtype="float64")),
        errors="coerce",
    )
    latest_nav = pd.to_numeric(
        frame.get("Latest_NAV", pd.Series(index=frame.index, dtype="float64")),
        errors="coerce",
    )
    avg_nav = pd.to_numeric(
        frame.get("Avg_NAV", pd.Series(index=frame.index, dtype="float64")),
        errors="coerce",
    )
    frame["Invested_Value"] = units * avg_nav
    frame["Current_Value"] = units * latest_nav
    frame["P&L"] = frame["Current_Value"] - frame["Invested_Value"]
    frame["Returns_%"] = (
        frame["P&L"].div(frame["Invested_Value"].where(frame["Invested_Value"].ne(0)))
        .fillna(0.0)
        * 100
    )
    nav_dates = pd.to_datetime(
        frame.get("NAV_Date", pd.Series(index=frame.index, dtype="string")),
        errors="coerce",
    )
    frame["NAV_age_days"] = (pd.Timestamp(date.today()) - nav_dates).dt.days
    return frame


def _render_fund_research(holdings: pd.DataFrame, *, mode: str) -> None:
    frame = _fund_frame(holdings)
    if mode == "research":
        st.caption(
            "Mutual-fund research summarizes your holdings using their reported "
            "NAVs; NAV history and external fund comparisons are not available here."
        )
        query = st.text_input(
            "Find a mutual fund",
            key="mutual_fund_research_query",
            placeholder="Search fund name or ISIN",
        )
        if query:
            matching = frame["Fund"].str.contains(query, case=False, na=False)
            if "ISIN" in frame:
                matching |= frame["ISIN"].str.contains(query, case=False, na=False)
            frame = frame[matching]
    else:
        st.caption(
            "Screen your saved funds by return and reported NAV age. NAV age is "
            "unknown when the statement does not include a date."
        )
        floor, age = st.columns(2)
        with floor:
            minimum_return = st.number_input(
                "Minimum return (%)",
                min_value=-100.0,
                max_value=1000.0,
                value=-100.0,
                step=1.0,
                key="mutual_fund_scanner_minimum_return",
            )
        with age:
            maximum_nav_age = st.number_input(
                "Maximum NAV age (days)",
                min_value=0,
                max_value=3650,
                value=365,
                step=1,
                key="mutual_fund_scanner_max_nav_age",
            )
        include_undated = st.checkbox(
            "Include funds without a NAV date",
            value=True,
            key="mutual_fund_scanner_include_undated",
        )
        has_nav_date = frame["NAV_age_days"].notna()
        frame = frame[
            frame["Returns_%"].ge(minimum_return)
            & (
                frame["NAV_age_days"].between(0, maximum_nav_age)
                | (include_undated & ~has_nav_date)
            )
        ]
    if frame.empty:
        st.info("No mutual-fund holdings match this view yet.")
        return
    if mode == "research":
        current_value = float(frame["Current_Value"].sum())
        invested = float(frame["Invested_Value"].sum())
        overall_return = (
            (current_value / invested - 1) * 100 if invested else 0.0
        )
        value_col, return_col = st.columns(2)
        value_col.metric("Current value in view", f"₹{current_value:,.2f}")
        return_col.metric("Weighted return in view", f"{overall_return:+.2f}%")
    st.dataframe(frame, hide_index=True, width="stretch")


def render_local_asset_research(
    asset: str, holdings: pd.DataFrame, *, mode: str
) -> None:
    """Show asset-specific tools using only data actually available to the app."""
    if mode not in {"research", "scanner"}:
        raise ValueError("Research mode must be 'research' or 'scanner'.")
    if asset == "Mutual Funds":
        _render_fund_research(holdings, mode=mode)
    else:
        raise ValueError(f"No local research profile is defined for {asset}.")

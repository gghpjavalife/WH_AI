"""Mutual-fund statement import and holdings view."""

import hashlib
import io
from collections.abc import Callable

import pandas as pd
import streamlit as st

from core.constants import mutual_fund_column_names
from ..shared.performance import style_returns

_MUTUAL_FUND_COLUMNS = mutual_fund_column_names()


def validate_mutual_funds(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"Fund", "Units", "Avg_NAV", "Latest_NAV"}
    if not required.issubset(frame.columns):
        raise ValueError(
            "The CSV must include Fund, Units, Avg_NAV, and Latest_NAV columns."
        )
    normalized = frame.copy()
    for column in ("Units", "Avg_NAV", "Latest_NAV"):
        normalized[column] = pd.to_numeric(normalized[column], errors="raise")
    if (
        normalized["Fund"].isna().any()
        or normalized["Fund"].astype(str).str.strip().eq("").any()
    ):
        raise ValueError("Every mutual-fund row must include a fund name.")
    normalized["Fund"] = normalized["Fund"].astype(str).str.strip()
    if (
        not normalized[["Units", "Avg_NAV", "Latest_NAV"]]
        .map(
            lambda value: pd.notna(value)
            and value < float("inf")
            and value > float("-inf")
        )
        .all()
        .all()
        or normalized["Units"].lt(0).any()
        or normalized["Avg_NAV"].lt(0).any()
        or normalized["Latest_NAV"].le(0).any()
    ):
        raise ValueError("The CSV contains invalid units, cost NAV, or latest NAV.")
    normalized["Folio"] = (
        normalized["Folio"].fillna("").astype(str) if "Folio" in normalized else ""
    )
    normalized["ISIN"] = (
        normalized["ISIN"].fillna("").astype(str) if "ISIN" in normalized else ""
    )
    normalized["NAV_Date"] = (
        normalized["NAV_Date"].fillna("").astype(str)
        if "NAV_Date" in normalized
        else ""
    )
    return normalized[_MUTUAL_FUND_COLUMNS]


def render_mutual_funds(
    holdings: pd.DataFrame, error: str | None
) -> None:
    st.caption(
        "Broker-provided holdings use the latest reported NAV, which may be from "
        "the previous valuation day. A CSV import can be used where the broker "
        "does not provide a funds endpoint. Live mutual-fund holdings are available "
        "from Upstox and Zerodha; Angel One and Dhan require a statement import."
    )
    upload = st.file_uploader(
        "Import a mutual-fund statement CSV",
        type=["csv"],
        key="mutual_fund_csv",
        help=(
            "Required columns: Fund, Units, Avg_NAV, Latest_NAV. "
            "Folio, ISIN, and NAV_Date are optional."
        ),
    )
    if upload is not None:
        content = upload.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        if digest != st.session_state.mutual_fund_upload_digest:
            try:
                imported = validate_mutual_funds(pd.read_csv(io.BytesIO(content)))
            except (
                pd.errors.ParserError,
                UnicodeDecodeError,
                TypeError,
                ValueError,
            ) as exc:
                st.error(f"CSV import failed: {exc}")
            else:
                st.session_state.mutual_funds = imported
                st.session_state.mutual_fund_upload_digest = digest
                st.session_state.mutual_fund_upload_error = None
                st.rerun()
    if st.session_state.mutual_fund_upload_error:
        st.error(st.session_state.mutual_fund_upload_error)
    if error:
        st.info(error)
    if holdings.empty:
        if not error:
            st.info("No mutual-fund holdings were returned by the connected broker.")
        return
    display = holdings.copy()
    display["Invested_Value"] = display["Units"] * display["Avg_NAV"]
    display["Current_Value"] = display["Units"] * display["Latest_NAV"]
    display["P&L"] = display["Current_Value"] - display["Invested_Value"]
    display["Returns_%"] = display.apply(
        lambda row: row["P&L"] / row["Invested_Value"] * 100
        if row["Invested_Value"]
        else 0.0,
        axis=1,
    )
    theme = getattr(st.context.theme, "type", "light")
    st.dataframe(style_returns(display, theme=theme), hide_index=True)


def render_mutual_funds_workspace(
    holdings: pd.DataFrame,
    error: str | None,
    *,
    run_analysis: Callable[..., None],
    show_approved_trades: Callable[[str], None],
    render_local_asset_research: Callable[..., None],
) -> None:
    from ..analysis.panel import render_analysis_panel

    render_mutual_funds(holdings, error)
    st.subheader("Mutual-fund analysis")
    render_analysis_panel(
        "mutual_funds",
        "mutual funds",
        run_analysis,
        show_approved_trades,
    )
    with st.expander("Market research", icon=":material/query_stats:"):
        render_local_asset_research(
            "Mutual Funds", holdings, mode="research"
        )
    with st.expander("Market scanner", icon=":material/filter_list:"):
        render_local_asset_research(
            "Mutual Funds", holdings, mode="scanner"
        )

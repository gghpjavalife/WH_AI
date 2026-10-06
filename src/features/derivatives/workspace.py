"""Options and futures capability disclosure."""

from collections.abc import Callable

import streamlit as st

from ..analysis.panel import render_analysis_panel


def render_derivatives(instrument_type: str) -> None:
    st.info(
        f"{instrument_type} positions are not separately classified by the current "
        "broker adapters. The app does not yet model verified contract metadata, "
        "expiries, option chains, margin requirements, or instrument-specific "
        "quotes, so it will not display estimated positions or offer live orders here."
    )
    st.caption(
        "Open derivative positions returned in the broker's generic positions feed "
        "may currently be included under Trading. This section can be enabled when "
        "the selected broker's contract and order feeds are integrated."
    )


def render_derivatives_workspace(
    run_analysis: Callable[..., None],
    show_approved_trades: Callable[[str], None],
) -> None:
    render_derivatives("Options and futures")
    st.subheader("Options analysis")
    render_analysis_panel(
        "options", "options", run_analysis, show_approved_trades
    )
    st.subheader("Futures analysis")
    render_analysis_panel(
        "futures", "futures", run_analysis, show_approved_trades
    )
    with st.expander("Market research", icon=":material/query_stats:"):
        st.info(
            "F&O research needs verified option-chain and futures-contract "
            "quotes. The connected broker feed does not currently provide "
            "those inputs, so no underlying-stock indicators are substituted."
        )
        st.caption(
            "Planned F&O research inputs: underlying, expiry, strike, "
            "implied volatility, Greeks, open interest, and bid/ask spread."
        )
    with st.expander("Market scanner", icon=":material/filter_list:"):
        st.info(
            "F&O screening is unavailable until verified contract and quote "
            "data are connected. No trades or estimated contract values are "
            "generated."
        )
        expiry_column, liquidity_column = st.columns(2)
        with expiry_column:
            st.number_input(
                "Maximum days to expiry",
                min_value=0,
                max_value=365,
                value=30,
                disabled=True,
                key="fno_scanner_max_expiry_days",
            )
            st.number_input(
                "Minimum open interest",
                min_value=0,
                max_value=1_000_000_000,
                value=100_000,
                disabled=True,
                key="fno_scanner_minimum_open_interest",
            )
        with liquidity_column:
            st.number_input(
                "Minimum contract volume",
                min_value=0,
                max_value=1_000_000_000,
                value=10_000,
                disabled=True,
                key="fno_scanner_minimum_volume",
            )
            st.number_input(
                "Maximum bid/ask spread (%)",
                min_value=0.0,
                max_value=100.0,
                value=1.0,
                disabled=True,
                key="fno_scanner_maximum_spread",
            )
        st.caption(
            "These contract-specific filters activate when verified expiry, "
            "open-interest, volume, and quote feeds are integrated."
        )

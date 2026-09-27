"""Options and futures capability disclosure."""

import streamlit as st


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

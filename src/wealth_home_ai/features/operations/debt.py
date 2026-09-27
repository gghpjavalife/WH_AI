"""User-managed debt portfolio view."""

import pandas as pd
import streamlit as st

from ..portfolio import empty_debt_holdings, validate_debt_holdings


def render_debt(debt_holdings: pd.DataFrame) -> None:
    st.caption(
        "Debt assets are entered manually because the connected broker does not "
        "provide a debt-holdings feed. Values remain in this browser session."
    )
    starting_data = debt_holdings if not debt_holdings.empty else empty_debt_holdings()
    edited = st.data_editor(
        starting_data,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_config={
            "Instrument": st.column_config.TextColumn("Instrument", required=True),
            "Principal": st.column_config.NumberColumn(
                "Principal (₹)", min_value=0.0, format="₹%.2f"
            ),
            "Current_Value": st.column_config.NumberColumn(
                "Current value (₹)", min_value=0.0, format="₹%.2f"
            ),
            "Annual_Rate_%": st.column_config.NumberColumn(
                "Annual rate (%)", min_value=0.0, format="%.2f%%"
            ),
            "Maturity_Date": st.column_config.TextColumn(
                "Maturity date (optional)", help="Use YYYY-MM-DD when applicable."
            ),
        },
        key="debt_holdings_editor",
    )
    if st.button("Save debt holdings", key="save_debt_holdings"):
        try:
            st.session_state.debt_holdings = validate_debt_holdings(edited)
        except (TypeError, ValueError) as error:
            st.error(f"Debt holdings were not saved: {error}")
        else:
            st.success("Debt holdings saved for this session.")
            st.rerun()
    if debt_holdings.empty:
        st.caption("Add a row above to include fixed deposits, bonds, or other debt assets.")
    else:
        total_principal = float(debt_holdings["Principal"].sum())
        total_value = float(debt_holdings["Current_Value"].sum())
        annual_interest = float(
            (
                debt_holdings["Principal"]
                * debt_holdings["Annual_Rate_%"]
                / 100
            ).sum()
        )
        with st.container(horizontal=True):
            st.metric("Principal", f"₹{total_principal:,.2f}", border=True)
            st.metric("Current value", f"₹{total_value:,.2f}", border=True)
            st.metric("Indicative annual interest", f"₹{annual_interest:,.2f}", border=True)
        st.dataframe(debt_holdings, hide_index=True)

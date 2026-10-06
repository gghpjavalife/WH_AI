"""Shared application footer."""

import streamlit as st


def render_footer() -> None:
    with st.container(
        horizontal=True,
        horizontal_alignment="distribute",
        vertical_alignment="center",
    ):
        st.caption("GGHP · Private session")
        st.caption("Informational only; not investment advice.")

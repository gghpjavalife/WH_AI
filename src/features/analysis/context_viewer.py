"""On-demand access to the complete locally retained stock context."""

from __future__ import annotations

import json
from typing import Any

import streamlit as st


def render_context_viewer(results: dict[str, dict[str, Any]], *, key: str) -> None:
    available = {label: result for label, result in results.items()
                 if isinstance(result, dict) and isinstance(result.get("context"), dict)}
    if not available:
        return
    panel = st.expander("Full source data and context", key=f"{key}_context_viewer",
                        on_change="rerun", icon=":material/database:")
    if not panel.open:
        return
    with panel:
        st.caption("Complete provider data for one selected stock; missing values are not estimates.")
        label = st.selectbox("Stock", sorted(available), key=f"{key}_context_viewer_stock")
        result = available[label]
        data = {"context": result["context"], "local_gatekeeper": result["gate"],
                "updated_at": result.get("updated_at"), "notice": result.get("notice")}
        st.json(data, expanded=False)
        st.download_button(
            "Download full context JSON",
            data=json.dumps(data, ensure_ascii=False, allow_nan=False),
            file_name="stock_context.json",
            mime="application/json",
            key=f"{key}_context_viewer_download",
        )

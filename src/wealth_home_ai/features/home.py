"""Portfolio home summary and interactive allocation visualization."""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .portfolio import calculate_allocation

ALLOCATION_DESCRIPTIONS = {
    "Equity": "Broker-reported long-term equity holdings",
    "Trading": "Open broker-reported trading positions (gross exposure)",
    "Mutual funds": "Units valued at each fund's latest reported NAV",
    "Options": "Not separately classified by the current broker adapter",
    "Futures": "Not separately classified by the current broker adapter",
}
ALLOCATION_COLORS = {
    "dark": {
        "Equity": "#2563EB",
        "Trading": "#7C3AED",
        "Mutual funds": "#B45309",
        "Options": "#BE185D",
        "Futures": "#475569",
    },
    "light": {
        "Equity": "#1D4ED8",
        "Trading": "#6D28D9",
        "Mutual funds": "#B45309",
        "Options": "#BE185D",
        "Futures": "#475569",
    },
}


def build_allocation_chart(
    metrics: dict[str, Any], theme_type: str = "dark"
) -> go.Figure:
    """Build a single hoverable, normalized stacked bar for the portfolio mix."""
    palette = ALLOCATION_COLORS.get(theme_type, ALLOCATION_COLORS["dark"])
    font_color = "#1F2937" if theme_type == "light" else "#E2E8F0"
    total = metrics["total_investments"]
    chart = go.Figure()
    for asset_name, value in metrics["allocation"].items():
        if value <= 0:
            continue
        share = value / total * 100 if total else 0.0
        chart.add_trace(
            go.Bar(
                name=asset_name,
                x=[share],
                y=["Portfolio"],
                orientation="h",
                marker={
                    "color": palette[asset_name],
                    "line": {
                        "color": (
                            "rgba(255,255,255,0.24)"
                            if theme_type == "dark"
                            else "rgba(15,23,42,0.18)"
                        ),
                        "width": 1,
                    },
                },
                customdata=[
                    [
                        value,
                        share,
                        metrics["asset_counts"][asset_name],
                        metrics["pnl_by_asset"][asset_name],
                        ALLOCATION_DESCRIPTIONS[asset_name],
                    ]
                ],
                text=[f"{asset_name} · {share:.1f}%" if share >= 12 else ""],
                textposition="inside",
                insidetextanchor="middle",
                textfont={
                    "family": "Inter, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
                    "size": 13,
                    "color": "#FFFFFF",
                },
                hovertemplate=(
                    "<b>%{fullData.name}</b><br>"
                    "Value: ₹%{customdata[0]:,.2f}<br>"
                    "Portfolio share: %{customdata[1]:.1f}%<br>"
                    "Positions/funds: %{customdata[2]}<br>"
                    "P&L: ₹%{customdata[3]:,.2f}<br>"
                    "%{customdata[4]}<extra></extra>"
                ),
            )
        )
    chart.update_layout(
        font={
            "family": "Inter, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
            "size": 13,
            "color": font_color,
        },
        barmode="stack",
        barnorm="percent",
        transition={"duration": 650, "easing": "cubic-in-out"},
        height=135,
        margin={"l": 8, "r": 8, "t": 0, "b": 0},
        showlegend=True,
        legend={
            "orientation": "h",
            "y": -0.16,
            "x": 0,
            "xanchor": "left",
            "font": {"size": 12, "color": font_color},
            "itemsizing": "constant",
            "traceorder": "normal",
        },
        xaxis={
            "visible": False,
            "range": [0, 100],
            "fixedrange": True,
        },
        yaxis={"visible": False, "fixedrange": True},
        uniformtext={"minsize": 11, "mode": "hide"},
        bargap=0.55,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return chart


def render_home_dashboard(
    balance: float,
    equity_holdings: pd.DataFrame,
    trading_positions: pd.DataFrame,
    mutual_funds: pd.DataFrame,
) -> dict[str, Any]:
    metrics = calculate_allocation(
        balance,
        equity_holdings,
        trading_positions,
        mutual_funds,
    )
    with st.container(horizontal=True):
        st.metric(
            "Invested",
            f"₹{metrics['invested_amount']:,.2f}",
            border=True,
            icon=":material/savings:",
            help="Total cost basis across supported asset classes.",
        )
        st.metric(
            "Total portfolio",
            f"₹{metrics['total_portfolio_value']:,.2f}",
            border=True,
            icon=":material/account_balance_wallet:",
            help="Current asset value plus available broker cash.",
        )
        st.metric(
            "Available cash",
            f"₹{metrics['cash']:,.2f}",
            border=True,
            icon=":material/payments:",
        )
        st.metric(
            "Combined P&L",
            f"₹{metrics['total_pnl']:,.2f}",
            border=True,
            icon=":material/trending_up:",
        )

    with st.container(gap="small"):
        st.subheader("Allocation")
        if metrics["total_investments"] > 0:
            theme_type = getattr(st.context.theme, "type", "dark")
            chart = build_allocation_chart(metrics, theme_type)
            st.plotly_chart(chart, width="stretch", key="portfolio_allocation")
        else:
            st.info("Connect and sync a broker to see allocation.")

    st.caption(
        "Hover any segment for value, share, position count, and P&L. Cash is "
        "separate; mutual funds use the latest reported NAV. "
        "Options and futures are not separately classified by the "
        "connected broker yet."
    )
    return metrics

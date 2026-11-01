"""Portfolio home summary and interactive allocation visualization."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ..analysis.panel import render_analysis_brief
from ..equities.workspace import build_equity_holdings_display
from ..portfolio.metrics import calculate_allocation
from ..shared.performance import style_returns
from ui.helpers import workspace_subtabs_css

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

    return metrics


def _render_home_holdings(
    equity_holdings: pd.DataFrame,
    trading_positions: pd.DataFrame,
    mutual_funds: pd.DataFrame,
    metrics: dict[str, Any],
) -> None:
    total_investments = metrics["total_investments"]
    allocations = metrics["allocation"]
    fno_value = allocations["Options"] + allocations["Futures"]

    def share(value: float) -> float:
        return (
            value / total_investments * 100
            if total_investments > 0
            else 0.0
        )

    tab_labels = [
        f"Equities · {share(allocations['Equity']):.1f}%",
        f"Trading · {share(allocations['Trading']):.1f}%",
        f"Mutual Funds · {share(allocations['Mutual funds']):.1f}%",
        f"F&O · {share(fno_value):.1f}%",
    ]
    theme = getattr(st.context.theme, "type", "light")

    with st.container(key="home_holdings_tabs"):
        st.html(workspace_subtabs_css("home_holdings_tabs"))
        equity_tab, trading_tab, funds_tab, fno_tab = st.tabs(tab_labels)

        with equity_tab:
            if equity_holdings.empty:
                st.info("No equity holdings were returned by the connected broker.")
            else:
                equity_display = build_equity_holdings_display(
                    equity_holdings, total_investments
                )
                st.dataframe(
                    style_returns(equity_display, theme=theme), hide_index=True
                )

        with trading_tab:
            if trading_positions.empty:
                st.info("The broker returned no open trading positions.")
            else:
                trading_display = trading_positions.copy()
                trading_display["Gross_Exposure"] = (
                    trading_display["Qty"].abs() * trading_display["LTP"]
                )
                trading_display["P&L"] = trading_display["Qty"] * (
                    trading_display["LTP"] - trading_display["Avg_Price"]
                )
                investment = (
                    trading_display["Qty"].abs() * trading_display["Avg_Price"]
                )
                trading_display["Returns_%"] = (
                    trading_display["P&L"]
                    .div(investment.where(investment.ne(0)))
                    .fillna(0.0)
                    * 100
                )
                trading_display["Allocation_%"] = (
                    trading_display["Gross_Exposure"]
                    .div(total_investments)
                    .fillna(0.0)
                    * 100
                    if total_investments > 0
                    else 0.0
                )
                st.dataframe(
                    style_returns(trading_display, theme=theme), hide_index=True
                )

        with funds_tab:
            if mutual_funds.empty:
                st.info("No mutual-fund holdings were returned by the connected broker.")
            else:
                fund_display = mutual_funds.copy()
                fund_display["Invested_Value"] = (
                    fund_display["Units"] * fund_display["Avg_NAV"]
                )
                fund_display["Current_Value"] = (
                    fund_display["Units"] * fund_display["Latest_NAV"]
                )
                fund_display["P&L"] = (
                    fund_display["Current_Value"] - fund_display["Invested_Value"]
                )
                fund_display["Returns_%"] = fund_display.apply(
                    lambda row: row["P&L"] / row["Invested_Value"] * 100
                    if row["Invested_Value"]
                    else 0.0,
                    axis=1,
                )
                fund_display["Allocation_%"] = (
                    fund_display["Current_Value"]
                    .div(total_investments)
                    .fillna(0.0)
                    * 100
                    if total_investments > 0
                    else 0.0
                )
                st.dataframe(
                    style_returns(fund_display, theme=theme), hide_index=True
                )

        with fno_tab:
            st.info(
                "Options and futures are not separately classified by the "
                "connected broker yet."
            )


def render_home_workspace(
    balance: float,
    equity_holdings: pd.DataFrame,
    trading_positions: pd.DataFrame,
    mutual_funds: pd.DataFrame,
    *,
    live_prices: dict[str, float],
    live_prices_updated_at: datetime | None,
    run_portfolio_analysis: Callable[[], None],
    render_portfolio_review: Callable[[], None],
) -> None:
    metrics = render_home_dashboard(
        balance, equity_holdings, trading_positions, mutual_funds
    )
    _render_home_holdings(
        equity_holdings,
        trading_positions,
        mutual_funds,
        metrics,
    )
    if live_prices:
        with st.expander("Live NSE quotes used for AI recommendations"):
            if live_prices_updated_at:
                st.caption(
                    "Broker-reported last traded prices, fetched for this analysis "
                    "at "
                    + live_prices_updated_at.strftime("%d %b %Y, %H:%M:%S IST")
                    + ". Prices are API quotes, not calculated estimates."
                )
            prices = pd.DataFrame(
                [
                    {"Ticker": ticker, "Live_Price": price}
                    for ticker, price in sorted(live_prices.items())
                ]
            )
            st.dataframe(prices, hide_index=True)
    render_analysis_brief(
        st.session_state.get("analysis_results", {}).get("all"),
        title="AI Portfolio Analysis",
        placeholder=True,
        collapsible=True,
        run_analysis=run_portfolio_analysis,
        last_analyzed_at=st.session_state.get("analysis_timestamps", {}).get(
            "all"
        ),
        analysis_metadata=st.session_state.get("analysis_metadata", {}).get("all"),
        storage_error=st.session_state.get("analysis_storage_errors", {}).get(
            "all"
        ),
    )
    render_portfolio_review()

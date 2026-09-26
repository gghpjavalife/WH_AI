"""Wealth Home AI: session-isolated multi-broker portfolio dashboard."""

from __future__ import annotations

import os
import sqlite3
import hashlib
import io
import json
from datetime import datetime, time, timedelta
from html import escape
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from google.genai.errors import APIError

from .broker_factory import (
    BrokerAPIError,
    BrokerCapabilityError,
    BrokerFactory,
    BrokerInterface,
    MUTUAL_FUND_COLUMNS,
    TICKER_MAP,
)
from .jev_rules import JevRuleEngine, SECTOR_BY_TICKER
from .oauth_state_store import consume_oauth_state, create_oauth_state
from .upstox_helper import (
    ask_llm_agent,
    send_telegram_alert,
)

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

APP_TITLE = "🏡 Wealth Home: Safe Multi-Broker Portfolio & AI Companion"
PORTFOLIO_COLUMNS = ["Ticker", "Qty", "Avg_Price", "LTP"]

st.set_page_config(
    page_title=APP_TITLE,
    layout="wide",
    initial_sidebar_state="expanded",
)

st.session_state.setdefault("broker_state", None)
st.session_state.setdefault("broker_name", None)
st.session_state.setdefault("token", None)
st.session_state.setdefault(
    "portfolio", pd.DataFrame(columns=PORTFOLIO_COLUMNS)
)
st.session_state.setdefault(
    "equity_holdings", pd.DataFrame(columns=PORTFOLIO_COLUMNS)
)
st.session_state.setdefault(
    "mutual_funds", pd.DataFrame(columns=MUTUAL_FUND_COLUMNS)
)
st.session_state.setdefault("mutual_fund_error", None)
st.session_state.setdefault("mutual_fund_updated_at", None)
st.session_state.setdefault("mutual_fund_manual_import", False)
st.session_state.setdefault("mutual_fund_upload_digest", None)
st.session_state.setdefault("mutual_fund_upload_error", None)
st.session_state.setdefault("balance", 0.0)
st.session_state.setdefault("last_sync", None)
st.session_state.setdefault("last_sync_attempt", None)
st.session_state.setdefault("sync_failed", False)
st.session_state.setdefault("alerted_risk_tickers", [])
st.session_state.setdefault("risk_high_water", {})
st.session_state.setdefault("risk_alerts", [])
st.session_state.setdefault("risk_errors", [])
st.session_state.setdefault("telegram_chat_id", "")
st.session_state.setdefault("analysis_result", None)
st.session_state.setdefault("planning_budget_inr", None)
st.session_state.setdefault("analysis_live_prices", {})
st.session_state.setdefault("analysis_live_prices_updated_at", None)
st.session_state.setdefault("analysis_price_error", None)
st.session_state.setdefault("approved_trades", [])
st.session_state.setdefault("audit_trail", [])
st.session_state.setdefault("login_error", None)

st.sidebar.title("🏡 Wealth Home AI")
st.sidebar.caption("Private broker workspace")
st.sidebar.markdown("---")


def _redirect_url() -> str:
    configured = os.environ.get("UPSTOX_REDIRECT_URI", "").strip()
    if configured:
        return configured
    current_url = str(st.context.url or "")
    if current_url:
        parts = urlsplit(current_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
    return "http://localhost:8501"


def _is_trading_session() -> bool:
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    return now.weekday() < 5 and time(9, 15) <= now.time() <= time(15, 30)


def _fetch_market_risk(
    adapter: BrokerInterface, token: str, ticker: str
) -> dict[str, float]:
    return adapter.fetch_market_risk(token, ticker)


def _clear_sensitive_angel_inputs() -> None:
    for key in (
        "angel_api_key",
        "angel_client_id",
        "angel_password",
        "angel_totp_secret",
    ):
        if key in st.session_state:
            del st.session_state[key]


def _authenticate_angel_one() -> None:
    adapter = BrokerFactory.create_adapter(
        "Angel One",
        api_key=st.session_state.get("angel_api_key") or None,
        client_id=st.session_state.get("angel_client_id") or None,
        password=st.session_state.get("angel_password") or None,
        totp_secret=st.session_state.get("angel_totp_secret") or None,
    )
    try:
        token = adapter.authenticate(None, "")
    except (BrokerAPIError, requests.RequestException, ValueError):
        st.session_state.login_error = (
            "Angel One could not authenticate. Check your credentials, TOTP clock, "
            "and network connection."
        )
    else:
        st.session_state.token = token
        st.session_state.broker_state = adapter
        st.session_state.broker_name = "Angel One"
        st.session_state.last_sync = None
        st.session_state.last_sync_attempt = None
        st.session_state.alerted_risk_tickers = []
        st.session_state.risk_high_water = {}
        st.session_state.analysis_result = None
        st.session_state.approved_trades = []
        st.session_state.mutual_fund_manual_import = True
        st.session_state.login_error = None
    finally:
        _clear_sensitive_angel_inputs()


def _show_login() -> None:
    st.title(APP_TITLE)
    st.subheader("Onboarding portal")
    st.info(
        "AI suggestions are informational only and are not investment advice. "
        "Broker/API availability, hosting quotas, and exchange or broker charges "
        "depend on their providers."
    )
    broker_choice = st.selectbox(
        "Select broker",
        ["Upstox", "Angel One"],
        key="selected_broker",
    )

    if st.session_state.login_error:
        st.error(st.session_state.login_error)
        st.session_state.login_error = None

    if broker_choice == "Upstox":
        adapter = BrokerFactory.create_adapter("Upstox")
        if not adapter.configured:
            st.warning(
                "Configure UPSTOX_API_KEY and UPSTOX_API_SECRET in the server "
                "environment before connecting."
            )
        else:
            redirect_url = _redirect_url()
            st.caption(f"Registered callback URL: {redirect_url}")
            if st.button("Connect with Upstox", type="primary"):
                try:
                    login_url = adapter.get_login_url(redirect_url)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    try:
                        if adapter.oauth_state is None:
                            raise ValueError("Upstox did not create an OAuth state.")
                        create_oauth_state(adapter.oauth_state)
                    except (sqlite3.Error, OSError, ValueError):
                        st.error(
                            "Could not safely start Upstox sign-in. Check that the "
                            "application can write its local OAuth state database."
                        )
                    else:
                        st.html(
                            "<a href=\""
                            + escape(login_url, quote=True)
                            + "\" target=\"_self\" rel=\"noopener noreferrer\">"
                            "Continue to Upstox sign-in</a>"
                        )

            authorization_code = st.query_params.get("code")
            oauth_error = st.query_params.get("error")
            if oauth_error:
                st.error("Upstox sign-in was cancelled or rejected.")
                st.query_params.clear()
            if authorization_code and not oauth_error:
                returned_state = st.query_params.get("state")
                try:
                    state_is_valid = consume_oauth_state(returned_state)
                except (sqlite3.Error, OSError, ValueError):
                    st.error(
                        "Could not verify the Upstox callback because the local "
                        "OAuth state store is unavailable."
                    )
                    st.query_params.clear()
                else:
                    if not state_is_valid:
                        st.error(
                            "Upstox sign-in could not be verified. Its one-time "
                            "verification may have expired or already been used. "
                            "Reconnect and complete sign-in within 10 minutes."
                        )
                        st.query_params.clear()
                    else:
                        try:
                            token = adapter.authenticate(
                                str(authorization_code), redirect_url
                            )
                        except (BrokerAPIError, requests.RequestException, ValueError):
                            st.error(
                                "Upstox authentication failed. Verify the registered "
                                "callback URL and try again."
                            )
                            st.query_params.clear()
                        else:
                            st.session_state.token = token
                            st.session_state.broker_state = adapter
                            st.session_state.broker_name = "Upstox"
                            st.session_state.last_sync = None
                            st.session_state.last_sync_attempt = None
                            st.session_state.alerted_risk_tickers = []
                            st.session_state.risk_high_water = {}
                            st.session_state.analysis_result = None
                            st.session_state.approved_trades = []
                            st.query_params.clear()
                            st.rerun()

    else:
        st.caption(
            "Angel One uses programmatic SmartAPI sign-in. Your client ID, password, "
            "and TOTP secret are cleared after login; the API key stays in this session."
        )
        api_key_from_environment = os.environ.get("ANGEL_API_KEY", "").strip()
        if api_key_from_environment:
            st.caption("Angel One API key loaded from server environment.")
        with st.form("angel_one_login"):
            if not api_key_from_environment:
                st.text_input("Angel One API key", type="password", key="angel_api_key")
            st.text_input("Client ID", key="angel_client_id")
            st.text_input("Password", type="password", key="angel_password")
            st.text_input(
                "TOTP secret",
                type="password",
                key="angel_totp_secret",
                help="The secret used to generate your current one-time password.",
            )
            st.form_submit_button(
                "Connect with Angel One",
                type="primary",
                on_click=_authenticate_angel_one,
            )


def _position_pnl(portfolio: pd.DataFrame) -> tuple[float, float]:
    if portfolio.empty:
        return 0.0, 0.0
    quantity = pd.to_numeric(portfolio["Qty"], errors="coerce").fillna(0.0)
    average = pd.to_numeric(portfolio["Avg_Price"], errors="coerce").fillna(0.0)
    ltp = pd.to_numeric(portfolio["LTP"], errors="coerce").fillna(0.0)
    total_pnl = float((quantity * (ltp - average)).sum())
    invested = float((quantity.abs() * average).sum())
    return total_pnl, (total_pnl / invested * 100 if invested else 0.0)


def _mutual_fund_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if not set(("Fund", "Units", "Avg_NAV", "Latest_NAV")).issubset(frame.columns):
        raise ValueError(
            "The CSV must include Fund, Units, Avg_NAV, and Latest_NAV columns."
        )
    normalized = frame.copy()
    for column in ("Units", "Avg_NAV", "Latest_NAV"):
        normalized[column] = pd.to_numeric(normalized[column], errors="raise")
    if normalized["Fund"].isna().any() or normalized["Fund"].astype(str).str.strip().eq("").any():
        raise ValueError("Every mutual-fund row must include a fund name.")
    if (
        not normalized[["Units", "Avg_NAV", "Latest_NAV"]]
        .map(lambda value: pd.notna(value) and value < float("inf") and value > float("-inf"))
        .all()
        .all()
        or normalized["Units"].lt(0).any()
        or normalized["Avg_NAV"].lt(0).any()
        or normalized["Latest_NAV"].le(0).any()
    ):
        raise ValueError("The CSV contains invalid units, cost NAV, or latest NAV.")
    normalized["Folio"] = (
        normalized["Folio"].fillna("").astype(str)
        if "Folio" in normalized
        else ""
    )
    normalized["NAV_Date"] = (
        normalized["NAV_Date"].fillna("").astype(str)
        if "NAV_Date" in normalized
        else ""
    )
    return normalized[MUTUAL_FUND_COLUMNS]


def _decorate_equities(portfolio: pd.DataFrame) -> pd.DataFrame:
    if portfolio.empty:
        return portfolio.copy()
    display = portfolio.copy()
    display["Investment"] = display["Qty"].abs() * display["Avg_Price"]
    display["Market_Value"] = display["Qty"] * display["LTP"]
    display["P&L"] = display["Qty"] * (display["LTP"] - display["Avg_Price"])
    display["Returns_%"] = display.apply(
        lambda row: row["P&L"] / row["Investment"] * 100
        if row["Investment"]
        else 0.0,
        axis=1,
    )
    return display


def _decorate_mutual_funds(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    display = frame.copy()
    display["Invested_Value"] = display["Units"] * display["Avg_NAV"]
    display["Current_Value"] = display["Units"] * display["Latest_NAV"]
    display["P&L"] = display["Current_Value"] - display["Invested_Value"]
    display["Returns_%"] = display.apply(
        lambda row: row["P&L"] / row["Invested_Value"] * 100
        if row["Invested_Value"]
        else 0.0,
        axis=1,
    )
    return display


@st.fragment(run_every=300)
def polling_sequence() -> None:
    token = st.session_state.token
    adapter = st.session_state.broker_state
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    should_sync = token and adapter and (
        st.session_state.last_sync_attempt is None
        or (
            _is_trading_session()
            and now - st.session_state.last_sync_attempt >= timedelta(minutes=5)
        )
    )
    if should_sync:
        st.session_state.last_sync_attempt = now
        try:
            balance = adapter.fetch_balance(token)
            portfolio = adapter.fetch_positions(token)
            equity_holdings = adapter.fetch_holdings(token)
        except (BrokerAPIError, requests.RequestException, ValueError, RuntimeError):
            st.session_state.sync_failed = True
        else:
            st.session_state.balance = balance
            st.session_state.portfolio = portfolio
            st.session_state.equity_holdings = equity_holdings
            st.session_state.last_sync = datetime.now(ZoneInfo("Asia/Kolkata"))
            st.session_state.sync_failed = False
            try:
                mutual_funds = adapter.fetch_mutual_fund_holdings(token)
            except BrokerCapabilityError as error:
                st.session_state.mutual_fund_error = str(error)
                st.session_state.mutual_fund_manual_import = True
            except (
                BrokerAPIError,
                requests.RequestException,
                ValueError,
                RuntimeError,
            ):
                st.session_state.mutual_fund_manual_import = True
                st.session_state.mutual_fund_error = (
                    "Mutual-fund holdings are temporarily unavailable from this broker."
                )
            else:
                broker_funds = _mutual_fund_frame(mutual_funds)
                if broker_funds.empty:
                    st.session_state.mutual_fund_error = (
                        "The broker returned no mutual-fund rows. You can still add "
                        "them with the statement CSV importer."
                    )
                    st.session_state.mutual_fund_manual_import = True
                else:
                    st.session_state.mutual_funds = broker_funds
                    st.session_state.mutual_fund_error = None
                    st.session_state.mutual_fund_manual_import = False
                    st.session_state.mutual_fund_updated_at = datetime.now(
                        ZoneInfo("Asia/Kolkata")
                    )

    balance = float(st.session_state.balance)
    portfolio = st.session_state.portfolio
    equity_holdings = st.session_state.equity_holdings
    mutual_funds = st.session_state.mutual_funds
    holdings_pnl, holdings_return = _position_pnl(equity_holdings)
    positions_pnl, positions_return = _position_pnl(portfolio)
    mutual_funds_display = _decorate_mutual_funds(mutual_funds)
    mutual_funds_pnl = (
        float(mutual_funds_display["P&L"].sum())
        if not mutual_funds_display.empty
        else 0.0
    )
    total_pnl = holdings_pnl + positions_pnl + mutual_funds_pnl
    equity_market_value = (
        float((equity_holdings["Qty"] * equity_holdings["LTP"]).sum())
        if not equity_holdings.empty
        else 0.0
    )
    if not portfolio.empty:
        equity_market_value += float((portfolio["Qty"] * portfolio["LTP"]).sum())
    mutual_funds_market_value = (
        float(mutual_funds_display["Current_Value"].sum())
        if not mutual_funds_display.empty
        else 0.0
    )
    total_market_value = equity_market_value + mutual_funds_market_value
    invested_value = (
        float((equity_holdings["Qty"].abs() * equity_holdings["Avg_Price"]).sum())
        if not equity_holdings.empty
        else 0.0
    )
    if not portfolio.empty:
        invested_value += float(
            (portfolio["Qty"].abs() * portfolio["Avg_Price"]).sum()
        )
    if not mutual_funds_display.empty:
        invested_value += float(mutual_funds_display["Invested_Value"].sum())
    total_return = total_pnl / invested_value * 100 if invested_value else 0.0
    (
        metric_balance,
        metric_market_value,
        metric_pnl,
        metric_holdings,
        metric_positions,
        metric_mfs,
    ) = st.columns(6)
    metric_balance.metric("Available balance", f"₹{balance:,.2f}")
    metric_market_value.metric("Invested market value", f"₹{total_market_value:,.2f}")
    metric_pnl.metric(
        "Live portfolio P&L",
        f"₹{total_pnl:,.2f}",
        delta=f"{total_return:.2f}% absolute return",
    )
    metric_holdings.metric("Equity holdings", len(equity_holdings))
    metric_positions.metric("Trading positions", len(portfolio))
    metric_mfs.metric("Mutual funds", len(mutual_funds))

    st.caption(
        f"Equity P&L: ₹{holdings_pnl + positions_pnl:,.2f} "
        f"(holdings {holdings_return:.2f}%, trading positions {positions_return:.2f}%) "
        f"· Mutual-fund P&L: ₹{mutual_funds_pnl:,.2f}. "
        "Equity LTPs are broker quotes; mutual funds use their latest reported NAV."
    )

    if st.session_state.sync_failed:
        st.caption("Broker refresh unavailable; displaying the last successful data.")
    elif st.session_state.last_sync:
        st.caption(
            "Last broker sync: "
            f"{st.session_state.last_sync.strftime('%d %b %Y, %H:%M:%S IST')}"
        )

    if not equity_holdings.empty:
        st.subheader("Long-term equity holdings")
        st.dataframe(_decorate_equities(equity_holdings), hide_index=True)
    if not portfolio.empty:
        st.subheader("Trading positions")
        st.dataframe(_decorate_equities(portfolio), hide_index=True)
    if not mutual_funds_display.empty:
        st.subheader("Mutual-fund holdings")
        st.dataframe(mutual_funds_display, hide_index=True)
        st.caption(
            "Mutual-fund valuation uses the broker's latest reported NAV, which "
            "may be from the previous valuation day rather than a live quote."
        )
    if st.session_state.mutual_fund_error:
        st.info(st.session_state.mutual_fund_error)
    elif mutual_funds_display.empty:
        st.info("No mutual-fund holdings were returned by the connected broker.")

    risk_rows = st.session_state.risk_alerts
    risk_errors = st.session_state.risk_errors
    high_water = st.session_state.risk_high_water
    risk_portfolio = equity_holdings.copy()
    known_equity_tickers = set(
        risk_portfolio["Ticker"].astype(str).str.upper()
    ) if not risk_portfolio.empty else set()
    if not portfolio.empty:
        additional_positions = portfolio[
            ~portfolio["Ticker"].astype(str).str.upper().isin(known_equity_tickers)
        ]
        risk_portfolio = pd.concat(
            [risk_portfolio, additional_positions], ignore_index=True
        )
    held_tickers = set(risk_portfolio["Ticker"].astype(str).str.upper())
    for ticker in list(high_water):
        if ticker not in held_tickers:
            del high_water[ticker]
    if should_sync:
        risk_rows = []
        risk_errors = []
        for row in risk_portfolio.itertuples(index=False):
            ticker = str(row.Ticker).strip().upper()
            try:
                market_risk = _fetch_market_risk(adapter, token, ticker)
            except (
                BrokerAPIError,
                requests.RequestException,
                RuntimeError,
                ValueError,
            ) as error:
                if isinstance(error, requests.RequestException):
                    status_code = (
                        error.response.status_code
                        if error.response is not None
                        else None
                    )
                    reason = (
                        f"HTTP {status_code}"
                        if status_code is not None
                        else "network unavailable"
                    )
                else:
                    reason = str(error)
                risk_errors.append(f"{ticker}: {reason}")
                continue
            high_water[ticker] = max(
                float(high_water.get(ticker, market_risk["LTP"])),
                market_risk["LTP"],
            )
            risk_boundary = high_water[ticker] - 3 * market_risk["ATR"]
            if market_risk["LTP"] < risk_boundary:
                risk_rows.append((ticker, float(row.Qty), risk_boundary))
        st.session_state.risk_alerts = risk_rows
        st.session_state.risk_errors = risk_errors

    if risk_errors:
        st.caption(
            "ATR check unavailable (no exit signal inferred): "
            + "; ".join(sorted(set(risk_errors)))
        )
    st.caption(
        "Risk boundary: highest observed price in this session minus 3 × "
        "14-trading-day ATR. A failed candle/quote request is not treated as a breach."
    )

    breached_tickers = {ticker for ticker, _, _ in risk_rows}
    st.session_state.alerted_risk_tickers = [
        ticker
        for ticker in st.session_state.alerted_risk_tickers
        if ticker in breached_tickers
    ]
    for ticker, quantity, boundary in risk_rows:
        st.error(
            f"🚨 Market Exit warning: {ticker} is below its 3-ATR floor "
            f"(₹{boundary:,.2f}). Review before placing an order."
        )
        if ticker not in st.session_state.alerted_risk_tickers:
            private_chat_id = str(
                st.session_state.get("telegram_chat_id", "")
            ).strip()
            if os.environ.get("TELEGRAM_BOT_TOKEN") and private_chat_id:
                try:
                    send_telegram_alert(
                        f"Market Exit warning: {ticker} fell below its 3-ATR "
                        f"risk boundary of INR {boundary:.2f}.",
                        chat_id=private_chat_id,
                    )
                except (requests.RequestException, RuntimeError):
                    st.caption(f"Telegram alert could not be delivered for {ticker}.")
                else:
                    st.session_state.alerted_risk_tickers.append(ticker)
            else:
                st.caption(
                    "Telegram alert not sent; configure the bot token and this "
                    "session's private Telegram chat ID."
                )
        if st.button(
            f"🚨 Market Exit — {ticker}",
            key=f"market_exit_{ticker}",
            type="primary",
            help="Places a live market order to close this position.",
        ):
            side = "SELL" if quantity > 0 else "BUY"
            try:
                result = adapter.place_order(
                    token, ticker, int(abs(quantity)), side
                )
            except (BrokerAPIError, requests.RequestException, ValueError, RuntimeError):
                st.error(f"The market exit order for {ticker} was not placed.")
            else:
                st.success(f"Market {side.lower()} order submitted for {ticker}.")
                st.write(result)
                st.rerun()


def _run_analysis() -> None:
    raw_budget = st.session_state.get("planning_budget_inr")
    if isinstance(raw_budget, bool):
        st.session_state.analysis_error = "Enter a valid positive scenario budget."
        st.session_state.analysis_result = None
        st.session_state.approved_trades = []
        return
    try:
        planning_budget = float(raw_budget)
    except (TypeError, ValueError):
        planning_budget = 0.0
    if (
        not isfinite(planning_budget)
        or planning_budget <= 0
        or planning_budget > 100_000_000
    ):
        st.session_state.analysis_error = (
            "Enter a scenario budget between ₹1 and ₹10 crore before running analysis."
        )
        st.session_state.analysis_result = None
        st.session_state.approved_trades = []
        st.session_state.audit_trail = []
        return

    positions = st.session_state.portfolio
    equity_holdings = st.session_state.equity_holdings
    mutual_funds = st.session_state.mutual_funds
    portfolio = pd.concat(
        [equity_holdings, positions], ignore_index=True
    ).drop_duplicates(subset=["Ticker", "Qty", "Avg_Price"], keep="last")
    summary = json.dumps(
        {
            "long_term_equity_holdings": equity_holdings.to_dict(orient="records"),
            "open_trading_positions": positions.to_dict(orient="records"),
            "mutual_fund_holdings": mutual_funds.to_dict(orient="records"),
        },
        ensure_ascii=True,
    )
    mode = st.session_state.get("analysis_mode", "Portfolio and cash review")
    context = (
        f"Analysis mode: {mode}. Broker-reported equity holdings, trading "
        "positions, mutual funds when available, actual broker cash, and a separate "
        f"user-selected hypothetical investment budget of INR {planning_budget:,.2f} "
        "are supplied. Size the scenario recommendations to the user-selected "
        "budget, not to broker cash. The scenario budget does not represent cash "
        "actually available in the brokerage account. "
        "Fund NAVs are the broker's last reported NAV and may not be live. "
        "Do not claim news or data that the application did not supply."
    )
    try:
        live_prices = st.session_state.broker_state.fetch_live_prices(
            st.session_state.token
        )
    except (
        BrokerAPIError,
        requests.RequestException,
        RuntimeError,
        ValueError,
    ):
        live_prices = {}
        st.session_state.analysis_live_prices_updated_at = datetime.now(
            ZoneInfo("Asia/Kolkata")
        )
        st.session_state.analysis_price_error = (
            "Could not retrieve a fresh broker quote. Portfolio analysis will "
            "still run, but the app will not submit unpriced recommendations."
        )
    else:
        st.session_state.analysis_live_prices_updated_at = datetime.now(
            ZoneInfo("Asia/Kolkata")
        )
        st.session_state.analysis_price_error = (
            None
            if live_prices
            else "The broker quote endpoint returned no current prices. Portfolio "
            "analysis will still run; price-based deployments remain disabled."
        )
    st.session_state.analysis_live_prices = live_prices
    context += (
        "\nThe authenticated quote endpoint returned "
        + (
            "current prices for the listed symbols."
            if live_prices
            else "no usable current prices. Provide portfolio analysis only and "
            "return an empty cash_deployment_list; never estimate prices."
        )
    )
    try:
        analysis = ask_llm_agent(
            portfolio_summary=summary,
            available_cash=planning_budget,
            market_context=context,
            live_prices=live_prices,
        )
    except APIError as error:
        st.session_state.analysis_result = None
        st.session_state.approved_trades = []
        st.session_state.audit_trail = []
        detail = error.details
        error_info = detail.get("error", detail) if isinstance(detail, dict) else {}
        error_details = error_info.get("details", [])
        if not isinstance(error_details, list):
            error_details = []
        reasons = {
            item.get("reason")
            for item in error_details
            if isinstance(item, dict)
        }
        status = error_info.get("status", error.status)
        code = error_info.get("code", error.code)
        try:
            http_code = int(code)
        except (TypeError, ValueError):
            http_code = error.code
        if "API_KEY_INVALID" in reasons or status in {
            "UNAUTHENTICATED",
            "PERMISSION_DENIED",
        }:
            message = (
                "Google rejected GEMINI_API_KEY. Create or copy a valid Gemini API "
                "key from Google AI Studio, replace GEMINI_API_KEY in the server "
                "environment or .env file, then restart Streamlit."
            )
        elif http_code == 429 or status == "RESOURCE_EXHAUSTED":
            message = (
                "Gemini API quota or rate limit reached. Check the key's project "
                "and billing/quota in Google AI Studio, then retry later."
            )
        elif http_code == 503 or status == "UNAVAILABLE":
            message = (
                "Gemini is temporarily unavailable (HTTP 503). This normally "
                "indicates a Google-side service/capacity issue, not an invalid API "
                "key. The app retries transient failures automatically; wait a few "
                "minutes and run the analysis again. If it persists, check Google "
                "AI Studio service status and try again later."
            )
        elif http_code in {500, 502, 504} or status == "INTERNAL":
            message = (
                f"Gemini returned a temporary server error (HTTP {http_code}). "
                "The app retries transient failures automatically; wait a few "
                "minutes and run the analysis again."
            )
        elif http_code == 404:
            message = (
                "Gemini model gemini-3.8-flash is unavailable for this API key or "
                "project. Check model access and API enablement in Google AI Studio."
            )
        else:
            message = (
                f"Gemini API request failed (HTTP {http_code or 'unknown'}). Check the "
                "API key, model access, project quota, and network connection."
            )
        st.session_state.analysis_error = message
        return
    except (RuntimeError, ValueError):
        st.session_state.analysis_result = None
        st.session_state.approved_trades = []
        st.session_state.audit_trail = []
        st.session_state.analysis_error = (
            "Portfolio analysis failed while validating the Gemini response. "
            "Retry, and verify that the Gemini service returned valid JSON."
        )
        return

    sectors_held = {
        SECTOR_BY_TICKER[ticker]
        for ticker in portfolio["Ticker"].astype(str).str.upper()
        if ticker in SECTOR_BY_TICKER
    } if not portfolio.empty else set()
    engine = JevRuleEngine(
        {
            "cash_balance": planning_budget,
            "actual_broker_balance": float(st.session_state.balance),
            "live_prices": live_prices,
            "sector_holdings": sorted(sectors_held),
            "portfolio": portfolio,
            "llm_targets": analysis["cash_deployment_list"],
        }
    )
    trades, audit_trail = engine.run()
    st.session_state.analysis_result = analysis
    st.session_state.approved_trades = trades
    st.session_state.audit_trail = audit_trail
    st.session_state.analysis_error = None


def _show_approved_trades() -> None:
    for index, trade in enumerate(st.session_state.approved_trades):
        ticker = trade["Ticker"]
        quantity = trade["Qty"]
        current_price = float(
            trade.get(
                "Current_Price",
                st.session_state.analysis_live_prices.get(ticker, 0.0),
            )
        )
        planned_cost = quantity * current_price
        actual_cash = float(st.session_state.balance)
        symbol_is_orderable = ticker in TICKER_MAP
        can_deploy_now = (
            symbol_is_orderable
            and isinstance(quantity, int)
            and quantity > 0
            and current_price > 0
            and planned_cost <= actual_cash
        )
        with st.container(border=True):
            st.markdown(
                f"**{ticker}** · {quantity} share(s) · scenario allocation "
                f"₹{quantity * trade['Entry_Price']:,.2f}"
            )
            st.markdown(
                f"Current broker LTP: ₹{current_price:,.2f} · "
                f"Suggested entry: ₹{trade['Entry_Price']:,.2f} · "
                f"Target: ₹{trade['Target_Price']:,.2f} · "
                f"Stop: ₹{trade['Stop_Loss']:,.2f}"
            )
            st.markdown(
                f"Confidence: {trade['Confidence_Score']:.1f}% · "
                f"Potential reward/risk: {trade['Risk_Reward_Ratio']:.2f}:1 · "
                f"Indicative horizon: {trade['Holding_Period']}"
            )
            st.markdown(f"**Why considered:** {trade.get('Reasoning', '')}")
            st.markdown(f"**Entry idea:** {trade.get('Entry_Rationale', '')}")
            st.markdown(f"**Risk / thesis invalidation:** {trade.get('Risk_Rationale', '')}")
            if not can_deploy_now:
                if not symbol_is_orderable:
                    st.caption(
                        "Scenario-only recommendation: this holding has a live "
                        "quote but is not enabled for order placement by this app."
                    )
                else:
                    st.caption(
                        f"Scenario-only recommendation: ₹{planned_cost:,.2f} "
                        "estimated order value exceeds actual broker cash "
                        f"₹{actual_cash:,.2f}."
                    )
            if st.button(
                "⚡ Instant Deploy",
                key=f"instant_deploy_{ticker}_{index}",
                type="primary",
                disabled=not can_deploy_now,
                help="Submits a live market buy order after rechecking available cash.",
            ):
                if (
                    ticker not in TICKER_MAP
                    or isinstance(quantity, bool)
                    or not isinstance(quantity, int)
                    or quantity <= 0
                    or trade["Confidence_Score"] < 90
                ):
                    st.error("This recommendation is not valid for order execution.")
                    continue
                adapter = st.session_state.broker_state
                token = st.session_state.token
                try:
                    latest_cash = adapter.fetch_balance(token)
                    live_market = _fetch_market_risk(adapter, token, ticker)
                    st.session_state.balance = latest_cash
                    if quantity * live_market["LTP"] > latest_cash:
                        st.error(
                            "The latest live price and broker balance no longer "
                            "support this order quantity."
                        )
                        continue
                    result = adapter.place_order(token, ticker, quantity, "BUY")
                except (
                    BrokerAPIError,
                    requests.RequestException,
                    RuntimeError,
                    ValueError,
                ):
                    st.error(f"The market buy order for {ticker} was not placed.")
                else:
                    st.success(f"Market buy order submitted for {ticker}.")
                    st.write(result)
                    st.session_state.approved_trades = [
                        item
                        for item in st.session_state.approved_trades
                        if item["Ticker"] != ticker
                    ]
                    st.rerun()


def _show_workspace() -> None:
    st.title("🏡 Wealth Home AI")
    st.header("Active workspace")
    st.caption(
        "Recommendations are AI-generated and require your explicit order action. "
        "The scenario budget sizes a hypothetical plan and is separate from actual "
        "broker cash. Market orders can execute at prices different from displayed prices."
    )
    polling_sequence()
    if st.session_state.planning_budget_inr is None:
        st.session_state.planning_budget_inr = max(
            float(st.session_state.balance), 1.0
        )
    with st.sidebar:
        st.subheader("Connected broker")
        st.write(st.session_state.broker_name)
        prompt_mode = st.selectbox(
            "Analysis mode",
            ["Portfolio and cash review", "Risk-aware deployment review"],
            key="analysis_mode",
        )
        st.caption(
            "Gemini analysis is advisory. Rule-engine checks do not guarantee "
            "profit or prevent investment loss."
        )
        st.number_input(
            "Scenario budget for recommendations (INR)",
            min_value=1.0,
            max_value=100_000_000.0,
            step=10_000.0,
            format="%.2f",
            key="planning_budget_inr",
            help=(
                "Used only to size a hypothetical recommendation plan. It does not "
                "change your broker balance or authorize an order."
            ),
        )
        st.caption(
            f"Actual broker cash: ₹{float(st.session_state.balance):,.2f}. "
            "A larger scenario budget is for planning only; live orders still "
            "require sufficient actual broker cash."
        )
        st.caption(
            "Candidates below 90% confidence can appear in the analysis, but the "
            "rule engine will not approve them for a deployment plan."
        )
        telegram_chat_id = st.text_input(
            "Private Telegram chat ID",
            key="telegram_chat_id",
            help="Used only in this browser session for your alerts.",
        )
        if not os.environ.get("TELEGRAM_BOT_TOKEN"):
            st.caption(
                "Telegram alerts require TELEGRAM_BOT_TOKEN in the server environment."
            )
        elif not telegram_chat_id.strip():
            st.caption("Add your chat ID to receive alerts privately.")
        st.caption(
            "If the broker does not return funds, import a statement CSV with "
            "Fund, Units, Avg_NAV, Latest_NAV; Folio and NAV_Date are optional."
        )
        mutual_fund_file = st.file_uploader(
            "Mutual-fund statement CSV",
            type=["csv"],
            key="mutual_fund_csv",
        )
        if mutual_fund_file is not None:
            csv_bytes = mutual_fund_file.getvalue()
            digest = hashlib.sha256(csv_bytes).hexdigest()
            if digest != st.session_state.mutual_fund_upload_digest:
                try:
                    uploaded_frame = pd.read_csv(io.BytesIO(csv_bytes))
                    st.session_state.mutual_funds = _mutual_fund_frame(
                        uploaded_frame
                    )
                except (pd.errors.ParserError, UnicodeDecodeError, ValueError):
                    st.session_state.mutual_fund_upload_error = (
                        "CSV import failed. Use numeric values and the required "
                        "Fund, Units, Avg_NAV, Latest_NAV columns."
                    )
                else:
                    st.session_state.mutual_fund_upload_digest = digest
                    st.session_state.mutual_fund_upload_error = None
                    st.rerun()
        if st.session_state.mutual_fund_upload_error:
            st.error(st.session_state.mutual_fund_upload_error)

    if st.session_state.analysis_live_prices:
        with st.expander("Live NSE quotes used for AI recommendations"):
            if st.session_state.analysis_live_prices_updated_at:
                st.caption(
                    "Broker-reported last traded prices, fetched for this analysis "
                    "at "
                    + st.session_state.analysis_live_prices_updated_at.strftime(
                        "%d %b %Y, %H:%M:%S IST"
                    )
                    + ". Prices are API quotes, not calculated estimates."
                )
            prices = pd.DataFrame(
                [
                    {"Ticker": ticker, "Live_Price": price}
                    for ticker, price in sorted(
                        st.session_state.analysis_live_prices.items()
                    )
                ]
            )
            st.dataframe(prices, hide_index=True)

    st.divider()
    st.button(
        "🔮 Run LLM Portfolio Analysis",
        type="primary",
        on_click=_run_analysis,
    )

    if st.session_state.get("analysis_error"):
        st.error(st.session_state.analysis_error)
    if st.session_state.analysis_price_error:
        st.warning(st.session_state.analysis_price_error)
    analysis = st.session_state.analysis_result
    if analysis:
        st.subheader("Portfolio analysis")
        st.caption(
            f"Scenario recommendations were sized for "
            f"₹{float(st.session_state.planning_budget_inr):,.2f}; actual broker "
            f"cash is ₹{float(st.session_state.balance):,.2f}."
        )
        st.text(analysis["analysis"])
        candidates = analysis["cash_deployment_list"]
        if candidates:
            st.subheader("AI candidate recommendations")
            st.caption(
                "These are analysis candidates, not orders. The rule engine "
                "filters by confidence, available balance, and sector."
            )
            st.dataframe(
                pd.DataFrame(candidates),
                hide_index=True,
            )
        with st.expander("JevRuleEngine audit trail"):
            st.code("\n".join(st.session_state.audit_trail))
        if st.session_state.approved_trades:
            st.subheader("Verified candidate deployments")
            st.warning(
                f"Analysis mode selected: {prompt_mode}. Review each live market "
                "order before submitting."
            )
            _show_approved_trades()
        else:
            high_confidence_candidates = [
                candidate
                for candidate in candidates
                if candidate["Confidence_Score"] >= 90
            ]
            if high_confidence_candidates and all(
                candidate["Target_Price"] > float(st.session_state.balance)
                for candidate in high_confidence_candidates
            ):
                st.info(
                    "No whole share from the high-confidence candidates fits the "
                    f"available ₹{float(st.session_state.balance):,.2f} balance. "
                    "The analysis is shown, but no order was approved."
                )
            elif (
                st.session_state.analysis_live_prices
                and float(st.session_state.balance)
                < min(st.session_state.analysis_live_prices.values())
            ):
                lowest_quote = min(
                    st.session_state.analysis_live_prices.values()
                )
                st.info(
                    f"Analysis completed, but the available "
                    f"₹{float(st.session_state.balance):,.2f} is below the lowest "
                    f"supported live share price (₹{lowest_quote:,.2f}). "
                    "No whole-share order can be approved."
                )
            else:
                st.info(
                    "The rule engine approved no trades for the current facts. "
                    "Review its audit trail for the confidence or budget check."
                )

    st.sidebar.button("Log out", on_click=_logout)


def _logout() -> None:
    for key in (
        "token",
        "broker_state",
        "broker_name",
        "balance",
        "portfolio",
        "equity_holdings",
        "mutual_funds",
        "mutual_fund_error",
        "mutual_fund_updated_at",
        "mutual_fund_manual_import",
        "mutual_fund_upload_digest",
        "mutual_fund_upload_error",
        "last_sync",
        "last_sync_attempt",
        "sync_failed",
        "analysis_result",
        "planning_budget_inr",
        "analysis_live_prices",
        "analysis_live_prices_updated_at",
        "analysis_price_error",
        "approved_trades",
        "audit_trail",
        "analysis_error",
        "alerted_risk_tickers",
        "risk_high_water",
        "risk_alerts",
        "risk_errors",
    ):
        st.session_state[key] = None
    st.session_state.portfolio = pd.DataFrame(columns=PORTFOLIO_COLUMNS)
    st.session_state.equity_holdings = pd.DataFrame(columns=PORTFOLIO_COLUMNS)
    st.session_state.mutual_funds = pd.DataFrame(columns=MUTUAL_FUND_COLUMNS)
    st.session_state.balance = 0.0
    st.session_state.alerted_risk_tickers = []
    st.session_state.risk_high_water = {}
    st.session_state.risk_alerts = []
    st.session_state.risk_errors = []
    st.session_state.last_sync_attempt = None
    if "telegram_chat_id" in st.session_state:
        del st.session_state["telegram_chat_id"]


if st.session_state.token is None:
    _show_login()
else:
    _show_workspace()

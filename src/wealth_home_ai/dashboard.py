"""Wealth Home AI: session-isolated multi-broker portfolio dashboard."""

from __future__ import annotations

import sqlite3
import hashlib
import json
from datetime import datetime, timedelta
from math import isfinite
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from google.genai.errors import APIError

from .broker_factory import (
    BrokerAPIError,
    BrokerCapabilityError,
    BrokerFactory,
    BrokerInterface,
    DhanAdapter,
    MUTUAL_FUND_COLUMNS,
    TICKER_MAP,
    UpstoxAdapter,
    ZerodhaAdapter,
)
from .jev_rules import JevRuleEngine, SECTOR_BY_TICKER
from .features.analysis import render_analysis_panel
from .features.home import render_home_dashboard
from .features.operations.debt import render_debt
from .features.operations.derivatives import render_derivatives
from .features.operations.equity import render_equity
from .features.operations.mutual_funds import (
    render_mutual_funds,
    validate_mutual_funds,
)
from .features.operations.trading import render_trading
from .features.portfolio import empty_debt_holdings
from .oauth_state_store import consume_oauth_state_context, create_oauth_state
from .settings import settings
from .ui_helpers import broker_connect_button_css, same_tab_link_html
from .upstox_helper import (
    LLMProviderError,
    ask_llm_agent,
    send_telegram_alert,
)

APP_TITLE = settings.app_title
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
st.session_state.setdefault("debt_holdings", empty_debt_holdings())
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
st.session_state.setdefault("telegram_bot_token", "")
st.session_state.setdefault("telegram_chat_id", "")
st.session_state.setdefault("analysis_result", None)
st.session_state.setdefault("analysis_results", {})
st.session_state.setdefault("analysis_errors", {})
st.session_state.setdefault("analysis_price_errors", {})
st.session_state.setdefault("ai_settings_prompt", "")
st.session_state.setdefault("llm_provider_settings", {})
st.session_state.setdefault("planning_budget_inr", None)
st.session_state.setdefault("analysis_live_prices", {})
st.session_state.setdefault("analysis_live_prices_updated_at", None)
st.session_state.setdefault("analysis_price_error", None)
st.session_state.setdefault("approved_trades", [])
st.session_state.setdefault("approved_trades_by_scope", {})
st.session_state.setdefault("audit_trail", [])
st.session_state.setdefault("audit_trails_by_scope", {})
st.session_state.setdefault("login_error", None)
st.session_state.setdefault("pending_broker_name", None)
st.session_state.setdefault("pending_broker_adapter", None)
st.session_state.setdefault("pending_login_url", None)
st.session_state.setdefault("pending_redirect_url", None)
st.session_state.setdefault("pending_oauth_fingerprint", None)
st.session_state.setdefault("pending_oauth_expires_at", None)

st.sidebar.title("🏡 Wealth Home AI")
st.sidebar.caption("Private broker workspace")
st.sidebar.markdown("---")


def _redirect_url() -> str:
    configured = settings.upstox_redirect_uri
    if configured:
        return configured
    current_url = str(st.context.url or "")
    if current_url:
        parts = urlsplit(current_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
    return settings.default_redirect_uri


def _render_same_tab_link(label: str, url: str) -> None:
    st.html(same_tab_link_html(label, url))


def _is_trading_session() -> bool:
    now = datetime.now(ZoneInfo(settings.timezone))
    return (
        now.weekday() < 5
        and settings.trading_session_start
        <= now.time()
        <= settings.trading_session_end
    )


def _fetch_market_risk(
    adapter: BrokerInterface, token: str, ticker: str
) -> dict[str, float]:
    return adapter.fetch_market_risk(token, ticker)


def _render_telegram_controls() -> None:
    with st.sidebar.expander(
        "Telegram notifications", icon=":material/notifications:"
    ):
        st.caption(
            "Bot credentials stay in this browser session and are not saved to "
            "server configuration."
        )
        st.text_input(
            "Telegram bot token",
            type="password",
            key="telegram_bot_token",
            help="Create or manage a bot with BotFather. Do not share its token.",
        )
        st.text_input(
            "Private Telegram chat ID",
            key="telegram_chat_id",
            help="The bot must be started by the recipient before it can send messages.",
        )
        effective_bot_token = str(st.session_state.telegram_bot_token).strip()
        effective_chat_id = str(st.session_state.telegram_chat_id).strip()
        if st.button(
            "Send test notification",
            key="telegram_test_notification",
            disabled=not (effective_bot_token and effective_chat_id),
        ):
            try:
                send_telegram_alert(
                    "Wealth Home AI Telegram notifications are connected.",
                    chat_id=effective_chat_id,
                    bot_token=effective_bot_token,
                )
            except (requests.RequestException, RuntimeError):
                st.error("Telegram could not deliver the test notification.")
            else:
                st.success("Test notification sent.")


def _clear_sensitive_angel_inputs() -> None:
    for key in (
        "angel_api_key",
        "angel_client_id",
        "angel_password",
        "angel_totp_secret",
    ):
        if key in st.session_state:
            del st.session_state[key]


def _prepare_oauth_login(
    broker_name: str, api_key: str, api_secret: str
) -> tuple[str | None, str | None]:
    redirect_url = _redirect_url()
    fingerprint = hashlib.sha256(
        json.dumps(
            [broker_name, api_key, api_secret, redirect_url],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        st.session_state.pending_broker_name == broker_name
        and st.session_state.get("pending_oauth_fingerprint") == fingerprint
        and st.session_state.pending_login_url
        and st.session_state.get("pending_oauth_expires_at", 0)
        > datetime.now().timestamp()
    ):
        return st.session_state.pending_login_url, None

    adapter = BrokerFactory.create_adapter(
        broker_name, api_key=api_key, api_secret=api_secret
    )
    try:
        login_url = adapter.get_login_url(redirect_url)
        oauth_state = getattr(adapter, "oauth_state", None)
        if not oauth_state:
            raise ValueError(f"{broker_name} did not create an OAuth state.")
        create_oauth_state(
            oauth_state,
            {
                "broker": broker_name,
                "api_key": api_key,
                "api_secret": api_secret,
                "redirect_url": redirect_url,
            },
        )
    except (BrokerAPIError, sqlite3.Error, OSError, ValueError):
        st.session_state.pending_broker_name = None
        st.session_state.pending_broker_adapter = None
        st.session_state.pending_login_url = None
        st.session_state.pending_redirect_url = None
        st.session_state.pending_oauth_fingerprint = None
        st.session_state.pending_oauth_expires_at = None
        return None, (
            f"Could not start {broker_name} sign-in. Check the app credentials, "
            "registered callback URL, and OAuth state storage."
        )

    st.session_state.pending_broker_name = broker_name
    st.session_state.pending_broker_adapter = adapter
    st.session_state.pending_login_url = login_url
    st.session_state.pending_redirect_url = redirect_url
    st.session_state.pending_oauth_fingerprint = fingerprint
    st.session_state.pending_oauth_expires_at = (
        datetime.now().timestamp() + settings.oauth_state_ttl_seconds
    )
    return login_url, None


def _clear_pending_login() -> None:
    for key in (
        "pending_broker_name",
        "pending_broker_adapter",
        "pending_login_url",
        "pending_redirect_url",
        "pending_oauth_fingerprint",
        "pending_oauth_expires_at",
    ):
        st.session_state[key] = None


def _clear_broker_login_inputs() -> None:
    for key in (
        "upstox_api_key",
        "upstox_api_secret",
        "angel_api_key",
        "angel_client_id",
        "angel_password",
        "angel_totp_secret",
        "zerodha_api_key",
        "zerodha_api_secret",
        "dhan_client_id",
        "dhan_api_key",
        "dhan_api_secret",
    ):
        st.session_state.pop(key, None)


def _authenticate_angel_one() -> None:
    required_fields = {
        "angel_api_key": "API key",
        "angel_client_id": "Client ID",
        "angel_password": "password",
        "angel_totp_secret": "TOTP secret",
    }
    missing_fields = [
        label
        for key, label in required_fields.items()
        if not str(st.session_state.get(key, "")).strip()
    ]
    if missing_fields:
        st.session_state.login_error = (
            "Enter the required Angel One details: " + ", ".join(missing_fields) + "."
        )
        return
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


def _start_dhan_login() -> None:
    client_id = str(st.session_state.get("dhan_client_id", "")).strip()
    api_key = str(st.session_state.get("dhan_api_key", "")).strip()
    api_secret = str(st.session_state.get("dhan_api_secret", "")).strip()
    if not client_id or not api_key or not api_secret:
        missing = []
        if not client_id:
            missing.append("Client ID")
        if not api_key:
            missing.append("API key")
        if not api_secret:
            missing.append("API secret")
        st.session_state.login_error = "Enter the Dhan " + ", ".join(missing) + "."
        return

    adapter = DhanAdapter(
        api_key=api_key, api_secret=api_secret, client_id=client_id
    )
    _clear_pending_login()
    try:
        redirect_url = _redirect_url()
        login_url = adapter.get_login_url(redirect_url)
    except (BrokerAPIError, requests.RequestException, ValueError):
        st.session_state.login_error = (
            "Could not start Dhan sign-in. Check your Dhan API credentials, "
            "Client ID, and registered callback URL."
        )
    else:
        st.session_state.pending_broker_name = "Dhan"
        st.session_state.pending_broker_adapter = adapter
        st.session_state.pending_login_url = login_url
        st.session_state.pending_redirect_url = redirect_url
        st.session_state.login_error = None
    finally:
        st.session_state.pop("dhan_api_secret", None)
        st.session_state.pop("dhan_api_key", None)
        st.session_state.pop("dhan_client_id", None)


def _complete_broker_login(
    broker_name: str, adapter: BrokerInterface, token: str
) -> None:
    st.session_state.token = token
    st.session_state.broker_state = adapter
    st.session_state.broker_name = broker_name
    st.session_state.last_sync = None
    st.session_state.last_sync_attempt = None
    st.session_state.alerted_risk_tickers = []
    st.session_state.risk_high_water = {}
    st.session_state.analysis_result = None
    st.session_state.analysis_results = {}
    st.session_state.analysis_errors = {}
    st.session_state.approved_trades = []
    st.session_state.approved_trades_by_scope = {}
    st.session_state.audit_trail = []
    st.session_state.audit_trails_by_scope = {}
    st.session_state.portfolio = pd.DataFrame(columns=PORTFOLIO_COLUMNS)
    st.session_state.equity_holdings = pd.DataFrame(columns=PORTFOLIO_COLUMNS)
    st.session_state.mutual_funds = pd.DataFrame(columns=MUTUAL_FUND_COLUMNS)
    st.session_state.balance = 0.0
    st.session_state.mutual_fund_manual_import = broker_name in {
        "Angel One",
        "Zerodha",
        "Dhan",
    }
    st.session_state.mutual_fund_error = None
    st.session_state.pending_broker_name = None
    st.session_state.pending_broker_adapter = None
    st.session_state.pending_login_url = None
    st.session_state.pending_redirect_url = None
    st.session_state.pending_oauth_fingerprint = None
    st.session_state.pending_oauth_expires_at = None
    st.session_state.login_error = None
    _clear_broker_login_inputs()


def _show_login_legacy() -> None:
    st.title(APP_TITLE)
    st.subheader("Onboarding portal")
    st.info(
        "AI suggestions are informational only and are not investment advice. "
        "Broker/API availability, hosting quotas, and exchange or broker charges "
        "depend on their providers."
    )
    st.caption(
        "Enter credentials for the selected broker. Broker passwords, OTP/TOTP "
        "secrets, and API keys are used only for this sign-in and are not saved "
        "to server configuration."
    )
    broker_choice = st.selectbox(
        "Select broker",
        ["Upstox", "Angel One", "Zerodha", "Dhan"],
        key="selected_broker",
    )

    if st.session_state.login_error:
        st.error(st.session_state.login_error)
        st.session_state.login_error = None

    if broker_choice == "Upstox":
        redirect_url = _redirect_url()
        st.caption(f"Registered callback URL: {redirect_url}")
        st.caption(
            "Upstox API key and secret identify the developer app, not your personal "
            "Upstox login. Each user authorizes their account on Upstox."
        )
        st.caption(
            "To switch Upstox accounts, sign out of the current app session, then "
            "authorize the other account on Upstox. If Upstox keeps the previous "
            "login, sign out there or use a private browser window."
        )

        authorization_code = st.query_params.get("code")
        oauth_error = st.query_params.get("error")
        if oauth_error:
            st.error("Upstox sign-in was cancelled or rejected.")
            st.session_state.pending_broker_name = None
            st.session_state.pending_broker_adapter = None
            st.session_state.pending_login_url = None
            st.session_state.pending_redirect_url = None
            st.query_params.clear()
        if (
            st.session_state.pending_broker_name == "Upstox"
            and st.session_state.pending_login_url
        ):
            st.html(
                "<a href=\""
                + escape(st.session_state.pending_login_url, quote=True)
                + "\" target=\"_self\" rel=\"noopener noreferrer\">"
                "Open Upstox sign-in</a>"
            )
        if authorization_code and not oauth_error:
            pending_adapter = st.session_state.pending_broker_adapter
            if (
                st.session_state.pending_broker_name != "Upstox"
                or not isinstance(pending_adapter, UpstoxAdapter)
            ):
                returned_state = st.query_params.get("state")
                try:
                    state_is_valid = consume_oauth_state(returned_state)
                except (sqlite3.Error, OSError, ValueError):
                    st.error(
                        "Could not verify this Upstox callback. Start a new sign-in."
                    )
                    st.query_params.clear()
                    return
                if not state_is_valid:
                    st.error(
                        "This Upstox callback expired or was already used. Start a "
                        "new sign-in."
                    )
                    st.query_params.clear()
                    return
                st.session_state.upstox_recovery_code = str(authorization_code)
                st.session_state.upstox_recovery_redirect_url = redirect_url
                st.info(
                    "Upstox returned to a fresh browser session, so the temporary "
                    "sign-in context was not available. Re-enter the same developer "
                    "app credentials to finish this one-time authorization."
                )
                st.query_params.clear()
            else:
                returned_state = st.query_params.get("state")
                try:
                    state_is_valid = consume_oauth_state(returned_state)
                except (sqlite3.Error, OSError, ValueError):
                    st.error(
                        "Could not verify the Upstox callback because the local "
                        "OAuth state store is unavailable."
                    )
                    st.session_state.pending_broker_name = None
                    st.session_state.pending_broker_adapter = None
                    st.session_state.pending_login_url = None
                    st.session_state.pending_redirect_url = None
                    st.query_params.clear()
                else:
                    if not state_is_valid:
                        st.error(
                            "Upstox sign-in could not be verified. Its one-time "
                            "verification may have expired or already been used. "
                            "Reconnect and complete sign-in within 10 minutes."
                        )
                        st.session_state.pending_broker_name = None
                        st.session_state.pending_broker_adapter = None
                        st.session_state.pending_login_url = None
                        st.session_state.pending_redirect_url = None
                        st.query_params.clear()
                    else:
                        try:
                            token = pending_adapter.authenticate(
                                str(authorization_code),
                                str(
                                    st.session_state.pending_redirect_url
                                    or redirect_url
                                ),
                            )
                        except (
                            BrokerAPIError,
                            requests.RequestException,
                            ValueError,
                        ):
                            st.error(
                                "Upstox authentication failed. Verify the registered "
                                "callback URL and try again."
                            )
                            st.session_state.pending_broker_name = None
                            st.session_state.pending_broker_adapter = None
                            st.session_state.pending_login_url = None
                            st.session_state.pending_redirect_url = None
                            st.query_params.clear()
                        else:
                            _complete_broker_login("Upstox", pending_adapter, token)
                            st.query_params.clear()
                            st.rerun()

        if st.session_state.get("upstox_recovery_code"):
            with st.form("upstox_recovery_login"):
                st.text_input(
                    "Upstox API key",
                    key="upstox_api_key",
                    type="password",
                )
                st.text_input(
                    "Upstox API secret",
                    key="upstox_api_secret",
                    type="password",
                )
                st.form_submit_button(
                    "Finish Upstox sign-in",
                    type="primary",
                    on_click=_complete_upstox_recovery,
                )
        else:
            with st.form("upstox_login"):
                st.text_input(
                    "Upstox API key",
                    key="upstox_api_key",
                    type="password",
                )
                st.text_input(
                    "Upstox API secret",
                    key="upstox_api_secret",
                    type="password",
                )
                st.form_submit_button(
                    "Connect with Upstox",
                    type="primary",
                    on_click=_start_upstox_login,
                )

    elif broker_choice == "Angel One":
        st.caption(
            "Angel One uses programmatic SmartAPI sign-in. Your client ID, password, "
            "and TOTP secret are cleared after login."
        )
        st.caption("All four fields are required to connect.")
        with st.form("angel_one_login"):
            st.text_input(
                "Angel One API key",
                type="password",
                key="angel_api_key",
            )
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
    elif broker_choice == "Zerodha":
        redirect_url = _redirect_url()
        st.caption(f"Registered callback URL: {redirect_url}")
        st.caption(
            "Enter the API key and secret from your Kite Connect developer app. "
            "Both are required; the secret is cleared after sign-in starts."
        )
        request_token = st.query_params.get("request_token")
        if st.query_params.get("status") == "error":
            st.error("Zerodha sign-in was cancelled or rejected.")
            st.session_state.pending_broker_adapter = None
            st.session_state.pending_broker_name = None
            st.session_state.pending_login_url = None
            st.query_params.clear()
        if request_token:
            pending_adapter = st.session_state.pending_broker_adapter
            returned_state = st.query_params.get("state")
            if (
                st.session_state.pending_broker_name != "Zerodha"
                or not isinstance(pending_adapter, ZerodhaAdapter)
            ):
                st.error(
                    "This Zerodha callback has no matching sign-in in this browser "
                    "session. Start a new connection."
                )
                st.query_params.clear()
            else:
                try:
                    valid_state = consume_oauth_state(returned_state)
                    if not valid_state:
                        raise ValueError("Zerodha OAuth state did not match.")
                    token = pending_adapter.authenticate(
                        str(request_token),
                        str(st.session_state.pending_redirect_url or redirect_url),
                    )
                except (
                    BrokerAPIError,
                    requests.RequestException,
                    sqlite3.Error,
                    OSError,
                    ValueError,
                ):
                    st.error(
                        "Zerodha sign-in failed or expired. Confirm the callback URL "
                        "and credentials, then start a new connection."
                    )
                    st.session_state.pending_broker_adapter = None
                    st.session_state.pending_broker_name = None
                    st.session_state.pending_login_url = None
                    st.query_params.clear()
                else:
                    _complete_broker_login("Zerodha", pending_adapter, token)
                    st.query_params.clear()
                    st.rerun()
        with st.form("zerodha_login"):
            st.text_input(
                "Zerodha API key",
                key="zerodha_api_key",
                type="password",
            )
            st.text_input(
                "Zerodha API secret",
                key="zerodha_api_secret",
                type="password",
            )
            st.form_submit_button(
                "Continue to Zerodha",
                type="primary",
                on_click=_start_zerodha_login,
            )
        if (
            st.session_state.pending_broker_name == "Zerodha"
            and st.session_state.pending_login_url
        ):
            st.html(
                "<a href=\""
                + escape(st.session_state.pending_login_url, quote=True)
                + "\" target=\"_self\" rel=\"noopener noreferrer\">"
                "Open Zerodha sign-in</a>"
            )
    else:
        st.caption(
            "Dhan credentials must belong to the same account. Enter the Client ID, "
            "API key, and API secret for the account you want to connect."
        )
        st.caption(
            "Dhan may require you to subscribe to its Data APIs for live quotes and "
            "allow-list a static server IP before API orders can be placed."
        )
        redirect_url = _redirect_url()
        st.caption(
            "Register this exact callback URL with the Dhan API app: "
            + redirect_url
        )
        token_id = st.query_params.get("tokenId")
        if st.query_params.get("error"):
            st.error("Dhan sign-in was cancelled or rejected.")
            st.session_state.pending_broker_adapter = None
            st.session_state.pending_broker_name = None
            st.session_state.pending_login_url = None
            st.query_params.clear()
        if token_id:
            pending_adapter = st.session_state.pending_broker_adapter
            if (
                st.session_state.pending_broker_name != "Dhan"
                or not isinstance(pending_adapter, DhanAdapter)
            ):
                st.error(
                    "This Dhan callback has no matching authorization in this "
                    "browser session. Start a new connection."
                )
                st.query_params.clear()
            else:
                try:
                    token = pending_adapter.authenticate(
                        str(token_id),
                        str(st.session_state.pending_redirect_url or redirect_url),
                    )
                except (BrokerAPIError, requests.RequestException, ValueError):
                    st.error(
                        "Dhan sign-in failed or expired. Confirm your registered "
                        "callback URL and start a new connection."
                    )
                    st.session_state.pending_broker_adapter = None
                    st.session_state.pending_broker_name = None
                    st.session_state.pending_login_url = None
                    st.query_params.clear()
                else:
                    _complete_broker_login("Dhan", pending_adapter, token)
                    st.query_params.clear()
                    st.rerun()
        with st.form("dhan_login"):
            st.text_input("Dhan Client ID", key="dhan_client_id")
            st.text_input(
                "Dhan API key",
                key="dhan_api_key",
                type="password",
            )
            st.text_input(
                "Dhan API secret",
                key="dhan_api_secret",
                type="password",
            )
            st.form_submit_button(
                "Continue to Dhan",
                type="primary",
                on_click=_start_dhan_login,
            )
        if (
            st.session_state.pending_broker_name == "Dhan"
            and st.session_state.pending_login_url
        ):
            st.html(
                "<a href=\""
                + escape(st.session_state.pending_login_url, quote=True)
                + "\" target=\"_self\" rel=\"noopener noreferrer\">"
                "Open Dhan sign-in</a>"
            )


def _exchange_oauth_callback(
    broker_name: str, code: str, returned_state: str | None
) -> tuple[BrokerInterface, str]:
    context = consume_oauth_state_context(returned_state)
    if (
        context is None
        or context.get("broker") != broker_name
        or not all(
            isinstance(context.get(field), str) and context[field]
            for field in ("api_key", "api_secret", "redirect_url")
        )
    ):
        raise ValueError(f"{broker_name} callback context is missing or expired.")
    adapter = BrokerFactory.create_adapter(
        broker_name,
        api_key=context["api_key"],
        api_secret=context["api_secret"],
    )
    token = adapter.authenticate(code, context["redirect_url"])
    return adapter, token


def _show_login() -> None:
    st.title(APP_TITLE)
    st.html(broker_connect_button_css())
    st.subheader("Onboarding portal")
    st.info(
        "AI suggestions are informational only and are not investment advice. "
        "Broker/API availability, hosting quotas, and exchange or broker charges "
        "depend on their providers."
    )
    st.caption(
        "Broker credentials are used only for sign-in and are not saved to server "
        "configuration."
    )

    if st.query_params.get("request_token") or st.query_params.get("status") == "error":
        callback_broker = "Zerodha"
    elif st.query_params.get("tokenId"):
        callback_broker = "Dhan"
    elif st.query_params.get("code") or (
        st.query_params.get("error") and st.query_params.get("state")
    ):
        callback_broker = "Upstox"
    elif st.query_params.get("error"):
        callback_broker = "Dhan"
    else:
        callback_broker = None

    if callback_broker:
        broker_choice = callback_broker
        st.caption(f"Completing {broker_choice} sign-in…")
    else:
        broker_choice = st.selectbox(
            "Select broker",
            ["Upstox", "Angel One", "Zerodha", "Dhan"],
            key="selected_broker",
        )

    if st.session_state.login_error:
        st.error(st.session_state.login_error)
        st.session_state.login_error = None

    if broker_choice in {"Upstox", "Zerodha"}:
        redirect_url = _redirect_url()
        if not callback_broker:
            st.caption(f"Registered callback URL: {redirect_url}")
        callback_error = st.query_params.get("error")
        if callback_error or (
            broker_choice == "Zerodha"
            and st.query_params.get("status") == "error"
        ):
            st.error(f"{broker_choice} sign-in was cancelled or rejected.")
            _clear_pending_login()
            _clear_broker_login_inputs()
            st.query_params.clear()
        elif broker_choice == "Upstox" and st.query_params.get("code"):
            try:
                adapter, token = _exchange_oauth_callback(
                    "Upstox",
                    str(st.query_params["code"]),
                    st.query_params.get("state"),
                )
            except (
                BrokerAPIError,
                requests.RequestException,
                sqlite3.Error,
                OSError,
                ValueError,
            ):
                st.error(
                    "Upstox sign-in failed or expired. Check the registered callback "
                    "URL and app credentials, then start a new connection."
                )
                _clear_pending_login()
                _clear_broker_login_inputs()
                st.query_params.clear()
            else:
                _complete_broker_login("Upstox", adapter, token)
                st.query_params.clear()
                st.rerun()
        elif broker_choice == "Zerodha" and st.query_params.get("request_token"):
            try:
                adapter, token = _exchange_oauth_callback(
                    "Zerodha",
                    str(st.query_params["request_token"]),
                    st.query_params.get("state"),
                )
            except (
                BrokerAPIError,
                requests.RequestException,
                sqlite3.Error,
                OSError,
                ValueError,
            ):
                st.error(
                    "Zerodha sign-in failed or expired. Confirm the callback URL and "
                    "credentials, then start a new connection."
                )
                _clear_pending_login()
                _clear_broker_login_inputs()
                st.query_params.clear()
            else:
                _complete_broker_login("Zerodha", adapter, token)
                st.query_params.clear()
                st.rerun()
        elif not callback_broker:
            if broker_choice == "Upstox":
                api_key_key, api_secret_key = "upstox_api_key", "upstox_api_secret"
                button_label = "Connect with Upstox"
            else:
                api_key_key, api_secret_key = (
                    "zerodha_api_key",
                    "zerodha_api_secret",
                )
                button_label = "Connect with Zerodha"
            api_key = str(st.session_state.get(api_key_key, "")).strip()
            api_secret = str(st.session_state.get(api_secret_key, "")).strip()
            st.text_input(
                f"{broker_choice} API key",
                key=api_key_key,
                type="password",
                live=True,
            )
            st.text_input(
                f"{broker_choice} API secret",
                key=api_secret_key,
                type="password",
                live=True,
            )
            login_url = None
            login_error = None
            if api_key and api_secret:
                login_url, login_error = _prepare_oauth_login(
                    broker_choice, api_key, api_secret
                )
            if login_error:
                st.error(login_error)
            if api_key and api_secret and login_url:
                _render_same_tab_link(button_label, login_url)
            else:
                st.button(
                    button_label,
                    key=f"connect_{broker_choice.lower()}",
                    type="primary",
                    disabled=True,
                )

    elif broker_choice == "Angel One":
        if not callback_broker:
            st.caption(
                "Angel One requires its API key, Client ID, password, and TOTP secret."
            )
            st.text_input(
                "Angel One API key",
                type="password",
                key="angel_api_key",
                live=True,
            )
            st.text_input("Client ID", key="angel_client_id", live=True)
            st.text_input(
                "Password",
                type="password",
                key="angel_password",
                live=True,
            )
            st.text_input(
                "TOTP secret",
                type="password",
                key="angel_totp_secret",
                help="The secret used to generate your current one-time password.",
                live=True,
            )
            required = (
                "angel_api_key",
                "angel_client_id",
                "angel_password",
                "angel_totp_secret",
            )
            st.button(
                "Connect with Angel One →",
                key="connect_angel_one",
                type="primary",
                on_click=_authenticate_angel_one,
                disabled=not all(
                    str(st.session_state.get(key, "")).strip() for key in required
                ),
            )

    else:
        if not callback_broker:
            st.caption(
                "Dhan credentials must belong to the account you want to connect."
            )
            st.caption(
                "Register this exact callback URL with the Dhan API app: "
                + _redirect_url()
            )
            st.text_input("Dhan Client ID", key="dhan_client_id", live=True)
            st.text_input(
                "Dhan API key",
                key="dhan_api_key",
                type="password",
                live=True,
            )
            st.text_input(
                "Dhan API secret",
                key="dhan_api_secret",
                type="password",
                live=True,
            )
            required = ("dhan_client_id", "dhan_api_key", "dhan_api_secret")
            st.button(
                "Connect with Dhan →",
                key="connect_dhan",
                type="primary",
                on_click=_start_dhan_login,
                disabled=not all(
                    str(st.session_state.get(key, "")).strip() for key in required
                ),
            )
            if (
                st.session_state.pending_broker_name == "Dhan"
                and st.session_state.pending_login_url
            ):
                _render_same_tab_link(
                    "Open Dhan sign-in", st.session_state.pending_login_url
                )
        elif st.query_params.get("error"):
            st.error("Dhan sign-in was cancelled or rejected.")
            _clear_pending_login()
            _clear_broker_login_inputs()
            st.query_params.clear()
        elif st.query_params.get("tokenId"):
            adapter = st.session_state.pending_broker_adapter
            if (
                st.session_state.pending_broker_name != "Dhan"
                or not isinstance(adapter, DhanAdapter)
            ):
                st.error(
                    "This Dhan callback has no matching authorization in this "
                    "browser session. Start a new connection in the same browser tab."
                )
                st.query_params.clear()
            else:
                try:
                    token = adapter.authenticate(
                        str(st.query_params["tokenId"]),
                        str(st.session_state.pending_redirect_url or _redirect_url()),
                    )
                except (BrokerAPIError, requests.RequestException, ValueError):
                    st.error(
                        "Dhan sign-in failed or expired. Confirm your callback URL "
                        "and start a new connection."
                    )
                    _clear_pending_login()
                    _clear_broker_login_inputs()
                    st.query_params.clear()
                else:
                    _complete_broker_login("Dhan", adapter, token)
                    st.query_params.clear()
                    st.rerun()


@st.fragment(run_every=settings.dashboard_poll_seconds)
def polling_sequence() -> None:
    token = st.session_state.token
    adapter = st.session_state.broker_state
    now = datetime.now(ZoneInfo(settings.timezone))
    should_sync = token and adapter and (
        st.session_state.last_sync_attempt is None
        or (
            _is_trading_session()
            and now - st.session_state.last_sync_attempt >= timedelta(
                minutes=settings.sync_interval_minutes
            )
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
            st.session_state.last_sync = datetime.now(ZoneInfo(settings.timezone))
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
                broker_funds = validate_mutual_funds(mutual_funds)
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
                        ZoneInfo(settings.timezone)
                    )

    balance = float(st.session_state.balance)
    portfolio = st.session_state.portfolio
    equity_holdings = st.session_state.equity_holdings
    mutual_funds = st.session_state.mutual_funds
    render_home_dashboard(
        balance,
        equity_holdings,
        portfolio,
        mutual_funds,
        st.session_state.debt_holdings,
    )

    if st.session_state.sync_failed:
        st.caption("Broker refresh unavailable; displaying the last successful data.")
    elif st.session_state.last_sync:
        st.caption(
            "Last broker sync: "
            f"{st.session_state.last_sync.strftime('%d %b %Y, %H:%M:%S IST')}"
        )

    _render_telegram_controls()
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
            risk_boundary = (
                high_water[ticker] - settings.atr_multiplier * market_risk["ATR"]
            )
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
        f"{settings.atr_period}-trading-day ATR. A failed candle/quote request "
        "is not treated as a breach."
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
            telegram_bot_token = str(
                st.session_state.get("telegram_bot_token", "")
            ).strip()
            if telegram_bot_token and private_chat_id:
                try:
                    send_telegram_alert(
                        f"Market Exit warning: {ticker} fell below its 3-ATR "
                        f"risk boundary of INR {boundary:.2f}.",
                        chat_id=private_chat_id,
                        bot_token=telegram_bot_token,
                    )
                except (requests.RequestException, RuntimeError, ValueError):
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


def _run_analysis(scope: str = "all") -> None:
    scope_titles = {
        "all": "entire portfolio",
        "equity": "equity holdings",
        "debt": "debt holdings",
        "trading": "trading positions",
        "options": "options",
        "futures": "futures",
        "mutual_funds": "mutual funds",
    }
    if scope not in scope_titles:
        raise ValueError(f"Unsupported analysis scope: {scope}")

    def fail(message: str) -> None:
        st.session_state.analysis_errors[scope] = message
        st.session_state.analysis_results.pop(scope, None)
        st.session_state.approved_trades_by_scope[scope] = []

    provider = str(st.session_state.get("llm_provider", "Gemini"))
    provider_slug = provider.lower().replace(" ", "_").replace("-", "_")
    api_key_widget = (
        "user_gemini_api_key"
        if provider == "Gemini"
        else f"user_llm_api_key_{provider_slug}"
    )
    api_key = str(st.session_state.get(api_key_widget) or "").strip()
    if not api_key:
        fail(f"Enter an API key for {provider} beside the AI analysis button.")
        st.session_state.ai_settings_prompt = (
            f"Analysis needs a {provider} API key. Enter it beside the Run AI "
            "analysis button, then try again."
        )
        st.rerun()
    model_choice = str(
        st.session_state.get(
            f"llm_model_selection_{provider_slug}",
            settings.llm_provider_models[provider][0]
            if settings.llm_provider_models.get(provider)
            else "",
        )
    ).strip()
    configured_models = settings.llm_provider_models.get(provider, ())
    if not model_choice or (
        provider != "Custom OpenAI-compatible"
        and model_choice not in configured_models
    ):
        fail(f"Select a model configured for {provider}.")
        return
    selected_model = model_choice
    provider_base_url = (
        str(
            st.session_state.get("llm_custom_base_url")
            or settings.llm_custom_base_url
        ).strip()
        if provider == "Custom OpenAI-compatible"
        else None
    )
    if provider == "Custom OpenAI-compatible" and not provider_base_url:
        fail("Enter the API base URL for the selected custom provider.")
        st.session_state.ai_settings_prompt = (
            "Analysis needs an API base URL for the custom provider. Add it beside "
            "the model selector or configure LLM_CUSTOM_BASE_URL."
        )
        st.rerun()

    raw_budget = st.session_state.get("planning_budget_inr")
    if isinstance(raw_budget, bool):
        fail("Enter a valid positive scenario budget.")
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
        fail(
            "Enter a scenario budget between ₹1 and ₹10 crore before running analysis."
        )
        return

    positions = st.session_state.portfolio
    equity_holdings = st.session_state.equity_holdings
    mutual_funds = st.session_state.mutual_funds
    debt_holdings = st.session_state.debt_holdings
    equity_and_positions = pd.concat(
        [equity_holdings, positions], ignore_index=True
    ).drop_duplicates(subset=["Ticker", "Qty", "Avg_Price"], keep="last")
    summary_by_scope = {
        "all": {
            "long_term_equity_holdings": equity_holdings.to_dict(orient="records"),
            "open_trading_positions": positions.to_dict(orient="records"),
            "debt_holdings": debt_holdings.to_dict(orient="records"),
            "mutual_fund_holdings": mutual_funds.to_dict(orient="records"),
        },
        "equity": {"equity_holdings": equity_holdings.to_dict(orient="records")},
        "debt": {"debt_holdings": debt_holdings.to_dict(orient="records")},
        "trading": {"trading_positions": positions.to_dict(orient="records")},
        "options": {"options_positions": []},
        "futures": {"futures_positions": []},
        "mutual_funds": {"mutual_fund_holdings": mutual_funds.to_dict(orient="records")},
    }
    scoped_portfolio = {
        "all": equity_and_positions,
        "equity": equity_holdings,
        "trading": positions,
    }.get(scope, pd.DataFrame(columns=PORTFOLIO_COLUMNS))
    summary = json.dumps(summary_by_scope[scope], ensure_ascii=True)
    mode = st.session_state.get("analysis_mode", "Portfolio and cash review")
    context = (
        f"Analysis scope: {scope_titles[scope]}. Analysis mode: {mode}. "
        "Consider only the supplied scoped records. "
        "Broker-reported equity holdings, trading positions, manually provided "
        "debt assets, mutual funds when available, actual broker cash, and a separate "
        f"user-selected hypothetical investment budget of INR {planning_budget:,.2f} "
        "are supplied. Size the scenario recommendations to the user-selected "
        "budget, not to broker cash. The scenario budget does not represent cash "
        "actually available in the brokerage account. "
        "Fund NAVs are the broker's last reported NAV and may not be live. "
        "Do not claim news or data that the application did not supply. "
        "For manually entered debt, do not invent rates, ratings, maturities, "
        "or liquidity terms."
    )
    if scope in {"all", "equity", "trading"}:
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
            price_error = (
                "Could not retrieve a fresh broker quote. Analysis will still run, "
                "but the app will not submit unpriced recommendations."
            )
        else:
            price_error = (
                None
                if live_prices
                else "The broker returned no current prices; price-based deployments "
                "remain disabled."
            )
        st.session_state.analysis_price_errors[scope] = price_error
        if scope == "all":
            st.session_state.analysis_price_error = price_error
        st.session_state.analysis_live_prices_updated_at = datetime.now(
            ZoneInfo(settings.timezone)
        )
    else:
        live_prices = {}
        price_error = (
            f"No live broker quote feed is configured for {scope_titles[scope]}; "
            "this run is portfolio review only."
        )
        st.session_state.analysis_price_errors[scope] = price_error
    if scope in {"all", "equity", "trading"}:
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
            api_key=api_key,
            model=str(selected_model),
            provider=provider,
            base_url=provider_base_url,
        )
    except APIError as error:
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
                "Google rejected this Gemini key. Check the key and project access "
                "in Google AI Studio, then update the key beside the AI analysis button."
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
                f"Gemini model {selected_model} is unavailable for this API key or "
                "project. Check model access and API enablement in Google AI Studio."
            )
        else:
            message = (
                f"Gemini API request failed (HTTP {http_code or 'unknown'}). Check the "
                "API key, model access, project quota, and network connection."
            )
        fail(message)
        return
    except LLMProviderError as error:
        fail(str(error))
        return
    except (RuntimeError, ValueError) as error:
        fail(f"Portfolio analysis failed: {error}")
        return

    sectors_held = {
        SECTOR_BY_TICKER[ticker]
        for ticker in scoped_portfolio["Ticker"].astype(str).str.upper()
        if ticker in SECTOR_BY_TICKER
    } if not scoped_portfolio.empty else set()
    engine = JevRuleEngine(
        {
            "cash_balance": planning_budget,
            "actual_broker_balance": float(st.session_state.balance),
            "live_prices": live_prices,
            "sector_holdings": sorted(sectors_held),
            "portfolio": scoped_portfolio,
            "llm_targets": analysis["cash_deployment_list"],
        }
    )
    trades, audit_trail = engine.run()
    st.session_state.analysis_results[scope] = analysis
    st.session_state.approved_trades_by_scope[scope] = trades
    st.session_state.audit_trails_by_scope[scope] = audit_trail
    st.session_state.analysis_errors.pop(scope, None)
    if scope == "all":
        st.session_state.analysis_result = analysis
        st.session_state.approved_trades = trades
        st.session_state.audit_trail = audit_trail


def _show_approved_trades(scope: str) -> None:
    approved_trades = st.session_state.approved_trades_by_scope.get(scope, [])
    for index, trade in enumerate(approved_trades):
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
                key=f"instant_deploy_{scope}_{ticker}_{index}",
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
                    st.session_state.approved_trades_by_scope[scope] = [
                        item
                        for item in approved_trades
                        if item["Ticker"] != ticker
                    ]
                    st.rerun()


def _show_workspace() -> None:
    st.header("Wealth Home AI", icon=":material/account_balance_wallet:")
    st.badge(
        f"{st.session_state.broker_name} connected",
        icon=":material/check_circle:",
        color="green",
    )
    st.caption(
        "Portfolio overview, AI insights, and broker actions in one workspace."
    )
    polling_sequence()
    if st.session_state.planning_budget_inr is None:
        st.session_state.planning_budget_inr = max(
            float(st.session_state.balance), 1.0
        )
    with st.sidebar:
        st.subheader("Connected broker")
        st.write(st.session_state.broker_name)
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

    st.subheader("AI portfolio review", divider="gray")
    st.caption(
        "A single run reviews equity, trading, debt and mutual-fund records together. "
        "Scoped AI reviews are also available in each workspace tab."
    )
    analysis_mode_descriptions = {
        "Portfolio and cash review": (
            "Reviews your holdings, diversification, and cash position. It does not "
            "build an order deployment plan."
        ),
        "Risk-aware deployment review": (
            "Reviews your portfolio and creates a hypothetical, risk-aware plan "
            "within the scenario budget. It does not submit orders."
        ),
    }
    mode_column, budget_column = st.columns([1, 1.25])
    with mode_column:
        selected_analysis_mode = st.selectbox(
            "Analysis mode",
            ["Portfolio and cash review", "Risk-aware deployment review"],
            key="analysis_mode",
            help=(
                "Choose whether AI reviews portfolio conditions only or also "
                "builds a hypothetical deployment plan."
            ),
        )
    with budget_column:
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
    st.caption(analysis_mode_descriptions[selected_analysis_mode])
    st.caption(
        f"Actual broker cash: ₹{float(st.session_state.balance):,.2f}. "
        "Scenario budget is for planning only; live orders require sufficient "
        "actual cash. Candidates below 90% confidence will not be approved."
    )
    render_analysis_panel(
        "all", "your full portfolio", _run_analysis, _show_approved_trades
    )

    st.subheader("Investment workspaces", divider="gray")
    equity_tab, debt_tab, trading_tab, options_tab, futures_tab, mf_tab = st.tabs(
        [
            ":material/show_chart: Equities",
            ":material/account_balance: Debt",
            ":material/swap_vert: Trading",
            ":material/tune: Options",
            ":material/candlestick_chart: Futures",
            ":material/pie_chart: Mutual funds",
        ]
    )
    with equity_tab:
        render_equity(st.session_state.equity_holdings)
        render_analysis_panel("equity", "equity", _run_analysis, _show_approved_trades)
    with debt_tab:
        render_debt(st.session_state.debt_holdings)
        render_analysis_panel("debt", "debt", _run_analysis, _show_approved_trades)
    with trading_tab:
        render_trading(st.session_state.portfolio)
        render_analysis_panel("trading", "trading", _run_analysis, _show_approved_trades)
    with options_tab:
        render_derivatives("Options")
        render_analysis_panel("options", "options", _run_analysis, _show_approved_trades)
    with futures_tab:
        render_derivatives("Futures")
        render_analysis_panel("futures", "futures", _run_analysis, _show_approved_trades)
    with mf_tab:
        render_mutual_funds(
            st.session_state.mutual_funds,
            st.session_state.mutual_fund_error,
        )
        render_analysis_panel(
            "mutual_funds", "mutual funds", _run_analysis, _show_approved_trades
        )

    st.sidebar.button("Disconnect broker / switch account", on_click=_logout)


def _logout() -> None:
    llm_session_keys = tuple(
        key
        for key in st.session_state
        if key.startswith(
            (
                "user_llm_api_key_",
                "llm_model_selection_",
            )
        )
    )
    for key in (
        "token",
        "broker_state",
        "broker_name",
        "balance",
        "portfolio",
        "equity_holdings",
        "mutual_funds",
        "debt_holdings",
        "mutual_fund_error",
        "mutual_fund_updated_at",
        "mutual_fund_manual_import",
        "mutual_fund_upload_digest",
        "mutual_fund_upload_error",
        "last_sync",
        "last_sync_attempt",
        "sync_failed",
        "analysis_result",
        "analysis_results",
        "analysis_errors",
        "ai_settings_prompt",
        "analysis_price_errors",
        "planning_budget_inr",
        "analysis_live_prices",
        "analysis_live_prices_updated_at",
        "analysis_price_error",
        "approved_trades",
        "approved_trades_by_scope",
        "audit_trail",
        "audit_trails_by_scope",
        "analysis_error",
        "alerted_risk_tickers",
        "risk_high_water",
        "risk_alerts",
        "risk_errors",
        "user_gemini_api_key",
        "llm_model_selection",
        "llm_custom_model",
        "llm_custom_base_url",
        "upstox_api_key",
        "upstox_api_secret",
        "zerodha_api_key",
        "zerodha_api_secret",
        "dhan_api_key",
        "dhan_api_secret",
        "dhan_client_id",
        "telegram_bot_token",
        "telegram_chat_id",
        "pending_broker_name",
        "pending_broker_adapter",
        "pending_login_url",
        "pending_redirect_url",
        "pending_oauth_fingerprint",
        "pending_oauth_expires_at",
        *llm_session_keys,
    ):
        st.session_state[key] = None
    _clear_broker_login_inputs()
    st.session_state.llm_provider_settings = {}
    st.session_state.portfolio = pd.DataFrame(columns=PORTFOLIO_COLUMNS)
    st.session_state.equity_holdings = pd.DataFrame(columns=PORTFOLIO_COLUMNS)
    st.session_state.mutual_funds = pd.DataFrame(columns=MUTUAL_FUND_COLUMNS)
    st.session_state.debt_holdings = empty_debt_holdings()
    st.session_state.balance = 0.0
    st.session_state.alerted_risk_tickers = []
    st.session_state.risk_high_water = {}
    st.session_state.risk_alerts = []
    st.session_state.risk_errors = []
    st.session_state.analysis_results = {}
    st.session_state.analysis_errors = {}
    st.session_state.analysis_price_errors = {}
    st.session_state.pop("telegram_bot_token", None)
    st.session_state.pop("telegram_chat_id", None)
    st.session_state.approved_trades_by_scope = {}
    st.session_state.audit_trails_by_scope = {}
    if "debt_holdings_editor" in st.session_state:
        del st.session_state["debt_holdings_editor"]
    st.session_state.last_sync_attempt = None


if st.session_state.token is None:
    _show_login()
else:
    _show_workspace()

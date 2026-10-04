"""GGHP: session-isolated multi-broker portfolio dashboard."""

from __future__ import annotations

import sqlite3
import hashlib
import html
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from math import isfinite
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
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
    GenericDynamicAdapter,
    MUTUAL_FUND_COLUMNS,
    TICKER_MAP,
    UpstoxAdapter,
    ZerodhaAdapter,
)
from .jev_rules import (
    JevRuleEngine,
    SECTOR_BY_TICKER,
    buy_execution_block_reason,
)
from .features.analysis import render_analysis_panel
from .features.home import render_home_dashboard
from .features.market import render_market_scanner
from .features.operations.derivatives import render_derivatives
from .features.operations.equity import render_equity
from .features.operations.mutual_funds import (
    render_mutual_funds,
    validate_mutual_funds,
)
from .features.operations.trading import render_trading
from .features.workspace_research import (
    render_local_asset_research as render_local_asset_research_view,
)
from .diagnostics import log_failure, log_success
from .cloud_services import (
    list_dynamic_brokers,
    load_dynamic_broker,
    register_dynamic_broker,
    send_resend_email,
    turso_is_configured,
)
from .notifications import send_email_alert
from .oauth_state_store import consume_oauth_state_context, create_oauth_state
from .settings import (
    APP_BRAND,
    APP_BRAND_DESCRIPTION,
    APP_BRAND_EXPANSION,
    LLM_PROVIDER_BASE_URLS,
    settings,
)
from .app_help import answer_app_help_question
from .ui_helpers import (
    app_animation_css,
    brand_lockup_html,
    gold_theme_css,
    broker_connect_button_css,
    same_tab_link_html,
)
from .upstox_helper import (
    LLMProviderError,
    ask_llm_agent,
    send_whatsapp_update,
    whatsapp_update_is_configured,
)

APP_TITLE = settings.app_title
PORTFOLIO_COLUMNS = ["Ticker", "Qty", "Avg_Price", "LTP"]
BROKER_FIELD_GUIDES = {
    "Upstox": {
        "API key": (
            "Identifies your Upstox developer app during OAuth sign-in.",
            "Create an API app and copy its API key.",
            "Upstox authentication guide",
            "https://upstox.com/developer/api-documentation/authentication/",
        ),
        "API secret": (
            "Proves your app's identity when the authorization code is exchanged.",
            "Copy the API secret from the same API app; keep it private.",
            "Upstox authentication guide",
            "https://upstox.com/developer/api-documentation/authentication/",
        ),
        "Callback URL": (
            "Tells Upstox where to return you after you approve account access.",
            "Register this exact URL in your API app without changing its path or trailing slash.",
            "Upstox authentication guide",
            "https://upstox.com/developer/api-documentation/authentication/",
        ),
    },
    "Zerodha": {
        "API key": (
            "Identifies your Kite Connect app on the Zerodha login page.",
            "Create a Kite Connect app and copy its API key.",
            "Kite Connect login guide",
            "https://kite.trade/docs/connect/v3/user/",
        ),
        "API secret": (
            "Verifies your app while Zerodha exchanges the one-time request token.",
            "Copy the API secret from the same Kite Connect app; keep it private.",
            "Kite Connect login guide",
            "https://kite.trade/docs/connect/v3/user/",
        ),
        "Callback URL": (
            "Lets Zerodha return the approved sign-in to this app.",
            "Register this exact URL in your Kite Connect app.",
            "Kite Connect login guide",
            "https://kite.trade/docs/connect/v3/user/",
        ),
    },
    "Angel One": {
        "Callback URL": (
            "Angel One SmartAPI signs in directly and does not redirect back to the app.",
            "No callback URL is needed for Angel One sign-in.",
            "Angel One SmartAPI",
            "https://smartapi.angelone.in/",
        ),
        "API key": (
            "Identifies the SmartAPI application used to request broker data.",
            "Create or view an API key in your SmartAPI account.",
            "Angel One SmartAPI",
            "https://smartapi.angelone.in/",
        ),
        "Client ID": (
            "Identifies your Angel One trading account for sign-in.",
            "Use the client ID shown in your Angel One account profile.",
            "Angel One account",
            "https://www.angelone.in/",
        ),
        "Password": (
            "Authenticates your Angel One account for this broker session.",
            "Enter your Angel One account password; it is cleared after sign-in.",
            "Angel One account",
            "https://www.angelone.in/",
        ),
        "TOTP secret": (
            "Generates the time-based one-time code required for SmartAPI sign-in.",
            "Use the authenticator secret from your Angel One TOTP setup, not a current 6-digit code.",
            "Angel One support",
            "https://www.angelone.in/support",
        ),
    },
    "Dhan": {
        "Client ID": (
            "Identifies the Dhan account that will authorize this API app.",
            "Find your client ID in your Dhan profile; use the same account as the API app.",
            "Dhan authentication guide",
            "https://dhanhq.co/docs/v2/authentication/",
        ),
        "API key": (
            "Identifies the account-specific Dhan API app during consent creation.",
            "In Dhan Web, open My Profile → Access DhanHQ APIs → API key.",
            "Dhan authentication guide",
            "https://dhanhq.co/docs/v2/authentication/",
        ),
        "API secret": (
            "Proves the Dhan API app's identity when consent is exchanged for a token.",
            "Copy the API secret generated with that API key; keep it private.",
            "Dhan authentication guide",
            "https://dhanhq.co/docs/v2/authentication/",
        ),
        "Callback URL": (
            "Returns your browser to this app after you approve Dhan access.",
            "Register this exact URL while creating the API key in Dhan Web.",
            "Dhan authentication guide",
            "https://dhanhq.co/docs/v2/authentication/",
        ),
    },
}


st.set_page_config(
    page_title=APP_TITLE,
    page_icon=":material/verified_user:",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.html(app_animation_css())
st.html(gold_theme_css())


def _render_brand_lockup() -> None:
    st.html(brand_lockup_html(APP_BRAND, APP_BRAND_EXPANSION, APP_BRAND_DESCRIPTION))


def _render_header_feature_badges() -> None:
    with st.container(
        horizontal=True,
        vertical_alignment="center",
        gap="small",
        wrap=True,
    ):
        st.badge(
            "AI insights",
            icon=":material/lightbulb:",
            color="violet",
        )
        with st.popover(
            ":material/info:",
            type="tertiary",
            help="What AI insights do and when to use them.",
        ):
            st.markdown("**AI-assisted portfolio and market review**")
            st.markdown(
                "Run a full-portfolio review or a workspace-specific analysis "
                "for equity, trades, mutual funds, and the F&O workspace. "
                "Depending on the selected mode, the AI can summarize holdings "
                "and risks or draft a hypothetical deployment plan with candidate "
                "scores and explanations."
            )
            st.markdown(
                "- Market research and scanners show technical indicators and "
                "signals from public historical data; they do not guarantee "
                "future performance.\n"
                "- Portfolio AI analysis runs only when you submit it and uses "
                "the provider, model, and key you selected. Provider quotas or "
                "charges may apply.\n"
                "- Recommendations are advisory. They are checked by JEV rules "
                "and are never submitted automatically.\n"
                "- **Ask the AI guide** is a separate local app guide and "
                "does not use an AI key."
            )
            st.markdown(
                "[Google AI Studio and Gemini API](https://ai.google.dev/gemini-api/docs) · "
                "[SEBI investor education](https://investor.sebi.gov.in/)"
            )

        st.badge(
            "JEV guardrails",
            icon=":material/verified_user:",
            color="blue",
        )
        with st.popover(
            ":material/info:",
            type="tertiary",
            help="How the deterministic JEV checks protect app-issued buys.",
        ):
            st.markdown("**Explainable checks before a recommendation is approved**")
            st.markdown(
                "The JEV engine evaluates candidate trades in a fixed order and "
                "keeps an audit trail of the checks and outcome:"
            )
            st.markdown(
                "1. **Daily-loss circuit breaker:** blocks new buys for 24 hours "
                "when the user-reported realized daily loss is at or below −3%.\n"
                "2. **Trading window:** allows new buys only on weekdays from "
                "09:45 to 15:00 IST.\n"
                "3. **Focus cap:** blocks new buys at eight active positions.\n"
                "4. **Confidence boundary:** excludes AI candidates below 70%.\n"
                "5. **Capital and allocation:** above ₹20,000 cash, sizes "
                "equal-weight scenarios within the user's allocation cap.\n"
                "6. **Scarce-cash triage:** favors affordable candidates in "
                "sectors not already held, then ranks by confidence."
            )
            st.caption(
                "These checks apply to orders placed through this app, not the "
                "broker's own platform. Daily realized loss is entered manually "
                "because a verified common broker feed is unavailable."
            )
            st.markdown(
                "[NSE market timings](https://www.nseindia.com/market-data/market-timings) · "
                "[SEBI investor education](https://investor.sebi.gov.in/)"
            )


def _user_is_authenticated() -> bool:
    return bool(st.user.get("is_logged_in", False))


if settings.require_login and not _user_is_authenticated():
    _render_brand_lockup()
    st.caption("Sign in securely to open your private investment workspace.")
    if st.button("Sign in", type="primary", icon=":material/login:"):
        st.login()
    st.stop()

st.session_state.setdefault("login_steps", [])
st.session_state.setdefault("dynamic_broker_states", {})
st.session_state.setdefault("user_preferences", {})
st.session_state.setdefault("chat_history", [])
st.session_state.setdefault("tenant_session_id", uuid4().hex)
st.session_state.setdefault("max_allocation_pct", 100.0)
st.session_state.setdefault("realized_daily_loss_pct", 0.0)
st.session_state.setdefault("resend_api_key_session", "")
st.session_state.setdefault("resend_sender_session", settings.resend_sender)
st.session_state.setdefault("email_delivery_provider", "SMTP")
st.session_state.setdefault("broker_profile_name", "")
st.session_state.setdefault("profile_fetch_attempted", False)
st.session_state.setdefault("buy_lock_until", None)
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
st.session_state.setdefault("broker_data_errors", {})
st.session_state.setdefault("risk_high_water", {})
st.session_state.setdefault("risk_alerts", [])
st.session_state.setdefault("risk_errors", [])
st.session_state.setdefault("whatsapp_account_sid", "")
st.session_state.setdefault("whatsapp_auth_token", "")
st.session_state.setdefault("whatsapp_sender", "")
st.session_state.setdefault("whatsapp_recipient", "")
st.session_state.setdefault("email_smtp_host", "")
st.session_state.setdefault("email_smtp_port", 587)
st.session_state.setdefault("email_smtp_username", "")
st.session_state.setdefault("email_smtp_password", "")
st.session_state.setdefault("email_sender", "")
st.session_state.setdefault("email_recipient", "")
st.session_state.setdefault("alerted_risk_channels", [])
st.session_state.setdefault("analysis_result", None)
st.session_state.setdefault("analysis_results", {})
st.session_state.setdefault("analysis_errors", {})
st.session_state.setdefault("analysis_price_errors", {})
st.session_state.setdefault("ai_settings_prompt", "")
st.session_state.setdefault("llm_provider_settings", {})
st.session_state.pop("sidebar_openrouter_api_key", None)
st.session_state.pop("user_llm_api_key_openrouter", None)
st.session_state.pop("llm_model_selection_openrouter", None)
if isinstance(st.session_state.llm_provider_settings, dict):
    st.session_state.llm_provider_settings.pop("OpenRouter", None)
if st.session_state.get("llm_provider") not in LLM_PROVIDER_BASE_URLS:
    st.session_state.llm_provider = "Gemini"
st.session_state.setdefault("planning_budget_inr", 100_000.0)
st.session_state.setdefault("analysis_live_prices", {})
st.session_state.setdefault("analysis_live_prices_updated_at", None)
st.session_state.setdefault("market_ai_candidates", [])
st.session_state.setdefault("market_ai_candidates_updated_at", None)
if not isinstance(st.session_state.market_ai_candidates, list):
    st.session_state.market_ai_candidates = []
st.session_state.setdefault("analysis_price_error", None)
st.session_state.setdefault("approved_trades", [])
st.session_state.setdefault("approved_trades_by_scope", {})
st.session_state.setdefault("audit_trail", [])
st.session_state.setdefault("audit_trails_by_scope", {})
st.session_state.setdefault("enabled_jev_rules", [1, 2, 3, 4, 5, 6])
st.session_state.setdefault("jev_rule_selection_before_analysis", [1, 2, 3, 4, 5, 6])
st.session_state.setdefault("jev_rule_selection_after_analysis", [1, 2, 3, 4, 5, 6])
st.session_state.setdefault("max_allocation_pct", 30.0)
st.session_state.setdefault("realized_daily_loss_pct", 0.0)
st.session_state.setdefault("buy_lock_until", None)
st.session_state.setdefault("login_error", None)
st.session_state.setdefault("pending_broker_name", None)
st.session_state.setdefault("pending_broker_adapter", None)
st.session_state.setdefault("pending_login_url", None)
st.session_state.setdefault("pending_redirect_url", None)
st.session_state.setdefault("pending_oauth_fingerprint", None)
st.session_state.setdefault("pending_oauth_expires_at", None)


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


def _render_oauth_tab_link(label: str, url: str) -> None:
    """Render an explicit broker authorization link in a separate tab."""
    safe_url = html.escape(url, quote=True)
    safe_label = html.escape(label)
    st.html(
        '<a href="'
        + safe_url
        + '" target="_blank" rel="noopener noreferrer" '
        'style="display:inline-flex;align-items:center;justify-content:center;'
        'gap:.55rem;padding:.72rem 1.25rem;border:1px solid #16a34a;'
        'border-radius:.75rem;background:linear-gradient(135deg,#22c55e 0%,'
        '#15803d 100%);box-shadow:0 5px 14px rgba(22,163,74,.28);color:#fff;'
        'font-weight:600;text-decoration:none">'
        f"<span>{safe_label}</span>"
        '<span aria-hidden="true" style="font-size:1.1rem">&#8599;</span></a>'
    )


def _render_app_header(connected_broker: str | None = None) -> None:
    with st.container(border=True, gap="medium"):
        with st.container(
            horizontal=True,
            horizontal_alignment="distribute",
            vertical_alignment="center",
            gap="medium",
            wrap=True,
        ):
            _render_brand_lockup()
            with st.container(
                horizontal=True,
                horizontal_alignment="right",
                vertical_alignment="center",
                gap="small",
                wrap=True,
            ):
                _render_portfolio_share()
                _render_notification_settings()
                _render_agent()
        with st.container(
            horizontal=True,
            horizontal_alignment="distribute",
            vertical_alignment="center",
            gap="small",
            wrap=True,
        ):
            _render_header_feature_badges()
            if connected_broker:
                with st.container(
                    horizontal=True,
                    vertical_alignment="center",
                    gap="small",
                    wrap=True,
                ):
                    st.badge(
                        f"{connected_broker} connected",
                        icon=":material/check_circle:",
                        color="green",
                    )
                    st.button(
                        "Switch account",
                        key="header_disconnect_broker",
                        on_click=_logout,
                        icon=":material/swap_horiz:",
                        help="Disconnect this broker and connect another account.",
                    )
            else:
                st.badge(
                    "No broker connected",
                    icon=":material/link_off:",
                    color="gray",
                )


def _tenant_identifier() -> str:
    if _user_is_authenticated():
        email = str(st.user.get("email", "")).strip().lower()
        subject = str(st.user.get("sub", "")).strip()
        if email or subject:
            return email or subject
    return "local-session:" + str(st.session_state.tenant_session_id)


def _render_sidebar_settings() -> None:
    if settings.require_login and _user_is_authenticated():
        with st.sidebar:
            st.button(
                "Sign out",
                icon=":material/logout:",
                on_click=_logout_oidc,
            )


def _render_risk_controls() -> None:
    st.markdown("**Risk & execution settings**")
    allocation_column, loss_column = st.columns(2)
    with allocation_column:
        st.number_input(
            "Maximum scenario allocation (%)",
            min_value=0.0,
            max_value=100.0,
            step=5.0,
            key="max_allocation_pct",
            help="Caps the hypothetical deployment budget used by trade rules.",
        )
    with loss_column:
        st.number_input(
            "Realized daily loss (%)",
            min_value=-100.0,
            max_value=100.0,
            step=0.25,
            key="realized_daily_loss_pct",
            help=(
                "Enter your broker's realized daily P&L percentage. The current "
                "broker adapters do not provide a verified daily realized-P&L feed."
            ),
        )
    st.session_state.user_preferences.update(
        {
            "max_allocation_pct": st.session_state.max_allocation_pct,
            "realized_daily_loss_pct": st.session_state.realized_daily_loss_pct,
        }
    )
    st.caption(
        "A reported realized loss of −3% or more activates the 24-hour buy "
        "circuit breaker. Open-position mark-to-market P&L is not included; "
        "the loss value is user-reported, not broker-verified."
    )
    if float(st.session_state.realized_daily_loss_pct) <= -3.0:
        st.warning(
            "Reported daily loss has reached the circuit-breaker threshold. "
            "New app-issued buys will be blocked."
        )


APP_TAB_LABELS = (
    "Home",
    "Equities",
    "Trades",
    "F&O",
    "Mutual Funds",
)

APP_TABS = (
    ":material/home: Home",
    ":material/show_chart: Equities",
    ":material/swap_horiz: Trades",
    ":material/query_stats: F&O",
    ":material/savings: Mutual Funds",
)


def _render_header_tabs():
    legacy_navigation = {
        "Home": APP_TAB_LABELS[0],
        "Equities": APP_TAB_LABELS[1],
        "Equity / Debt": APP_TAB_LABELS[1],
        "Debt": APP_TAB_LABELS[1],
        "Trading": APP_TAB_LABELS[2],
        "Trades": APP_TAB_LABELS[2],
        "Options": APP_TAB_LABELS[3],
        "Futures": APP_TAB_LABELS[3],
        "F&O": APP_TAB_LABELS[3],
        "Mutual Funds": APP_TAB_LABELS[4],
        "Mutual funds": APP_TAB_LABELS[4],
        "Market research": APP_TAB_LABELS[1],
        "Market scanner": APP_TAB_LABELS[1],
        ":material/home: Home": APP_TAB_LABELS[0],
        ":material/account_balance: Equity / Debt": APP_TAB_LABELS[1],
        ":material/show_chart: Equities": APP_TAB_LABELS[1],
        ":material/swap_horiz: Trades": APP_TAB_LABELS[2],
        ":material/swap_vert: Trades": APP_TAB_LABELS[2],
        ":material/query_stats: F&O": APP_TAB_LABELS[3],
        ":material/savings: Mutual Funds": APP_TAB_LABELS[4],
    }
    current_navigation = st.session_state.get("app_navigation")
    if current_navigation in legacy_navigation:
        st.session_state["app_navigation"] = legacy_navigation[current_navigation]
        current_navigation = st.session_state["app_navigation"]
    if current_navigation in APP_TAB_LABELS:
        index = APP_TAB_LABELS.index(current_navigation)
        st.session_state["app_navigation"] = APP_TABS[index]
    st.session_state["_run_seq"] = st.session_state.get("_run_seq", 0) + 1
    if st.session_state.pop("return_to_home", False):
        st.session_state["app_navigation"] = APP_TABS[0]
    tabs = st.tabs(
        APP_TABS,
        key="app_navigation",
        on_change="rerun",
        width="stretch",
    )
    return {
        label: tab
        for label, tab in zip(APP_TAB_LABELS, tabs, strict=True)
    }


def _render_app_footer() -> None:
    with st.container(
        horizontal=True,
        horizontal_alignment="distribute",
        vertical_alignment="center",
    ):
        st.caption("GGHP · Private session")
        st.caption("Informational only; not investment advice.")


def _render_broker_credential_field(
    broker: str,
    field: str,
    label: str,
    *,
    key: str | None = None,
    value: str | None = None,
    input_type: str = "default",
    disabled: bool = False,
    help_text: str,
    live: bool = True,
) -> None:
    why, how, link_label, link_url = BROKER_FIELD_GUIDES[broker][field]
    with st.container(
        horizontal=True,
        width="content",
        vertical_alignment="center",
        gap="xxsmall",
    ):
        st.markdown(label, width="content")
        with st.popover(
            ":material/help:",
            type="tertiary",
            help=f"Why this field is needed and how to get it: {label}",
        ):
            st.markdown(f"**Why you need this**\n\n{why}")
            st.markdown(f"**How to get it**\n\n{how}")
            st.caption(help_text)
            st.markdown(f"[{link_label} ↗]({link_url})")
    input_options = {
        "type": input_type,
        "disabled": disabled,
        "live": live,
        "label_visibility": "collapsed",
        "width": 760,
    }
    if key is not None:
        input_options["key"] = key
    if value is not None:
        input_options["value"] = value
    st.text_input(label, **input_options)


def _render_inline_help_label(label: str, explanation: str) -> None:
    with st.container(
        horizontal=True,
        width="content",
        vertical_alignment="center",
        gap="xxsmall",
    ):
        st.markdown(label, width="content")
        with st.popover(
            ":material/help:",
            type="tertiary",
            help=explanation,
        ):
            st.write(explanation)


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


def _session_whatsapp_configuration() -> dict[str, str]:
    return {
        "account_sid": str(st.session_state.get("whatsapp_account_sid", "")),
        "auth_token": str(st.session_state.get("whatsapp_auth_token", "")),
        "sender": str(st.session_state.get("whatsapp_sender", "")),
        "recipient": str(st.session_state.get("whatsapp_recipient", "")),
    }


def _render_notification_settings() -> None:
    with st.popover(
        "WhatsApp",
        icon=":material/chat:",
        help="Configure WhatsApp risk alerts for this browser session.",
    ):
        st.caption(
            "Uses Twilio WhatsApp. Credentials stay in this browser session."
        )
        st.link_button(
            "WhatsApp setup guide ↗",
            "https://www.twilio.com/docs/whatsapp/sandbox",
            width="stretch",
        )
        st.text_input("Twilio Account SID", key="whatsapp_account_sid")
        st.text_input(
            "Twilio Auth Token",
            type="password",
            key="whatsapp_auth_token",
        )
        st.text_input(
            "WhatsApp sender",
            key="whatsapp_sender",
            placeholder="whatsapp:+14155238886",
            help="Use the WhatsApp-enabled Twilio number in whatsapp:+countrycode format.",
        )
        st.text_input(
            "WhatsApp recipient",
            key="whatsapp_recipient",
            placeholder="whatsapp:+15551234567",
            help="Use your verified/test recipient number in whatsapp:+countrycode format.",
        )
        if st.button(
            "Send test WhatsApp",
            key="whatsapp_test_notification",
            icon=":material/send:",
        ):
            try:
                send_whatsapp_update(
                    "GGHP WhatsApp notifications are connected.",
                    _session_whatsapp_configuration(),
                )
            except (requests.RequestException, RuntimeError, ValueError) as error:
                log_failure("WhatsApp test notification", error)
                st.error("WhatsApp test notification could not be delivered.")
            else:
                st.success("WhatsApp test notification sent.")

    with st.popover(
        "Email",
        icon=":material/alternate_email:",
        help="Configure secure email alerts for this browser session.",
    ):
        st.selectbox(
            "Email delivery",
            ("SMTP", "Resend"),
            key="email_delivery_provider",
        )
        email_provider = st.session_state.email_delivery_provider
        st.caption(
            "Credentials stay in this browser session."
            if email_provider == "SMTP"
            else "Uses the Resend HTTPS API. The API key stays in this browser session."
        )
        if email_provider == "Resend":
            st.link_button(
                "Resend API keys ↗",
                "https://resend.com/api-keys",
                width="stretch",
            )
            st.text_input(
                "Resend API key",
                type="password",
                key="resend_api_key_session",
            )
            st.text_input(
                "Verified Resend sender",
                key="resend_sender_session",
                placeholder="GGHP <updates@example.com>",
            )
            st.text_input("Recipient email", key="email_recipient")
        else:
            st.link_button(
                "Gmail app password guide ↗",
                "https://support.google.com/accounts/answer/185833",
                width="stretch",
            )
            st.text_input("SMTP server", key="email_smtp_host")
            st.selectbox(
                "SMTP port",
                options=(465, 587),
                key="email_smtp_port",
                help="Use 465 for SSL or 587 for STARTTLS.",
            )
            st.text_input("SMTP username", key="email_smtp_username")
            st.text_input(
                "SMTP password / app password",
                type="password",
                key="email_smtp_password",
            )
            st.text_input("From email", key="email_sender")
            st.text_input("Recipient email", key="email_recipient")
        if st.button(
            "Send test email",
            key="email_test_notification",
            icon=":material/send:",
        ):
            try:
                if email_provider == "Resend":
                    send_resend_email(
                        "GGHP email notifications are connected",
                        "<p>GGHP email notifications are connected.</p>",
                        str(st.session_state.email_recipient),
                        api_key=str(st.session_state.resend_api_key_session),
                        sender=str(st.session_state.resend_sender_session),
                    )
                else:
                    send_email_alert(
                        "GGHP email notifications are connected.",
                        smtp_host=str(st.session_state.email_smtp_host),
                        smtp_port=int(st.session_state.email_smtp_port),
                        username=str(st.session_state.email_smtp_username),
                        password=str(st.session_state.email_smtp_password),
                        sender=str(st.session_state.email_sender),
                        recipient=str(st.session_state.email_recipient),
                    )
            except (OSError, requests.RequestException, RuntimeError, ValueError) as error:
                log_failure("Email test notification", error)
                st.error("Email test notification could not be delivered.")
            else:
                st.success("Test email sent.")


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
    except (BrokerAPIError, sqlite3.Error, OSError, ValueError) as error:
        log_failure(f"{broker_name} OAuth setup", error, broker=broker_name)
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
    for key in tuple(st.session_state):
        if key.startswith("dynamic_broker_token_"):
            st.session_state.pop(key, None)
    st.session_state.dynamic_broker_states = {}
    st.session_state.broker_profile_name = ""
    st.session_state.profile_fetch_attempted = False
    st.session_state.buy_lock_until = None
    st.session_state.chat_history = []


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
    except (BrokerAPIError, requests.RequestException, ValueError) as error:
        log_failure("Angel One authentication", error, broker="Angel One")
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
        st.session_state.broker_data_errors = {}
        st.session_state.alerted_risk_channels = []
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
    except (BrokerAPIError, requests.RequestException, ValueError) as error:
        log_failure("Dhan authorization setup", error, broker="Dhan")
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
    st.session_state.broker_profile_name = ""
    st.session_state.profile_fetch_attempted = False
    st.session_state.buy_lock_until = None
    st.session_state.chat_history = []
    st.session_state.return_to_home = True
    st.session_state.last_sync = None
    st.session_state.last_sync_attempt = None
    st.session_state.broker_data_errors = {}
    st.session_state.alerted_risk_channels = []
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
        width=760,
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
                ) as error:
                    log_failure("Upstox OAuth callback", error, broker="Upstox")
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


def _render_broker_connection() -> None:
    st.html(broker_connect_button_css())
    st.caption(
        "Connect a broker to load your portfolio, cash balance, and supported "
        "holdings. Credentials are used only for sign-in and are not saved to "
        "server configuration. AI suggestions are informational, not investment "
        "advice; provider availability and charges may apply."
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
        dynamic_brokers = []
        if turso_is_configured():
            try:
                dynamic_brokers = list_dynamic_brokers(_tenant_identifier())
            except (requests.RequestException, RuntimeError, ValueError) as error:
                log_failure("Custom broker registry lookup", error)
                st.caption("The custom broker registry is temporarily unavailable.")
        broker_choice = st.selectbox(
            "Select broker",
            [
                "Upstox",
                "Angel One",
                "Zerodha",
                "Dhan",
                *[f"Custom broker: {name}" for name in dynamic_brokers],
            ],
            key="selected_broker",
        )

    if st.session_state.login_error:
        st.error(st.session_state.login_error)
        st.session_state.login_error = None

    if broker_choice.startswith("Custom broker: "):
        broker_name = broker_choice.removeprefix("Custom broker: ").strip()
        if st.button(
            f"Load {broker_name} connection",
            key="load_dynamic_broker_configuration",
        ):
            try:
                configuration = load_dynamic_broker(
                    _tenant_identifier(), broker_name
                )
                if configuration is None:
                    raise ValueError("This custom broker is no longer registered.")
                adapter = BrokerFactory.create_dynamic_adapter(configuration)
                st.session_state.dynamic_broker_states[broker_name] = adapter
            except (
                requests.RequestException,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                log_failure("Custom broker configuration", error)
                st.error("The custom broker configuration could not be loaded.")
            else:
                st.rerun()
        adapter = st.session_state.dynamic_broker_states.get(broker_name)
        if isinstance(adapter, GenericDynamicAdapter):
            token_value = st.text_input(
                "Custom broker bearer token",
                key=f"dynamic_broker_token_{hashlib.sha256(broker_name.encode()).hexdigest()[:12]}",
                type="password",
            )
            if st.button(
                f"Connect with {broker_name}",
                key=f"connect_dynamic_broker_{hashlib.sha256(broker_name.encode()).hexdigest()[:12]}",
                type="primary",
                disabled=not token_value.strip(),
            ):
                token = adapter.authenticate(token_value, "")
                _complete_broker_login(f"Custom: {broker_name}", adapter, token)
                st.rerun()
            st.caption(
                "Read-only connection. Broker tokens remain in this session; "
                "order placement is disabled for custom adapters."
            )
    elif broker_choice in {"Upstox", "Zerodha"}:
        redirect_url = _redirect_url()
        if not callback_broker:
            _render_broker_credential_field(
                broker_choice,
                "Callback URL",
                "Callback URL",
                value=redirect_url,
                disabled=True,
                help_text=(
                    "Register this exact address in your broker developer app "
                    "so it can return you here after authorization."
                ),
            )
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
            ) as error:
                log_failure("Upstox OAuth callback", error, broker="Upstox")
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
            _render_broker_credential_field(
                broker_choice,
                "API key",
                f"{broker_choice} API key",
                key=api_key_key,
                input_type="password",
                help_text="Public identifier for your broker developer app.",
            )
            _render_broker_credential_field(
                broker_choice,
                "API secret",
                f"{broker_choice} API secret",
                key=api_secret_key,
                input_type="password",
                help_text="Private credential used to verify the broker app; never share it.",
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
                if broker_choice == "Upstox":
                    _render_oauth_tab_link(button_label, login_url)
                    with st.container(border=True):
                        st.markdown("**Upstox sign-in is ready**")
                        st.write(
                            "Click the green button to open Upstox in a new tab. "
                            "Complete approval there; the original app tab stays open "
                            "and finishes the connection when Upstox redirects back."
                        )
                        st.caption(
                            "If no tab appears, allow pop-ups for this site and click "
                            "Connect with Upstox again."
                        )
                else:
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
                "Enter the credentials in each field and use its adjacent Guide "
                "for the purpose, setup steps, and official reference."
            )
            _render_broker_credential_field(
                "Angel One",
                "Callback URL",
                "Callback URL",
                value="Not required for Angel One",
                disabled=True,
                help_text=(
                    "Angel One authenticates directly and does not use an OAuth "
                    "callback URL."
                ),
            )
            _render_broker_credential_field(
                "Angel One",
                "API key",
                "Angel One API key",
                key="angel_api_key",
                input_type="password",
                help_text="Identifies the SmartAPI application used for this connection.",
            )
            _render_broker_credential_field(
                "Angel One",
                "Client ID",
                "Client ID",
                key="angel_client_id",
                help_text="Your account identifier shown in your Angel One profile.",
            )
            _render_broker_credential_field(
                "Angel One",
                "Password",
                "Password",
                key="angel_password",
                input_type="password",
                help_text="Your Angel One account password; not stored after authentication.",
            )
            _render_broker_credential_field(
                "Angel One",
                "TOTP secret",
                "TOTP secret",
                key="angel_totp_secret",
                input_type="password",
                help_text="Authenticator setup secret, not the rotating 6-digit TOTP code.",
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
            _render_broker_credential_field(
                "Dhan",
                "Callback URL",
                "Callback URL",
                value=_redirect_url(),
                disabled=True,
                help_text=(
                    "Register this exact address in your Dhan API app so Dhan "
                    "can return you here after authorization."
                ),
            )
            _render_broker_credential_field(
                "Dhan",
                "Client ID",
                "Dhan Client ID",
                key="dhan_client_id",
                help_text="Account identifier; it must match the account that created this API app.",
            )
            _render_broker_credential_field(
                "Dhan",
                "API key",
                "Dhan API key",
                key="dhan_api_key",
                input_type="password",
                help_text="App identifier generated for the selected Dhan account.",
            )
            _render_broker_credential_field(
                "Dhan",
                "API secret",
                "Dhan API secret",
                key="dhan_api_secret",
                input_type="password",
                help_text="Private app credential paired with your Dhan API key.",
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
                except (
                    BrokerAPIError,
                    requests.RequestException,
                    ValueError,
                ) as error:
                    log_failure("Dhan OAuth callback", error, broker="Dhan")
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
    _render_dynamic_broker_registry_panel()


def _render_dynamic_broker_registry_panel() -> None:
    with st.expander(
        "Register a custom broker (read-only)",
        icon=":material/add_link:",
    ):
        if not turso_is_configured():
            st.info(
                "Set TURSO_PRIMARY_DB_URL and TURSO_AUTH_TOKEN in the server "
                "environment to enable tenant-scoped broker definitions."
            )
            st.link_button(
                "Turso setup guide ↗",
                "https://docs.turso.tech/",
                width="stretch",
            )
            return
        st.caption(
            "Only the server-allow-listed HTTPS host is accepted. Configure "
            "DYNAMIC_BROKER_ALLOWED_HOSTS on the server. Tokens and passwords "
            "are never stored with this endpoint definition."
        )
        with st.form("dynamic_broker_registration"):
            name = st.text_input(
                "Broker display name",
                max_chars=80,
                placeholder="My broker API",
            )
            api_base = st.text_input(
                "Broker API base URL",
                placeholder="https://api.yourbroker.example",
            )
            balance_path = st.text_input(
                "Balance endpoint path",
                value="/v1/account/balance",
            )
            positions_path = st.text_input(
                "Positions endpoint path",
                value="/v1/portfolio/positions",
            )
            holdings_path = st.text_input(
                "Long-term holdings endpoint path",
                value="/v1/portfolio/holdings",
            )
            profile_path = st.text_input(
                "Profile endpoint path (optional)",
                value="/v1/account/profile",
            )
            prices_path = st.text_input(
                "Live-prices endpoint path (optional)",
                value="/v1/market/prices",
            )
            submitted = st.form_submit_button(
                "Save read-only broker",
                type="primary",
                icon=":material/save:",
            )
        if submitted:
            configuration = {
                "api_base": api_base,
                "endpoints": {
                    "balance": balance_path,
                    "positions": positions_path,
                    "holdings": holdings_path,
                    "profile": profile_path,
                    "live_prices": prices_path,
                },
            }
            try:
                register_dynamic_broker(
                    _tenant_identifier(), name, configuration
                )
            except (
                requests.RequestException,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                log_failure("Custom broker registration", error)
                st.error(
                    "The broker was not saved. Check the name, allow-listed host, "
                    "endpoint paths, and Turso configuration."
                )
            else:
                st.success("Read-only broker definition saved.")
                st.rerun()


def _show_login() -> None:
    _render_sidebar_settings()
    _render_app_header()
    with st.container(border=True, gap="medium"):
        tabs = _render_header_tabs()
        if tabs[APP_TAB_LABELS[0]].open:
            with tabs[APP_TAB_LABELS[0]]:
                _render_broker_connection()
                _render_portfolio_ai_review()
        for asset in APP_TAB_LABELS[1:]:
            if tabs[asset].open:
                with tabs[asset]:
                    _render_asset_workspace(asset, broker_connected=False)
        _render_app_footer()


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
        data_errors: dict[str, str] = {}
        data_refreshed = False
        broker_name = str(st.session_state.broker_name)
        fetchers = {
            "cash": adapter.fetch_balance,
            "trading": adapter.fetch_positions,
            "equity": adapter.fetch_holdings,
            "mutual_funds": adapter.fetch_mutual_fund_holdings,
        }
        operations = {
            "cash": "Cash balance refresh",
            "trading": "Trading positions refresh",
            "equity": "Equity holdings refresh",
            "mutual_funds": "Mutual-fund holdings refresh",
        }
        with st.status(
            f"Connecting to {broker_name} and loading account data…",
            expanded=False,
        ) as sync_status:
            with ThreadPoolExecutor(
                max_workers=len(fetchers), thread_name_prefix="broker-sync"
            ) as executor:
                pending = {
                    executor.submit(fetcher, token): feed
                    for feed, fetcher in fetchers.items()
                }
                completed = 0
                for future in as_completed(pending):
                    feed = pending[future]
                    completed += 1
                    sync_status.update(
                        label=f"Loading {broker_name} data ({completed}/{len(fetchers)})…"
                    )
                    try:
                        result = future.result()
                    except BrokerCapabilityError as error:
                        if feed != "mutual_funds":
                            raise
                        st.session_state.mutual_fund_error = str(error)
                        st.session_state.mutual_fund_manual_import = True
                    except (
                        BrokerAPIError,
                        requests.RequestException,
                        ValueError,
                        RuntimeError,
                    ) as error:
                        diagnostic = log_failure(
                            operations[feed], error, broker=broker_name
                        )
                        if feed == "mutual_funds":
                            data_errors[feed] = (
                                f"{diagnostic} The last successful data remains available."
                            )
                            st.session_state.mutual_fund_error = diagnostic
                            st.session_state.mutual_fund_manual_import = True
                        else:
                            data_errors[feed] = diagnostic
                    else:
                        if feed == "cash":
                            st.session_state.balance = result
                            item_count = 1
                        elif feed == "trading":
                            st.session_state.portfolio = result
                            item_count = len(result)
                        elif feed == "equity":
                            st.session_state.equity_holdings = result
                            item_count = len(result)
                        else:
                            try:
                                broker_funds = validate_mutual_funds(result)
                            except (TypeError, ValueError) as error:
                                diagnostic = log_failure(
                                    "Mutual-fund holdings validation",
                                    error,
                                    broker=broker_name,
                                )
                                data_errors[feed] = (
                                    f"{diagnostic} The last successful data remains available."
                                )
                                st.session_state.mutual_fund_error = diagnostic
                                st.session_state.mutual_fund_manual_import = True
                                continue
                            st.session_state.mutual_funds = broker_funds
                            st.session_state.mutual_fund_error = (
                                "No mutual-fund holdings were returned. You can import a "
                                "current statement CSV."
                                if broker_funds.empty
                                else None
                            )
                            st.session_state.mutual_fund_manual_import = (
                                broker_funds.empty
                            )
                            st.session_state.mutual_fund_updated_at = datetime.now(
                                ZoneInfo(settings.timezone)
                            )
                            item_count = len(broker_funds)
                        log_success(
                            operations[feed],
                            broker=broker_name,
                            item_count=item_count,
                        )
                        data_refreshed = True
            sync_status.update(
                label=(
                    f"{broker_name} data loaded"
                    if not data_errors
                    else f"{broker_name} connected; some data could not be loaded"
                ),
                state="complete",
            )

        st.session_state.broker_data_errors = data_errors
        st.session_state.sync_failed = bool(data_errors)
        if data_refreshed:
            st.session_state.last_sync = datetime.now(ZoneInfo(settings.timezone))

    equity_holdings = st.session_state.equity_holdings
    portfolio = st.session_state.portfolio
    if st.session_state.sync_failed:
        failed_feeds = {
            "cash": "cash balance",
            "trading": "trading positions",
            "equity": "equity holdings",
            "mutual_funds": "mutual-fund holdings",
        }
        failed_labels = [
            failed_feeds[feed]
            for feed in st.session_state.broker_data_errors
            if feed in failed_feeds
        ]
        st.warning(
            "Could not refresh "
            + ", ".join(failed_labels)
            + "; showing the last successful data."
        )
        with st.expander("Refresh diagnostics"):
            for feed, diagnostic in st.session_state.broker_data_errors.items():
                st.caption(f"**{feed.replace('_', ' ').title()}:** {diagnostic}")
    elif st.session_state.last_sync:
        st.caption(
            "Last broker sync: "
            f"{st.session_state.last_sync.strftime('%d %b %Y, %H:%M:%S IST')}"
        )

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
                log_failure(
                    f"Market risk quote for {ticker}",
                    error,
                    broker=st.session_state.broker_name,
                )
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
    st.session_state.alerted_risk_channels = [
        delivery
        for delivery in st.session_state.alerted_risk_channels
        if delivery.split(":", 1)[0] in breached_tickers
    ]
    for ticker, quantity, boundary in risk_rows:
        st.error(
            f"🚨 Market Exit warning: {ticker} is below its 3-ATR floor "
            f"(₹{boundary:,.2f}). Review before placing an order."
        )
        alert_text = (
            f"Market Exit warning: {ticker} fell below its 3-ATR "
            f"risk boundary of INR {boundary:.2f}."
        )
        whatsapp_configuration = _session_whatsapp_configuration()
        whatsapp_configured = whatsapp_update_is_configured(
            whatsapp_configuration
        )
        email_provider = str(st.session_state.email_delivery_provider)
        if email_provider == "Resend":
            email_configured = all(
                (
                    bool(
                        str(st.session_state.get("resend_api_key_session", "")).strip()
                        or settings.resend_api_key
                    ),
                    bool(
                        str(st.session_state.get("resend_sender_session", "")).strip()
                        or settings.resend_sender
                    ),
                    bool(str(st.session_state.get("email_recipient", "")).strip()),
                )
            )
        else:
            email_username = str(st.session_state.email_smtp_username).strip()
            email_password = str(st.session_state.email_smtp_password).strip()
            email_configured = all(
                str(st.session_state[key]).strip()
                for key in ("email_smtp_host", "email_sender", "email_recipient")
            ) and bool(email_username) == bool(email_password)
        delivery_options = (
            ("WhatsApp", "whatsapp", whatsapp_configured),
            ("Email", "email", email_configured),
        )
        enabled_channels = [item for item in delivery_options if item[2]]
        delivered = 0
        for channel_label, channel_key, configured in enabled_channels:
            delivery_key = f"{ticker}:{channel_key}"
            if delivery_key in st.session_state.alerted_risk_channels:
                delivered += 1
                continue
            try:
                if channel_key == "whatsapp":
                    send_whatsapp_update(
                        alert_text, whatsapp_configuration
                    )
                else:
                    if email_provider == "Resend":
                        send_resend_email(
                            f"GGHP risk alert: {ticker}",
                            f"<p>{html.escape(alert_text)}</p>",
                            str(st.session_state.email_recipient),
                            api_key=str(st.session_state.resend_api_key_session),
                            sender=str(st.session_state.resend_sender_session),
                        )
                    else:
                        send_email_alert(
                            alert_text,
                            smtp_host=str(st.session_state.email_smtp_host),
                            smtp_port=int(st.session_state.email_smtp_port),
                            username=str(st.session_state.email_smtp_username),
                            password=str(st.session_state.email_smtp_password),
                            sender=str(st.session_state.email_sender),
                            recipient=str(st.session_state.email_recipient),
                        )
            except (
                OSError,
                requests.RequestException,
                RuntimeError,
                ValueError,
            ) as error:
                log_failure(f"{channel_label} risk notification", error)
                st.caption(f"{channel_label} alert could not be delivered for {ticker}.")
            else:
                st.session_state.alerted_risk_channels.append(delivery_key)
                delivered += 1
        if enabled_channels and delivered == 0:
            st.caption(
                "No risk alert sent. Review the configured WhatsApp or email "
                "settings in the header."
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
            except (
                BrokerAPIError,
                requests.RequestException,
                ValueError,
                RuntimeError,
            ) as error:
                log_failure(
                    f"Market exit order for {ticker}",
                    error,
                    broker=st.session_state.broker_name,
                )
                st.error(f"The market exit order for {ticker} was not placed.")
            else:
                st.success(f"Market {side.lower()} order submitted for {ticker}.")
                st.write(result)
                st.rerun()


def _run_analysis(scope: str = "all") -> None:
    scope_titles = {
        "all": "entire portfolio",
        "equity": "equity holdings",
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
    equity_and_positions = pd.concat(
        [equity_holdings, positions], ignore_index=True
    ).drop_duplicates(subset=["Ticker", "Qty", "Avg_Price"], keep="last")
    summary_by_scope = {
        "all": {
            "long_term_equity_holdings": equity_holdings.to_dict(orient="records"),
            "open_trading_positions": positions.to_dict(orient="records"),
            "mutual_fund_holdings": mutual_funds.to_dict(orient="records"),
        },
        "equity": {"equity_holdings": equity_holdings.to_dict(orient="records")},
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
        "mutual funds when available, actual broker cash, and a separate "
        f"user-selected hypothetical investment budget of INR {planning_budget:,.2f} "
        "are supplied. Size the scenario recommendations to the user-selected "
        "budget, not to broker cash. The scenario budget does not represent cash "
        "actually available in the brokerage account. "
        "Fund NAVs are the broker's last reported NAV and may not be live. "
        "Do not claim news or data that the application did not supply."
    )
    market_candidates = st.session_state.get("market_ai_candidates", [])
    candidate_tickers = [
        str(item.get("Ticker", "")).strip().upper()
        for item in market_candidates
        if isinstance(item, dict) and item.get("Ticker")
    ]
    if scope in {"all", "equity", "trading"}:
        portfolio_tickers = (
            equity_and_positions["Ticker"].dropna().astype(str).str.strip().str.upper()
            .tolist()
        )
        quote_tickers = sorted(set(portfolio_tickers + candidate_tickers))
        if st.session_state.get("broker_state") and st.session_state.get("token"):
            try:
                live_prices = st.session_state.broker_state.fetch_live_prices(
                    st.session_state.token, quote_tickers
                )
            except (
                BrokerAPIError,
                requests.RequestException,
                RuntimeError,
                ValueError,
            ) as error:
                log_failure(
                    "Analysis live-price refresh",
                    error,
                    broker=st.session_state.broker_name,
                )
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
        else:
            live_prices = {}
            price_error = (
                "No broker is connected, so live quotes and price-based deployments "
                "are unavailable. Portfolio analysis can still run on the supplied data."
            )
        st.session_state.analysis_price_errors[scope] = price_error
        if scope == "all":
            st.session_state.analysis_price_error = price_error
        st.session_state.analysis_live_prices_updated_at = datetime.now(
            ZoneInfo(settings.timezone)
        )
        live_prices = {
            str(ticker).strip().upper(): price
            for ticker, price in live_prices.items()
        }
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
    if scope in {"all", "equity", "trading"}:
        priced_candidates = [
            item
            for item in market_candidates
            if isinstance(item, dict)
            and str(item.get("Ticker", "")).strip().upper() in live_prices
        ]
        candidate_tickers = [
            str(item["Ticker"]).strip().upper() for item in priced_candidates
        ]
        context += (
            "\nLatest rules-based NSE scan candidates (only Strong Buy/Buy; positive "
            "indicator checks scored across the complete available indicator set): "
            + json.dumps(priced_candidates, ensure_ascii=True)
            + (
                " Only recommend from these screened and broker-priced candidates."
                if priced_candidates
                else " No current scanned and broker-priced Strong Buy/Buy candidates "
                "are available; return an empty cash_deployment_list."
            )
        )
    try:
        analysis = ask_llm_agent(
            portfolio_summary=summary,
            available_cash=planning_budget,
            market_context=context,
            live_prices=live_prices,
            candidate_tickers=candidate_tickers,
            api_key=api_key,
            model=str(selected_model),
            provider=provider,
            base_url=provider_base_url,
        )
    except APIError as error:
        log_failure(f"{provider} portfolio analysis", error)
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
        log_failure(f"{provider} portfolio analysis", error)
        fail(str(error))
        return
    except (RuntimeError, ValueError) as error:
        log_failure(f"{provider} portfolio analysis", error)
        fail(f"Portfolio analysis failed: {error}")
        return

    sectors_held = {
        SECTOR_BY_TICKER[ticker]
        for ticker in scoped_portfolio["Ticker"].astype(str).str.upper()
        if ticker in SECTOR_BY_TICKER
    } if not scoped_portfolio.empty else set()
    selected_rule_numbers = sorted(
        int(rule_number)
        for rule_number in st.session_state.get("enabled_jev_rules", [1, 2, 3, 4, 5, 6])
        if int(rule_number) in range(1, 7)
    )
    engine = JevRuleEngine(
        {
            "cash_balance": min(
                planning_budget, max(float(st.session_state.balance), 0.0)
            ),
            "actual_broker_balance": float(st.session_state.balance),
            "active_positions_count": int(
                equity_and_positions.loc[equity_and_positions["Qty"].ne(0)]
                .drop_duplicates(subset=["Ticker"])
                .shape[0]
            ),
            "realized_daily_loss_pct": float(
                st.session_state.realized_daily_loss_pct
            ),
            "max_allocation_pct": float(st.session_state.max_allocation_pct),
            "buy_lock_until": st.session_state.buy_lock_until,
            "live_prices": live_prices,
            "sector_holdings": sorted(sectors_held),
            "portfolio": scoped_portfolio,
            "llm_targets": analysis["cash_deployment_list"],
            "enabled_rules": selected_rule_numbers,
        }
    )
    trades, audit_trail = engine.run()
    st.session_state.buy_lock_until = engine.facts.get("buy_lock_until")
    st.session_state.analysis_results[scope] = analysis
    st.session_state.approved_trades_by_scope[scope] = trades
    st.session_state.audit_trails_by_scope[scope] = audit_trail
    st.session_state.jev_rule_selection_after_analysis = selected_rule_numbers
    st.session_state.analysis_errors.pop(scope, None)
    if scope == "all":
        st.session_state.analysis_result = analysis
        st.session_state.approved_trades = trades
        st.session_state.audit_trail = audit_trail


def _active_equity_position_count(*frames: pd.DataFrame) -> int:
    valid_frames = [
        frame
        for frame in frames
        if {"Ticker", "Qty"}.issubset(frame.columns)
    ]
    if not valid_frames:
        return 0
    combined = pd.concat(valid_frames, ignore_index=True)
    combined = combined.drop_duplicates(
        subset=[
            column
            for column in ("Ticker", "Qty", "Avg_Price")
            if column in combined.columns
        ],
        keep="last",
    )
    active = combined.loc[combined["Qty"].ne(0), "Ticker"]
    return int(active.astype(str).str.upper().nunique())


def _portfolio_for_scope(
    scope: str, portfolio: pd.DataFrame, equity_holdings: pd.DataFrame
) -> pd.DataFrame:
    if scope == "equity":
        return equity_holdings
    if scope == "trading":
        return portfolio
    if scope == "all":
        return pd.concat([equity_holdings, portfolio], ignore_index=True)
    return pd.DataFrame(columns=PORTFOLIO_COLUMNS)


def _buy_execution_block_reason(
    active_positions_count: int,
    now: datetime | None = None,
) -> str | None:
    reason, lock_until = buy_execution_block_reason(
        float(st.session_state.get("realized_daily_loss_pct", 0.0)),
        st.session_state.get("buy_lock_until"),
        active_positions_count,
        now,
    )
    st.session_state.buy_lock_until = lock_until
    return reason


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
        active_position_count = _active_equity_position_count(
            st.session_state.equity_holdings,
            st.session_state.portfolio,
        )
        buy_block_reason = _buy_execution_block_reason(active_position_count)
        scenario_budget = float(
            st.session_state.get("planning_budget_inr", actual_cash)
        )
        allocation_limit = (
            min(max(scenario_budget, 0.0), max(actual_cash, 0.0))
            * float(st.session_state.max_allocation_pct)
            / 100.0
        )
        symbol_is_orderable = ticker in TICKER_MAP and not isinstance(
            st.session_state.broker_state, GenericDynamicAdapter
        )
        can_deploy_now = (
            symbol_is_orderable
            and isinstance(quantity, int)
            and quantity > 0
            and float(trade.get("Confidence_Score", 0.0)) >= 70.0
            and current_price > 0
            and planned_cost <= actual_cash
            and planned_cost <= allocation_limit
            and buy_block_reason is None
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
                if buy_block_reason:
                    st.caption(f"Live order disabled: {buy_block_reason}")
                elif not symbol_is_orderable:
                    st.caption(
                        "Scenario-only recommendation: this holding has a live "
                        "quote but is not enabled for order placement by this app."
                    )
                elif planned_cost > allocation_limit:
                    st.caption(
                        "Live order disabled: estimated order value exceeds the "
                        f"current allocation cap of ₹{allocation_limit:,.2f}."
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
                    or float(trade.get("Confidence_Score", 0.0)) < 70.0
                ):
                    st.error("This recommendation is not valid for order execution.")
                    continue
                adapter = st.session_state.broker_state
                token = st.session_state.token
                try:
                    latest_cash = adapter.fetch_balance(token)
                    latest_positions = adapter.fetch_positions(token)
                    latest_holdings = adapter.fetch_holdings(token)
                    latest_position_count = _active_equity_position_count(
                        latest_holdings, latest_positions
                    )
                    latest_block_reason = _buy_execution_block_reason(
                        latest_position_count
                    )
                    if latest_block_reason:
                        st.error(f"Order blocked: {latest_block_reason}")
                        continue
                    live_market = _fetch_market_risk(adapter, token, ticker)
                    st.session_state.balance = latest_cash
                    latest_scenario_budget = float(
                        st.session_state.get("planning_budget_inr", latest_cash)
                    )
                    latest_allocation_limit = (
                        min(max(latest_scenario_budget, 0.0), max(latest_cash, 0.0))
                        * float(st.session_state.max_allocation_pct)
                        / 100.0
                    )
                    latest_order_value = quantity * live_market["LTP"]
                    if (
                        latest_order_value > latest_cash
                        or latest_order_value > latest_allocation_limit
                    ):
                        st.error(
                            "The latest live price, broker balance, or allocation "
                            "cap no longer supports this order quantity."
                        )
                        continue
                    final_block_reason = _buy_execution_block_reason(
                        latest_position_count
                    )
                    if final_block_reason:
                        st.error(f"Order blocked: {final_block_reason}")
                        continue
                    result = adapter.place_order(token, ticker, quantity, "BUY")
                except (
                    BrokerAPIError,
                    requests.RequestException,
                    RuntimeError,
                    ValueError,
                ) as error:
                    log_failure(
                        f"Market buy order for {ticker}",
                        error,
                        broker=st.session_state.broker_name,
                    )
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
    _render_sidebar_settings()
    _render_app_header(str(st.session_state.broker_name or "Broker"))
    with st.container(border=True, gap="medium"):
        profile_name = _fetch_broker_profile_name()
        if profile_name:
            st.caption(f"Signed in as **{profile_name}**")
        tabs = _render_header_tabs()
        polling_sequence()
        if st.session_state.planning_budget_inr is None:
            st.session_state.planning_budget_inr = max(
                float(st.session_state.balance), 1.0
            )
        if tabs[APP_TAB_LABELS[0]].open:
            with tabs[APP_TAB_LABELS[0]]:
                render_home_dashboard(
                    float(st.session_state.balance),
                    st.session_state.equity_holdings,
                    st.session_state.portfolio,
                    st.session_state.mutual_funds,
                )
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
                _render_portfolio_ai_review()
        for asset in APP_TAB_LABELS[1:]:
            if tabs[asset].open:
                with tabs[asset]:
                    _render_asset_workspace(asset, broker_connected=True)
        _render_app_footer()


def _fetch_broker_profile_name() -> str:
    if st.session_state.profile_fetch_attempted:
        return str(st.session_state.broker_profile_name or "")
    st.session_state.profile_fetch_attempted = True
    adapter = st.session_state.broker_state
    token = st.session_state.token
    if adapter is None or not token:
        return ""
    try:
        profile = adapter.fetch_profile(token)
    except (
        BrokerAPIError,
        requests.RequestException,
        RuntimeError,
        ValueError,
    ) as error:
        log_failure(
            "Broker profile lookup",
            error,
            broker=st.session_state.broker_name,
        )
        return ""
    name = profile.get("name", "").strip() if isinstance(profile, dict) else ""
    st.session_state.broker_profile_name = name
    return name


def _portfolio_report() -> tuple[str, str]:
    rows: list[dict[str, object]] = []
    for asset, frame in (
        ("Equity", st.session_state.equity_holdings),
        ("Trades", st.session_state.portfolio),
    ):
        for item in frame.to_dict(orient="records"):
            quantity = float(item.get("Qty", 0) or 0)
            average = float(item.get("Avg_Price", 0) or 0)
            price = float(item.get("LTP", 0) or 0)
            rows.append(
                {
                    "Asset": asset,
                    "Holding": str(item.get("Ticker", "")),
                    "Quantity": quantity,
                    "Value (INR)": quantity * price,
                    "P&L (INR)": quantity * (price - average),
                }
            )
    for item in st.session_state.mutual_funds.to_dict(orient="records"):
        units = float(item.get("Units", 0) or 0)
        average_nav = float(item.get("Avg_NAV", 0) or 0)
        latest_nav = float(item.get("Latest_NAV", 0) or 0)
        rows.append(
            {
                "Asset": "Mutual fund",
                "Holding": str(item.get("Fund", "")),
                "Quantity": units,
                "Value (INR)": units * latest_nav,
                "P&L (INR)": units * (latest_nav - average_nav),
            }
        )
    report = pd.DataFrame(
        rows,
        columns=["Asset", "Holding", "Quantity", "Value (INR)", "P&L (INR)"],
    )
    cash = float(st.session_state.balance)
    broker = str(st.session_state.broker_name or "No broker connected")
    generated = datetime.now(ZoneInfo(settings.timezone)).strftime(
        "%d %b %Y, %H:%M IST"
    )
    text = (
        f"GGHP portfolio update · {generated}\n"
        f"Broker: {broker} · Available cash: INR {cash:,.2f}\n"
        f"Invested holdings shown: {len(report)}\n"
        + (
            report.to_string(index=False, max_rows=20)
            if not report.empty
            else "No holdings are currently available."
        )
    )
    compact_text = text[:1500]
    if len(text) > len(compact_text):
        compact_text += "\n… Report shortened; email has the full report."
    safe_broker = html.escape(broker)
    safe_generated = html.escape(generated)
    safe_table = (
        report.to_html(index=False, border=0, escape=True, classes="holdings")
        if not report.empty
        else "<p>No holdings are currently available.</p>"
    )
    html_report = (
        "<!doctype html><html><body style=\"font-family:Arial,sans-serif;"
        "color:#172033;max-width:900px;margin:auto\">"
        "<h1 style=\"color:#1d4ed8\">GGHP portfolio update</h1>"
        f"<p>{safe_generated} · {safe_broker}</p>"
        f"<p><strong>Available cash:</strong> ₹{cash:,.2f}</p>"
        f"{safe_table}"
        "<p style=\"color:#64748b;font-size:12px\">Informational only; "
        "not investment advice. Market prices may be delayed.</p>"
        "</body></html>"
    )
    return compact_text, html_report


def _render_portfolio_share() -> None:
    with st.popover(
        ":material/share:",
        help="Share a portfolio update",
        type="tertiary",
    ):
        st.caption(
            "Send a snapshot only when you choose. Delivery targets use the "
            "WhatsApp and Email settings in the header."
        )
        email_provider = str(st.session_state.get("email_delivery_provider", "SMTP"))
        email_recipient = str(st.session_state.get("email_recipient", ""))
        email_ready = bool(email_recipient.strip()) and (
            bool(
                str(st.session_state.get("resend_api_key_session", "")).strip()
                or settings.resend_api_key
            )
            and bool(
                str(st.session_state.get("resend_sender_session", "")).strip()
                or settings.resend_sender
            )
            if email_provider == "Resend"
            else bool(
                str(st.session_state.get("email_smtp_host", "")).strip()
                and str(st.session_state.get("email_sender", "")).strip()
            )
        )
        whatsapp_configuration = _session_whatsapp_configuration()
        whatsapp_ready = whatsapp_update_is_configured(
            whatsapp_configuration
        )
        email_column, whatsapp_column = st.columns(2)
        with email_column:
            if st.button(
                "Email full portfolio report",
                key="share_portfolio_email",
                icon=":material/mail:",
                disabled=not email_ready,
            ):
                try:
                    report_text, report_html = _portfolio_report()
                    if email_provider == "Resend":
                        send_resend_email(
                            "Your GGHP portfolio update",
                            report_html,
                            email_recipient,
                            api_key=str(
                                st.session_state.get("resend_api_key_session", "")
                            ),
                            sender=str(
                                st.session_state.get("resend_sender_session", "")
                            ),
                        )
                    else:
                        send_email_alert(
                            report_text,
                            smtp_host=str(st.session_state.get("email_smtp_host", "")),
                            smtp_port=int(st.session_state.get("email_smtp_port", 587)),
                            username=str(
                                st.session_state.get("email_smtp_username", "")
                            ),
                            password=str(
                                st.session_state.get("email_smtp_password", "")
                            ),
                            sender=str(st.session_state.get("email_sender", "")),
                            recipient=email_recipient,
                        )
                except (
                    OSError,
                    requests.RequestException,
                    RuntimeError,
                    ValueError,
                ) as error:
                    log_failure("Portfolio email report", error)
                    st.error("The portfolio email could not be delivered.")
                else:
                    st.success("Portfolio report sent to the configured email.")
        with whatsapp_column:
            if st.button(
                "Share summary via WhatsApp",
                key="share_portfolio_whatsapp",
                icon=":material/chat:",
                disabled=not whatsapp_ready,
            ):
                try:
                    report_text, _ = _portfolio_report()
                    send_whatsapp_update(
                        report_text, whatsapp_configuration
                    )
                except (
                    OSError,
                    requests.RequestException,
                    RuntimeError,
                    ValueError,
                ) as error:
                    log_failure("Portfolio WhatsApp report", error)
                    st.error("The WhatsApp portfolio summary could not be delivered.")
                else:
                    st.success("Portfolio summary sent to the configured WhatsApp.")
        if not email_ready or not whatsapp_ready:
            st.caption(
                "Configure at least one delivery channel in the header to enable sharing."
            )


def _render_agent() -> None:
    st.session_state.setdefault("chat_history", [])
    with st.popover(
        "Ask the AI guide",
        icon=":material/smart_toy:",
        help="Local app guide for GGHP No AI API key or external request required.",
    ):
        st.caption(
            "Get guidance about app features, broker connections, workspaces, "
            "research, analysis, risk controls, and notifications. This local "
            "guide does not require an AI provider key."
        )
        for message in st.session_state.chat_history[-8:]:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
        with st.form("wealth_home_chat_form", clear_on_submit=True):
            question = st.text_input(
                "Ask a question",
                max_chars=2000,
                placeholder="How do I connect a broker or use a workspace?",
            )
            submitted = st.form_submit_button(
                "Send",
                type="primary",
                icon=":material/send:",
            )
        if not submitted:
            return
        try:
            answer = answer_app_help_question(
                question,
                history=st.session_state.chat_history,
            )
        except ValueError as error:
            st.error(str(error))
            return
        st.session_state.chat_history.extend(
            [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ]
        )
        st.rerun()


def _workspace_symbols(*frames: pd.DataFrame) -> list[str]:
    symbols = []
    for frame in frames:
        if not frame.empty and "Ticker" in frame:
            symbols.extend(
                str(ticker).strip().upper()
                for ticker in frame["Ticker"].dropna()
                if str(ticker).strip()
            )
    return list(dict.fromkeys(symbols))


def _render_asset_workspace(asset: str, *, broker_connected: bool) -> None:
    if not broker_connected:
        st.info(
            "No broker is connected. Account data and broker-dependent analysis "
            "will be available here after connecting from Home."
        )

    if asset == "Equities":
        if st.session_state.broker_data_errors.get("equity"):
            st.warning(st.session_state.broker_data_errors["equity"])
        render_equity(st.session_state.equity_holdings)
        render_analysis_panel(
            "equity", "equity", _run_analysis, _show_approved_trades
        )
        equity_symbols = _workspace_symbols(st.session_state.equity_holdings)
        with st.expander("Market scanner", icon=":material/filter_list:"):
            render_market_scanner(
                broker_connected=broker_connected,
                scope="Equities",
                symbols=equity_symbols,
            )
        return

    if asset == "Trades":
        if st.session_state.broker_data_errors.get("trading"):
            st.warning(st.session_state.broker_data_errors["trading"])
        render_trading(st.session_state.portfolio)
        st.subheader("Trades analysis")
        render_analysis_panel(
            "trading", "trading", _run_analysis, _show_approved_trades
        )
        symbols = _workspace_symbols(st.session_state.portfolio)
        with st.expander("Market scanner", icon=":material/filter_list:"):
            render_market_scanner(
                broker_connected=broker_connected,
                scope="Trading",
                symbols=symbols,
            )
        return

    if asset == "F&O":
        render_derivatives("Options and futures")
        st.subheader("Options analysis")
        render_analysis_panel(
            "options", "options", _run_analysis, _show_approved_trades
        )
        st.subheader("Futures analysis")
        render_analysis_panel(
            "futures", "futures", _run_analysis, _show_approved_trades
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
        return

    if asset == "Mutual Funds":
        render_mutual_funds(
            st.session_state.mutual_funds,
            st.session_state.broker_data_errors.get("mutual_funds")
            or st.session_state.mutual_fund_error,
        )
        st.subheader("Mutual-fund analysis")
        render_analysis_panel(
            "mutual_funds",
            "mutual funds",
            _run_analysis,
            _show_approved_trades,
        )
        with st.expander("Market research", icon=":material/query_stats:"):
            render_local_asset_research_view(
                "Mutual Funds", st.session_state.mutual_funds, mode="research"
            )
        with st.expander("Market scanner", icon=":material/filter_list:"):
            render_local_asset_research_view(
                "Mutual Funds", st.session_state.mutual_funds, mode="scanner"
            )
        return

def _reevaluate_analysis_rules(
    scope: str, analysis: dict[str, Any], enabled_rules: list[int]
) -> None:
    portfolio = st.session_state.portfolio
    equity_holdings = st.session_state.equity_holdings
    scoped_portfolio = _portfolio_for_scope(scope, portfolio, equity_holdings)
    active_positions_count = _active_equity_position_count(portfolio, equity_holdings)
    sectors_held = {
        SECTOR_BY_TICKER[ticker]
        for ticker in scoped_portfolio.get("Ticker", pd.Series(dtype=str))
        .astype(str)
        .str.upper()
        if ticker in SECTOR_BY_TICKER
    }
    budget = float(st.session_state.get("planning_budget_inr") or 0.0)
    live_prices = st.session_state.get("analysis_live_prices", {})
    engine = JevRuleEngine(
        {
            "cash_balance": min(budget, max(float(st.session_state.get("balance", 0.0)), 0.0)),
            "actual_broker_balance": float(st.session_state.get("balance", 0.0)),
            "active_positions_count": active_positions_count,
            "realized_daily_loss_pct": float(st.session_state.realized_daily_loss_pct),
            "max_allocation_pct": float(st.session_state.max_allocation_pct),
            "buy_lock_until": st.session_state.buy_lock_until,
            "live_prices": live_prices,
            "sector_holdings": sorted(sectors_held),
            "portfolio": scoped_portfolio,
            "llm_targets": analysis.get("cash_deployment_list", []),
            "enabled_rules": enabled_rules,
        }
    )
    approved, audit = engine.run()
    st.session_state.approved_trades_by_scope[scope] = approved
    st.session_state.audit_trails_by_scope[scope] = audit
    if scope == "all":
        st.session_state.approved_trades = approved
        st.session_state.audit_trail = audit


st.session_state.reevaluate_analysis_rules = _reevaluate_analysis_rules


def _render_portfolio_ai_review() -> None:
    with st.expander(
        "Portfolio planning & safeguards",
        icon=":material/shield_with_heart:",
    ):
        st.caption(
            "Review equity, trading, and mutual-fund records together. "
            "Recommendations are advisory and orders are never submitted automatically."
        )
        if not st.session_state.get("token"):
            st.info(
                "This planning panel works without a connected broker. Add portfolio "
                "holdings in the relevant workspace for personalized analysis; live "
                "prices, broker cash, and order execution require a broker connection."
            )
        _render_risk_controls()
        analysis_mode_descriptions = {
            "Portfolio and cash review": (
                "Reviews your holdings, diversification, and cash position. It does "
                "not build an order deployment plan."
            ),
            "Risk-aware deployment review": (
                "Reviews your portfolio and creates a hypothetical, risk-aware plan "
                "within the scenario budget. It does not submit orders."
            ),
        }
        mode_column, budget_column = st.columns([1, 1.25])
        with mode_column:
            _render_inline_help_label(
                "Analysis mode",
                "Choose whether AI reviews portfolio conditions only or also builds "
                "a hypothetical deployment plan.",
            )
            selected_analysis_mode = st.selectbox(
                "Analysis mode",
                list(analysis_mode_descriptions),
                key="analysis_mode",
                label_visibility="collapsed",
            )
        with budget_column:
            _render_inline_help_label(
                "Scenario budget for recommendations (INR)",
                "Used only to size a hypothetical recommendation plan. It does not "
                "change your broker balance or authorize an order.",
            )
            st.number_input(
                "Scenario budget for recommendations (INR)",
                min_value=1.0,
                max_value=100_000_000.0,
                step=10_000.0,
                format="%.2f",
                key="planning_budget_inr",
                label_visibility="collapsed",
            )
        st.caption(analysis_mode_descriptions[selected_analysis_mode])
        st.caption(
            f"Actual broker cash: ₹{float(st.session_state.balance):,.2f}. "
            "Scenario budget is for planning only; live orders require sufficient "
            "actual cash. Candidates below 70% confidence will not be approved."
        )
        render_analysis_panel(
            "all", "your full portfolio", _run_analysis, _show_approved_trades
        )

def _logout() -> None:
    st.session_state.return_to_home = True
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
        "mutual_fund_error",
        "mutual_fund_updated_at",
        "mutual_fund_manual_import",
        "mutual_fund_upload_digest",
        "mutual_fund_upload_error",
        "last_sync",
        "last_sync_attempt",
        "sync_failed",
        "broker_data_errors",
        "analysis_result",
        "analysis_results",
        "analysis_errors",
        "ai_settings_prompt",
        "analysis_price_errors",
        "planning_budget_inr",
        "analysis_live_prices",
        "analysis_live_prices_updated_at",
        "market_ai_candidates",
        "market_ai_candidates_updated_at",
        "analysis_price_error",
        "approved_trades",
        "approved_trades_by_scope",
        "audit_trail",
        "audit_trails_by_scope",
        "analysis_error",
        "alerted_risk_channels",
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
    st.session_state.balance = 0.0
    st.session_state.alerted_risk_channels = []
    st.session_state.risk_high_water = {}
    st.session_state.risk_alerts = []
    st.session_state.risk_errors = []
    st.session_state.analysis_results = {}
    st.session_state.analysis_errors = {}
    st.session_state.analysis_price_errors = {}
    st.session_state.market_ai_candidates = []
    st.session_state.market_ai_candidates_updated_at = None
    st.session_state.approved_trades_by_scope = {}
    st.session_state.audit_trails_by_scope = {}
    st.session_state.last_sync_attempt = None
    st.session_state.broker_data_errors = {}
    st.session_state.user_preferences = {}
    st.session_state.max_allocation_pct = 100.0
    st.session_state.realized_daily_loss_pct = 0.0
    st.session_state.resend_api_key_session = ""
    st.session_state.resend_sender_session = settings.resend_sender
    st.session_state.email_delivery_provider = "SMTP"
    st.session_state.whatsapp_account_sid = ""
    st.session_state.whatsapp_auth_token = ""
    st.session_state.whatsapp_sender = ""
    st.session_state.whatsapp_recipient = ""
    st.session_state.email_smtp_host = ""
    st.session_state.email_smtp_port = 587
    st.session_state.email_smtp_username = ""
    st.session_state.email_smtp_password = ""
    st.session_state.email_sender = ""
    st.session_state.email_recipient = ""


def _logout_oidc() -> None:
    _logout()
    st.logout()


if st.session_state.token is None:
    _show_login()
else:
    _show_workspace()


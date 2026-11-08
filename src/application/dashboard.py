"""Compose the Streamlit app from its feature workspaces and shared UI."""

from __future__ import annotations

import hashlib
import html
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from google.genai.errors import APIError

from brokers.factory import (
    BrokerAPIError,
    BrokerCapabilityError,
    BrokerFactory,
    BrokerInterface,
    DhanAdapter,
    GenericDynamicAdapter,
    UpstoxAdapter,
    ZerodhaAdapter,
)
from core.constants import (
    AppNavigation,
    AppTab,
    LegacyAppNavigation,
    LLMProvider,
    LLMProviderBaseURL,
    mutual_fund_column_names,
    portfolio_column_names,
)
from brokers.factory import TICKER_MAP
from features.trades.rules import (
    JevRuleEngine,
    SECTOR_BY_TICKER,
    buy_execution_block_reason,
)
from features.analysis.panel import (
    portfolio_analysis_is_configured,
    render_analysis_panel,
)
from features.analysis.holdings import _cancel_holding_jobs
from features.home.workspace import render_home_workspace
from features.cart.workspace import render_cart_workspace
from features.portfolio.metrics import calculate_allocation
from features.equities.scanner import cancel_scanner_jobs, render_market_scanner
from features.derivatives.workspace import render_derivatives_workspace
from features.equities.workspace import render_equities_workspace
from features.mutual_funds.workspace import (
    render_mutual_funds_workspace,
    validate_mutual_funds,
)
from features.trades.workspace import render_trades_workspace
from features.mutual_funds.research import (
    render_local_asset_research as render_local_asset_research_view,
)
from core.logging import log_failure, log_success
from services.cloud import (
    clear_legacy_dynamic_brokers,
    send_resend_email,
    turso_is_configured,
)
from services.analysis_jobs import (
    submit_analysis_task,
    submit_portfolio_analysis,
)
from services.user_settings import load_user_settings
from services.database import DatabaseError, DatabaseSecurityError
from storage import StorageRequestPending, get_storage_provider
from storage.indexeddb_provider import (
    has_pending_browser_write,
    process_browser_storage_bridge,
)
from security.oauth import consume_oauth_state_context, create_oauth_state
from core.config import settings
from application.help import answer_app_help_question
from ui.helpers import (
    app_animation_css,
    gold_theme_css,
    broker_connect_button_css,
    same_tab_link_html,
    signed_in_label_html,
)
from ui.header import render_brand_lockup, render_header
from ui.footer import render_footer
from ui.user_settings import (
    apply_settings_to_session,
    default_broker_callback_url,
    open_user_settings_for_broker_credentials,
    render_user_settings,
)
from services.ai import (
    LLMProviderError,
    send_whatsapp_update,
    whatsapp_update_is_configured,
)

APP_TITLE = settings.app_title
APP_TAB_LABELS = tuple(tab.value for tab in AppTab)
APP_TABS = tuple(tab.value for tab in AppNavigation)
MUTUAL_FUND_COLUMNS = mutual_fund_column_names()
PORTFOLIO_COLUMNS = portfolio_column_names()
SUPPORTED_LLM_PROVIDERS = tuple(
    item.provider.value for item in LLMProviderBaseURL
)
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
BROKER_LOGIN_INPUT_KEYS = {
    "Upstox": {"api_key": "upstox_api_key", "api_secret": "upstox_api_secret"},
    "Zerodha": {"api_key": "zerodha_api_key", "api_secret": "zerodha_api_secret"},
    "Angel One": {
        "api_key": "angel_api_key",
        "client_id": "angel_client_id",
        "password": "angel_password",
        "totp_secret": "angel_totp_secret",
    },
    "Dhan": {
        "client_id": "dhan_client_id",
        "api_key": "dhan_api_key",
        "api_secret": "dhan_api_secret",
    },
}


def _user_is_authenticated() -> bool:
    return bool(st.user.get("is_logged_in", False))


def _initialize_session() -> None:
    defaults = {
        "login_steps": [], "dynamic_broker_states": {}, "user_preferences": {},
        "chat_history": [], "stock_cart": [], "max_allocation_pct": 100.0,
        "realized_daily_loss_pct": 0.0, "broker_profile_name": "",
        "broker_profile_identifier": "", "profile_fetch_attempted": False,
        "buy_lock_until": None, "broker_state": None, "broker_name": None,
        "token": None, "portfolio": pd.DataFrame(columns=PORTFOLIO_COLUMNS),
        "equity_holdings": pd.DataFrame(columns=PORTFOLIO_COLUMNS),
        "mutual_funds": pd.DataFrame(columns=MUTUAL_FUND_COLUMNS),
        "mutual_fund_error": None, "mutual_fund_updated_at": None,
        "mutual_fund_manual_import": False, "mutual_fund_upload_digest": None,
        "mutual_fund_upload_error": None, "balance": 0.0, "last_sync": None,
        "last_sync_attempt": None, "sync_failed": False, "broker_data_errors": {},
        "risk_high_water": {}, "risk_alerts": [], "risk_errors": [],
        "alerted_risk_channels": [], "analysis_result": None,
        "analysis_results": {}, "analysis_timestamps": {}, "analysis_metadata": {},
        "analysis_errors": {}, "portfolio_overview_analysis": None,
        "portfolio_overview_timestamp": None, "portfolio_overview_metadata": None,
        "analysis_price_errors": {}, "ai_settings_prompt": "",
        "llm_provider_settings": {}, "planning_budget_inr": 100_000.0,
        "analysis_live_prices": {}, "analysis_live_prices_updated_at": None,
        "market_ai_candidates": [], "market_ai_candidates_updated_at": None,
        "analysis_price_error": None, "approved_trades": [],
        "approved_trades_by_scope": {}, "audit_trail": [], "audit_trails_by_scope": {},
        "enabled_jev_rules": [1, 2, 3, 4, 5, 6],
        "jev_rule_selection_before_analysis": [1, 2, 3, 4, 5, 6],
        "jev_rule_selection_after_analysis": [1, 2, 3, 4, 5, 6],
        "login_error": None, "pending_broker_name": None,
        "pending_broker_adapter": None, "pending_login_url": None,
        "pending_redirect_url": None, "pending_oauth_fingerprint": None,
        "pending_oauth_expires_at": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    for key in ("sidebar_openrouter_api_key", "user_llm_api_key_openrouter",
                "llm_model_selection_openrouter"):
        st.session_state.pop(key, None)
    if isinstance(st.session_state.llm_provider_settings, dict):
        st.session_state.llm_provider_settings.pop("OpenRouter", None)
    if st.session_state.get("llm_provider") not in SUPPORTED_LLM_PROVIDERS:
        st.session_state.llm_provider = LLMProvider.GEMINI.value
    if not isinstance(st.session_state.market_ai_candidates, list):
        st.session_state.market_ai_candidates = []


def _redirect_url() -> str:
    return str(
        st.session_state.get("broker_callback_url")
        or default_broker_callback_url()
    )


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


def _render_app_header() -> None:
    render_header(
        render_share=_render_portfolio_share,
        render_settings=lambda: render_user_settings(_tenant_identifier()),
        render_agent=_render_agent,
    )


def _tenant_identifier() -> str:
    if _user_is_authenticated():
        email = str(st.user.get("email", "")).strip().lower()
        subject = str(st.user.get("sub", "")).strip()
        if email or subject:
            return email or subject
    return "local-user"


def _load_user_settings_for_user() -> None:
    owner_identifier = _tenant_identifier()
    if st.session_state.get("user_settings_loaded_for") == owner_identifier:
        return
    try:
        get_storage_provider(settings).get_or_create_device_id()
    except StorageRequestPending:
        pass
    except (OSError, RuntimeError, ValueError) as error:
        log_failure("Local device ID initialization", error)
        st.session_state["user_settings_error"] = str(error)
        return
    try:
        saved_settings = load_user_settings(owner_identifier)
    except StorageRequestPending:
        return
    except (OSError, RuntimeError, DatabaseError, ValueError) as error:
        log_failure("User settings load", error)
        st.session_state["user_settings_error"] = str(error)
        st.session_state["user_settings_loaded_for"] = owner_identifier
        return
    apply_settings_to_session(saved_settings)
    st.session_state["user_settings_loaded_for"] = owner_identifier
    st.session_state["user_settings_error"] = None


def _clear_legacy_analysis_storage() -> None:
    if st.session_state.get("legacy_analysis_storage_cleared"):
        return
    try:
        get_storage_provider(settings).delete_collection("portfolio_analysis")
    except StorageRequestPending:
        return
    except (OSError, RuntimeError, DatabaseError, ValueError) as error:
        log_failure("Legacy analysis storage cleanup", error)
        st.warning("Previously saved AI analysis could not be cleared from this device.")
        return
    st.session_state.legacy_analysis_storage_cleared = True


def _clear_legacy_cloud_brokers_for_user() -> None:
    if not turso_is_configured():
        return
    owner = _tenant_identifier()
    if st.session_state.get("legacy_cloud_cleanup_owner") == owner:
        return
    try:
        clear_legacy_dynamic_brokers(owner)
    except (OSError, requests.RequestException, RuntimeError, ValueError) as error:
        log_failure("Legacy broker storage cleanup", error)
        st.warning("Legacy broker storage could not be cleared for this user.")
        return
    st.session_state.legacy_cloud_cleanup_owner = owner


def _render_sidebar_settings() -> None:
    if settings.require_login and _user_is_authenticated():
        with st.sidebar:
            st.button(
                "Sign out",
                icon=":material/logout:",
                on_click=_logout_oidc,
            )


def _render_header_tabs():
    current_navigation = st.session_state.get("app_navigation")
    cart_count = len(st.session_state.get("stock_cart", []))
    tab_options = (*APP_TABS[:-1], f"{APP_TABS[-1]} ({cart_count})")
    if isinstance(current_navigation, str) and current_navigation.startswith(APP_TABS[-1] + " ("):
        current_navigation = APP_TABS[-1]
    legacy_target = (
        LegacyAppNavigation.target_for(current_navigation)
        if isinstance(current_navigation, str)
        else None
    )
    selected_index = None
    if legacy_target is not None:
        selected_index = APP_TAB_LABELS.index(legacy_target.value)
    elif isinstance(current_navigation, str):
        canonical_navigation = current_navigation
        for status_label in (" · Constructive", " · Review", " · Risk"):
            canonical_navigation = canonical_navigation.replace(status_label, "")
        for status_icon in ("🟢", "🔴"):
            canonical_navigation = canonical_navigation.replace(
                f" {status_icon} ", " "
            )
        if canonical_navigation in APP_TABS:
            selected_index = APP_TABS.index(canonical_navigation)
        elif canonical_navigation in APP_TAB_LABELS:
            selected_index = APP_TAB_LABELS.index(canonical_navigation)
    st.session_state["_run_seq"] = st.session_state.get("_run_seq", 0) + 1
    if st.session_state.pop("return_to_home", False):
        selected_index = 0

    if selected_index is None:
        selected_index = 0
    st.session_state["app_navigation"] = tab_options[selected_index]
    with st.container(key="app_navigation_tabs"):
        tabs = st.tabs(
            tab_options,
            key="app_navigation",
            on_change="rerun",
            width="stretch",
        )
    return {
        label: tab
        for label, tab in zip(APP_TAB_LABELS, tabs, strict=True)
    }


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
    on_change: Callable[[], None] | None = None,
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
    if on_change is not None:
        input_options["on_change"] = on_change
    st.text_input(label, **input_options)


def _mark_dashboard_broker_credentials_dirty() -> None:
    st.session_state["dashboard_broker_credentials_dirty"] = str(
        st.session_state.get("selected_broker", "")
    )


def _reset_dashboard_broker_credentials() -> None:
    st.session_state.pop("dashboard_broker_credentials_dirty", None)
    st.session_state.pop("dashboard_broker_credentials_loaded_for", None)


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


def _session_whatsapp_configuration() -> dict[str, str]:
    phone = str(st.session_state.get("user_profile_phone", "")).strip()
    number = "".join(
        character
        for character in phone
        if character.isdigit() or character == "+"
    )
    if number and not number.startswith("+"):
        number = "+" + number
    return {"recipient": f"whatsapp:{number}" if number else ""}


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
    except (
        BrokerAPIError,
        DatabaseError,
        DatabaseSecurityError,
        OSError,
        ValueError,
    ) as error:
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
    st.session_state.broker_profile_identifier = ""
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
        _complete_broker_login("Angel One", adapter, token)
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
    _cancel_holding_jobs()
    cancel_scanner_jobs()
    active_job = st.session_state.pop("analysis_job", None)
    if active_job:
        active_job["future"].cancel()
    st.session_state.token = token
    st.session_state.broker_state = adapter
    st.session_state.broker_name = broker_name
    st.session_state.stock_context_cache = {}
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
    st.session_state.analysis_timestamps = {}
    st.session_state.analysis_metadata = {}
    st.session_state.analysis_errors = {}
    st.session_state.portfolio_overview_analysis = None
    st.session_state.portfolio_overview_timestamp = None
    st.session_state.portfolio_overview_metadata = None
    st.session_state.analysis_price_errors = {}
    st.session_state.analysis_price_error = None
    st.session_state.analysis_live_prices = {}
    st.session_state.analysis_live_prices_updated_at = None
    st.session_state.broker_profile_identifier = ""
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
        "holdings. Configure broker credentials and callback URL in User settings. "
        "Credentials are used only for sign-in and are never read from server "
        "configuration. Settings "
        "explain which values are stored and require explicit consent before "
        "saving confidential values. AI suggestions are informational, not "
        "investment advice; provider availability and charges may apply."
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

    broker_profiles = list(
        st.session_state.get("registered_broker_profiles", [])
    )
    broker_profile = None
    if callback_broker:
        broker_type = callback_broker
        broker_choice = callback_broker
        st.caption(f"Completing {broker_choice} sign-in…")
    else:
        active_profiles = broker_profiles
        if not active_profiles:
            st.info(
                "No supported broker configuration is available. Open "
                "**User settings → Brokers** to review your settings."
            )
            return
        active_names = [profile["name"] for profile in active_profiles]
        broker_choice = st.selectbox(
            "Select a broker",
            active_names,
            key="selected_broker",
            on_change=_reset_dashboard_broker_credentials,
        )
        broker_profile = next(
            profile
            for profile in active_profiles
            if profile["name"] == broker_choice
        )
        broker_type = str(broker_profile["type"])
        credentials_value = st.session_state.get(
            "settings_broker_credentials", {}
        )
        credentials = (
            credentials_value.get(broker_profile["id"], {})
            if isinstance(credentials_value, dict)
            else {}
        )
        credentials = credentials if isinstance(credentials, dict) else {}
        login_keys = BROKER_LOGIN_INPUT_KEYS[broker_type]
        credentials_fingerprint = hashlib.sha256(
            json.dumps(credentials, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if (
            st.session_state.get("dashboard_broker_credentials_loaded_for")
            != broker_type
            or st.session_state.get("dashboard_broker_credentials_fingerprint")
            != credentials_fingerprint
        ) and st.session_state.get("dashboard_broker_credentials_dirty") != broker_type:
            for field, login_key in login_keys.items():
                st.session_state[login_key] = str(credentials.get(field, ""))
            st.session_state["dashboard_broker_credentials_loaded_for"] = broker_type
            st.session_state[
                "dashboard_broker_credentials_fingerprint"
            ] = credentials_fingerprint

        credentials_saved = bool(
            st.session_state.get("settings_sensitive_consent", False)
        )
        if credentials_saved:
            for field, login_key in login_keys.items():
                saved_value = str(credentials.get(field, "")).strip()
                if saved_value:
                    st.session_state[login_key] = saved_value
        saved_fields = {
            field: str(value).strip()
            for field, value in credentials.items()
            if str(value).strip()
        }
        missing_fields = tuple(
            field
            for field in login_keys
            if not (
                credentials_saved
                and str(credentials.get(field, "")).strip()
            )
        )
        credentials_securely_saved = credentials_saved and bool(saved_fields)

        required_fields = tuple(
            field
            for field in BROKER_FIELD_GUIDES.get(broker_type, {})
            if field != "Callback URL"
        )
        if missing_fields:
            st.warning(
                f"{broker_type} credentials are not configured. Enter the "
                "missing details below to connect.",
                icon=":material/lock:",
            )
        elif not credentials_saved:
            st.warning(
                f"{broker_type} credentials are available for this session "
                "only. Use the button below to save them with your consent.",
                icon=":material/lock:",
            )

        fields_to_render = (
            required_fields
            if not credentials_saved
            else tuple(field for field in required_fields if field.lower().replace(" ", "_") in missing_fields)
        )
        if fields_to_render:
            for field in fields_to_render:
                credential_name = field.lower().replace(" ", "_")
                login_key = login_keys[credential_name]
                _render_broker_credential_field(
                    broker_type,
                    field,
                    field,
                    key=login_key,
                    input_type=(
                        "password"
                        if credential_name
                        in {"api_key", "api_secret", "password", "totp_secret"}
                        else "default"
                    ),
                    help_text=(
                        "This credential is session-only until you save it "
                        "with explicit consent in User settings."
                    ),
                    on_change=_mark_dashboard_broker_credentials_dirty,
                )
        if fields_to_render:
            pending_credentials = {
                field: str(st.session_state.get(key, "")).strip()
                or str(credentials.get(field, "")).strip()
                for field, key in login_keys.items()
            }
            has_credentials_to_review = any(pending_credentials.values())
            st.button(
                "Review and save credentials",
                key=f"save_{broker_type.lower().replace(' ', '_')}_credentials",
                icon=":material/save:",
                disabled=not has_credentials_to_review,
                on_click=open_user_settings_for_broker_credentials,
                args=(_tenant_identifier(), broker_type, pending_credentials),
            )

    def _show_saved_credentials_reminder() -> None:
        if not callback_broker and broker_profile and credentials_securely_saved:
            st.success(
                "Broker credentials are saved securely and encrypted for future sessions.",
                icon=":material/lock:",
            )

    if st.session_state.login_error:
        st.error(st.session_state.login_error)
        st.session_state.login_error = None

    if broker_type == "Custom read-only API" and broker_profile:
        broker_name = broker_profile["name"]
        if st.button(
            f"Load {broker_name} connection",
            key="load_dynamic_broker_configuration",
        ):
            try:
                adapter = BrokerFactory.create_dynamic_adapter(
                    broker_profile["configuration"]
                )
                st.session_state.dynamic_broker_states[broker_name] = adapter
            except (
                requests.RequestException,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                log_failure("Custom broker configuration", error)
                st.error("The broker configuration could not be loaded.")
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
    elif broker_type in {"Upstox", "Zerodha"}:
        if not callback_broker:
            st.caption(
                "Credentials and callback URL are managed in User settings."
            )
        callback_error = st.query_params.get("error")
        if callback_error or (
            broker_type == "Zerodha"
            and st.query_params.get("status") == "error"
        ):
            st.error(f"{broker_choice} sign-in was cancelled or rejected.")
            _clear_pending_login()
            _clear_broker_login_inputs()
            st.query_params.clear()
        elif broker_type == "Upstox" and st.query_params.get("code"):
            try:
                adapter, token = _exchange_oauth_callback(
                    "Upstox",
                    str(st.query_params["code"]),
                    st.query_params.get("state"),
                )
            except StorageRequestPending:
                st.info("Checking the broker callback state in browser storage…")
                return
            except (
                BrokerAPIError,
                requests.RequestException,
                DatabaseError,
                DatabaseSecurityError,
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
        elif broker_type == "Zerodha" and st.query_params.get("request_token"):
            try:
                adapter, token = _exchange_oauth_callback(
                    "Zerodha",
                    str(st.query_params["request_token"]),
                    st.query_params.get("state"),
                )
            except StorageRequestPending:
                st.info("Checking the broker callback state in browser storage…")
                return
            except (
                BrokerAPIError,
                requests.RequestException,
                DatabaseError,
                DatabaseSecurityError,
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
            _show_saved_credentials_reminder()
            if broker_type == "Upstox":
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
            login_url = None
            login_error = None
            if api_key and api_secret:
                login_url, login_error = _prepare_oauth_login(
                    broker_type, api_key, api_secret
                )
            if login_error:
                st.error(login_error)
            if api_key and api_secret and login_url:
                if has_pending_browser_write("oauth_states"):
                    st.info("Saving the one-time callback state in browser storage…")
                elif broker_choice == "Upstox":
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

    elif broker_type == "Angel One":
        if not callback_broker:
            _show_saved_credentials_reminder()
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

    elif broker_type == "Dhan":
        if not callback_broker:
            _show_saved_credentials_reminder()
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
def _show_login() -> None:
    _render_sidebar_settings()
    _render_app_header()
    if st.session_state.get("analysis_job"):
        _render_analysis_progress()
    with st.container(border=True, gap="medium"):
        tabs = _render_header_tabs()
        if tabs[APP_TAB_LABELS[0]].open:
            with tabs[APP_TAB_LABELS[0]]:
                _render_broker_connection()
                _render_portfolio_ai_review()
        for asset in APP_TAB_LABELS[1:-1]:
            if tabs[asset].open:
                with tabs[asset]:
                    _render_asset_workspace(asset, broker_connected=False)
        if tabs[AppTab.CART.value].open:
            with tabs[AppTab.CART.value]:
                render_cart_workspace()
        render_footer()


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
        with st.spinner(f"Loading {broker_name} account data…"):
            with ThreadPoolExecutor(
                max_workers=len(fetchers), thread_name_prefix="broker-sync"
            ) as executor:
                pending = {
                    executor.submit(fetcher, token): feed
                    for feed, fetcher in fetchers.items()
                }
                for future in as_completed(pending):
                    feed = pending[future]
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

        st.session_state.broker_data_errors = data_errors
        st.session_state.sync_failed = bool(data_errors)
        if data_refreshed:
            st.session_state.last_sync = datetime.now(ZoneInfo(settings.timezone))
            if not data_errors:
                st.toast(
                    f"{broker_name} portfolio data refreshed.",
                    icon=":material/check_circle:",
                )

    equity_holdings = st.session_state.equity_holdings
    portfolio = st.session_state.portfolio
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
        email_recipient = str(
            st.session_state.get("user_profile_email", "")
        ).strip()
        email_configured = bool(
            email_recipient and settings.resend_api_key and settings.resend_sender
        )
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
                    send_resend_email(
                        f"GGHP risk alert: {ticker}",
                        f"<p>{html.escape(alert_text)}</p>",
                        email_recipient,
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
                "No risk alert sent. Add a contact destination in Profile and "
                "check the app's server-side notification configuration."
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


def _record_analysis_failure(scope: str, message: str) -> None:
    st.session_state.analysis_errors[scope] = message
    st.session_state.analysis_results.pop(scope, None)
    st.session_state.analysis_timestamps.pop(scope, None)
    st.session_state.analysis_metadata.pop(scope, None)
    st.session_state.approved_trades_by_scope[scope] = []
    if scope == "portfolio_overview":
        st.session_state.portfolio_overview_analysis = None
        st.session_state.portfolio_overview_timestamp = None
        st.session_state.portfolio_overview_metadata = None


def _complete_analysis_job(job: dict[str, Any]) -> None:
    scope = str(job["scope"])
    provider = str(job["provider"])
    selected_model = str(job["model"])
    api_key = str(job["api_key"])
    future = job["future"]
    try:
        analysis = future.result()
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
                "in Google AI Studio, then update the key in User settings."
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
        elif http_code == 400 or status == "INVALID_ARGUMENT":
            provider_message = (
                error_info.get("message")
                if isinstance(error_info, dict)
                else None
            ) or getattr(error, "message", "")
            provider_message = str(provider_message or "").strip()
            if api_key:
                provider_message = provider_message.replace(api_key, "[redacted]")
            provider_message = " ".join(provider_message.split())
            safe_message = html.escape(provider_message[:400])
            message = (
                "Gemini rejected the analysis request. Check that the selected "
                f"model ({selected_model}) is supported for this key."
                + (f" Provider detail: {safe_message}" if safe_message else "")
            )
        else:
            message = (
                f"Gemini API request failed (HTTP {http_code or 'unknown'}). Check the "
                "API key, model access, project quota, and network connection."
            )
        _record_analysis_failure(scope, message)
        return
    except LLMProviderError as error:
        log_failure(f"{provider} portfolio analysis", error)
        _record_analysis_failure(scope, str(error))
        return
    except (RuntimeError, ValueError) as error:
        log_failure(f"{provider} portfolio analysis", error)
        _record_analysis_failure(scope, f"Portfolio analysis failed: {error}")
        return

    if job.get("portfolio_overview_only"):
        st.session_state.portfolio_overview_analysis = analysis
        analyzed_at = datetime.now(ZoneInfo(settings.timezone)).isoformat()
        st.session_state.portfolio_overview_timestamp = analyzed_at
        st.session_state.portfolio_overview_metadata = {
            "provider": provider,
            "model": selected_model,
        }
        st.session_state.analysis_errors.pop("portfolio_overview", None)
        return

    engine = JevRuleEngine(
        {
            "cash_balance": min(
                job["planning_budget"], max(job["balance"], 0.0)
            ),
            "actual_broker_balance": job["balance"],
            "active_positions_count": job["active_positions_count"],
            "realized_daily_loss_pct": job["realized_daily_loss_pct"],
            "max_allocation_pct": job["max_allocation_pct"],
            "live_prices": job["live_prices"],
            "sector_holdings": job["sectors_held"],
            "portfolio": job["scoped_portfolio"],
            "llm_targets": analysis["cash_deployment_list"],
            "enabled_rules": job["selected_rule_numbers"],
            "buy_lock_until": job["buy_lock_until"],
        }
    )
    trades, audit_trail = engine.run()
    st.session_state.buy_lock_until = engine.facts.get("buy_lock_until")
    st.session_state.analysis_results[scope] = analysis
    analyzed_at = datetime.now(ZoneInfo(settings.timezone)).isoformat()
    st.session_state.analysis_timestamps[scope] = analyzed_at
    st.session_state.analysis_metadata[scope] = {
        "provider": provider,
        "model": selected_model,
    }
    st.session_state.approved_trades_by_scope[scope] = trades
    st.session_state.audit_trails_by_scope[scope] = audit_trail
    st.session_state.jev_rule_selection_after_analysis = job[
        "selected_rule_numbers"
    ]
    st.session_state.analysis_errors.pop(scope, None)
    if scope == "all":
        st.session_state.analysis_result = analysis
        st.session_state.approved_trades = trades
        st.session_state.audit_trail = audit_trail

@st.fragment(run_every=1)
def _render_analysis_progress() -> None:
    job = st.session_state.get("analysis_job")
    if not job:
        return
    if job["future"].done():
        if job["stage"] == "quotes":
            try:
                live_prices = job["future"].result()
                if not isinstance(live_prices, dict):
                    raise TypeError(
                        "Broker live-price response must be a mapping."
                    )
            except (
                BrokerAPIError,
                requests.RequestException,
                RuntimeError,
                ValueError,
                TypeError,
            ) as error:
                log_failure(
                    "Analysis live-price refresh",
                    error,
                    broker=job["broker_name"],
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
            _submit_analysis_request(job, live_prices, price_error)
            st.rerun(scope="app")
        else:
            st.session_state["analysis_job"] = None
            _complete_analysis_job(job)
            st.rerun(scope="app")
def _submit_analysis_request(
    job: dict[str, Any],
    live_prices: dict[str, Any],
    price_error: str | None,
) -> None:
    scope = str(job["scope"])
    live_prices = {
        str(ticker).strip().upper(): price
        for ticker, price in live_prices.items()
    }
    st.session_state.analysis_price_errors[scope] = price_error
    if scope == "all":
        st.session_state.analysis_price_error = price_error
    if scope in {"all", "equity", "trading"}:
        st.session_state.analysis_live_prices_updated_at = datetime.now(
            ZoneInfo(settings.timezone)
        )
        st.session_state.analysis_live_prices = live_prices
    context = job["context"]
    context += (
        "\nThe authenticated quote endpoint returned "
        + (
            "current prices for the listed symbols."
            if live_prices
            else "no usable current prices. Provide portfolio analysis only and "
            "return an empty cash_deployment_list; never estimate prices."
        )
    )
    market_candidates = job["market_candidates"]
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
    else:
        candidate_tickers = []
    job["live_prices"] = live_prices
    job["stage"] = "ai"
    job["future"] = submit_portfolio_analysis(
        portfolio_summary=job["summary"],
        available_cash=job["planning_budget"],
        market_context=context,
        live_prices=live_prices,
        candidate_tickers=candidate_tickers,
        api_key=job["api_key"],
        model=job["model"],
        provider=job["provider"],
        base_url=job["provider_base_url"],
    )


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
        _record_analysis_failure(scope, message)

    active_job = st.session_state.get("analysis_job")
    if active_job and not active_job["future"].done():
        return

    provider = str(
        st.session_state.get("llm_provider", LLMProvider.GEMINI.value)
    )
    provider_slug = provider.lower().replace(" ", "_").replace("-", "_")
    api_key_widget = (
        "user_gemini_api_key"
        if provider == LLMProvider.GEMINI
        else f"user_llm_api_key_{provider_slug}"
    )
    api_key = str(st.session_state.get(api_key_widget) or "").strip()
    if not api_key:
        fail(f"Enter an API key for {provider} in User settings.")
        st.session_state.ai_settings_prompt = (
            f"Analysis needs a {provider} API key. Add it in User settings, "
            "then try again."
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
    if not model_choice:
        fail(f"Select or enter a model ID for {provider}.")
        return
    selected_model = model_choice
    provider_base_url = (
        str(
            st.session_state.get("llm_custom_base_url")
            or settings.llm_custom_base_url
        ).strip()
        if provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE
        else None
    )
    if (
        provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE
        and not provider_base_url
    ):
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
    quote_tickers: list[str] = []
    if scope in {"all", "equity", "trading"}:
        portfolio_tickers = (
            equity_and_positions["Ticker"].dropna().astype(str).str.strip().str.upper()
            .tolist()
        )
        quote_tickers = sorted(set(portfolio_tickers + candidate_tickers))
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
    job = {
        "scope": scope,
        "stage": "quotes",
        "provider": provider,
        "model": str(selected_model),
        "api_key": api_key,
        "provider_base_url": provider_base_url,
        "summary": summary,
        "context": context,
        "market_candidates": list(market_candidates),
        "planning_budget": planning_budget,
        "balance": float(st.session_state.balance),
        "buy_lock_until": st.session_state.buy_lock_until,
        "realized_daily_loss_pct": float(
            st.session_state.realized_daily_loss_pct
        ),
        "max_allocation_pct": float(st.session_state.max_allocation_pct),
        "active_positions_count": int(
            equity_and_positions.loc[equity_and_positions["Qty"].ne(0)]
            .drop_duplicates(subset=["Ticker"])
            .shape[0]
        ),
        "live_prices": {},
        "sectors_held": sorted(sectors_held),
        "scoped_portfolio": scoped_portfolio.copy(),
        "selected_rule_numbers": selected_rule_numbers,
    }
    adapter = st.session_state.get("broker_state")
    token = st.session_state.get("token")
    if scope in {"all", "equity", "trading"} and adapter and token:
        job["broker_name"] = str(st.session_state.get("broker_name", "broker"))
        job["future"] = submit_analysis_task(
            adapter.fetch_live_prices,
            token,
            quote_tickers,
        )
    else:
        price_error = (
            f"No live broker quote feed is configured for {scope_titles[scope]}; "
            "this run is portfolio review only."
            if scope not in {"all", "equity", "trading"}
            else "No broker is connected, so live quotes and price-based deployments "
            "are unavailable. Portfolio analysis can still run on the supplied data."
        )
        _submit_analysis_request(job, {}, price_error)
    st.session_state["analysis_job"] = job
    st.rerun(scope="app")


def _run_portfolio_overview() -> None:
    active_job = st.session_state.get("analysis_job")
    if active_job and not active_job["future"].done():
        return

    provider = str(
        st.session_state.get("llm_provider", LLMProvider.GEMINI.value)
    )
    provider_slug = provider.lower().replace(" ", "_").replace("-", "_")
    api_key_widget = (
        "user_gemini_api_key"
        if provider == LLMProvider.GEMINI
        else f"user_llm_api_key_{provider_slug}"
    )
    api_key = str(st.session_state.get(api_key_widget) or "").strip()
    model = str(
        st.session_state.get(
            f"llm_model_selection_{provider_slug}",
            settings.llm_provider_models[provider][0]
            if settings.llm_provider_models.get(provider)
            else "",
        )
    ).strip()
    if not api_key or not model:
        _record_analysis_failure(
            "portfolio_overview",
            f"Set a {provider} API key and model in User settings before analysis.",
        )
        return

    provider_base_url = (
        str(
            st.session_state.get("llm_custom_base_url")
            or settings.llm_custom_base_url
        ).strip()
        if provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE
        else None
    )
    if provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE and not provider_base_url:
        _record_analysis_failure(
            "portfolio_overview",
            "Set the custom provider's API base URL before analysis.",
        )
        return

    summary = json.dumps(
        {
            "long_term_equity_holdings": (
                st.session_state.equity_holdings.to_dict(orient="records")
            ),
            "open_trading_positions": (
                st.session_state.portfolio.to_dict(orient="records")
            ),
            "mutual_fund_holdings": (
                st.session_state.mutual_funds.to_dict(orient="records")
            ),
            "reported_broker_cash_inr": float(st.session_state.balance),
        },
        ensure_ascii=True,
    )
    job = {
        "scope": "portfolio_overview",
        "stage": "overview",
        "portfolio_overview_only": True,
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "provider_base_url": provider_base_url,
        "future": submit_portfolio_analysis(
            portfolio_summary=summary,
            available_cash=0.0,
            market_context=(
                "Only broker-reported holding quantities, prices, NAVs, and cash "
                "were supplied; they may be stale. No external market scan, "
                "deployment budget, or JEV rule data was supplied."
            ),
            live_prices={},
            candidate_tickers=[],
            api_key=api_key,
            model=model,
            provider=provider,
            base_url=provider_base_url,
            portfolio_only=True,
        ),
    }
    st.session_state.analysis_errors.pop("portfolio_overview", None)
    st.session_state["analysis_job"] = job
    st.rerun(scope="app")


def _request_analysis(scope: str) -> None:
    if portfolio_analysis_is_configured():
        _run_analysis(scope)
        return
    st.session_state.analysis_setup_dialog_scope = scope


def _request_portfolio_overview() -> None:
    if portfolio_analysis_is_configured():
        _run_portfolio_overview()
        return
    st.session_state.analysis_setup_dialog_scope = "portfolio_overview"


def _pending_analysis_setup_scope() -> str | None:
    """Wait to show analysis setup until another settings dialog is dismissed."""
    feedback = st.session_state.get("user_settings_feedback", {})
    if (
        st.session_state.get("user_settings_dialog_open")
        or (
            isinstance(feedback, dict)
            and feedback.get("kind") == "success"
        )
    ):
        return None
    scope = st.session_state.get("analysis_setup_dialog_scope")
    return str(scope) if scope else None


def _close_analysis_setup_dialog() -> None:
    st.session_state.pop("analysis_setup_dialog_scope", None)


@st.dialog("AI analysis setup", on_dismiss=_close_analysis_setup_dialog)
def _render_analysis_setup_dialog(scope: str) -> None:
    configured_providers = [
        item
        for item in st.session_state.get("settings_ai_provider_configs", [])
        if isinstance(item, dict)
        and item.get("id")
        and item.get("provider")
        in {provider.provider.value for provider in LLMProviderBaseURL}
    ]
    configured_provider = None
    if configured_providers:
        selected_config_key = f"analysis_setup_config_{scope}"
        valid_ids = {str(item["id"]) for item in configured_providers}
        selected_config_id = str(
            st.session_state.get(
                selected_config_key,
                st.session_state.get("settings_ai_active_config_id", ""),
            )
        )
        if selected_config_id not in valid_ids:
            selected_config_id = str(configured_providers[0]["id"])
        st.session_state[selected_config_key] = selected_config_id
        configured_by_id = {
            str(item["id"]): item for item in configured_providers
        }
        selected_config_id = st.selectbox(
            "Configured AI provider",
            options=list(configured_by_id),
            format_func=lambda config_id: (
                f"{configured_by_id[config_id].get('name') or configured_by_id[config_id]['provider']} "
                f"· {configured_by_id[config_id]['model'] or 'model not set'}"
            ),
            key=selected_config_key,
        )
        configured_provider = configured_by_id[selected_config_id]
        provider = str(configured_provider["provider"])
        provider_slug = provider.lower().replace(" ", "_").replace("-", "_")
        model = str(configured_provider.get("model", "")).strip()
        base_url = str(configured_provider.get("base_url", "")).strip()
        config_keys = st.session_state.get(
            "settings_ai_provider_config_keys", {}
        )
        session_config_keys = st.session_state.get(
            "analysis_setup_session_keys", {}
        )
        api_key = str(
            config_keys.get(selected_config_id)
            or session_config_keys.get(selected_config_id, "")
        ).strip()
        if (
            not api_key
            and selected_config_id
            == str(st.session_state.get("settings_ai_active_config_id", ""))
        ):
            api_key = str(
                st.session_state.get(
                    "user_gemini_api_key"
                    if provider == LLMProvider.GEMINI.value
                    else f"user_llm_api_key_{provider_slug}",
                    "",
                )
            ).strip()
        provider_label = str(
            configured_provider.get("name") or provider
        )
        st.write(
            f"Complete the missing **{provider_label}** settings to run this "
            "analysis. New values are used for this session only."
        )
    else:
        provider_names = [
            provider.provider.value for provider in LLMProviderBaseURL
        ]
        current_provider = str(
            st.session_state.get("llm_provider", LLMProvider.GEMINI.value)
        )
        selected_provider_key = f"analysis_setup_provider_{scope}"
        if selected_provider_key not in st.session_state:
            st.session_state[selected_provider_key] = (
                current_provider
                if current_provider in provider_names
                else LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
            )
        selected_provider = st.selectbox(
            "AI provider",
            options=provider_names,
            accept_new_options=True,
            key=selected_provider_key,
            help=(
                "Choose a provider or enter another provider name that supports "
                "the OpenAI-compatible API."
            ),
        )
        custom_provider = (
            selected_provider not in provider_names
            or selected_provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
        )
        provider = (
            LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
            if custom_provider
            else str(selected_provider)
        )
        provider_slug = provider.lower().replace(" ", "_").replace("-", "_")
        provider_label = str(selected_provider)
        if custom_provider:
            manual_name_slug = (
                str(selected_provider).lower().replace(" ", "_").replace("-", "_")
            )
            name_key = f"analysis_setup_provider_name_{scope}_{manual_name_slug}"
            if name_key not in st.session_state:
                st.session_state[name_key] = (
                    ""
                    if selected_provider
                    == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
                    else str(selected_provider)
                )
            provider_label = st.text_input(
                "Provider name",
                key=name_key,
                placeholder="For example, OpenRouter",
            )
            provider_label = str(provider_label or "").strip()
        model = str(
            st.session_state.get(
                f"llm_model_selection_{provider_slug}",
                next(iter(settings.llm_provider_models.get(provider, ())), ""),
            )
        ).strip()
        base_url = str(
            st.session_state.get(
                "llm_custom_base_url", settings.llm_custom_base_url
            )
        ).strip()
        api_key_widget = (
            "user_gemini_api_key"
            if provider == LLMProvider.GEMINI.value
            else f"user_llm_api_key_{provider_slug}"
        )
        api_key = str(st.session_state.get(api_key_widget, "")).strip()
        st.write(
            f"Enter the missing **{provider_label or provider}** settings. "
            "Values are used for this session only."
        )

    api_key_widget = (
        "user_gemini_api_key"
        if provider == LLMProvider.GEMINI.value
        else f"user_llm_api_key_{provider_slug}"
    )
    model_widget = f"llm_model_selection_{provider_slug}"
    custom_provider = provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
    input_suffix = (
        str(configured_provider["id"])
        if configured_provider
        else provider_slug
    )
    form_key = (
        f"analysis_setup_form_{scope}_"
        f"{input_suffix}"
    )
    with st.form(form_key):
        api_key_input = ""
        if not api_key:
            api_key_input = st.text_input(
                f"{provider_label or provider} API key",
                type="password",
                key=f"analysis_setup_api_key_{scope}_{input_suffix}",
            )
        model_input = st.text_input(
            "Model ID",
            value=model,
            key=f"analysis_setup_model_{scope}_{input_suffix}",
        )
        base_url_input = base_url
        if custom_provider:
            base_url_input = st.text_input(
                "API base URL",
                value=base_url,
                key=f"analysis_setup_base_url_{scope}_{input_suffix}",
                placeholder="https://api.example.com/v1",
            )
        submitted = st.form_submit_button(
            "Continue",
            type="primary",
            icon=":material/auto_awesome:",
        )

    if not submitted:
        if st.button("Cancel", key=f"analysis_setup_cancel_{scope}"):
            st.session_state.pop("analysis_setup_dialog_scope", None)
            st.rerun()
        return
    api_key = str(api_key or api_key_input).strip()
    model = str(model_input).strip()
    base_url = str(base_url_input).strip()
    if not api_key:
        st.error(f"Enter an API key for {provider_label or provider}.")
        return
    if not model:
        st.error(f"Enter a model ID for {provider_label or provider}.")
        return
    if custom_provider and not base_url:
        st.error("Enter an API base URL for the custom provider.")
        return
    if custom_provider and not provider_label:
        st.error("Enter a name for the custom provider.")
        return

    st.session_state.llm_provider = provider
    st.session_state[api_key_widget] = api_key
    st.session_state[model_widget] = model
    if custom_provider:
        st.session_state.llm_custom_base_url = base_url
        st.session_state.settings_ai_custom_provider_name = provider_label
    if configured_provider:
        session_config_keys = dict(
            st.session_state.get("analysis_setup_session_keys", {})
        )
        session_config_keys[str(configured_provider["id"])] = api_key
        st.session_state.analysis_setup_session_keys = session_config_keys
    st.session_state.pop("analysis_setup_dialog_scope", None)
    if scope == "portfolio_overview":
        _run_portfolio_overview()
    else:
        _run_analysis(scope)
    st.rerun()


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
    _render_app_header()
    if st.session_state.get("analysis_job"):
        _render_analysis_progress()
    with st.container(border=True, gap="medium"):
        profile_name = _fetch_broker_profile_name()
        tabs = _render_header_tabs()
        polling_sequence()
        if st.session_state.planning_budget_inr is None:
            st.session_state.planning_budget_inr = max(
                float(st.session_state.balance), 1.0
            )
        if tabs[APP_TAB_LABELS[0]].open:
            with tabs[APP_TAB_LABELS[0]]:
                _render_workspace_account_context(
                    "home",
                    profile_name,
                )
                render_home_workspace(
                    float(st.session_state.balance),
                    st.session_state.equity_holdings,
                    st.session_state.portfolio,
                    st.session_state.mutual_funds,
                    live_prices=st.session_state.analysis_live_prices,
                    live_prices_updated_at=(
                        st.session_state.analysis_live_prices_updated_at
                    ),
                    run_portfolio_analysis=_request_portfolio_overview,
                    render_portfolio_review=_render_portfolio_ai_review,
                )
        for asset in APP_TAB_LABELS[1:-1]:
            if tabs[asset].open:
                with tabs[asset]:
                    _render_workspace_account_context(
                        asset.casefold().replace("&", "and").replace(" ", "_"),
                        profile_name,
                    )
                    _render_asset_workspace(asset, broker_connected=True)
        if tabs[AppTab.CART.value].open:
            with tabs[AppTab.CART.value]:
                render_cart_workspace()
        render_footer()


def _render_workspace_account_context(
    scope: str,
    profile_name: str,
) -> None:
    status_column, account_column = st.columns(
        [2, 3],
        vertical_alignment="center",
        gap="small",
    )
    with status_column:
        last_sync = st.session_state.last_sync
        account_status = f"{st.session_state.broker_name} connected"
        st.badge(
            account_status,
            icon=":material/check_circle:",
            color="green",
        )
        if last_sync:
            st.markdown(
                ":gray[Last broker sync:] "
                '<span style="color:rgba(250,250,250,.6);font-size:.875rem">'
                f"<strong>{last_sync.strftime('%d %b %Y, %H:%M:%S IST')}</strong>"
                "</span>",
                unsafe_allow_html=True,
            )
        else:
            st.caption("Broker data sync pending")
    with account_column:
        with st.container(
            horizontal=True,
            horizontal_alignment="right",
            vertical_alignment="center",
            gap="small",
            wrap=False,
        ):
            if profile_name:
                st.html(
                    signed_in_label_html(profile_name),
                    width="content",
                )
            st.button(
                " ",
                key=f"workspace_disconnect_{scope}",
                type="primary",
                on_click=_logout,
                icon=":material/swap_horiz:",
                help="Disconnect this broker and connect another account.",
            )
    if st.session_state.sync_failed:
        st.warning(
            "Some broker data could not be refreshed; showing the last "
            "successful data."
        )
        with st.expander("Refresh diagnostics"):
            for feed, diagnostic in st.session_state.broker_data_errors.items():
                st.caption(f"**{feed.replace('_', ' ').title()}:** {diagnostic}")


def _fetch_broker_profile_name() -> str:
    if st.session_state.profile_fetch_attempted:
        return str(st.session_state.broker_profile_name or "")
    st.session_state.profile_fetch_attempted = True
    adapter = st.session_state.broker_state
    token = st.session_state.token
    st.session_state.broker_profile_identifier = str(
        getattr(adapter, "client_id", "") or ""
    ).strip()
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
    profile_identifier = (
        profile.get("user_id") or profile.get("client_id")
        if isinstance(profile, dict)
        else ""
    )
    st.session_state.broker_profile_identifier = str(
        profile_identifier or st.session_state.broker_profile_identifier or name
    ).strip()
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
            "email and phone saved in your profile. The app operator must "
            "configure Resend and/or Twilio delivery."
        )
        email_recipient = str(
            st.session_state.get("user_profile_email", "")
        ).strip()
        email_ready = bool(
            email_recipient and settings.resend_api_key and settings.resend_sender
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
                    _, report_html = _portfolio_report()
                    send_resend_email(
                        "Your GGHP portfolio update",
                        report_html,
                        email_recipient,
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
                "Add an email address and/or phone number under User settings → "
                "Profile. Email needs operator-configured Resend; WhatsApp needs "
                "operator-configured Twilio."
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
        render_equities_workspace(
            st.session_state.equity_holdings,
            st.session_state.broker_data_errors.get("equity"),
            broker_connected=broker_connected,
            symbols=_workspace_symbols(st.session_state.equity_holdings),
            run_analysis=_request_analysis,
            show_approved_trades=_show_approved_trades,
            total_investments=calculate_allocation(
                float(st.session_state.balance),
                st.session_state.equity_holdings,
                st.session_state.portfolio,
                st.session_state.mutual_funds,
            )["total_investments"],
        )
    elif asset == "Trades":
        render_trades_workspace(
            st.session_state.portfolio,
            st.session_state.broker_data_errors.get("trading"),
            broker_connected=broker_connected,
            symbols=_workspace_symbols(st.session_state.portfolio),
            run_analysis=_request_analysis,
            show_approved_trades=_show_approved_trades,
        )
    elif asset == "F&O":
        render_derivatives_workspace(_run_analysis, _show_approved_trades)
    elif asset == "Mutual Funds":
        render_mutual_funds_workspace(
            st.session_state.mutual_funds,
            st.session_state.broker_data_errors.get("mutual_funds")
            or st.session_state.mutual_fund_error,
            run_analysis=_request_analysis,
            show_approved_trades=_show_approved_trades,
            render_local_asset_research=render_local_asset_research_view,
        )
    else:
        raise ValueError(f"Unsupported workspace: {asset}")

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
        if float(st.session_state.realized_daily_loss_pct) <= -3.0:
            st.warning(
                "Reported daily loss has reached the circuit-breaker threshold. "
                "New app-issued buys will be blocked."
            )
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
            "all",
            "your full portfolio",
            _request_analysis,
            _show_approved_trades,
        )

def _logout() -> None:
    _cancel_holding_jobs()
    cancel_scanner_jobs()
    st.session_state.return_to_home = True
    st.session_state.stock_context_cache = {}
    active_analysis_job = st.session_state.pop("analysis_job", None)
    if active_analysis_job:
        active_analysis_job["future"].cancel()
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
        "broker_profile_name",
        "broker_profile_identifier",
        "profile_fetch_attempted",
        "analysis_result",
        "analysis_results",
        "portfolio_overview_analysis",
        "portfolio_overview_timestamp",
        "portfolio_overview_metadata",
        "analysis_timestamps",
        "analysis_metadata",
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
        "pending_broker_name",
        "pending_broker_adapter",
        "pending_login_url",
        "pending_redirect_url",
        "broker_callback_url",
        "pending_oauth_fingerprint",
        "pending_oauth_expires_at",
    ):
        st.session_state[key] = None
    st.session_state.portfolio = pd.DataFrame(columns=PORTFOLIO_COLUMNS)
    st.session_state.equity_holdings = pd.DataFrame(columns=PORTFOLIO_COLUMNS)
    st.session_state.mutual_funds = pd.DataFrame(columns=MUTUAL_FUND_COLUMNS)
    st.session_state.balance = 0.0
    st.session_state.alerted_risk_channels = []
    st.session_state.risk_high_water = {}
    st.session_state.risk_alerts = []
    st.session_state.risk_errors = []
    st.session_state.analysis_results = {}
    st.session_state.analysis_timestamps = {}
    st.session_state.analysis_metadata = {}
    st.session_state.analysis_errors = {}
    st.session_state.analysis_price_errors = {}
    st.session_state.market_ai_candidates = []
    st.session_state.market_ai_candidates_updated_at = None
    st.session_state.approved_trades_by_scope = {}
    st.session_state.audit_trails_by_scope = {}
    st.session_state.last_sync_attempt = None
    st.session_state.broker_data_errors = {}


def _logout_oidc() -> None:
    _logout()
    _clear_broker_login_inputs()
    for key in tuple(st.session_state):
        if key.startswith(
            (
                "user_settings_",
                "settings_profile_",
                "settings_ai_",
                "settings_sensitive_",
                "user_profile_",
                "user_llm_api_key_",
                "llm_model_selection_",
            )
        ) or key in {
            "user_gemini_api_key",
            "llm_custom_model",
            "llm_custom_base_url",
            "user_settings_loaded_for",
            "user_settings_form_cache",
            "user_settings_dialog_open",
            "user_settings_feedback",
            "analysis_setup_session_keys",
            "tested_ai_provider_fingerprints",
        }:
            st.session_state.pop(key, None)
    st.session_state.llm_provider_settings = {}
    st.session_state.llm_provider = LLMProvider.GEMINI.value
    st.logout()


def main() -> None:
    st.set_page_config(
        page_title=APP_TITLE, page_icon=":material/verified_user:",
        layout="wide", initial_sidebar_state="collapsed",
    )
    st.html(app_animation_css())
    st.html(gold_theme_css())
    if settings.require_login and not _user_is_authenticated():
        render_brand_lockup()
        st.caption("Sign in securely to open your private investment workspace.")
        if st.button("Sign in", type="primary", icon=":material/login:"):
            st.login()
        st.stop()
    _initialize_session()
    _load_user_settings_for_user()
    _clear_legacy_analysis_storage()
    _clear_legacy_cloud_brokers_for_user()
    st.session_state.reevaluate_analysis_rules = _reevaluate_analysis_rules
    if st.session_state.token is None:
        _show_login()
    else:
        _show_workspace()
    analysis_setup_scope = _pending_analysis_setup_scope()
    if analysis_setup_scope:
        _render_analysis_setup_dialog(analysis_setup_scope)
    process_browser_storage_bridge()

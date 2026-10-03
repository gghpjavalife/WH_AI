"""Application settings with safe defaults and environment overrides."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

LLM_PROVIDER_BASE_URLS = {
    "Gemini": None,
    "OpenAI": "https://api.openai.com/v1",
    "Anthropic": "https://api.anthropic.com/v1",
    "Groq": "https://api.groq.com/openai/v1",
    "Together AI": "https://api.together.xyz/v1",
    "Mistral": "https://api.mistral.ai/v1",
    "DeepSeek": "https://api.deepseek.com/v1",
    "Custom OpenAI-compatible": None,
}

DEFAULT_LLM_PROVIDER_MODELS = {
    "Gemini": (
        "gemini-3.8-flash",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ),
    "OpenAI": ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"),
    "Anthropic": (
        "claude-3-5-haiku-latest",
        "claude-3-7-sonnet-latest",
        "claude-sonnet-4-20250514",
    ),
    "Groq": (
        "llama-3.3-70b-versatile",
        "deepseek-r1-distill-llama-70b",
        "llama-3.1-8b-instant",
    ),
    "Together AI": (
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "meta-llama/Llama-3.1-8B-Instruct-Turbo",
        "Qwen/Qwen2.5-72B-Instruct-Turbo",
    ),
    "Mistral": ("mistral-small-latest", "mistral-large-latest", "open-mistral-nemo"),
    "DeepSeek": ("deepseek-chat", "deepseek-reasoner"),
    "Custom OpenAI-compatible": (),
}


def configured_value(name: str, default: Any = "") -> Any:
    """Read a deployment setting from environment variables or Streamlit secrets."""
    value = os.environ.get(name)
    if value is not None:
        return value
    try:
        import streamlit as st
        from streamlit.errors import StreamlitSecretNotFoundError

        if not st.runtime.exists():
            return default
        return st.secrets.get(name, default)
    except (StreamlitSecretNotFoundError, RuntimeError):
        return default


def _int_env(name: str, default: int) -> int:
    value = configured_value(name, None)
    return default if value is None else int(value)


def _float_env(name: str, default: float) -> float:
    value = configured_value(name, None)
    return default if value is None else float(value)


def _dynamic_broker_hosts() -> tuple[str, ...]:
    configured = configured_value("DYNAMIC_BROKER_ALLOWED_HOSTS", "")
    values = configured.split(",") if isinstance(configured, str) else configured
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(str(host).strip().lower() for host in values if str(host).strip())


APP_BRAND = "GGHP"
APP_BRAND_EXPANSION = "Governed Growth & Hedged Portfolios"
APP_BRAND_DESCRIPTION = "Governed multi-broker portfolio companion"


@dataclass(frozen=True)
class Settings:
    app_title: str = f"{APP_BRAND} | Governed Portfolio Companion"
    timezone: str = "Asia/Kolkata"
    default_redirect_uri: str = "http://localhost:8501"
    upstox_redirect_uri: str = ""
    upstox_api_base: str = "https://api.upstox.com"
    upstox_login_url: str = "https://api.upstox.com/v2/login/authorization/dialog"
    upstox_token_url: str = "https://api.upstox.com/v2/login/authorization/token"
    zerodha_api_base: str = "https://api.kite.trade"
    zerodha_login_url: str = "https://kite.zerodha.com/connect/login"
    dhan_api_base: str = "https://api.dhan.co/v2"
    dhan_auth_base: str = "https://auth.dhan.co"
    dhan_instruments_url: str = (
        "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
    )
    oauth_database_path: str = str(PROJECT_ROOT / ".wealth_home_oauth.sqlite3")
    oauth_credential_encryption_key: str = ""
    http_connect_timeout_seconds: int = 5
    http_read_timeout_seconds: int = 20
    database_timeout_seconds: int = 10
    oauth_state_ttl_seconds: int = 600
    market_lookback_days: int = 60
    atr_period: int = 14
    atr_multiplier: float = 3.0
    trading_session_start: time = time(9, 15)
    trading_session_end: time = time(15, 30)
    sync_interval_minutes: int = 5
    dashboard_poll_seconds: int = 300
    llm_provider_models: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: DEFAULT_LLM_PROVIDER_MODELS.copy()
    )
    llm_custom_base_url: str = ""
    llm_temperature: float = 0.1
    llm_max_recommendations: int = 5
    llm_retry_attempts: int = 4
    llm_retry_initial_delay_seconds: float = 1.0
    llm_retry_max_delay_seconds: float = 6.0
    llm_retry_exponent: float = 2.0
    llm_retry_jitter: float = 0.2
    resend_api_key: str = ""
    resend_sender: str = ""
    hf_token: str = ""
    turso_primary_db_url: str = ""
    turso_auth_token: str = ""
    dynamic_broker_allowed_hosts: tuple[str, ...] = ()
    require_login: bool = False

    def __post_init__(self) -> None:
        positive_values = (
            self.http_connect_timeout_seconds,
            self.http_read_timeout_seconds,
            self.database_timeout_seconds,
            self.oauth_state_ttl_seconds,
            self.market_lookback_days,
            self.atr_period,
            self.atr_multiplier,
            self.sync_interval_minutes,
            self.dashboard_poll_seconds,
            self.llm_max_recommendations,
            self.llm_retry_attempts,
            self.llm_retry_initial_delay_seconds,
            self.llm_retry_max_delay_seconds,
            self.llm_retry_exponent,
        )
        if any(value <= 0 for value in positive_values):
            raise ValueError("Configured numeric settings must be positive.")

    @classmethod
    def from_environment(cls) -> Settings:
        defaults = cls()
        provider_models = {
            provider: tuple(models)
            for provider, models in DEFAULT_LLM_PROVIDER_MODELS.items()
        }
        configured_provider_models = os.environ.get("LLM_PROVIDER_MODELS", "").strip()
        if configured_provider_models:
            try:
                parsed_provider_models = json.loads(configured_provider_models)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "LLM_PROVIDER_MODELS must be a JSON object of provider names "
                    "to model ID lists."
                ) from exc
            if not isinstance(parsed_provider_models, dict):
                raise ValueError("LLM_PROVIDER_MODELS must be a JSON object.")
            for provider, models in parsed_provider_models.items():
                if provider not in LLM_PROVIDER_BASE_URLS:
                    raise ValueError(
                        f"LLM_PROVIDER_MODELS contains unsupported provider {provider!r}."
                    )
                if provider == "Custom OpenAI-compatible":
                    raise ValueError(
                        "Custom OpenAI-compatible model IDs are entered per user "
                        "and cannot be configured as a shared model list."
                    )
                if (
                    not isinstance(models, list)
                    or not models
                    or any(not isinstance(model, str) or not model.strip() for model in models)
                ):
                    raise ValueError(
                        f"LLM_PROVIDER_MODELS[{provider!r}] must be a non-empty "
                        "list of model IDs."
                    )
                provider_models[provider] = tuple(
                    dict.fromkeys(model.strip() for model in models)
                )
        return cls(
            app_title=os.environ.get("APP_TITLE", "").strip() or defaults.app_title,
            timezone=os.environ.get("APP_TIMEZONE", defaults.timezone),
            default_redirect_uri=os.environ.get(
                "DEFAULT_REDIRECT_URI", defaults.default_redirect_uri
            ),
            upstox_redirect_uri=os.environ.get("UPSTOX_REDIRECT_URI", "").strip(),
            upstox_api_base=os.environ.get(
                "UPSTOX_API_BASE", defaults.upstox_api_base
            ),
            upstox_login_url=os.environ.get(
                "UPSTOX_LOGIN_URL", defaults.upstox_login_url
            ),
            upstox_token_url=os.environ.get(
                "UPSTOX_TOKEN_URL", defaults.upstox_token_url
            ),
            zerodha_api_base=os.environ.get(
                "ZERODHA_API_BASE", defaults.zerodha_api_base
            ),
            zerodha_login_url=os.environ.get(
                "ZERODHA_LOGIN_URL", defaults.zerodha_login_url
            ),
            dhan_api_base=os.environ.get("DHAN_API_BASE", defaults.dhan_api_base),
            dhan_auth_base=os.environ.get(
                "DHAN_AUTH_BASE", defaults.dhan_auth_base
            ),
            dhan_instruments_url=os.environ.get(
                "DHAN_INSTRUMENTS_URL", defaults.dhan_instruments_url
            ),
            oauth_database_path=(
                os.environ.get("WEALTH_HOME_OAUTH_DB", "").strip()
                or defaults.oauth_database_path
            ),
            oauth_credential_encryption_key=os.environ.get(
                "OAUTH_CREDENTIAL_ENCRYPTION_KEY", ""
            ).strip(),
            http_connect_timeout_seconds=_int_env(
                "HTTP_CONNECT_TIMEOUT_SECONDS",
                defaults.http_connect_timeout_seconds,
            ),
            http_read_timeout_seconds=_int_env(
                "HTTP_READ_TIMEOUT_SECONDS", defaults.http_read_timeout_seconds
            ),
            database_timeout_seconds=_int_env(
                "DATABASE_TIMEOUT_SECONDS", defaults.database_timeout_seconds
            ),
            oauth_state_ttl_seconds=_int_env(
                "OAUTH_STATE_TTL_SECONDS", defaults.oauth_state_ttl_seconds
            ),
            market_lookback_days=_int_env(
                "MARKET_LOOKBACK_DAYS", defaults.market_lookback_days
            ),
            atr_period=_int_env("ATR_PERIOD", defaults.atr_period),
            atr_multiplier=_float_env("ATR_MULTIPLIER", defaults.atr_multiplier),
            trading_session_start=time.fromisoformat(
                os.environ.get(
                    "TRADING_SESSION_START", defaults.trading_session_start.isoformat()
                )
            ),
            trading_session_end=time.fromisoformat(
                os.environ.get(
                    "TRADING_SESSION_END", defaults.trading_session_end.isoformat()
                )
            ),
            sync_interval_minutes=_int_env(
                "SYNC_INTERVAL_MINUTES", defaults.sync_interval_minutes
            ),
            dashboard_poll_seconds=_int_env(
                "DASHBOARD_POLL_SECONDS", defaults.dashboard_poll_seconds
            ),
            llm_provider_models=provider_models,
            llm_custom_base_url=os.environ.get(
                "LLM_CUSTOM_BASE_URL", defaults.llm_custom_base_url
            ).strip(),
            llm_temperature=_float_env(
                "LLM_TEMPERATURE", defaults.llm_temperature
            ),
            llm_max_recommendations=_int_env(
                "LLM_MAX_RECOMMENDATIONS", defaults.llm_max_recommendations
            ),
            llm_retry_attempts=_int_env(
                "LLM_RETRY_ATTEMPTS", defaults.llm_retry_attempts
            ),
            llm_retry_initial_delay_seconds=_float_env(
                "LLM_RETRY_INITIAL_DELAY_SECONDS",
                defaults.llm_retry_initial_delay_seconds,
            ),
            llm_retry_max_delay_seconds=_float_env(
                "LLM_RETRY_MAX_DELAY_SECONDS",
                defaults.llm_retry_max_delay_seconds,
            ),
            llm_retry_exponent=_float_env(
                "LLM_RETRY_EXPONENT", defaults.llm_retry_exponent
            ),
            llm_retry_jitter=_float_env(
                "LLM_RETRY_JITTER", defaults.llm_retry_jitter
            ),
            resend_api_key=str(configured_value("RESEND_API_KEY", "")).strip(),
            resend_sender=str(configured_value("RESEND_SENDER", "")).strip(),
            hf_token=str(configured_value("HF_TOKEN", "")).strip(),
            turso_primary_db_url=str(
                configured_value("TURSO_PRIMARY_DB_URL", "")
            ).strip(),
            turso_auth_token=str(configured_value("TURSO_AUTH_TOKEN", "")).strip(),
            dynamic_broker_allowed_hosts=_dynamic_broker_hosts(),
            require_login=str(
                configured_value("WEALTH_HOME_REQUIRE_LOGIN", "false")
            ).strip().lower()
            in {"1", "true", "yes"},
        )


settings = Settings.from_environment()

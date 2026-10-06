"""Application settings and environment configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import time
from typing import Any

from dotenv import load_dotenv

from .constants import (
    AppIdentity,
    BrokerEndpoint,
    LLMProvider,
    LLMProviderBaseURL,
    LLMProviderModels,
    ProjectPath,
    RuntimeDefault,
    default_llm_provider_models,
)

load_dotenv(ProjectPath.ROOT.value / ".env")


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


def _dynamic_broker_hosts() -> tuple[str, ...]:
    configured = configured_value("DYNAMIC_BROKER_ALLOWED_HOSTS", "")
    values = configured.split(",") if isinstance(configured, str) else configured
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(str(host).strip().lower() for host in values if str(host).strip())


@dataclass(frozen=True)
class Settings:
    app_title: str = AppIdentity.TITLE.value
    timezone: str = AppIdentity.TIMEZONE.value
    default_redirect_uri: str = AppIdentity.DEFAULT_REDIRECT_URI.value
    upstox_redirect_uri: str = ""
    upstox_api_base: str = BrokerEndpoint.UPSTOX_API_BASE.value
    upstox_login_url: str = BrokerEndpoint.UPSTOX_LOGIN_URL.value
    upstox_token_url: str = BrokerEndpoint.UPSTOX_TOKEN_URL.value
    zerodha_api_base: str = BrokerEndpoint.ZERODHA_API_BASE.value
    zerodha_login_url: str = BrokerEndpoint.ZERODHA_LOGIN_URL.value
    dhan_api_base: str = BrokerEndpoint.DHAN_API_BASE.value
    dhan_auth_base: str = BrokerEndpoint.DHAN_AUTH_BASE.value
    dhan_instruments_url: str = BrokerEndpoint.DHAN_INSTRUMENTS_URL.value
    oauth_database_path: str = str(
        ProjectPath.ROOT.value / ".wealth_home_oauth.sqlite3"
    )
    database_encryption_key: str = ""
    oauth_credential_encryption_key: str = ""
    http_connect_timeout_seconds: int = RuntimeDefault.HTTP_CONNECT_TIMEOUT_SECONDS.value
    http_read_timeout_seconds: int = RuntimeDefault.HTTP_READ_TIMEOUT_SECONDS.value
    database_timeout_seconds: int = RuntimeDefault.DATABASE_TIMEOUT_SECONDS.value
    oauth_state_ttl_seconds: int = RuntimeDefault.OAUTH_STATE_TTL_SECONDS.value
    market_lookback_days: int = RuntimeDefault.MARKET_LOOKBACK_DAYS.value
    atr_period: int = RuntimeDefault.ATR_PERIOD.value
    atr_multiplier: float = RuntimeDefault.ATR_MULTIPLIER.value
    trading_session_start: time = time.fromisoformat(
        RuntimeDefault.TRADING_SESSION_START.value
    )
    trading_session_end: time = time.fromisoformat(
        RuntimeDefault.TRADING_SESSION_END.value
    )
    sync_interval_minutes: int = RuntimeDefault.SYNC_INTERVAL_MINUTES.value
    dashboard_poll_seconds: int = RuntimeDefault.DASHBOARD_POLL_SECONDS.value
    llm_provider_models: dict[str, tuple[str, ...]] = field(
        default_factory=default_llm_provider_models
    )
    llm_custom_base_url: str = ""
    llm_temperature: float = RuntimeDefault.LLM_TEMPERATURE.value
    llm_max_recommendations: int = RuntimeDefault.LLM_MAX_RECOMMENDATIONS.value
    llm_retry_attempts: int = RuntimeDefault.LLM_RETRY_ATTEMPTS.value
    llm_retry_initial_delay_seconds: float = (
        RuntimeDefault.LLM_RETRY_INITIAL_DELAY_SECONDS.value
    )
    llm_retry_max_delay_seconds: float = (
        RuntimeDefault.LLM_RETRY_MAX_DELAY_SECONDS.value
    )
    llm_retry_exponent: float = RuntimeDefault.LLM_RETRY_EXPONENT.value
    llm_retry_jitter: float = RuntimeDefault.LLM_RETRY_JITTER.value
    resend_api_key: str = ""
    resend_sender: str = ""
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
        provider_models = default_llm_provider_models()
        configured_provider_models = os.environ.get("LLM_PROVIDER_MODELS", "").strip()
        if configured_provider_models:
            try:
                parsed_provider_models = json.loads(configured_provider_models)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "LLM_PROVIDER_MODELS must be a JSON object of provider names to model ID lists."
                ) from exc
            if not isinstance(parsed_provider_models, dict):
                raise ValueError("LLM_PROVIDER_MODELS must be a JSON object.")
            for provider, models in parsed_provider_models.items():
                if provider not in {
                    item.provider.value for item in LLMProviderBaseURL
                }:
                    raise ValueError(
                        f"LLM_PROVIDER_MODELS contains unsupported provider {provider!r}."
                    )
                if provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE:
                    raise ValueError(
                        "Custom OpenAI-compatible model IDs are entered per user and cannot be configured as a shared model list."
                    )
                if (
                    not isinstance(models, list)
                    or not models
                    or any(not isinstance(model, str) or not model.strip() for model in models)
                ):
                    raise ValueError(
                        f"LLM_PROVIDER_MODELS[{provider!r}] must be a non-empty list of model IDs."
                    )
                provider_models[provider] = tuple(
                    dict.fromkeys(model.strip() for model in models)
                )
        return cls(
            upstox_redirect_uri=os.environ.get("UPSTOX_REDIRECT_URI", "").strip(),
            oauth_database_path=(
                os.environ.get("WEALTH_HOME_OAUTH_DB", "").strip()
                or defaults.oauth_database_path
            ),
            oauth_credential_encryption_key=os.environ.get(
                "OAUTH_CREDENTIAL_ENCRYPTION_KEY", ""
            ).strip(),
            database_encryption_key=str(
                configured_value("WEALTH_HOME_DB_ENCRYPTION_KEY", "")
            ).strip(),
            llm_provider_models=provider_models,
            llm_custom_base_url=os.environ.get(
                "LLM_CUSTOM_BASE_URL", defaults.llm_custom_base_url
            ).strip(),
            resend_api_key=str(configured_value("RESEND_API_KEY", "")).strip(),
            resend_sender=str(configured_value("RESEND_SENDER", "")).strip(),
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

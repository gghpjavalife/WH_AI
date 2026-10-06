"""User profile, broker, AI-provider, and risk settings UI."""

from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4
from math import isfinite
from typing import Any

import requests
import streamlit as st

from core.config import settings
from core.constants import LLMProvider, LLMProviderBaseURL
from core.logging import log_failure
from services.ai import test_ai_provider_configuration
from services.user_settings import load_user_settings, save_user_settings
from services.database import DatabaseError, DatabaseSecurityError

BROKER_CREDENTIAL_FIELDS = {
    "Upstox": {"api_key": "API key", "api_secret": "API secret"},
    "Zerodha": {"api_key": "API key", "api_secret": "API secret"},
    "Angel One": {
        "api_key": "API key",
        "client_id": "Client ID",
        "password": "Password",
        "totp_secret": "TOTP secret",
    },
    "Dhan": {
        "client_id": "Client ID",
        "api_key": "API key",
        "api_secret": "API secret",
    },
}
PROVIDER_OPTIONS = tuple(item.provider.value for item in LLMProviderBaseURL)
PROVIDER_WIDGET_KEYS = {
    provider: (
        "user_gemini_api_key"
        if provider == LLMProvider.GEMINI.value
        else "user_llm_api_key_"
        + provider.lower().replace(" ", "_").replace("-", "_")
    )
    for provider in PROVIDER_OPTIONS
}
BROKER_CHOICES = tuple(BROKER_CREDENTIAL_FIELDS)
BROKER_IDS = {
    broker: f"builtin-{_slug}"
    for broker, _slug in zip(
        BROKER_CHOICES,
        ("upstox", "zerodha", "angel-one", "dhan"),
        strict=True,
    )
}
BROKER_GUIDES = {
    "Upstox": "https://upstox.com/developer/api-documentation/authentication/",
    "Angel One": "https://smartapi.angelone.in/",
    "Zerodha": "https://kite.trade/docs/connect/v3/user/",
    "Dhan": "https://dhanhq.co/docs/v2/authentication/",
}
BROKER_CREDENTIAL_INFO = {
    "Upstox": {
        "api_key": (
            "Identifies your Upstox developer app during OAuth sign-in. "
            "Create an API app and copy its API key."
        ),
        "api_secret": (
            "Proves your app's identity during authorization-code exchange. "
            "Copy the secret from the same API app and keep it private."
        ),
    },
    "Zerodha": {
        "api_key": (
            "Identifies your Kite Connect app on the Zerodha login page. "
            "Create a Kite Connect app and copy its API key."
        ),
        "api_secret": (
            "Verifies your app when Zerodha exchanges the request token. "
            "Copy the secret from the same Kite Connect app and keep it private."
        ),
    },
    "Angel One": {
        "api_key": "Identifies the SmartAPI application used to request broker data.",
        "client_id": "Your Angel One trading account's client ID.",
        "password": "Your Angel One account password.",
        "totp_secret": (
            "The TOTP secret for your Angel One account. It is used to generate "
            "the time-based one-time password during sign-in."
        ),
    },
    "Dhan": {
        "client_id": "Your Dhan trading account's client ID.",
        "api_key": "Identifies the Dhan API application linked to your account.",
        "api_secret": (
            "The API secret paired with your Dhan API key; used to create "
            "the authorization session."
        ),
    },
}
AI_KEY_GUIDES = {
    LLMProvider.GEMINI.value: "https://aistudio.google.com/app/apikey",
    LLMProvider.OPENAI.value: "https://platform.openai.com/api-keys",
    LLMProvider.ANTHROPIC.value: "https://console.anthropic.com/settings/keys",
    LLMProvider.GROQ.value: "https://console.groq.com/keys",
    LLMProvider.TOGETHER_AI.value: "https://api.together.xyz/settings/api-keys",
    LLMProvider.MISTRAL.value: "https://console.mistral.ai/api-keys/",
    LLMProvider.DEEPSEEK.value: "https://platform.deepseek.com/api_keys",
    LLMProvider.OPENROUTER.value: "https://openrouter.ai/settings/keys",
    LLMProvider.XAI.value: "https://console.x.ai/",
    LLMProvider.CEREBRAS.value: "https://cloud.cerebras.ai/",
}
PHONE_COUNTRIES = (
    ("India", "+91", "🇮🇳"),
    ("United States", "+1", "🇺🇸"),
    ("Canada", "+1", "🇨🇦"),
    ("United Kingdom", "+44", "🇬🇧"),
    ("Australia", "+61", "🇦🇺"),
    ("New Zealand", "+64", "🇳🇿"),
    ("Singapore", "+65", "🇸🇬"),
    ("United Arab Emirates", "+971", "🇦🇪"),
    ("Saudi Arabia", "+966", "🇸🇦"),
    ("Qatar", "+974", "🇶🇦"),
    ("Kuwait", "+965", "🇰🇼"),
    ("Oman", "+968", "🇴🇲"),
    ("Bahrain", "+973", "🇧🇭"),
    ("South Africa", "+27", "🇿🇦"),
    ("Nigeria", "+234", "🇳🇬"),
    ("Kenya", "+254", "🇰🇪"),
    ("Pakistan", "+92", "🇵🇰"),
    ("Bangladesh", "+880", "🇧🇩"),
    ("Sri Lanka", "+94", "🇱🇰"),
    ("Nepal", "+977", "🇳🇵"),
    ("Philippines", "+63", "🇵🇭"),
    ("Malaysia", "+60", "🇲🇾"),
    ("Indonesia", "+62", "🇮🇩"),
    ("Thailand", "+66", "🇹🇭"),
    ("Vietnam", "+84", "🇻🇳"),
    ("Japan", "+81", "🇯🇵"),
    ("South Korea", "+82", "🇰🇷"),
    ("China", "+86", "🇨🇳"),
    ("Hong Kong", "+852", "🇭🇰"),
    ("Taiwan", "+886", "🇹🇼"),
    ("Germany", "+49", "🇩🇪"),
    ("France", "+33", "🇫🇷"),
    ("Italy", "+39", "🇮🇹"),
    ("Spain", "+34", "🇪🇸"),
    ("Netherlands", "+31", "🇳🇱"),
    ("Belgium", "+32", "🇧🇪"),
    ("Switzerland", "+41", "🇨🇭"),
    ("Sweden", "+46", "🇸🇪"),
    ("Norway", "+47", "🇳🇴"),
    ("Denmark", "+45", "🇩🇰"),
    ("Finland", "+358", "🇫🇮"),
    ("Ireland", "+353", "🇮🇪"),
    ("Poland", "+48", "🇵🇱"),
    ("Portugal", "+351", "🇵🇹"),
    ("Greece", "+30", "🇬🇷"),
    ("Turkey", "+90", "🇹🇷"),
    ("Ukraine", "+380", "🇺🇦"),
    ("Brazil", "+55", "🇧🇷"),
    ("Mexico", "+52", "🇲🇽"),
    ("Argentina", "+54", "🇦🇷"),
    ("Chile", "+56", "🇨🇱"),
    ("Colombia", "+57", "🇨🇴"),
    ("Peru", "+51", "🇵🇪"),
)
PHONE_COUNTRY_LABELS = tuple(
    f"{flag} {name} ({calling_code})"
    for name, calling_code, flag in PHONE_COUNTRIES
)
PHONE_COUNTRY_BY_LABEL = {
    label: country
    for label, country in zip(PHONE_COUNTRY_LABELS, PHONE_COUNTRIES, strict=True)
}
PHONE_COUNTRY_BY_CODE = {
    calling_code: next(
        country for country in PHONE_COUNTRIES if country[1] == calling_code
    )
    for calling_code in {country[1] for country in PHONE_COUNTRIES}
}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _provider_slug(provider: str) -> str:
    return provider.lower().replace(" ", "_").replace("-", "_")


def _broker_settings_widget_key(broker: str, field: str) -> str:
    return f"user_settings_{broker}_{field}"


def _split_phone_number(
    phone: str,
    saved_country_name: str = "",
) -> tuple[str, str]:
    normalized = re.sub(r"[\s()-]", "", phone.strip())
    if normalized.lower().startswith("whatsapp:"):
        normalized = normalized.split(":", 1)[1]
    if saved_country_name:
        for country in PHONE_COUNTRIES:
            if country[0] == saved_country_name and normalized.startswith(country[1]):
                label = f"{country[2]} {country[0]} ({country[1]})"
                return label, normalized[len(country[1]):]
    for calling_code in sorted(PHONE_COUNTRY_BY_CODE, key=len, reverse=True):
        if normalized.startswith(calling_code):
            country = PHONE_COUNTRY_BY_CODE[calling_code]
            label = f"{country[2]} {country[0]} ({calling_code})"
            return label, normalized[len(calling_code):]
    return PHONE_COUNTRY_LABELS[0], ""


def apply_settings_to_session(
    user_settings: dict[str, Any],
    *,
    initialize_form_state: bool = True,
) -> None:
    """Hydrate the existing broker and analysis flows from the user profile."""
    preferences = _mapping(user_settings.get("preferences"))
    sensitive = _mapping(user_settings.get("sensitive"))
    profile = _mapping(sensitive.get("profile"))
    legacy_broker_credentials = _mapping(sensitive.get("broker_credentials"))
    ai_api_keys = _mapping(sensitive.get("ai_api_keys"))
    notifications = _mapping(sensitive.get("notifications"))

    st.session_state["user_profile_name"] = str(profile.get("name", ""))
    email = str(profile.get("email") or notifications.get("email_recipient", ""))
    phone = str(profile.get("phone") or notifications.get("whatsapp_recipient", ""))
    phone_country, phone_national_number = _split_phone_number(
        phone,
        str(profile.get("phone_country", "")),
    )
    st.session_state["user_profile_email"] = email
    st.session_state["user_profile_phone"] = phone
    if initialize_form_state:
        st.session_state["settings_profile_name"] = str(profile.get("name", ""))
        st.session_state["settings_profile_email"] = email
        st.session_state["settings_profile_phone_country"] = phone_country
        st.session_state["settings_profile_phone_national"] = phone_national_number
    for obsolete_key in (
        "email_delivery_provider",
        "email_recipient",
        "email_sender",
        "email_smtp_host",
        "email_smtp_password",
        "email_smtp_port",
        "email_smtp_username",
        "resend_api_key_session",
        "resend_sender_session",
        "whatsapp_account_sid",
        "whatsapp_auth_token",
        "whatsapp_recipient",
        "whatsapp_sender",
        "user_settings_email_recipient",
        "user_settings_email_sender",
        "user_settings_email_smtp_password",
        "user_settings_email_smtp_username",
        "user_settings_resend_api_key",
        "user_settings_whatsapp_account_sid",
        "user_settings_whatsapp_auth_token",
        "user_settings_whatsapp_recipient",
        "user_settings_whatsapp_sender",
        "settings_email_delivery_provider",
        "settings_email_smtp_host",
        "settings_email_smtp_port",
        "settings_resend_sender",
    ):
        st.session_state.pop(obsolete_key, None)
    saved_profiles = preferences.get("broker_profiles", [])
    if not isinstance(saved_profiles, list):
        saved_profiles = []
    saved_profiles_by_type = {
        str(item.get("type")): item
        for item in saved_profiles
        if isinstance(item, dict) and item.get("type") in BROKER_CHOICES
    }
    st.session_state["registered_broker_profiles"] = [
        {
            "id": BROKER_IDS[broker],
            "name": broker,
            "type": broker,
            "active": True,
            "description": "",
            "configuration": {},
        }
        for broker in BROKER_CHOICES
    ]
    saved_profile_credentials = _mapping(sensitive.get("broker_profiles"))
    saved_profiles_by_id = {
        str(item.get("id")): item
        for item in saved_profiles
        if isinstance(item, dict) and item.get("id")
    }
    broker_credentials: dict[str, dict[str, str]] = {}
    for profile in st.session_state["registered_broker_profiles"]:
        saved_profile = saved_profiles_by_type.get(profile["type"], {})
        credentials = _mapping(
            saved_profile_credentials.get(profile["id"])
            or saved_profile_credentials.get(str(saved_profile.get("id", "")))
            or legacy_broker_credentials.get(profile["type"])
        )
        broker_credentials[profile["id"]] = {
            field: str(credentials.get(field, ""))
            for field in BROKER_CREDENTIAL_FIELDS[profile["type"]]
        }
        if initialize_form_state:
            for field in BROKER_CREDENTIAL_FIELDS[profile["type"]]:
                st.session_state[
                    _broker_settings_widget_key(profile["type"], field)
                ] = broker_credentials[profile["id"]][field]
    st.session_state["settings_broker_credentials"] = broker_credentials

    provider_settings: dict[str, dict[str, str]] = {}
    saved_models = _mapping(preferences.get("ai_models"))
    for provider, widget_key in PROVIDER_WIDGET_KEYS.items():
        api_key = str(ai_api_keys.get(provider, ""))
        model_key = f"llm_model_selection_{_provider_slug(provider)}"
        models = settings.llm_provider_models.get(provider, ())
        model = str(saved_models.get(provider, models[0] if models else ""))
        st.session_state[widget_key] = api_key
        st.session_state[model_key] = model
        if initialize_form_state:
            st.session_state[f"user_settings_ai_key_{_provider_slug(provider)}"] = api_key
            st.session_state[f"user_settings_model_{_provider_slug(provider)}"] = model
        provider_settings[provider] = {
            "api_key": api_key,
            "model_selection": model,
            "base_url": str(preferences.get("ai_custom_base_url", "")),
        }
    st.session_state["llm_provider_settings"] = provider_settings
    selected_provider = str(preferences.get("ai_provider", LLMProvider.GEMINI.value))
    if selected_provider not in PROVIDER_OPTIONS:
        selected_provider = PROVIDER_OPTIONS[0]
    st.session_state["llm_provider"] = selected_provider
    custom_provider_name = str(preferences.get("ai_custom_provider_name", "")).strip()
    st.session_state["settings_ai_custom_provider_name"] = custom_provider_name
    saved_ai_configs = preferences.get("ai_provider_configs", [])
    if not isinstance(saved_ai_configs, list):
        saved_ai_configs = []
    configured_ai_keys = _mapping(sensitive.get("ai_provider_config_keys"))
    if not saved_ai_configs:
        legacy_keys = _mapping(sensitive.get("ai_api_keys"))
        saved_models = _mapping(preferences.get("ai_models"))
        for provider in PROVIDER_OPTIONS:
            api_key = str(legacy_keys.get(provider, ""))
            model = str(saved_models.get(provider, ""))
            if not api_key and (
                provider != selected_provider or not model
            ):
                continue
            saved_ai_configs.append(
                {
                    "id": f"legacy-{_provider_slug(provider)}",
                    "provider": provider,
                    "name": (
                        custom_provider_name
                        if provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
                        and custom_provider_name
                        else provider
                    ),
                    "model": model,
                    "base_url": (
                        str(preferences.get("ai_custom_base_url", ""))
                        if provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
                        else ""
                    ),
                }
            )
            configured_ai_keys[f"legacy-{_provider_slug(provider)}"] = api_key
    valid_ai_configs = [
        {
            "id": str(item.get("id", "")),
            "provider": str(item.get("provider", "")),
            "name": str(item.get("name", "")),
            "model": str(item.get("model", "")),
            "base_url": str(item.get("base_url", "")),
        }
        for item in saved_ai_configs
        if isinstance(item, dict)
        and item.get("id")
        and item.get("provider") in PROVIDER_OPTIONS
    ]
    st.session_state["settings_ai_provider_configs"] = valid_ai_configs
    st.session_state["settings_ai_provider_config_keys"] = {
        str(config["id"]): str(configured_ai_keys.get(config["id"], ""))
        for config in valid_ai_configs
    }
    active_config_id = str(preferences.get("ai_active_config_id", ""))
    active_config = next(
        (item for item in valid_ai_configs if item["id"] == active_config_id),
        None,
    )
    if active_config is None:
        active_config = next(
            (
                item
                for item in valid_ai_configs
                if item["provider"] == selected_provider
                and (
                    item["provider"] != LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
                    or item["name"] == custom_provider_name
                )
            ),
            None,
        )
    if active_config:
        st.session_state["settings_ai_active_config_id"] = active_config["id"]
        provider_slug = _provider_slug(active_config["provider"])
        runtime_api_key = str(
            st.session_state["settings_ai_provider_config_keys"].get(
                active_config["id"], ""
            )
        )
        st.session_state[PROVIDER_WIDGET_KEYS[active_config["provider"]]] = (
            runtime_api_key
        )
        st.session_state[f"llm_model_selection_{provider_slug}"] = active_config[
            "model"
        ]
        st.session_state["llm_provider"] = active_config["provider"]
        st.session_state["llm_custom_base_url"] = active_config["base_url"]
    else:
        st.session_state.pop("settings_ai_active_config_id", None)
    if initialize_form_state:
        st.session_state["settings_ai_provider"] = (
            custom_provider_name
            if selected_provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
            and custom_provider_name
            else selected_provider
        )
    st.session_state["llm_custom_base_url"] = str(
        preferences.get("ai_custom_base_url", settings.llm_custom_base_url)
    )

    profiles = st.session_state["registered_broker_profiles"]
    selected_broker = str(st.session_state.get("selected_broker", ""))
    if selected_broker not in {
        profile["name"] for profile in st.session_state["registered_broker_profiles"]
    }:
        selected_broker = ""
    active_profile_names = {
        profile["name"] for profile in profiles if profile["active"]
    }
    st.session_state["selected_broker"] = (
        selected_broker if selected_broker in active_profile_names
        else next(
            (profile["name"] for profile in profiles if profile["active"]),
            "",
        )
    )
    st.session_state["max_allocation_pct"] = float(
        preferences.get("max_allocation_pct", 30.0)
    )
    st.session_state["realized_daily_loss_pct"] = float(
        preferences.get("realized_daily_loss_pct", 0.0)
    )
    ai_provider = str(preferences.get("ai_provider", PROVIDER_OPTIONS[0]))
    if initialize_form_state:
        st.session_state["settings_ai_provider"] = (
            ai_provider if ai_provider in PROVIDER_OPTIONS else PROVIDER_OPTIONS[0]
        )
    if initialize_form_state:
        st.session_state["settings_ai_custom_base_url"] = str(
            preferences.get("ai_custom_base_url", settings.llm_custom_base_url)
        )
        st.session_state["settings_max_allocation_pct"] = float(
            preferences.get("max_allocation_pct", 30.0)
        )
        st.session_state["settings_realized_daily_loss_pct"] = float(
            preferences.get("realized_daily_loss_pct", 0.0)
        )
        st.session_state["settings_sensitive_consent"] = bool(
            user_settings.get("sensitive_consent", False)
        )


def _collect_sensitive_settings() -> dict[str, Any]:
    return {
        "profile": {
            "name": str(st.session_state.get("settings_profile_name", "")).strip(),
            "email": str(st.session_state.get("settings_profile_email", "")).strip(),
            "phone": _format_profile_phone(),
            "phone_country": PHONE_COUNTRY_BY_LABEL[
                str(
                    st.session_state.get(
                        "settings_profile_phone_country", PHONE_COUNTRY_LABELS[0]
                    )
                )
            ][0],
        },
        "broker_profiles": {
            str(profile_id): {
                str(field): str(value).strip()
                for field, value in _mapping(credentials).items()
            }
            for profile_id, credentials in _mapping(
                st.session_state.get("settings_broker_credentials", {})
            ).items()
        },
        "ai_api_keys": {
            provider: str(
                st.session_state.get(
                    f"user_settings_ai_key_{_provider_slug(provider)}", ""
                )
            ).strip()
            for provider in PROVIDER_OPTIONS
        },
        "ai_provider_config_keys": {
            str(config_id): str(value).strip()
            for config_id, value in _mapping(
                st.session_state.get("settings_ai_provider_config_keys")
            ).items()
        },
    }


def _validate_email(email: str) -> str | None:
    value = email.strip()
    if not value:
        return None
    if len(value) > 254 or not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",
        value,
    ):
        return "Enter a valid email address or leave it blank."
    local_part = value.split("@", 1)[0]
    if (
        len(local_part) > 64
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
    ):
        return "Enter a valid email address or leave it blank."
    return None


def _validate_phone(phone: str) -> str | None:
    value = phone.strip()
    if not value:
        return None
    formatted = re.fullmatch(
        r"\+([1-9]\d{0,2})[ \t-]((?:\d[ \t()-]?){9}\d)",
        value,
    )
    international = re.fullmatch(r"\+[1-9]\d{10,12}", value)
    if not formatted and not international:
        return (
            "Enter a country code and exactly 10 phone-number digits "
            "(for example, +91 98765 43210 or +919876543210), or leave it blank."
        )
    return None


def _format_profile_phone() -> str:
    country_label = str(
        st.session_state.get(
            "settings_profile_phone_country", PHONE_COUNTRY_LABELS[0]
        )
    )
    country = PHONE_COUNTRY_BY_LABEL.get(country_label)
    if country is None:
        raise ValueError("Choose a country from the phone country-code list.")
    national_number = str(
        st.session_state.get("settings_profile_phone_national", "")
    ).strip()
    if not national_number:
        return ""
    return f"{country[1]}{national_number}"


def _validate_phone_input(country_label: str, national_number: str) -> str | None:
    if not national_number.strip():
        return None
    country = PHONE_COUNTRY_BY_LABEL.get(country_label)
    if country is None:
        return "Choose a country from the phone country-code list."
    if not re.fullmatch(r"\d{10}", national_number.strip()):
        return "Enter exactly 10 digits after the selected country code."
    return _validate_phone(f"{country[1]}{national_number.strip()}")


def _validate_contact(profile: dict[str, Any]) -> str | None:
    return _validate_email(str(profile.get("email", ""))) or _validate_phone(
        str(profile.get("phone", ""))
    )


def _selected_ai_configuration() -> dict[str, str]:
    if "settings_ai_editor_provider" in st.session_state:
        provider_selection = str(
            st.session_state.get("settings_ai_editor_provider", "")
        ).strip()
        if not provider_selection:
            raise ValueError("Select or enter an AI provider.")
        if len(provider_selection) > 80:
            raise ValueError("Provider names must be 80 characters or fewer.")
        known_provider = provider_selection in PROVIDER_OPTIONS
        provider = (
            provider_selection
            if known_provider
            else LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
        )
        custom_provider_name = (
            str(
                st.session_state.get("settings_ai_editor_provider_name", "")
            ).strip()
            if provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
            and known_provider
            else provider_selection
            if not known_provider
            else ""
        )
        if (
            provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
            and not custom_provider_name
        ):
            raise ValueError("Enter a name for the custom AI provider.")
        provider_label = custom_provider_name or provider_selection
        api_key = str(
            st.session_state.get("settings_ai_editor_api_key", "")
        ).strip()
        model = str(st.session_state.get("settings_ai_editor_model", "")).strip()
        base_url = str(
            st.session_state.get("settings_ai_editor_base_url", "")
        ).strip()
        config_id = str(st.session_state.get("settings_ai_editor_id", ""))
        if len(model) > 200:
            raise ValueError("Model IDs must be 200 characters or fewer.")
        return {
            "provider": provider,
            "provider_selection": provider_label,
            "custom_provider_name": custom_provider_name,
            "api_key": api_key,
            "model": model,
            "base_url": base_url,
            "config_id": config_id,
        }

    provider_selection = str(
        st.session_state.get("settings_ai_provider", PROVIDER_OPTIONS[0])
    ).strip()
    if not provider_selection:
        raise ValueError("Select or enter an AI provider.")
    if len(provider_selection) > 80:
        raise ValueError("Provider names must be 80 characters or fewer.")
    known_provider = provider_selection in PROVIDER_OPTIONS
    provider = (
        provider_selection
        if known_provider
        else LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
    )
    if not known_provider:
        custom_provider_name = provider_selection
    elif provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value:
        custom_provider_name = str(
            st.session_state.get("settings_ai_custom_provider_name", "")
        ).strip()
    else:
        custom_provider_name = ""
    slug = _provider_slug(provider)
    api_key = str(
        st.session_state.get(f"user_settings_ai_key_{slug}", "")
    ).strip()
    model = str(
        st.session_state.get(f"user_settings_model_{slug}", "")
    ).strip()
    if len(model) > 200:
        raise ValueError("Model IDs must be 200 characters or fewer.")
    base_url = str(
        st.session_state.get("settings_ai_custom_base_url", "")
    ).strip()
    return {
        "provider": provider,
        "provider_selection": provider_selection,
        "custom_provider_name": custom_provider_name,
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
    }


def _ai_test_fingerprint(configuration: dict[str, str]) -> str:
    encoded = json.dumps(
        configuration,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _set_ai_editor(configuration: dict[str, Any] | None = None) -> None:
    configuration = configuration or {}
    st.session_state["settings_ai_editor_id"] = str(configuration.get("id", ""))
    st.session_state["settings_ai_editor_provider"] = str(
        configuration.get("provider", PROVIDER_OPTIONS[0])
    )
    st.session_state["settings_ai_editor_provider_name"] = str(
        configuration.get("name", "")
    )
    st.session_state["settings_ai_editor_api_key"] = str(
        _mapping(
            st.session_state.get("settings_ai_provider_config_keys")
        ).get(str(configuration.get("id", "")), "")
    )
    model = str(configuration.get("model", ""))
    st.session_state["settings_ai_editor_model"] = model
    provider = str(configuration.get("provider", PROVIDER_OPTIONS[0]))
    models = settings.llm_provider_models.get(provider, ())
    st.session_state["settings_ai_editor_suggested_model"] = (
        model if model in models else ""
    )
    st.session_state["settings_ai_editor_base_url"] = str(
        configuration.get("base_url", "")
    )
    st.session_state["settings_ai_editor_open"] = True
    st.session_state.pop("settings_ai_editor_feedback", None)


def _reset_ai_editor_model() -> None:
    st.session_state["settings_ai_editor_model"] = ""
    st.session_state["settings_ai_editor_suggested_model"] = ""
    if (
        st.session_state.get("settings_ai_editor_provider")
        == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
    ):
        config_id = str(st.session_state.get("settings_ai_editor_id", ""))
        existing = next(
            (
                item
                for item in st.session_state.get(
                    "settings_ai_provider_configs", []
                )
                if isinstance(item, dict) and item.get("id") == config_id
            ),
            None,
        )
        if (
            not existing
            or existing.get("provider")
            != LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
        ):
            st.session_state["settings_ai_editor_provider_name"] = ""
    tested = dict(
        _mapping(st.session_state.get("tested_ai_provider_fingerprints"))
    )
    tested.pop(
        str(st.session_state.get("settings_ai_editor_id", "")) or "new",
        None,
    )
    st.session_state["tested_ai_provider_fingerprints"] = tested


def _use_suggested_ai_model() -> None:
    selected_model = str(
        st.session_state.get("settings_ai_editor_suggested_model", "")
    )
    if selected_model:
        st.session_state["settings_ai_editor_model"] = selected_model


def _ai_config_matches(
    existing: dict[str, Any],
    configuration: dict[str, str],
) -> bool:
    if existing.get("provider") != configuration["provider"]:
        return False
    if configuration["provider"] == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value:
        return str(existing.get("name", "")).casefold() == str(
            configuration["custom_provider_name"]
        ).casefold()
    return True


def _activate_ai_configuration(configuration: dict[str, Any]) -> None:
    st.session_state["settings_ai_active_config_id"] = str(configuration["id"])
    provider = str(configuration["provider"])
    st.session_state["llm_provider"] = provider
    st.session_state[PROVIDER_WIDGET_KEYS[provider]] = str(
        _mapping(st.session_state.get("settings_ai_provider_config_keys")).get(
            str(configuration["id"]), ""
        )
    )
    slug = _provider_slug(provider)
    st.session_state[f"llm_model_selection_{slug}"] = str(configuration["model"])
    st.session_state["llm_custom_base_url"] = str(configuration.get("base_url", ""))


def _delete_ai_provider_configuration(
    owner_identifier: str,
    config_id: str,
) -> None:
    configurations = [
        dict(item)
        for item in st.session_state.get("settings_ai_provider_configs", [])
        if isinstance(item, dict) and str(item.get("id", "")) != config_id
    ]
    keys = dict(
        _mapping(st.session_state.get("settings_ai_provider_config_keys"))
    )
    keys.pop(config_id, None)
    existing = load_user_settings(owner_identifier)
    preferences = dict(_mapping(existing.get("preferences")))
    preferences["ai_provider_configs"] = configurations
    active_config_id = str(
        st.session_state.get("settings_ai_active_config_id", "")
    )
    active = next(
        (item for item in configurations if item.get("id") == active_config_id),
        configurations[0] if configurations else None,
    )
    active_config_id = str(active["id"]) if active else ""
    preferences["ai_active_config_id"] = active_config_id
    preferences["ai_provider"] = (
        str(active["provider"]) if active else LLMProvider.GEMINI.value
    )
    preferences["ai_custom_provider_name"] = (
        str(active.get("name", ""))
        if active
        and active["provider"] == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
        else ""
    )
    preferences["ai_custom_base_url"] = (
        str(active.get("base_url", ""))
        if active
        and active["provider"] == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
        else ""
    )
    consent = bool(st.session_state.get("settings_sensitive_consent", False))
    sensitive = _collect_sensitive_settings()
    sensitive["ai_provider_config_keys"] = keys
    api_keys = dict(_mapping(sensitive.get("ai_api_keys")))
    removed = next(
        (
            item
            for item in st.session_state.get("settings_ai_provider_configs", [])
            if isinstance(item, dict) and str(item.get("id", "")) == config_id
        ),
        None,
    )
    if removed and not any(
        item.get("provider") == removed.get("provider")
        for item in configurations
    ):
        api_keys.pop(str(removed.get("provider", "")), None)
    sensitive["ai_api_keys"] = api_keys
    save_user_settings(
        owner_identifier,
        preferences,
        sensitive,
        save_sensitive=consent,
    )
    st.session_state["settings_ai_provider_configs"] = configurations
    st.session_state["settings_ai_provider_config_keys"] = keys
    st.session_state["settings_ai_active_config_id"] = active_config_id
    st.session_state["user_settings_form_cache"] = {
        "owner": owner_identifier,
        "sensitive": sensitive,
        "sensitive_consent": consent,
    }
    apply_settings_to_session(
        {
            "preferences": preferences,
            "sensitive": sensitive,
            "sensitive_consent": consent,
        },
        initialize_form_state=False,
    )


def _save_ai_provider_configuration(
    owner_identifier: str,
    configuration: dict[str, str],
) -> None:
    config_id = configuration["config_id"] or uuid4().hex
    provider = configuration["provider"]
    display_name = (
        configuration["custom_provider_name"]
        or configuration["provider_selection"]
    )
    record = {
        "id": config_id,
        "provider": provider,
        "name": display_name,
        "model": configuration["model"],
        "base_url": configuration["base_url"],
    }
    configurations = [
        dict(item)
        for item in st.session_state.get("settings_ai_provider_configs", [])
        if isinstance(item, dict)
    ]
    if any(
        str(item.get("id", "")) != config_id
        and _ai_config_matches(item, configuration)
        for item in configurations
    ):
        raise ValueError(
            "This provider already has a configuration. Edit the existing "
            "configuration instead of adding another."
        )
    replaced = False
    for index, item in enumerate(configurations):
        if item.get("id") == config_id:
            configurations[index] = record
            replaced = True
            break
    if not replaced:
        configurations.append(record)

    config_keys = dict(
        _mapping(st.session_state.get("settings_ai_provider_config_keys"))
    )
    config_keys[config_id] = configuration["api_key"]
    existing = load_user_settings(owner_identifier)
    preferences = dict(_mapping(existing.get("preferences")))
    preferences["ai_provider_configs"] = configurations
    active_config_id = str(
        st.session_state.get("settings_ai_active_config_id", "")
    )
    if not active_config_id:
        active_config_id = config_id
    st.session_state["settings_ai_active_config_id"] = active_config_id
    preferences["ai_active_config_id"] = active_config_id
    active = next(
        (item for item in configurations if item["id"] == active_config_id),
        record,
    )
    preferences["ai_provider"] = active["provider"]
    preferences["ai_custom_provider_name"] = (
        active["name"]
        if active["provider"] == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
        else ""
    )
    preferences["ai_custom_base_url"] = (
        active["base_url"]
        if active["provider"] == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
        else ""
    )
    ai_models = dict(_mapping(preferences.get("ai_models")))
    ai_models[provider] = configuration["model"]
    preferences["ai_models"] = ai_models

    consent = bool(st.session_state.get("settings_sensitive_consent", False))
    sensitive = _collect_sensitive_settings()
    sensitive["ai_provider_config_keys"] = config_keys
    api_keys = dict(_mapping(sensitive.get("ai_api_keys")))
    api_keys[provider] = configuration["api_key"]
    sensitive["ai_api_keys"] = api_keys
    if not consent:
        cache = _mapping(st.session_state.get("user_settings_form_cache"))
        if cache.get("owner") == owner_identifier:
            cached_sensitive = dict(_mapping(cache.get("sensitive")))
            cached_keys = dict(
                _mapping(cached_sensitive.get("ai_provider_config_keys"))
            )
            cached_keys[config_id] = configuration["api_key"]
            cached_sensitive["ai_provider_config_keys"] = cached_keys
            cache["sensitive"] = cached_sensitive
            st.session_state["user_settings_form_cache"] = cache

    save_user_settings(
        owner_identifier,
        preferences,
        sensitive,
        save_sensitive=consent,
    )
    st.session_state["settings_ai_provider_configs"] = configurations
    st.session_state["settings_ai_provider_config_keys"] = config_keys
    st.session_state["settings_ai_editor_id"] = config_id
    st.session_state["settings_ai_editor_open"] = False
    st.session_state["user_settings_form_cache"] = {
        "owner": owner_identifier,
        "sensitive": sensitive,
        "sensitive_consent": consent,
    }
    apply_settings_to_session(
        {
            "preferences": preferences,
            "sensitive": sensitive,
            "sensitive_consent": consent,
        },
        initialize_form_state=False,
    )
    st.session_state["settings_ai_editor_feedback"] = (
        f"{display_name} configuration saved. "
        + (
            "Its API key is encrypted for future sessions."
            if consent
            else "Its API key is available only for this session."
        )
    )


def _render_ai_provider_configurations(owner_identifier: str) -> None:
    configurations = [
        item
        for item in st.session_state.get("settings_ai_provider_configs", [])
        if isinstance(item, dict)
    ]
    if st.button(
        "+ AI provider config",
        key="add_ai_provider_config",
        type="secondary",
        icon=":material/add:",
        width="stretch",
    ):
        _set_ai_editor()

    if st.session_state.get("settings_ai_editor_open"):
        _render_ai_provider_editor(owner_identifier)

    feedback = str(st.session_state.pop("settings_ai_editor_feedback", ""))
    if feedback:
        st.success(feedback)
    if configurations:
        st.subheader("Configured providers")
        header = st.columns((2, 2, 1, 1, 1))
        for column, label in zip(
            header,
            ("Provider", "Model", "Active", "Edit", "Delete"),
            strict=True,
        ):
            with column:
                st.caption(label)
        for configuration in configurations:
            config_id = str(configuration["id"])
            safe_id = hashlib.sha256(config_id.encode("utf-8")).hexdigest()[:12]
            row = st.columns((2, 2, 1, 1, 1))
            with row[0]:
                st.text(str(configuration.get("name") or configuration["provider"]))
                if _mapping(
                    st.session_state.get("settings_ai_provider_config_keys")
                ).get(config_id):
                    st.caption(
                        "Encrypted"
                        if st.session_state.get("settings_sensitive_consent", False)
                        else "Session-only key"
                    )
            with row[1]:
                st.text(str(configuration.get("model", "")))
            with row[2]:
                if config_id == st.session_state.get("settings_ai_active_config_id"):
                    st.caption("Selected")
                elif st.button(
                    "Use",
                    key=f"use_ai_config_{safe_id}",
                    type="tertiary",
                ):
                    _activate_ai_configuration(configuration)
                    st.session_state["settings_ai_editor_feedback"] = (
                        "Provider selected for analysis."
                    )
                    st.rerun()
            with row[3]:
                if st.button("Edit", key=f"edit_ai_config_{safe_id}"):
                    _set_ai_editor(configuration)
                    st.rerun()
            with row[4]:
                if st.session_state.get("settings_ai_delete_pending") == config_id:
                    confirm_delete = st.button(
                        "Confirm",
                        key=f"confirm_delete_ai_config_{safe_id}",
                        type="primary",
                    )
                    cancel_delete = st.button(
                        "Cancel",
                        key=f"cancel_delete_ai_config_{safe_id}",
                    )
                    if cancel_delete:
                        st.session_state.pop("settings_ai_delete_pending", None)
                        st.rerun()
                elif st.button("Delete", key=f"delete_ai_config_{safe_id}"):
                    st.session_state["settings_ai_delete_pending"] = config_id
                    st.rerun()
                else:
                    confirm_delete = False
                if (
                    st.session_state.get("settings_ai_delete_pending") == config_id
                    and confirm_delete
                ):
                    try:
                        _delete_ai_provider_configuration(
                            owner_identifier,
                            config_id,
                        )
                    except (
                        OSError,
                        RuntimeError,
                        DatabaseError,
                        DatabaseSecurityError,
                        TypeError,
                        ValueError,
                    ) as error:
                        st.error(f"Provider configuration could not be deleted: {error}")
                    else:
                        st.session_state.pop("settings_ai_delete_pending", None)
                        st.session_state["settings_ai_editor_feedback"] = (
                            "Provider configuration deleted."
                        )
                        st.rerun()
            st.divider()


def _render_ai_provider_editor(owner_identifier: str) -> None:
    with st.container(border=True):
        st.subheader(
            "Edit provider"
            if st.session_state.get("settings_ai_editor_id")
            else "New provider"
        )
        provider_selection = st.selectbox(
            "AI provider",
            PROVIDER_OPTIONS,
            accept_new_options=True,
            placeholder="Search or select a provider",
            key="settings_ai_editor_provider",
            on_change=_reset_ai_editor_model,
            help=(
                "Choose a supported provider, or enter a provider name that "
                "offers an OpenAI-compatible API."
            ),
        )
        provider_selection = str(provider_selection).strip()
        is_custom = provider_selection not in PROVIDER_OPTIONS or (
            provider_selection == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
        )
        provider = (
            provider_selection
            if provider_selection in PROVIDER_OPTIONS
            else LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
        )
        if provider_selection == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value:
            st.text_input(
                "Provider name",
                key="settings_ai_editor_provider_name",
                placeholder="For example, OpenRouter",
            )
        guide = AI_KEY_GUIDES.get(provider)
        if guide:
            st.link_button(
                f"{provider} API key setup",
                guide,
                width="content",
            )
        st.text_input(
            "API key",
            key="settings_ai_editor_api_key",
            type="password",
            help="The API key is confidential and is saved only with your consent.",
        )
        models = settings.llm_provider_models.get(provider, ())
        if models:
            st.selectbox(
                "Suggested model",
                models,
                key="settings_ai_editor_suggested_model",
                on_change=_use_suggested_ai_model,
                placeholder="Choose a suggested model (optional)",
                help="Choose a suggested model or enter a custom model ID below.",
            )
        st.text_input(
            "Model",
            key="settings_ai_editor_model",
            placeholder="Enter a custom model ID",
        )
        if is_custom:
            st.text_input(
                "API base URL",
                key="settings_ai_editor_base_url",
                placeholder="https://api.example.com/v1",
            )
        st.caption(
            "Test sends a short request with no portfolio data. Provider charges "
            "may apply. Saving an API key for future sessions requires your "
            "explicit encrypted-storage consent."
        )
        editor_message = str(
            st.session_state.pop("settings_ai_editor_error", "")
        )
        if editor_message:
            st.error(editor_message)
        try:
            configuration = _selected_ai_configuration()
        except ValueError as error:
            configuration = None
            st.error(str(error))

        button_column, save_column, cancel_column = st.columns((1.4, 1.2, 0.7))
        with button_column:
            test_clicked = st.button(
                "Test provider setup",
                key="test_ai_provider_setup",
                icon=":material/wifi_tethering:",
                disabled=not bool(
                    configuration
                    and configuration["api_key"]
                    and configuration["model"]
                ),
            )
        tested = _mapping(st.session_state.get("tested_ai_provider_fingerprints"))
        fingerprint_key = (
            (configuration["config_id"] or "new")
            if configuration
            else ""
        )
        already_tested = bool(
            configuration
            and tested.get(fingerprint_key) == _ai_test_fingerprint(configuration)
        )
        duplicate_configuration = bool(
            configuration
            and any(
                str(item.get("id", "")) != configuration["config_id"]
                and _ai_config_matches(item, configuration)
                for item in st.session_state.get(
                    "settings_ai_provider_configs", []
                )
                if isinstance(item, dict)
            )
        )
        if duplicate_configuration:
            st.info(
                "This provider already has a configuration. Edit that entry "
                "instead of adding another."
            )
        with save_column:
            save_clicked = st.button(
                "Save",
                key="save_ai_provider_config",
                type="primary",
                icon=":material/save:",
                disabled=not (
                    already_tested and not duplicate_configuration
                ),
                width="stretch",
            )
        with cancel_column:
            cancel_clicked = st.button("Cancel", key="cancel_ai_provider_editor")

        if test_clicked and configuration:
            try:
                test_ai_provider_configuration(
                    provider=configuration["provider"],
                    api_key=configuration["api_key"],
                    model=configuration["model"],
                    base_url=configuration["base_url"],
                )
            except (
                OSError,
                requests.RequestException,
                RuntimeError,
                ValueError,
            ) as error:
                tested = dict(tested)
                tested.pop(fingerprint_key, None)
                st.session_state["tested_ai_provider_fingerprints"] = tested
                st.session_state["settings_ai_editor_error"] = (
                    f"Provider setup test failed: {error}"
                )
            else:
                tested = dict(tested)
                tested[fingerprint_key] = _ai_test_fingerprint(configuration)
                st.session_state["tested_ai_provider_fingerprints"] = tested
                st.session_state["settings_ai_editor_feedback"] = (
                    f"{configuration['provider_selection']} setup test succeeded."
                )
            st.rerun()

        if save_clicked and configuration:
            try:
                _save_ai_provider_configuration(owner_identifier, configuration)
            except (
                OSError,
                RuntimeError,
                DatabaseError,
                DatabaseSecurityError,
                TypeError,
                ValueError,
            ) as error:
                st.session_state["settings_ai_editor_error"] = (
                    f"Provider configuration could not be saved: {error}"
                )
                st.rerun()
            st.rerun()

        if cancel_clicked:
            st.session_state["settings_ai_editor_open"] = False
            st.rerun()


def _sync_broker_settings_from_form() -> None:
    profiles = []
    credentials: dict[str, dict[str, str]] = {}
    for broker in BROKER_CHOICES:
        broker_id = BROKER_IDS[broker]
        profiles.append(
            {
                "id": broker_id,
                "name": broker,
                "type": broker,
                "active": True,
                "description": "",
                "configuration": {},
            }
        )
        credentials[broker_id] = {
            field: str(
                st.session_state.get(
                    _broker_settings_widget_key(broker, field), ""
                )
            ).strip()
            for field in BROKER_CREDENTIAL_FIELDS[broker]
        }
    st.session_state["registered_broker_profiles"] = profiles
    st.session_state["settings_broker_credentials"] = credentials


def open_user_settings_for_broker_credentials(
    owner_identifier: str,
    broker_type: str,
    credentials: dict[str, str],
) -> None:
    """Open settings with current-session broker values ready for review."""
    if broker_type not in BROKER_CHOICES:
        st.session_state["user_settings_feedback"] = {
            "kind": "error",
            "message": "Choose one of the four supported brokers.",
        }
        return
    _open_settings_dialog(
        owner_identifier,
        pending_broker_credentials=(broker_type, credentials),
    )


def _save(owner_identifier: str) -> None:
    st.session_state.pop("user_settings_feedback", None)
    _sync_broker_settings_from_form()
    sensitive = _collect_sensitive_settings()
    contact_error = _validate_contact(_mapping(sensitive.get("profile")))
    if contact_error:
        _show_save_error(contact_error)
        return

    ai_configurations = [
        dict(item)
        for item in st.session_state.get("settings_ai_provider_configs", [])
        if isinstance(item, dict)
    ]
    active_config_id = str(
        st.session_state.get("settings_ai_active_config_id", "")
    )
    active_configuration = next(
        (
            item
            for item in ai_configurations
            if item.get("id") == active_config_id
        ),
        None,
    )
    if active_configuration is None and ai_configurations:
        active_configuration = ai_configurations[0]
        active_config_id = str(active_configuration["id"])
    ai_provider = (
        str(active_configuration["provider"])
        if active_configuration
        else str(st.session_state.get("llm_provider", PROVIDER_OPTIONS[0]))
    )
    ai_custom_provider_name = (
        str(active_configuration.get("name", ""))
        if active_configuration
        and ai_provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value
        else ""
    )
    ai_models = {
        provider: str(
            st.session_state.get(
                f"user_settings_model_{_provider_slug(provider)}", ""
            )
        ).strip()
        for provider in PROVIDER_OPTIONS
    }
    for configuration in ai_configurations:
        ai_models[str(configuration["provider"])] = str(
            configuration.get("model", "")
        )
    active_model = (
        str(active_configuration.get("model", ""))
        if active_configuration
        else str(
            st.session_state.get(
                f"llm_model_selection_{_provider_slug(ai_provider)}", ""
            )
        )
    )
    ai_models[ai_provider] = active_model
    ai_base_url = (
        str(active_configuration.get("base_url", ""))
        if active_configuration
        else str(st.session_state.get("llm_custom_base_url", ""))
    )
    preferences = {
        "broker_profiles": st.session_state.get("registered_broker_profiles", []),
        "ai_provider_configs": ai_configurations,
        "ai_active_config_id": active_config_id,
        "ai_provider": ai_provider,
        "ai_custom_provider_name": ai_custom_provider_name,
        "ai_models": ai_models,
        "ai_custom_base_url": ai_base_url,
        "max_allocation_pct": float(
            st.session_state.get("settings_max_allocation_pct", 30.0)
        ),
        "realized_daily_loss_pct": float(
            st.session_state.get("settings_realized_daily_loss_pct", 0.0)
        ),
    }
    allocation = preferences["max_allocation_pct"]
    daily_loss = preferences["realized_daily_loss_pct"]
    if not isfinite(allocation) or not 0 <= allocation <= 100:
        _show_save_error("Maximum scenario allocation must be between 0 and 100%.")
        return
    if not isfinite(daily_loss) or not -100 <= daily_loss <= 100:
        _show_save_error("Realized daily loss must be between -100 and 100%.")
        return
    save_sensitive = bool(
        st.session_state.get("settings_sensitive_consent", False)
    )
    try:
        save_user_settings(
            owner_identifier,
            preferences,
            sensitive,
            save_sensitive=save_sensitive,
        )
    except (
        OSError,
        RuntimeError,
        DatabaseError,
        DatabaseSecurityError,
        TypeError,
        ValueError,
    ) as error:
        _show_save_error(f"Settings could not be saved: {error}")
        return

    st.session_state["user_settings_form_cache"] = {
        "owner": owner_identifier,
        "sensitive": sensitive,
        "sensitive_consent": save_sensitive,
    }
    apply_settings_to_session(
        {
            "preferences": preferences,
            "sensitive": sensitive,
            "sensitive_consent": save_sensitive,
        },
        initialize_form_state=False,
    )
    st.session_state["user_settings_loaded_for"] = owner_identifier
    st.session_state["user_settings_error"] = None
    st.session_state["user_settings_dialog_open"] = False
    st.session_state.pop("dashboard_broker_credentials_dirty", None)
    st.session_state.pop("dashboard_broker_credentials_loaded_for", None)
    st.session_state["user_settings_feedback"] = {
        "kind": "success",
        "message": (
            "Settings saved successfully. Your profile, broker preferences, "
            "AI provider, and risk settings are up to date. "
            + (
                "Confidential fields are encrypted for future sessions."
                if save_sensitive
                else "Confidential fields remain session-only and will need to "
                "be entered again in a future session."
            )
        ),
    }
    st.rerun()


def _show_save_error(message: str) -> None:
    st.session_state["user_settings_feedback"] = {
        "kind": "error",
        "message": message,
    }
    st.error(message, icon=":material/error:")


def _render_broker_registry() -> None:
    st.caption(
        "Only the four built-in broker integrations are available. Configure "
        "each account below; credentials are confidential and saved only with "
        "your encrypted-storage consent."
    )
    for broker in BROKER_CHOICES:
        with st.expander(
            f"{broker} credentials",
            expanded=False,
        ):
            with st.container(
                horizontal=True,
                horizontal_alignment="distribute",
                vertical_alignment="center",
                wrap=False,
            ):
                st.caption(
                    "PRIVATE / CONFIDENTIAL · Credentials are sent to this broker only."
                )
                st.link_button(
                    "API setup guide",
                    BROKER_GUIDES[broker],
                    type="tertiary",
                    icon=":material/open_in_new:",
                    width="content",
                )
            for field, label in BROKER_CREDENTIAL_FIELDS[broker].items():
                st.text_input(
                    label,
                    key=_broker_settings_widget_key(broker, field),
                    type="password"
                    if field
                    in {"api_key", "api_secret", "password", "totp_secret"}
                    else "default",
                    help=BROKER_CREDENTIAL_INFO[broker][field],
                )
    _sync_broker_settings_from_form()


def _close_settings_dialog() -> None:
    st.session_state["user_settings_dialog_open"] = False
    st.session_state.pop("user_settings_feedback", None)


def _open_settings_dialog(
    owner_identifier: str,
    *,
    pending_broker_credentials: tuple[str, dict[str, str]] | None = None,
) -> None:
    try:
        saved_settings = load_user_settings(owner_identifier)
    except (
        OSError,
        RuntimeError,
        DatabaseError,
        DatabaseSecurityError,
        TypeError,
        ValueError,
    ) as error:
        st.session_state["user_settings_error"] = str(error)
        st.session_state["user_settings_feedback"] = {
            "kind": "error",
            "message": f"Saved settings could not be refreshed: {error}",
        }
        st.session_state["user_settings_dialog_open"] = True
        return

    cached_settings = _mapping(
        st.session_state.get("user_settings_form_cache")
    )
    if (
        not saved_settings["sensitive_consent"]
        and not cached_settings.get("sensitive_consent", False)
        and cached_settings.get("owner") == owner_identifier
    ):
        saved_settings["sensitive"] = _mapping(
            cached_settings.get("sensitive")
        )
    apply_settings_to_session(saved_settings, initialize_form_state=True)
    if pending_broker_credentials:
        broker_type, credentials = pending_broker_credentials
        for field in BROKER_CREDENTIAL_FIELDS[broker_type]:
            value = str(credentials.get(field, "")).strip()
            if value:
                st.session_state[_broker_settings_widget_key(broker_type, field)] = value
    st.session_state["user_settings_loaded_for"] = owner_identifier
    st.session_state["user_settings_error"] = None
    st.session_state.pop("user_settings_feedback", None)
    st.session_state["user_settings_dialog_open"] = True


@st.dialog(
    "User settings",
    width="medium",
    on_dismiss=_close_settings_dialog,
)
def _settings_dialog(owner_identifier: str) -> None:
    feedback = _mapping(st.session_state.get("user_settings_feedback"))
    if feedback.get("kind") == "success":
        st.success(
            str(feedback.get("message", "Your settings have been saved.")),
            icon=":material/check_circle:",
        )
        if st.button(
            "OK",
            key="dismiss_settings_saved",
            type="primary",
        ):
            _close_settings_dialog()
            st.rerun(scope="app")
        return

    st.caption(
        "Your non-confidential preferences are stored in this app's local "
        "SQLCipher-encrypted SQLite database. Contact details and credentials "
        "are session-only unless you explicitly approve their additional "
        "encrypted storage below."
    )
    load_error = st.session_state.get("user_settings_error")
    feedback = _mapping(st.session_state.get("user_settings_feedback"))
    if load_error and feedback.get("kind") != "error":
        st.error(
            "Saved encrypted settings could not be loaded. Correct the issue "
            "before saving. " + str(load_error),
            icon=":material/error:",
        )
    if feedback.get("kind") == "error":
        st.error(str(feedback.get("message", "")), icon=":material/error:")

    profile_tab, broker_tab, ai_tab, risk_tab = st.tabs(
        ["Profile", "Brokers", "AI providers", "Risk"]
    )
    with profile_tab:
        st.text_input("Your name", key="settings_profile_name")
        st.text_input(
            "Email address",
            key="settings_profile_email",
            placeholder="you@example.com",
        )
        email_error = _validate_email(
            str(st.session_state.get("settings_profile_email", ""))
        )
        if email_error:
            st.error(email_error)
        country_column, phone_column = st.columns((1, 1.5))
        with country_column:
            st.selectbox(
                "Country code",
                PHONE_COUNTRY_LABELS,
                key="settings_profile_phone_country",
            )
        with phone_column:
            st.text_input(
                "10-digit phone number",
                key="settings_profile_phone_national",
                placeholder="9876543210",
                max_chars=10,
            )
        phone_error = _validate_phone_input(
            str(st.session_state.get(
                "settings_profile_phone_country", PHONE_COUNTRY_LABELS[0]
            )),
            str(st.session_state.get("settings_profile_phone_national", "")),
        )
        if phone_error:
            st.error(phone_error)
        st.caption(
            "Optional. These are the destinations for email and WhatsApp "
            "notifications you request. Delivery requires the app operator to "
            "configure Resend and/or Twilio. Phone notifications use WhatsApp, "
            "not SMS. Choose your country code and enter exactly 10 digits."
        )

    with broker_tab:
        _render_broker_registry()

    with ai_tab:
        _render_ai_provider_configurations(owner_identifier)

    with risk_tab:
        st.caption(
            "A reported realized loss of -3% or more activates the 24-hour buy "
            "circuit breaker. Loss values are user-reported, not broker-verified."
        )
        st.number_input(
            "Maximum scenario allocation (%)",
            min_value=0.0,
            max_value=100.0,
            step=5.0,
            key="settings_max_allocation_pct",
        )
        st.number_input(
            "Realized daily loss (%)",
            min_value=-100.0,
            max_value=100.0,
            step=0.25,
            key="settings_realized_daily_loss_pct",
            help="This is user-reported; broker adapters do not provide a verified realized-P&L feed.",
        )

    st.warning(
        "Serious privacy notice: enabling consent stores your contact details "
        "and entered credentials on the app host for future sessions. They are "
        "encrypted at rest: SQLCipher protects the database, with an additional "
        "encrypted layer for confidential fields. Encryption does not protect "
        "data from someone who can access the running app and its required keys "
        "(the SQLCipher key in the OS vault/deployment secret and the credential "
        "encryption key). Only consent if you trust the device and its operator."
    )
    st.checkbox(
        "I explicitly consent to save my private/confidential fields encrypted "
        "for future sessions.",
        key="settings_sensitive_consent",
        help=(
            "Optional. If unchecked, public preferences are still saved but "
            "private/confidential values are removed from saved storage."
        ),
    )
    st.caption(
        "PUBLIC: broker names/types, endpoint URLs/paths, model choices, and "
        "risk preferences are saved by default. PRIVATE / CONFIDENTIAL: your "
        "name, email, phone, broker keys/secrets/passwords, and AI API keys are "
        "saved only with the consent above; otherwise enter them again in each "
        "new app session. Notification delivery credentials are configured by "
        "the app operator and are not entered here."
    )
    if st.button(
        "Save user settings",
        type="primary",
        icon=":material/save:",
        key="save_user_settings",
        width="stretch",
    ):
        _save(owner_identifier)


def render_user_settings(owner_identifier: str) -> None:
    if st.session_state.get("user_settings_error"):
        st.error(
            "Saved encrypted user settings could not be loaded. " +
            str(st.session_state["user_settings_error"])
        )
    feedback = _mapping(st.session_state.get("user_settings_feedback"))
    if (
        feedback.get("kind") == "error"
        and not st.session_state.get("user_settings_dialog_open")
    ):
        st.error(
            str(feedback.get("message", "")),
            icon=":material/error:",
        )
    st.button(
        "User settings",
        icon=":material/settings:",
        help="Manage your profile, broker connections, AI providers, and risk preferences.",
        key="open_user_settings",
        on_click=_open_settings_dialog,
        args=(owner_identifier,),
    )
    if st.session_state.get("user_settings_dialog_open") or (
        feedback.get("kind") == "success"
    ):
        _settings_dialog(owner_identifier)

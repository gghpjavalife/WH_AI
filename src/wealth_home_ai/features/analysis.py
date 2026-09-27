"""Full-portfolio and scoped AI analysis controls."""

from collections.abc import Callable

import pandas as pd
import streamlit as st

from ..settings import LLM_PROVIDER_BASE_URLS, settings


def _remember_provider_setting(
    provider: str, setting_name: str, widget_key: str
) -> None:
    provider_settings = st.session_state.setdefault("llm_provider_settings", {})
    provider_settings.setdefault(provider, {})[setting_name] = st.session_state.get(
        widget_key, ""
    )


def render_analysis_panel(
    scope: str,
    title: str,
    run_analysis: Callable[[str], None],
    show_approved_trades: Callable[[str], None],
) -> None:
    if scope == "all":
        if st.session_state.ai_settings_prompt:
            st.warning(st.session_state.ai_settings_prompt)
        provider_options = list(LLM_PROVIDER_BASE_URLS)
        if st.session_state.get("llm_provider") not in provider_options:
            st.session_state.llm_provider = "Gemini"

        provider_column, key_column, model_column, button_column = st.columns(
            [1.1, 1.5, 1.2, 1.25],
            vertical_alignment="bottom",
        )
        with provider_column:
            selected_provider = st.selectbox(
                "AI provider",
                provider_options,
                key="llm_provider",
                help="Select the service for your analysis.",
            )
        provider_slug = selected_provider.lower().replace(" ", "_").replace("-", "_")
        provider_settings = st.session_state.llm_provider_settings.setdefault(
            selected_provider, {}
        )
        api_key_widget = (
            "user_gemini_api_key"
            if selected_provider == "Gemini"
            else f"user_llm_api_key_{provider_slug}"
        )
        if api_key_widget not in st.session_state:
            st.session_state[api_key_widget] = str(
                provider_settings.get("api_key", "")
            )
        with key_column:
            st.text_input(
                (
                    f"{selected_provider} API key — required"
                    if st.session_state.ai_settings_prompt
                    else "API key"
                ),
                type="password",
                key=api_key_widget,
                help=(
                    "Used only for this browser session. It is not read from or "
                    "written to server configuration."
                ),
                on_change=_remember_provider_setting,
                args=(selected_provider, "api_key", api_key_widget),
            )
        if st.session_state.get(api_key_widget, "").strip():
            st.session_state.ai_settings_prompt = ""

        model_options = list(settings.llm_provider_models[selected_provider])
        model_selection_key = f"llm_model_selection_{provider_slug}"
        if model_selection_key not in st.session_state:
            default_model = (
                model_options[0]
                if model_options
                else ""
            )
            st.session_state[model_selection_key] = provider_settings.get(
                "model_selection", default_model
            )
        if selected_provider == "Custom OpenAI-compatible":
            st.text_input(
                "Model ID",
                key=model_selection_key,
                placeholder="Enter the model ID supported by your endpoint",
                help="Custom endpoints do not use a predefined model list.",
                on_change=_remember_provider_setting,
                args=(selected_provider, "model_selection", model_selection_key),
            )
        else:
            if st.session_state[model_selection_key] not in model_options:
                st.session_state[model_selection_key] = model_options[0]
            with model_column:
                st.selectbox(
                    "Model",
                    model_options,
                    key=model_selection_key,
                    help="Choose a model supported by the selected provider.",
                    on_change=_remember_provider_setting,
                    args=(selected_provider, "model_selection", model_selection_key),
                )
        if selected_provider == "Custom OpenAI-compatible":
            if st.session_state.get("llm_custom_base_url") is None:
                st.session_state.llm_custom_base_url = str(
                    provider_settings.get(
                        "base_url", settings.llm_custom_base_url
                    )
                )
            st.text_input(
                "API base URL",
                key="llm_custom_base_url",
                placeholder="https://api.example.com/v1",
                help=(
                    "Required for custom endpoints. You can set a deployment "
                    "default with LLM_CUSTOM_BASE_URL."
                ),
                on_change=_remember_provider_setting,
                args=(
                    selected_provider,
                    "base_url",
                    "llm_custom_base_url",
                ),
            )
        with button_column:
            if st.button(
                "Run AI analysis",
                type="primary",
                key=f"run_analysis_{scope}",
                width="stretch",
            ):
                run_analysis(scope)

    elif st.button(
        f"Run {title} analysis",
        type="secondary",
        key=f"run_analysis_{scope}",
    ):
        run_analysis(scope)

    error = st.session_state.analysis_errors.get(scope)
    if error:
        st.error(error)
    price_error = st.session_state.analysis_price_errors.get(scope)
    if price_error:
        st.warning(price_error)
    analysis = st.session_state.analysis_results.get(scope)
    if analysis is None:
        if scope != "all":
            st.caption(
                f"Run a scoped analysis using the {title.lower()} data above. "
                "It uses the provider and key selected beside the full-portfolio "
                "analysis button. Recommendations are advisory and never submitted "
                "automatically."
            )
        return

    st.caption(
        f"Scenario budget: ₹{float(st.session_state.planning_budget_inr):,.2f} · "
        f"actual broker cash: ₹{float(st.session_state.balance):,.2f}"
    )
    st.write(analysis["analysis"])
    candidates = analysis["cash_deployment_list"]
    if candidates:
        st.dataframe(pd.DataFrame(candidates), hide_index=True)
    with st.expander(f"{title} analysis audit trail"):
        st.code(
            "\n".join(st.session_state.audit_trails_by_scope.get(scope, []))
        )
    trades = st.session_state.approved_trades_by_scope.get(scope, [])
    if trades:
        st.warning(
            "Review each broker order carefully. Analysis and risk checks do not "
            "guarantee an execution price or investment outcome."
        )
        show_approved_trades(scope)
    elif candidates:
        st.info("No order passed the current quote, confidence, and budget checks.")

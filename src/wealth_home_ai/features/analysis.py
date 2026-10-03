"""Full-portfolio and scoped AI analysis controls."""

from collections.abc import Callable

import pandas as pd
import streamlit as st

from ..settings import LLM_PROVIDER_BASE_URLS, settings

LLM_KEY_GUIDES = {
    "Gemini": ("Google AI Studio", "https://aistudio.google.com/app/apikey"),
    "OpenAI": ("OpenAI platform", "https://platform.openai.com/api-keys"),
    "Anthropic": ("Anthropic console", "https://console.anthropic.com/settings/keys"),
    "Groq": ("Groq console", "https://console.groq.com/keys"),
    "Together AI": (
        "Together AI settings",
        "https://api.together.xyz/settings/api-keys",
    ),
    "Mistral": ("Mistral console", "https://console.mistral.ai/api-keys/"),
    "DeepSeek": ("DeepSeek platform", "https://platform.deepseek.com/api_keys"),
}


def _remember_provider_setting(
    provider: str, setting_name: str, widget_key: str
) -> None:
    provider_settings = st.session_state.setdefault("llm_provider_settings", {})
    provider_settings.setdefault(provider, {})[setting_name] = st.session_state.get(
        widget_key, ""
    )


def _render_guided_field_label(
    label: str,
    *,
    why: str,
    how: str,
    link_label: str | None = None,
    link_url: str | None = None,
) -> None:
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
            help=f"Why this field is needed and how to set it: {label}",
        ):
            st.markdown(f"**Why you need this**\n\n{why}")
            st.markdown(f"**How to set it**\n\n{how}")
            if link_label and link_url:
                st.markdown(f"[{link_label} ↗]({link_url})")


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
            _render_guided_field_label(
                "AI provider",
                why="Selects which AI service receives your analysis request.",
                how="Choose a provider for which you have an API key.",
            )
            selected_provider = st.selectbox(
                "AI provider",
                provider_options,
                key="llm_provider",
                label_visibility="collapsed",
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
            api_key_label = (
                f"{selected_provider} API key — required"
                if st.session_state.ai_settings_prompt
                else "API key"
            )
            key_guide = LLM_KEY_GUIDES.get(selected_provider)
            _render_guided_field_label(
                api_key_label,
                why=(
                    "Authorizes the selected AI provider to analyze your portfolio "
                    "and answer companion questions."
                ),
                how=(
                    f"Create an API key in the {selected_provider} developer "
                    "console and paste it into this field."
                    if key_guide
                    else "Create a key in your custom endpoint provider's "
                    "developer console. The key is sent only to the configured endpoint."
                ),
                link_label=key_guide[0] + " key guide" if key_guide else None,
                link_url=key_guide[1] if key_guide else None,
            )
            st.text_input(
                api_key_label,
                type="password",
                key=api_key_widget,
                label_visibility="collapsed",
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
            _render_guided_field_label(
                "Model ID",
                why="Selects which model receives your analysis request.",
                how="Copy the exact model ID supported by your custom endpoint.",
            )
            st.text_input(
                "Model ID",
                key=model_selection_key,
                placeholder="Enter the model ID supported by your endpoint",
                label_visibility="collapsed",
                on_change=_remember_provider_setting,
                args=(selected_provider, "model_selection", model_selection_key),
            )
        else:
            if st.session_state[model_selection_key] not in model_options:
                st.session_state[model_selection_key] = model_options[0]
            with model_column:
                _render_guided_field_label(
                    "Model",
                    why="Selects which AI model receives the analysis request.",
                    how="Choose a model enabled for your selected provider account.",
                )
                st.selectbox(
                    "Model",
                    model_options,
                    key=model_selection_key,
                    label_visibility="collapsed",
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
            _render_guided_field_label(
                "API base URL",
                why="Routes the analysis request to your custom AI endpoint.",
                how=(
                    "Copy the HTTPS base URL from your provider's API "
                    "documentation, including its version path when specified."
                ),
            )
            st.text_input(
                "API base URL",
                key="llm_custom_base_url",
                placeholder="https://api.example.com/v1",
                label_visibility="collapsed",
                on_change=_remember_provider_setting,
                args=(
                    selected_provider,
                    "base_url",
                    "llm_custom_base_url",
                ),
            )
        with button_column:
            if st.button(
                "🔮 Run LLM Portfolio Analysis",
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

    if scope in {"all", "equity", "trading"}:
        candidate_count = len(st.session_state.get("market_ai_candidates", []))
        if candidate_count:
            st.caption(
                f"AI candidate pool: {candidate_count} NSE Strong Buy/Buy stocks "
                "from the latest Equities scan. BSE listings and stocks without a "
                "usable broker quote are not eligible for an LLM recommendation."
            )
        else:
            st.caption(
                "Run an equity market scan first. Only broker-quoted NSE Strong Buy/Buy "
                "stocks from that scan can be recommended for deployment."
            )

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
    with st.expander("JEV rules engine · evaluation log", icon=":material/rule:"):
        st.caption(
            "Numbered checks show the order in which portfolio safeguards and "
            "allocation rules were evaluated. The letter in parentheses maps to "
            "the corresponding rule definition."
        )
        audit_lines = st.session_state.audit_trails_by_scope.get(scope, [])
        if audit_lines:
            numbered_lines = []
            rule_number = 0
            for line in audit_lines:
                if line.startswith("Rule "):
                    rule_number += 1
                    numbered_lines.append(f"{rule_number}. {line}")
                else:
                    numbered_lines.append(f"   {line}")
            st.code("\n".join(numbered_lines), language=None)
        else:
            st.info("No rule evaluation log is available for this analysis yet.")
    trades = st.session_state.approved_trades_by_scope.get(scope, [])
    if trades:
        st.warning(
            "Review each broker order carefully. Analysis and risk checks do not "
            "guarantee an execution price or investment outcome."
        )
        show_approved_trades(scope)
    elif candidates:
        st.info("No order passed the current quote, confidence, and budget checks.")

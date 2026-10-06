"""Full-portfolio and scoped AI analysis controls."""

from collections.abc import Callable

import pandas as pd
import streamlit as st

from core.constants import LLMProvider


def render_analysis_panel(
    scope: str,
    title: str,
    run_analysis: Callable[[str], None],
    show_approved_trades: Callable[[str], None],
) -> None:
    if scope == "all":
        if st.session_state.ai_settings_prompt:
            st.warning(st.session_state.ai_settings_prompt)
        selected_provider = str(
            st.session_state.get("llm_provider", LLMProvider.GEMINI.value)
        )
        provider_slug = selected_provider.lower().replace(" ", "_").replace("-", "_")
        api_key_widget = (
            "user_gemini_api_key"
            if selected_provider == LLMProvider.GEMINI.value
            else f"user_llm_api_key_{provider_slug}"
        )
        model_key = f"llm_model_selection_{provider_slug}"
        model = str(st.session_state.get(model_key, "")).strip()
        api_key_ready = bool(
            str(st.session_state.get(api_key_widget, "")).strip()
        )
        model_ready = bool(model)
        if selected_provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value:
            model_ready = model_ready and bool(
                str(st.session_state.get("llm_custom_base_url", "")).strip()
            )
        st.caption(
            f"AI setup: **{selected_provider}** · **{model or 'model not set'}**. "
            "Manage providers and credentials in User settings."
        )
        if not api_key_ready:
            st.info("Add an API key for the selected provider in User settings.")
        if st.button(
            "Run AI portfolio analysis",
            type="primary",
            key=f"run_analysis_{scope}",
            disabled=not (api_key_ready and model_ready),
            icon=":material/auto_awesome:",
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

    if scope == "all":
        rule_labels = {
            1: "Rule 1 — Portfolio circuit breaker",
            2: "Rule 2 — Trading window",
            3: "Rule 3 — Active position limit",
            4: "Rule 4 — Confidence threshold",
            5: "Rule 5 — Capital allocation",
            6: "Rule 6 — Limited-cash prioritization",
        }
        selected_rules = st.multiselect(
            "JEv rules for analysis",
            options=list(rule_labels),
            default=st.session_state.get("enabled_jev_rules", list(rule_labels)),
            format_func=lambda rule_number: rule_labels[rule_number],
            key="enabled_jev_rules",
        )
        selected_rules = sorted(int(rule_number) for rule_number in selected_rules)
        previous_rules = st.session_state.get("jev_rule_selection_after_analysis")
        if previous_rules is not None and selected_rules != previous_rules:
            st.session_state.jev_rule_selection_after_analysis = list(selected_rules)
            st.session_state.jev_rule_selection_before_analysis = list(selected_rules)
            for existing_scope, existing_analysis in st.session_state.analysis_results.items():
                reevaluate = st.session_state.get("reevaluate_analysis_rules")
                if reevaluate:
                    reevaluate(existing_scope, existing_analysis, selected_rules)
    else:
        rule_labels = {
            1: "Rule 1 — Portfolio circuit breaker",
            2: "Rule 2 — Trading window",
            3: "Rule 3 — Active position limit",
            4: "Rule 4 — Confidence threshold",
            5: "Rule 5 — Capital allocation",
            6: "Rule 6 — Limited-cash prioritization",
        }
        selected_rules = st.multiselect(
            "JEv rules for analysis",
            options=list(rule_labels),
            default=st.session_state.get("enabled_jev_rules", list(rule_labels)),
            format_func=lambda rule_number: rule_labels[rule_number],
            key="enabled_jev_rules",
        )

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

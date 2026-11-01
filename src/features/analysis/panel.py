"""Full-portfolio and scoped AI analysis controls."""

from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime
import html
import re
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from core.config import settings
from core.constants import LLMProvider


def analysis_sentiment(analysis: object) -> str | None:
    if not isinstance(analysis, dict):
        return None
    assessment = analysis.get("assessment")
    if not isinstance(assessment, dict):
        return None
    sentiment = assessment.get("sentiment")
    return (
        sentiment
        if isinstance(sentiment, str)
        and sentiment in {"positive", "cautious", "negative"}
        else None
    )


def analysis_status_icon(analysis: object) -> str | None:
    sentiment = analysis_sentiment(analysis)
    if sentiment is None:
        return None
    return "🟢" if sentiment == "positive" else "🔴"


def analysis_mentions_equity_holdings(
    analysis: object, holdings: pd.DataFrame
) -> bool:
    if not isinstance(analysis, dict) or "Ticker" not in holdings.columns:
        return False
    tickers = {
        str(ticker).strip().upper()
        for ticker in holdings["Ticker"].dropna()
        if str(ticker).strip()
    }
    if not tickers:
        return False

    text = " ".join(
        [
            str(analysis.get("analysis", "")),
            str(analysis.get("assessment", "")),
        ]
    ).upper()
    return any(
        re.search(rf"(?<![A-Z0-9]){re.escape(ticker)}(?![A-Z0-9])", text)
        for ticker in tickers
    )


def format_analysis_time(value: object) -> str:
    if isinstance(value, datetime):
        analyzed_at = value
    elif isinstance(value, str):
        try:
            analyzed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return "Analysis time unavailable"
    else:
        return "Analysis time unavailable"

    timezone = ZoneInfo(settings.timezone)
    if analyzed_at.tzinfo is None:
        analyzed_at = analyzed_at.replace(tzinfo=timezone)
    return analyzed_at.astimezone(timezone).strftime("%d %b %Y · %H:%M:%S %Z")


def _analysis_time_caption(
    value: object,
    metadata: dict[str, object] | None,
    *,
    empty_label: str,
) -> tuple[str, str]:
    analysis_time = format_analysis_time(value) if value else empty_label
    details = [analysis_time]
    if metadata:
        provenance = [
            f"{label}: {metadata[key]}"
            for key, label in (("provider", "Provider"), ("model", "Model"))
            if metadata.get(key)
        ]
        details.extend(provenance)
    return "Last AI analysis:", " · ".join(details)


def _render_analysis_header(
    run_button: Callable[[], None],
    *,
    last_analyzed_at: object,
    analysis_metadata: dict[str, object] | None,
    empty_label: str,
) -> None:
    action_column, time_column = st.columns(
        [1.8, 5.2],
        vertical_alignment="center",
        gap="medium",
    )
    with action_column:
        run_button()
    with time_column:
        caption, analysis_details = _analysis_time_caption(
            last_analyzed_at,
            analysis_metadata,
            empty_label=empty_label,
        )
        st.markdown(
            '<div style="text-align:right;white-space:nowrap;'
            'color:rgba(250,250,250,.6)">'
            f"{html.escape(caption)}&nbsp;<strong>"
            f"{html.escape(analysis_details)}</strong></div>",
            unsafe_allow_html=True,
        )


def render_analysis_brief(
    analysis: dict[str, object] | None,
    *,
    title: str,
    compact: bool = False,
    placeholder: bool = False,
    collapsible: bool = False,
    run_analysis: Callable[[], None] | None = None,
    last_analyzed_at: object = None,
    analysis_metadata: dict[str, object] | None = None,
    storage_error: str | None = None,
) -> None:
    analysis_job = st.session_state.get("analysis_job")
    analysis_running = bool(
        analysis_job and not analysis_job["future"].done()
    )
    analysis_available = analysis is not None
    run_button_label = (
        analysis_progress_label(analysis_job)
        if analysis_running
        else (
            "Refresh AI analysis"
            if analysis_available
            else "Run AI portfolio analysis"
        )
    )

    def render_run_button() -> None:
        if run_analysis and st.button(
            run_button_label,
            type="primary",
            key="run_analysis_home",
            icon=":material/auto_awesome:",
            disabled=analysis_running,
        ):
            run_analysis()

    if not analysis_available:
        if placeholder:
            section = (
                st.expander(title, expanded=False, icon=":material/auto_awesome:")
                if collapsible
                else st.container(border=True)
            )
            with section:
                _render_analysis_header(
                    render_run_button,
                    last_analyzed_at=last_analyzed_at,
                    analysis_metadata=analysis_metadata,
                    empty_label="Not run yet",
                )
                if storage_error:
                    st.warning(storage_error)
        return

    assessment = analysis.get("assessment")
    if not isinstance(assessment, dict):
        assessment = {
            "sentiment": "cautious",
            "summary": str(analysis.get("analysis", "")).strip()
            or "The provider did not return categorized assessment details.",
            "strengths": [],
            "warnings": ["Structured assessment details are unavailable."],
            "suggestions": [],
        }
    summary = assessment.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = str(analysis.get("analysis", "")).strip() or (
            "The provider did not return categorized assessment details."
        )
    section = (
        st.expander(title, expanded=False, icon=":material/auto_awesome:")
        if collapsible
        else nullcontext()
    )
    with section:
        _render_analysis_header(
            render_run_button,
            last_analyzed_at=last_analyzed_at,
            analysis_metadata=analysis_metadata,
            empty_label="Time unavailable",
        )
        if storage_error:
            st.warning(storage_error)
        st.write(summary)

        if not compact:
            narrative = str(analysis.get("analysis", "")).strip()
            if narrative and narrative != summary:
                with st.expander(
                    "Detailed portfolio overview",
                    icon=":material/subject:",
                ):
                    st.write(narrative)

        categories = (
            ("Strengths", "strengths", ":material/thumb_up:", "green"),
            ("Warnings", "warnings", ":material/thumb_down:", "red"),
            ("Suggestions", "suggestions", ":material/lightbulb:", "blue"),
        )
        columns = st.columns(len(categories))
        for column, (heading, key, icon, color) in zip(
            columns, categories, strict=True
        ):
            with column:
                st.badge(heading, icon=icon, color=color)
                items = assessment.get(key, [])
                if isinstance(items, list) and items:
                    for item in items:
                        st.markdown(f"- {item}")
                else:
                    st.caption(
                        f"No {heading.lower()} identified from the supplied data."
                    )


def portfolio_analysis_is_configured() -> bool:
    selected_provider = str(
        st.session_state.get("llm_provider", LLMProvider.GEMINI.value)
    )
    provider_slug = selected_provider.lower().replace(" ", "_").replace("-", "_")
    api_key_widget = (
        "user_gemini_api_key"
        if selected_provider == LLMProvider.GEMINI.value
        else f"user_llm_api_key_{provider_slug}"
    )
    model = str(
        st.session_state.get(
            f"llm_model_selection_{provider_slug}",
            next(
                iter(settings.llm_provider_models.get(selected_provider, ())),
                "",
            ),
        )
    ).strip()
    if selected_provider == LLMProvider.CUSTOM_OPENAI_COMPATIBLE.value:
        model = model and bool(
            str(
                st.session_state.get(
                    "llm_custom_base_url", settings.llm_custom_base_url
                )
            ).strip()
        )
    return bool(str(st.session_state.get(api_key_widget, "")).strip() and model)


def analysis_progress_label(analysis_job: dict[str, object] | None) -> str:
    if not analysis_job:
        return "Run AI portfolio analysis"
    if analysis_job.get("stage") == "quotes":
        return "Refreshing broker quotes..."
    scope = str(analysis_job.get("scope", "all"))
    scope_label = {
        "all": "portfolio",
        "equity": "equities",
        "trading": "trading positions",
    }.get(scope, scope.replace("_", " "))
    return f"Analyzing {scope_label}..."


def render_analysis_panel(
    scope: str,
    title: str,
    run_analysis: Callable[[str], None],
    show_approved_trades: Callable[[str], None],
) -> None:
    analysis_job = st.session_state.get("analysis_job")
    analysis_running = bool(
        analysis_job and not analysis_job["future"].done()
    )
    if scope == "all":
        if st.session_state.ai_settings_prompt:
            st.warning(st.session_state.ai_settings_prompt)
        selected_provider = str(
            st.session_state.get("llm_provider", LLMProvider.GEMINI.value)
        )
        provider_slug = selected_provider.lower().replace(" ", "_").replace("-", "_")
        model = str(
            st.session_state.get(f"llm_model_selection_{provider_slug}", "")
        ).strip()
        api_key_widget = (
            "user_gemini_api_key"
            if selected_provider == LLMProvider.GEMINI.value
            else f"user_llm_api_key_{provider_slug}"
        )
        st.caption(
            f"AI setup: **{selected_provider}** · "
            f"**{model or 'model not set'}**. "
            "Manage providers and credentials in User settings."
        )
        if not str(st.session_state.get(api_key_widget, "")).strip():
            st.info(
                "Run portfolio analysis to enter missing provider settings for "
                "this session."
            )
        if st.button(
            analysis_progress_label(analysis_job)
            if analysis_running
            else (
                "Refresh portfolio analysis"
                if st.session_state.get("analysis_results", {}).get(scope)
                else "Run portfolio analysis"
            ),
            type="primary",
            key=f"run_analysis_{scope}",
            icon=":material/auto_awesome:",
            disabled=analysis_running,
        ):
            run_analysis(scope)

    elif st.button(
        analysis_progress_label(analysis_job)
        if analysis_running
        else f"Run {title} analysis",
        type="secondary",
        key=f"run_analysis_{scope}",
        disabled=analysis_running,
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
    if scope != "all":
        render_analysis_brief(
            analysis,
            title=f"{title.title()} AI brief",
            compact=True,
        )
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

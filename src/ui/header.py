"""Shared application header components."""

from collections.abc import Callable

import streamlit as st

from core.constants import AppIdentity
from ui.helpers import brand_lockup_html


def render_brand_lockup() -> None:
    st.html(
        brand_lockup_html(
            AppIdentity.BRAND.value,
            AppIdentity.BRAND_EXPANSION.value,
            AppIdentity.BRAND_DESCRIPTION.value,
        )
    )


def render_feature_badges() -> None:
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


def render_header(
    connected_broker: str | None,
    *,
    render_share: Callable[[], None],
    render_settings: Callable[[], None],
    render_agent: Callable[[], None],
    on_disconnect: Callable[[], None],
) -> None:
    with st.container(border=True, gap="medium"):
        with st.container(
            horizontal=True,
            horizontal_alignment="distribute",
            vertical_alignment="center",
            gap="medium",
            wrap=True,
        ):
            render_brand_lockup()
            with st.container(
                horizontal=True,
                horizontal_alignment="right",
                vertical_alignment="center",
                gap="small",
                wrap=True,
            ):
                render_share()
                render_settings()
                render_agent()
        with st.container(
            horizontal=True,
            horizontal_alignment="distribute",
            vertical_alignment="center",
            gap="small",
            wrap=True,
        ):
            render_feature_badges()
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
                        on_click=on_disconnect,
                        icon=":material/swap_horiz:",
                        help="Disconnect this broker and connect another account.",
                    )
            else:
                st.badge(
                    "No broker connected",
                    icon=":material/link_off:",
                    color="gray",
                )

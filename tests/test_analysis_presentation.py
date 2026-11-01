import unittest

import pandas as pd
from streamlit.testing.v1 import AppTest

from features.analysis.panel import (
    analysis_mentions_equity_holdings,
    analysis_sentiment,
    format_analysis_time,
)
from ui.helpers import (
    analysis_tab_status_css,
    signed_in_label_html,
)


class AnalysisPresentationTests(unittest.TestCase):
    def test_signed_in_label_is_inline_grey_and_does_not_wrap(self):
        markup = signed_in_label_html("CHINNA KRISHNAIA GARI GURUSWAMY")

        self.assertIn("white-space:nowrap", markup)
        self.assertIn("color:rgba(250,250,250,.6)", markup)
        self.assertIn("Signed in as", markup)
        self.assertIn("CHINNA KRISHNAIA GARI GURUSWAMY", markup)

    def test_full_portfolio_analysis_matches_held_equity_ticker(self):
        holdings = pd.DataFrame({"Ticker": ["UJJIVANSFB", "SUZLON"]})
        analysis = {
            "analysis": "Portfolio gains are concentrated in UJJIVANSFB.",
            "assessment": {"warnings": ["SUZLON is underperforming."]},
        }

        self.assertTrue(analysis_mentions_equity_holdings(analysis, holdings))

    def test_analysis_time_is_formatted_for_the_configured_timezone(self):
        rendered_time = format_analysis_time("2026-11-01T10:00:00+05:30")

        self.assertIn("01 Nov 2026", rendered_time)
        self.assertIn(":", rendered_time)
        self.assertNotEqual("Analysis time unavailable", rendered_time)

    def test_recommendation_ticker_alone_does_not_match_equity_holdings(self):
        holdings = pd.DataFrame({"Ticker": ["RELIANCE"]})
        analysis = {
            "analysis": "No current holdings were discussed.",
            "cash_deployment_list": [{"Ticker": "RELIANCE"}],
        }

        self.assertFalse(analysis_mentions_equity_holdings(analysis, holdings))

    def test_analysis_tab_status_css_styles_status_backgrounds(self):
        css = analysis_tab_status_css({1: "cautious", 2: "positive"})

        self.assertIn("[role=\"tab\"]:nth-child(1)", css)
        self.assertIn("rgba(146, 64, 14, .3)", css)
        self.assertIn("[role=\"tab\"]:nth-child(2)", css)
        self.assertIn("rgba(22, 101, 52, .28)", css)

    def test_analysis_tab_status_css_ignores_unknown_status(self):
        self.assertEqual(analysis_tab_status_css({1: "unknown"}), "<style></style>")

    def test_analysis_summary_stays_visible_with_details_collapsed(self):
        app = AppTest.from_string(
            """
import streamlit as st
from features.analysis.panel import render_analysis_brief

def run_analysis():
    pass

st.session_state["analysis_timestamps"] = {
    "all": "2026-11-01T10:00:00+05:30",
}
st.session_state["analysis_metadata"] = {
    "all": {"provider": "Gemini", "model": "fast-model"},
}
render_analysis_brief(
    {
        "analysis": "The portfolio has mixed performance.",
        "assessment": {
            "sentiment": "cautious",
            "summary": "Portfolio performance is mixed.",
            "strengths": [],
            "warnings": ["Concentration risk."],
            "suggestions": [],
            "scenarios": [
                {
                    "title": "Downside case",
                    "condition": "The leading holding declines.",
                    "implication": "Review concentration.",
                },
            ],
        },
    },
    title="AI Portfolio Analysis",
    collapsible=True,
    run_analysis=run_analysis,
    last_analyzed_at=st.session_state["analysis_timestamps"]["all"],
    analysis_metadata=st.session_state["analysis_metadata"]["all"],
)
"""
        ).run()

        self.assertIn(
            "Portfolio performance is mixed.",
            [element.value for element in app.markdown],
        )
        self.assertEqual(app.expander[0].label, "AI Portfolio Analysis")
        self.assertFalse(app.expander[0].proto.expanded)
        self.assertIn("Refresh AI analysis", [button.label for button in app.button])
        rendered_text = [element.value for element in app.markdown]
        self.assertFalse(any("AI-powered portfolio review" in item for item in rendered_text))
        self.assertFalse(any("decision-support view" in item for item in rendered_text))
        self.assertFalse(any("Downside case" in item for item in rendered_text))
        self.assertFalse(any("Overall status" in item for item in rendered_text))
        timestamp_markup = next(
            item for item in rendered_text if "Last AI analysis:" in item
        )
        self.assertIn("text-align:right", timestamp_markup)
        self.assertIn("white-space:nowrap", timestamp_markup)
        self.assertIn(
            "Last AI analysis:&nbsp;<strong>01 Nov 2026 · 10:00:00 IST · "
            "Provider: Gemini · Model: fast-model</strong>",
            timestamp_markup,
        )
        self.assertNotIn("<br>", timestamp_markup)
        self.assertIn("color:rgba(250,250,250,.6)", timestamp_markup)
        self.assertEqual(
            analysis_sentiment(
                {"assessment": {"sentiment": "cautious"}}
            ),
            "cautious",
        )

    def test_analysis_button_runs_from_the_analysis_panel(self):
        app = AppTest.from_string(
            """
import streamlit as st
from features.analysis.panel import render_analysis_brief

def run_analysis():
    st.session_state["analysis_clicked"] = True

render_analysis_brief(
    None,
    title="AI Portfolio Analysis",
    placeholder=True,
    run_analysis=run_analysis,
)
"""
        ).run()

        app.button[0].click().run()

        self.assertTrue(app.session_state["analysis_clicked"])

    def test_signed_in_identity_is_icon_text_not_a_button(self):
        app = AppTest.from_string(
            """
import streamlit as st
st.markdown(
    ":gray[:material/account_circle: **Signed in as**] "
    "**CHINNA KRISHNAIA GARI GURUSWAMY**"
)
st.button(
    " ",
    key="disconnect",
    type="primary",
    icon=":material/swap_horiz:",
    help="Disconnect this broker and connect another account.",
)
"""
        ).run()

        self.assertIn(
            ":gray[:material/account_circle: **Signed in as**] "
            "**CHINNA KRISHNAIA GARI GURUSWAMY**",
            [element.value for element in app.markdown],
        )
        self.assertEqual([button.label for button in app.button], [" "])
        self.assertEqual(app.button[0].proto.type, "primary")

    def test_analysis_and_planning_panels_follow_portfolio_data(self):
        app = AppTest.from_string(
            """
import pandas as pd
import streamlit as st
from features.home.workspace import render_home_workspace

def render_portfolio_review():
    with st.expander(
        "Portfolio planning & safeguards",
        icon=":material/shield_with_heart:",
    ):
        st.caption("Planning controls")

empty_equities = pd.DataFrame(columns=["Ticker", "Qty", "Avg_Price", "LTP"])
empty_positions = pd.DataFrame(columns=["Ticker", "Qty", "Avg_Price", "LTP"])
empty_funds = pd.DataFrame(columns=["Units", "Avg_NAV", "Latest_NAV"])
render_home_workspace(
    0.0,
    empty_equities,
    empty_positions,
    empty_funds,
    live_prices={},
    live_prices_updated_at=None,
    run_portfolio_analysis=lambda: None,
    render_portfolio_review=render_portfolio_review,
)
"""
        ).run()

        expander_labels = [expander.label for expander in app.expander]
        self.assertLess(
            expander_labels.index("AI Portfolio Analysis"),
            expander_labels.index("Portfolio planning & safeguards"),
        )
        self.assertFalse(app.expander[0].proto.expanded)


if __name__ == "__main__":
    unittest.main()

import unittest

from features.home.workspace import build_allocation_chart
from ui.helpers import app_animation_css, brand_lockup_html


class UIAnimationTests(unittest.TestCase):
    def test_app_motion_has_entrances_feedback_and_reduced_motion_support(self):
        css = app_animation_css()

        self.assertIn("@keyframes guru-rise-in", css)
        self.assertIn('[data-testid="stMetric"]:hover', css)
        self.assertIn('[data-testid="stButton"] button:hover:not(:disabled)', css)
        self.assertIn('[data-testid="stPlotlyChart"]', css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("animation: none !important", css)

    def test_brand_has_a_reduced_motion_safe_live_equalizer_icon(self):
        html = brand_lockup_html("GGHP", "Governed Growth", "Portfolio companion")

        self.assertIn("@keyframes gghp-live-dance", html)
        self.assertIn('class="gghp-live-bars"', html)
        self.assertIn("prefers-reduced-motion: no-preference", html)
        self.assertIn("\n    LIVE", html)

    def test_portfolio_chart_uses_a_smooth_data_transition(self):
        chart = build_allocation_chart(
            {
                "total_investments": 100,
                "allocation": {"Equity": 100},
                "asset_counts": {"Equity": 1},
                "pnl_by_asset": {"Equity": 5},
            }
        )

        self.assertEqual(chart.layout.transition.duration, 650)
        self.assertEqual(chart.layout.transition.easing, "cubic-in-out")


if __name__ == "__main__":
    unittest.main()

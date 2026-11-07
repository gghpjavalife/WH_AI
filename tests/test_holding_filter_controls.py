import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from features.analysis.holdings import _refresh_holdings_research


class HoldingFilterControlsTests(unittest.TestCase):
    def test_refresh_is_supplied_to_the_shared_table(self):
        app = AppTest.from_string("""
import pandas as pd
import streamlit as st
from features.analysis.holdings import render_stock_holdings_table
st.session_state["stock_research_cache"] = {
    "selected_stock_research_ABC": {"signal": "Hold", "reason": "Local", "errors": {}}
}
render_stock_holdings_table(pd.DataFrame([{
    "Ticker": "ABC", "Qty": 1, "Avg_Price": 100, "LTP": 101,
    "Investment": 100, "Market_Value": 101, "P&L": 1,
    "Returns_%": 1, "Allocation_%": 100,
}]), key="example", theme="dark")
""")
        with patch("features.equities.scan_table.show_scan_table") as table:
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.button), 0)
            self.assertTrue(callable(table.call_args.kwargs["on_refresh"]))
            self.assertEqual(table.call_args.args[1]["tableKey"], "example")

    def test_refresh_clears_only_selected_research_and_all_local_context(self):
        state = {
            "stock_context_cache": {"context": "cached"},
            "stock_research_cache": {
                "selected_stock_research_ABC": "old",
                "selected_stock_research_XYZ": "keep",
            },
        }
        with patch("features.analysis.holdings.st.session_state", state), \
             patch("features.analysis.holdings.fetch_daily_history.clear") as history, \
             patch("features.analysis.holdings.fetch_company_fundamentals.clear") as fundamentals, \
             patch("features.analysis.holdings.fetch_company_news.clear") as news:
            _refresh_holdings_research({"ABC"})
        self.assertEqual(state["stock_context_cache"], {})
        self.assertEqual(state["stock_research_cache"], {"selected_stock_research_XYZ": "keep"})
        for clear in (history, fundamentals, news):
            clear.assert_called_once_with("ABC")


if __name__ == "__main__":
    unittest.main()

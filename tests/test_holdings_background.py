import threading
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from features.analysis import holdings


_APP = """
import pandas as pd
from features.analysis.holdings import render_stock_holdings_table
render_stock_holdings_table(pd.DataFrame([{
    "Ticker": "ABC", "Qty": 2, "Avg_Price": 100, "LTP": 110,
    "Market_Value": 220, "P&L": 20, "Returns_%": 10, "Allocation_%": 100,
}]), key="holdings_test", theme="dark")
"""


class HoldingsBackgroundTests(unittest.TestCase):
    def test_slow_research_does_not_block_reruns_or_duplicate_work(self):
        started = threading.Event()
        release = threading.Event()

        def research(ticker):
            started.set()
            if not release.wait(5):
                raise RuntimeError("Test research timed out.")
            return {"signal": "Buy", "reason": "Completed", "errors": {}}

        app = AppTest.from_string(_APP)
        with patch.object(holdings, "_analyze_selected_stock", side_effect=research) as fetch, \
                patch("features.equities.scan_table.show_scan_table") as table:
            try:
                app.run(timeout=3)
                self.assertTrue(started.wait(2))
                self.assertFalse(app.exception)
                self.assertEqual(table.call_args.args[0][0]["signal"], "NOT RATED")
                app.run(timeout=3)
                self.assertEqual(fetch.call_count, 1)
                future = next(iter(app.session_state["holding_research_jobs"].values()))
                release.set()
                future.result(timeout=3)
                app.run(timeout=3)
                self.assertFalse(app.exception)
                self.assertEqual(table.call_args.args[0][0]["signal"], "BUY")
                self.assertEqual(app.session_state["holding_research_jobs"], {})
            finally:
                release.set()

    def test_cancelled_jobs_are_not_reused(self):
        from concurrent.futures import Future

        future = Future()
        state = {"holding_research_jobs": {("research", "ABC"): future}}
        with patch.object(holdings.st, "session_state", state):
            holdings._cancel_holding_jobs()
        self.assertTrue(future.cancelled())
        self.assertNotIn("holding_research_jobs", state)

    def test_running_jobs_are_detached_on_broker_change(self):
        from concurrent.futures import Future

        future = Future()
        future.set_running_or_notify_cancel()
        state = {"holding_research_jobs": {("research", "ABC"): future}}
        with patch.object(holdings.st, "session_state", state):
            holdings._cancel_holding_jobs()
        future.set_result({"signal": "Buy"})
        self.assertNotIn("holding_research_jobs", state)
        self.assertNotIn("stock_research_cache", state)


if __name__ == "__main__":
    unittest.main()

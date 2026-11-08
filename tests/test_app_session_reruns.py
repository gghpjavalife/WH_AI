import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from runpy import run_path
from unittest.mock import patch

from application import dashboard
from streamlit.testing.v1 import AppTest


class AppSessionRerunTests(unittest.TestCase):
    def test_concurrent_sessions_each_render_on_every_run(self):
        barrier = threading.Barrier(2, timeout=5)

        def render_session():
            barrier.wait()

        path = str(Path(__file__).resolve().parents[1] / "app.py")
        with patch.object(dashboard, "main", side_effect=render_session) as main:
            for _ in range(2):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(run_path, [path, path]))
            self.assertEqual(main.call_count, 4)

    def test_broker_change_discards_pending_scanner_jobs(self):
        for action in ('_complete_broker_login("Upstox", "adapter", "token")', '_logout()'):
            with self.subTest(action=action):
                app = AppTest.from_string(
                    "from application.dashboard import "
                    "_initialize_session, _complete_broker_login, _logout\n"
                    "_initialize_session()\n" + action + "\n"
                )
                future = Future()
                cancel = threading.Event()
                app.session_state["_market_scan_jobs"] = {
                    "equities": {"future": future, "cancel": cancel}
                }
                app.run(timeout=10)
                self.assertFalse(app.exception)
                self.assertTrue(cancel.is_set())
                self.assertTrue(future.cancelled())
                self.assertNotIn("_market_scan_jobs", app.session_state)


if __name__ == "__main__":
    unittest.main()

import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
from streamlit.testing.v1 import AppTest


class AnalysisSessionOnlyTests(unittest.TestCase):
    def test_existing_analysis_is_not_restored_and_cleanup_is_queued(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
        app.session_state["token"] = "test-token"
        app.session_state["broker_state"] = Mock()
        app.session_state["broker_name"] = "Upstox"
        app.session_state["profile_fetch_attempted"] = True
        app.session_state["last_sync_attempt"] = pd.Timestamp.now(
            tz="Asia/Kolkata"
        ).to_pydatetime()
        app.session_state["_local_storage_cache"] = {
            "portfolio_analysis:old": {"analysis_json": '{"analysis":"old"}'},
            "user_settings:owner": {"preferences_json": "{}"},
        }

        app.run(timeout=20)

        self.assertFalse(app.exception)
        self.assertIsNone(app.session_state["portfolio_overview_analysis"])
        self.assertEqual(app.session_state["analysis_results"], {})
        self.assertEqual(
            app.session_state["_local_storage_cache"]["user_settings:owner"],
            {"preferences_json": "{}"},
        )
        self.assertNotIn(
            "portfolio_analysis:old", app.session_state["_local_storage_cache"]
        )
        self.assertTrue(any(
            request["action"] == "delete_collection"
            and request["collection"] == "portfolio_analysis"
            for request in app.session_state["_local_storage_queue"]
        ))

    def test_completed_overview_stays_in_session_without_storage_write(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
        app.session_state["token"] = "test-token"
        app.session_state["broker_state"] = Mock()
        app.session_state["broker_name"] = "Upstox"
        app.session_state["profile_fetch_attempted"] = True
        app.session_state["last_sync_attempt"] = pd.Timestamp.now(
            tz="Asia/Kolkata"
        ).to_pydatetime()
        analysis = {"analysis": "Session-only result", "cash_deployment_list": []}
        future = Future()
        future.set_result(analysis)
        app.session_state["analysis_job"] = {
            "scope": "portfolio_overview",
            "stage": "overview",
            "portfolio_overview_only": True,
            "provider": "Gemini",
            "model": "test-model",
            "api_key": "test-key",
            "future": future,
        }
        with patch("storage.indexeddb_provider.IndexedDBStorageProvider.put") as put:
            app.run(timeout=20)
            app.run(timeout=20)

        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["portfolio_overview_analysis"], analysis)
        self.assertFalse(
            any(call.args[0] == "portfolio_analysis" for call in put.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()

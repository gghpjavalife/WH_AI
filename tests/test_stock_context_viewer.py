import unittest

from streamlit.testing.v1 import AppTest


class StockContextViewerTests(unittest.TestCase):
    def test_complete_source_is_loaded_only_when_requested(self):
        app = AppTest.from_string("""
from features.analysis.context_viewer import render_context_viewer
render_context_viewer({
    "ABC.NS": {
        "context": {"source_data": {"rows": list(range(3000))}, "profile": {"ticker": "ABC.NS"}},
        "gate": {"is_eligible_for_llm": False}, "updated_at": "now", "notice": None,
    }
}, key="example")
""").run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("json")), 0)
        app.session_state["example_context_viewer"] = True
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("json")), 1)
        self.assertIn("2999", app.get("json")[0].value)
        self.assertEqual(len(app.get("download_button")), 1)


if __name__ == "__main__":
    unittest.main()

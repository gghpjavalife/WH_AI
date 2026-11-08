import logging
import os
import tempfile
import time
import unittest
from pathlib import Path

from services.stock_context_housekeeping import ContextRotatingFileHandler, remove_retired_artifacts


class ContextHousekeepingTests(unittest.TestCase):
    def test_only_old_explicitly_retired_files_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "retired-stock-context-1.frame"
            fresh = root / "retired-stock-context-2.cache"
            unrelated = root / "user-data.cache"
            for path in (old, fresh, unrelated):
                path.write_text("data", encoding="utf-8")
            os.utime(old, (time.time() - 100, time.time() - 100))
            os.utime(unrelated, (time.time() - 100, time.time() - 100))
            self.assertEqual(remove_retired_artifacts(root, older_than_seconds=10), 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())

    def test_rollover_cleans_explicit_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "retired-stock-context-1.stream"
            old.write_text("old", encoding="utf-8")
            os.utime(old, (time.time() - 100, time.time() - 100))
            handler = ContextRotatingFileHandler(root, max_bytes=40, backup_count=2, retention_seconds=10)
            try:
                record = logging.LogRecord("test", logging.WARNING, "", 1, "x" * 45, (), None)
                handler.emit(record)
                handler.emit(record)
            finally:
                handler.close()
            self.assertFalse(old.exists())
            self.assertTrue((root / "stock-context.log.1").exists())


if __name__ == "__main__":
    unittest.main()

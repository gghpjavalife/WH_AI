import asyncio
import json
import threading
import unittest
from unittest.mock import patch

from services.stock_context_models import StockSignalContext
from services.universe_scan import _scan_universe, async_execute_universe_scan


class UniverseScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_order_duplicates_failures_and_gate(self):
        async def fetch(symbol, horizon, **kwargs):
            if symbol == "ERROR":
                raise ValueError("private provider details")
            return StockSignalContext.model_validate({
                "horizon": horizon,
                "profile": {"ticker": symbol},
                "source_data": {"rows": [1, 2, 3]},
            })

        with patch("services.universe_scan.async_fetch_and_build_market_context", fetch):
            result = await async_execute_universe_scan([
                {"symbol": "abc"}, {"symbol": "ERROR"}, {"symbol": "abc"},
            ], "example-token")
        records = [json.loads(item) for item in result]
        self.assertEqual([item["symbol"] for item in records], ["ABC", "ERROR", "ABC"])
        self.assertEqual(records[0]["context"]["source_data"]["rows"], [1, 2, 3])
        self.assertTrue(all(item["early_exit"] for item in records))
        self.assertEqual(records[1]["status"], "error")
        self.assertNotIn("private provider details", records[1]["error"])
        self.assertNotIn("example-token", "".join(result))

    async def test_all_2328_tasks_keep_order_and_progress(self):
        seen = set()

        async def fetch(symbol, horizon, **kwargs):
            seen.add(id(kwargs["semaphore"]))
            await asyncio.sleep(0)
            return StockSignalContext.model_validate({
                "horizon": horizon, "profile": {"ticker": symbol},
            })

        progress = []
        universe = [{"symbol": f"S{i}"} for i in range(2328)]
        with patch("services.universe_scan.async_fetch_and_build_market_context", fetch), \
                patch("services.stock_context_models.logger"):
            results = await _scan_universe(
                universe, "", on_progress=lambda done, total: progress.append((done, total))
            )
        self.assertEqual([row["symbol"] for row in results],
                         [item["symbol"] for item in universe])
        self.assertEqual(len(seen), 1)
        self.assertEqual(progress[-1], (2328, 2328))

    async def test_cancel_does_not_return_partial_results(self):
        started = asyncio.Event()
        cancel = threading.Event()

        async def fetch(*args, **kwargs):
            started.set()
            await asyncio.sleep(60)

        with patch("services.universe_scan.async_fetch_and_build_market_context", fetch):
            task = asyncio.create_task(_scan_universe([{"symbol": "ABC"}], "", cancel=cancel))
            await started.wait()
            cancel.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)

    async def test_unexpected_error_cancels_other_tasks(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def fetch(symbol, *args, **kwargs):
            if symbol == "ERROR":
                await started.wait()
                raise AssertionError("unexpected failure")
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        with patch("services.universe_scan.async_fetch_and_build_market_context", fetch):
            with self.assertRaises(AssertionError):
                await _scan_universe([{"symbol": "ERROR"}, {"symbol": "WAIT"}], "")
        self.assertTrue(cancelled.is_set())

    async def test_validation_happens_before_any_requests(self):
        with patch("services.universe_scan.async_fetch_and_build_market_context") as fetch:
            with self.assertRaises(ValueError):
                await async_execute_universe_scan([{"symbol": "ABC"}, {"symbol": ""}], "")
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

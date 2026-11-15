import asyncio
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services import stage1_live_pulse as stage1


class Stage1LivePulseTests(unittest.TestCase):
    def test_chunks_deduplicate_and_respect_the_batch_limit(self):
        keys = [f"NSE_EQ|ISIN{index}" for index in range(1001)]
        keys.extend([keys[0], ""])

        batches = stage1.chunk_instrument_keys(keys)

        self.assertEqual([len(batch) for batch in batches], [500, 500, 1])
        self.assertEqual(len({key for batch in batches for key in batch}), 1001)

    def test_normalization_accepts_upstox_depth_lists_and_missing_values(self):
        quote = stage1.normalize_upstox_quote(
            "NSE_EQ|INE000A01001",
            {
                "last_price": "101.5",
                "ohlc": {"open": "99", "close": "100"},
                "volume": "50",
                "average_price": "100.25",
                "depth": {
                    "buy": [{"price": "101", "quantity": "12", "orders": "2"}],
                    "sell": [{"price": "102", "quantity": "9", "orders": "1"}],
                },
                "lower_circuit_limit": "90",
                "upper_circuit_limit": "101.5",
            },
        )

        self.assertEqual(quote["instrument_key"], "NSE_EQ|INE000A01001")
        self.assertEqual(quote["best_bid"], 101)
        self.assertEqual(quote["best_ask_quantity"], 9)
        self.assertEqual(quote["bid_ask_spread_pct"], 0.9901)
        self.assertTrue(quote["is_circuit_locked"])
        self.assertIsNone(quote["high"])
        self.assertEqual(quote["oi"], 0)

    def test_live_fetch_batches_and_maps_response_tokens_to_requested_keys(self):
        requests = []

        class Response:
            status_code = 200
            headers = {}

            def __init__(self, keys):
                self.keys = keys

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": {
                        f"NSE_EQ:{key.split('|')[1]}": {
                            "instrument_token": key.replace("|", ":"),
                            "symbol": "TEST",
                            "last_price": 123.0,
                        }
                        for key in self.keys
                    }
                }

        class Client:
            def __init__(self, *, headers, timeout):
                self.headers = headers
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, *, params):
                keys = params["instrument_key"].split(",")
                requests.append(keys)
                return Response(keys)

        with patch.object(stage1.httpx, "AsyncClient", Client):
            quotes = asyncio.run(
                stage1.fetch_live_market_pulse(
                    [f"NSE_EQ|ISIN{index}" for index in range(501)],
                    "test-access-token",
                    stagger_seconds=0,
                )
            )

        self.assertEqual([len(batch) for batch in requests], [500, 1])
        self.assertEqual(len(quotes), 501)
        self.assertIn("NSE_EQ|ISIN0", quotes)
        self.assertEqual(quotes["NSE_EQ|ISIN0"]["last_price"], 123.0)

    def test_cache_is_subset_specific_and_session_poll_safe(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "stage1.sqlite3"
            session_state = {}
            quotes = {"NSE_EQ|ONE": {"last_price": 5.0}}
            fetch = AsyncMock(return_value=quotes)
            with (
                patch.object(stage1, "is_indian_market_open", return_value=True),
                patch.object(stage1, "fetch_live_market_pulse", fetch),
            ):
                first = stage1.get_stage1_pulse_cached(
                    db_path, ["NSE_EQ|ONE"], "token", session_state=session_state
                )
                second = stage1.get_stage1_pulse_cached(
                    db_path, ["NSE_EQ|ONE"], "token", session_state=session_state
                )
                other_subset = stage1.get_stage1_pulse_cached(
                    db_path, ["NSE_EQ|TWO"], "token", session_state=session_state
                )

        self.assertEqual(first["source"], "LIVE_API")
        self.assertEqual(second["source"], "SESSION_CACHE")
        self.assertEqual(other_subset["source"], "LIVE_API")
        self.assertEqual(fetch.await_count, 2)

    def test_closed_market_uses_the_one_hour_snapshot_ttl(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "stage1.sqlite3"
            cache_key, _ = stage1._cache_key(["NSE_EQ|ONE"])
            snapshot_time = stage1.time.time() - 1800
            stage1._write_snapshot(
                db_path,
                cache_key,
                {"NSE_EQ|ONE": {"last_price": 5.0}},
                snapshot_time,
            )
            with (
                patch.object(stage1, "is_indian_market_open", return_value=False),
                patch.object(
                    stage1,
                    "fetch_live_market_pulse",
                    new=AsyncMock(side_effect=AssertionError("unexpected refresh")),
                ),
            ):
                result = stage1.get_stage1_pulse_cached(
                    db_path, ["NSE_EQ|ONE"], "token"
                )

        self.assertEqual(result["source"], "CACHE")
        self.assertFalse(result["market_open"])

    def test_sqlite_lease_coalesces_concurrent_session_refreshes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "stage1.sqlite3"
            call_count = 0
            call_lock = threading.Lock()

            async def fetch(*_args):
                nonlocal call_count
                with call_lock:
                    call_count += 1
                await asyncio.sleep(0.25)
                return {"NSE_EQ|ONE": {"last_price": 5.0}}

            def load():
                return stage1.get_stage1_pulse_cached(
                    db_path, ["NSE_EQ|ONE"], "token", session_state={}
                )

            with (
                patch.object(stage1, "is_indian_market_open", return_value=True),
                patch.object(stage1, "fetch_live_market_pulse", side_effect=fetch),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                first = executor.submit(load)
                second = executor.submit(load)
                results = [first.result(), second.result()]

        self.assertEqual(call_count, 1)
        self.assertEqual(
            {result["source"] for result in results}, {"LIVE_API", "CACHE"}
        )

    def test_future_dated_snapshot_is_not_considered_fresh(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "stage1.sqlite3"
            cache_key, _ = stage1._cache_key(["NSE_EQ|ONE"])
            stage1._write_snapshot(
                db_path,
                cache_key,
                {"NSE_EQ|ONE": {"last_price": 5.0}},
                stage1.time.time() + 60,
            )
            with (
                patch.object(stage1, "is_indian_market_open", return_value=True),
                patch.object(
                    stage1,
                    "fetch_live_market_pulse",
                    new=AsyncMock(return_value={"NSE_EQ|ONE": {"last_price": 6.0}}),
                ),
            ):
                result = stage1.get_stage1_pulse_cached(
                    db_path, ["NSE_EQ|ONE"], "token"
                )

        self.assertEqual(result["source"], "LIVE_API")
        self.assertEqual(result["quotes"]["NSE_EQ|ONE"]["last_price"], 6.0)

    def test_retry_after_is_parsed_as_seconds(self):
        self.assertEqual(stage1._retry_after_seconds("3.5", 1), 3.5)
        self.assertEqual(stage1._retry_after_seconds("invalid", 2), 2)

    def test_batch_fetch_retries_429_using_retry_after(self):
        sleeps = []

        class Response:
            def __init__(self, status_code, body=None, headers=None):
                self.status_code = status_code
                self.body = body or {}
                self.headers = headers or {}

            def raise_for_status(self):
                if self.status_code >= 400:
                    import httpx

                    request = httpx.Request("GET", stage1.UPSTOX_QUOTES_URL)
                    response = httpx.Response(self.status_code, request=request)
                    raise httpx.HTTPStatusError(
                        "HTTP error", request=request, response=response
                    )

            def json(self):
                return self.body

        class Client:
            def __init__(self):
                self.responses = [
                    Response(429, headers={"Retry-After": "4"}),
                    Response(
                        200,
                        {
                            "data": {
                                "NSE_EQ:ONE": {
                                    "instrument_token": "NSE_EQ|ONE",
                                    "last_price": 5,
                                }
                            }
                        },
                    ),
                ]

            async def get(self, *_args, **_kwargs):
                return self.responses.pop(0)

        async def record_sleep(delay):
            sleeps.append(delay)

        with patch.object(stage1.asyncio, "sleep", new=record_sleep):
            result = asyncio.run(
                stage1._fetch_single_batch(
                    Client(),
                    ["NSE_EQ|ONE"],
                    asyncio.Semaphore(1),
                    {"NSE_EQ|ONE": "NSE_EQ|ONE"},
                )
            )

        self.assertEqual(sleeps, [4.0])
        self.assertEqual(result["NSE_EQ|ONE"]["last_price"], 5.0)


if __name__ == "__main__":
    unittest.main()

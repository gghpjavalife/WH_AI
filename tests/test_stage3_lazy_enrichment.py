import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from services import stage3_lazy_enrichment as stage3


IST = ZoneInfo("Asia/Kolkata")


class Stage3LazyEnrichmentTests(unittest.TestCase):
    def test_yahoo_symbol_uses_listing_exchange(self):
        self.assertEqual(stage3._to_yahoo_symbol("reliance", "NSE"), "RELIANCE.NS")
        self.assertEqual(stage3._to_yahoo_symbol("TCS.BO", "NSE"), "TCS.BO")
        self.assertEqual(stage3._to_yahoo_symbol("reliance", "BSE_EQ"), "RELIANCE.BO")

    def test_option_chain_metrics_calculate_pcr_max_pain_and_atm_greeks(self):
        rows = [
            {
                "strike_price": 90,
                "underlying_spot_price": 101,
                "call_options": {
                    "market_data": {"oi": 20, "volume": 30},
                    "option_greeks": {"iv": 10, "delta": 0.8, "gamma": 0.1},
                },
                "put_options": {
                    "market_data": {"oi": 50, "volume": 60},
                    "option_greeks": {"iv": 20, "delta": -0.2, "theta": -1, "vega": 2},
                },
            },
            {
                "strike_price": 100,
                "underlying_spot_price": 101,
                "call_options": {
                    "market_data": {"oi": 80, "volume": 90},
                    "option_greeks": {"iv": 22, "delta": 0.5, "gamma": 0.0, "theta": -2, "vega": 4},
                },
                "put_options": {
                    "market_data": {"oi": 30, "volume": 45},
                    "option_greeks": {"iv": 26, "delta": -0.5, "gamma": 0.2, "theta": -3, "vega": 5},
                },
            },
            {
                "strike_price": 110,
                "call_options": {"market_data": {"oi": 60, "volume": 75}},
                "put_options": {"market_data": {"oi": 10, "volume": 15}},
            },
        ]

        result = stage3.compute_option_chain_metrics(rows)

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["total_call_oi"], 160)
        self.assertEqual(result["total_put_oi"], 90)
        self.assertEqual(result["pcr_oi"], 0.562)
        self.assertEqual(result["pcr_volume"], round(120 / 195, 3))
        self.assertEqual(result["max_pain_strike"], 100)
        self.assertEqual(result["atm_greeks"]["atm_strike"], 100)
        self.assertEqual(result["atm_greeks"]["atm_iv"], 24)
        self.assertEqual(result["atm_greeks"]["gamma"], 0.0)

    def test_empty_chain_and_zero_call_oi_return_explicit_missing_metrics(self):
        self.assertEqual(stage3.compute_option_chain_metrics([]), {"status": "EMPTY_CHAIN"})
        result = stage3.compute_option_chain_metrics(
            [{"strike_price": 100, "call_options": {}, "put_options": {}}],
            spot_price=100,
        )
        self.assertEqual(result["status"], "OK")
        self.assertIsNone(result["pcr_oi"])
        self.assertIsNone(result["pcr_volume"])

    def test_option_context_uses_nearest_nonexpired_contract(self):
        today = datetime.now(IST).date()
        expiries = [
            (today - timedelta(days=1)).isoformat(),
            (today + timedelta(days=14)).isoformat(),
            (today + timedelta(days=7)).isoformat(),
        ]
        requests = []

        class Response:
            status_code = 200
            headers = {}

            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                return None

            def json(self):
                return {"data": self.data}

        class Client:
            def __init__(self, *, headers, timeout):
                self.headers = headers
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, *, params):
                requests.append((url, params))
                if url == stage3.UPSTOX_OPTION_CONTRACT_URL:
                    return Response([{"expiry": expiry} for expiry in expiries])
                return Response(
                    [
                        {
                            "strike_price": 100,
                            "underlying_spot_price": 101,
                            "call_options": {"market_data": {"oi": 10}},
                            "put_options": {"market_data": {"oi": 20}},
                        }
                    ]
                )

        with patch.object(stage3.httpx, "AsyncClient", Client):
            result = asyncio.run(
                stage3.fetch_upstox_option_context(
                    "NSE_EQ|INE000A01001", "access-token", spot_price=101
                )
            )

        nearest = (today + timedelta(days=7)).isoformat()
        self.assertTrue(result["is_fno_eligible"])
        self.assertEqual(result["expiry"], nearest)
        self.assertEqual(result["available_expiries"][:2], [
            nearest, (today + timedelta(days=14)).isoformat()
        ])
        self.assertEqual(requests[1][1]["expiry_date"], nearest)
        self.assertEqual(result["pcr_oi"], 2.0)

    def test_non_fno_and_rate_limited_statuses_are_not_exceptions(self):
        class Response:
            headers = {"Retry-After": "0"}

            def __init__(self, status_code):
                self.status_code = status_code

            def raise_for_status(self):
                if self.status_code >= 400:
                    import httpx

                    request = httpx.Request("GET", stage3.UPSTOX_OPTION_CONTRACT_URL)
                    response = httpx.Response(self.status_code, request=request)
                    raise httpx.HTTPStatusError(
                        "HTTP error", request=request, response=response
                    )

            def json(self):
                return {"data": []}

        class Client:
            def __init__(self, status):
                self.status = status
                self.requests = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, *_args, **_kwargs):
                self.requests += 1
                return Response(self.status)

        clients = []

        async def get_with_status(status):
            client = Client(status)
            clients.append(client)
            with patch.object(
                stage3.httpx,
                "AsyncClient",
                lambda **_kwargs: client,
            ), patch.object(stage3, "_pace_upstox_request"), patch.object(
                stage3.asyncio, "sleep", new=AsyncMock()
            ):
                return await stage3.fetch_upstox_option_context(
                    "NSE_EQ|INE000A01001", "token"
                )

        not_fno = asyncio.run(get_with_status(404))
        limited = asyncio.run(get_with_status(429))
        self.assertEqual(not_fno, {"is_fno_eligible": False, "status": "NOT_FNO"})
        self.assertEqual(limited["status"], "RATE_LIMITED")
        self.assertEqual(limited["endpoint"], "option_contract")
        self.assertEqual(clients[1].requests, stage3.UPSTOX_MAX_RETRIES + 1)

    def test_option_contract_retries_429_and_succeeds_after_retry_after(self):
        today = datetime.now(IST).date()
        requests = []

        class Response:
            def __init__(self, status_code, data=None, headers=None):
                self.status_code = status_code
                self.data = data or []
                self.headers = headers or {}

            def raise_for_status(self):
                if self.status_code >= 400:
                    import httpx

                    request = httpx.Request("GET", stage3.UPSTOX_OPTION_CONTRACT_URL)
                    response = httpx.Response(self.status_code, request=request)
                    raise httpx.HTTPStatusError(
                        "HTTP error", request=request, response=response
                    )

            def json(self):
                return {"data": self.data}

        class Client:
            def __init__(self, *, headers, timeout):
                self.headers = headers

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, *, params):
                requests.append(url)
                if len(requests) == 1:
                    return Response(429, headers={"Retry-After": "0"})
                if url == stage3.UPSTOX_OPTION_CONTRACT_URL:
                    return Response(
                        200,
                        [{"expiry": (today + timedelta(days=7)).isoformat()}],
                    )
                return Response(200, [])

        with (
            patch.object(stage3.httpx, "AsyncClient", Client),
            patch.object(stage3, "_pace_upstox_request"),
            patch.object(stage3.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            result = asyncio.run(
                stage3.fetch_upstox_option_context("NSE_EQ|INE000A01001", "token")
            )

        self.assertEqual(result["status"], "EMPTY_CHAIN")
        self.assertEqual(requests[0], stage3.UPSTOX_OPTION_CONTRACT_URL)
        self.assertEqual(requests[1], stage3.UPSTOX_OPTION_CONTRACT_URL)
        self.assertEqual(requests[2], stage3.UPSTOX_OPTION_CHAIN_URL)
        sleep.assert_awaited_once_with(0.0)

    def test_upstox_options_retries_transient_server_errors_with_backoff(self):
        responses = iter(
            [
                type(
                    "Response",
                    (),
                    {"status_code": 503, "headers": {}},
                )(),
                type(
                    "Response",
                    (),
                    {"status_code": 200, "headers": {}},
                )(),
            ]
        )

        class Client:
            async def get(self, *_args, **_kwargs):
                return next(responses)

        async def run():
            with (
                patch.object(stage3, "_pace_upstox_request"),
                patch.object(stage3.asyncio, "sleep", new=AsyncMock()) as sleep,
            ):
                response = await stage3._upstox_get_with_retries(
                    Client(),
                    stage3.UPSTOX_OPTION_CONTRACT_URL,
                    params={"instrument_key": "NSE_EQ|ABC"},
                )
            return response, sleep

        response, sleep = asyncio.run(run())
        self.assertEqual(response.status_code, 200)
        sleep.assert_awaited_once_with(stage3.UPSTOX_RETRY_BASE_SECONDS)

    def test_yahoo_sections_return_analyst_news_and_safe_summary(self):
        class FakeTicker:
            def __init__(self, ticker, asynchronous):
                self.ticker = ticker

            def get_modules(self, modules):
                return {
                    "ABC.NS": {
                        "financialData": {
                            "targetMeanPrice": {"raw": 123.5},
                            "targetHighPrice": 150,
                            "targetLowPrice": 100,
                            "recommendationKey": "buy",
                            "numberOfAnalystOpinions": 4,
                        },
                        "summaryProfile": {
                            "sector": "Technology",
                            "longBusinessSummary": "A test company",
                        },
                        "recommendationTrend": [{"period": "0m", "strongBuy": 2}],
                    }
                }

            def news(self, count):
                return [
                    {
                        "title": "Company update",
                        "publisher": "Wire",
                        "providerPublishTime": 123,
                    }
                ]

        with patch.object(stage3, "Ticker", FakeTicker):
            result = stage3._fetch_yahoo_news_and_consensus_sync("ABC.NS")

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["target_mean_price"], 123.5)
        self.assertEqual(result["analyst_count"], 4)
        self.assertEqual(result["recent_headlines"][0]["title"], "Company update")

    def test_missing_upstox_token_still_allows_yahoo_enrichment(self):
        async def no_options(*_args, **_kwargs):
            return {
                "is_fno_eligible": None,
                "status": "UNAVAILABLE",
                "error": "An Upstox access token is required.",
            }

        with (
            patch.object(
                stage3,
                "fetch_upstox_option_context",
                side_effect=no_options,
            ),
            patch.object(
                stage3,
                "_fetch_yahoo_news_and_consensus_sync",
                return_value={"status": "OK", "recent_headlines": []},
            ),
        ):
            result = asyncio.run(
                stage3.fetch_single_stock_enrichment(
                    "", "ABC", "", exchange="NSE"
                )
            )

        self.assertEqual(result["derivatives"]["status"], "UNAVAILABLE")
        self.assertEqual(result["news_and_consensus"]["status"], "OK")

    def test_yahoo_failure_keeps_successful_options_data(self):
        async def options(*_args, **_kwargs):
            return {"is_fno_eligible": True, "status": "OK", "pcr_oi": 1.2}

        def yahoo_failure(_ticker):
            raise RuntimeError("Yahoo request failed")

        with (
            patch.object(stage3, "fetch_upstox_option_context", side_effect=options),
            patch.object(
                stage3, "_fetch_yahoo_news_and_consensus_sync", side_effect=yahoo_failure
            ),
        ):
            result = asyncio.run(
                stage3.fetch_single_stock_enrichment(
                    "NSE_EQ|ABC", "ABC", "token"
                )
            )

        self.assertEqual(result["derivatives"]["status"], "OK")
        self.assertEqual(result["news_and_consensus"]["status"], "UNAVAILABLE")

    def test_sqlite_cache_avoids_refetch_and_separates_credentials(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "stage3.sqlite3"
            call_count = 0
            lock = threading.Lock()

            async def fetch(**kwargs):
                nonlocal call_count
                with lock:
                    call_count += 1
                return {
                    "instrument_key": kwargs["instrument_key"],
                    "symbol": kwargs["symbol"],
                    "fetched_at": time_now,
                    "derivatives": {"status": "NOT_FNO"},
                    "news_and_consensus": {"status": "OK"},
                }

            time_now = 1_800_000_000.0
            with (
                patch.object(stage3.time, "time", return_value=time_now),
                patch.object(stage3, "fetch_single_stock_enrichment", side_effect=fetch),
            ):
                first = stage3.get_stage3_enrichment_cached(
                    db_path, "NSE_EQ|ABC", "ABC", "token-one"
                )
                second = stage3.get_stage3_enrichment_cached(
                    db_path, "NSE_EQ|ABC", "ABC", "token-one"
                )
                other_account = stage3.get_stage3_enrichment_cached(
                    db_path, "NSE_EQ|ABC", "ABC", "token-two"
                )

        self.assertEqual(first["cache_source"], "LIVE_API")
        self.assertEqual(second["cache_source"], "CACHE")
        self.assertEqual(other_account["cache_source"], "LIVE_API")
        self.assertEqual(call_count, 2)

    def test_cache_rejects_future_timestamp_and_invalid_ttl(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "stage3.sqlite3"
            cache_key = stage3._cache_key(
                "NSE_EQ|ABC", "ABC", "NSE", "token"
            )
            stage3._write_cache(
                db_path,
                cache_key,
                "NSE_EQ|ABC",
                "ABC",
                {
                    "instrument_key": "NSE_EQ|ABC",
                    "symbol": "ABC",
                    "fetched_at": 2000,
                    "derivatives": {},
                    "news_and_consensus": {},
                },
                2000,
            )
            with patch.object(stage3.time, "time", return_value=1000):
                self.assertIsNone(stage3._read_cache(db_path, cache_key))
        with self.assertRaises(ValueError):
            stage3.get_stage3_enrichment_cached(
                instrument_key="NSE_EQ|ABC",
                symbol="ABC",
                access_token="token",
                ttl_seconds=-1,
            )

    def test_rate_limited_cache_expires_early_to_allow_recovery(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "stage3.sqlite3"
            now = [1_800_000_000.0]
            calls = []

            async def limited(**kwargs):
                calls.append(kwargs["instrument_key"])
                return {
                    "instrument_key": kwargs["instrument_key"],
                    "symbol": kwargs["symbol"],
                    "fetched_at": now[0],
                    "derivatives": {
                        "status": "RATE_LIMITED",
                        "retry_after_seconds": 1,
                    },
                    "news_and_consensus": {"status": "OK"},
                }

            with (
                patch.object(stage3.time, "time", side_effect=lambda: now[0]),
                patch.object(stage3, "fetch_single_stock_enrichment", side_effect=limited),
            ):
                first = stage3.get_stage3_enrichment_cached(
                    db_path, "NSE_EQ|ABC", "ABC", "token"
                )
                now[0] += stage3.RATE_LIMIT_CACHE_MIN_SECONDS - 1
                cached = stage3.get_stage3_enrichment_cached(
                    db_path, "NSE_EQ|ABC", "ABC", "token"
                )
                now[0] += 2
                retried = stage3.get_stage3_enrichment_cached(
                    db_path, "NSE_EQ|ABC", "ABC", "token"
                )

        self.assertEqual(first["cache_source"], "LIVE_API")
        self.assertEqual(cached["cache_source"], "CACHE")
        self.assertEqual(retried["cache_source"], "LIVE_API")
        self.assertEqual(len(calls), 2)

    def test_sqlite_lease_coalesces_concurrent_enrichment_calls(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "stage3.sqlite3"
            call_count = 0
            lock = threading.Lock()

            async def fetch(**kwargs):
                nonlocal call_count
                with lock:
                    call_count += 1
                await asyncio.sleep(0.25)
                return {
                    "instrument_key": kwargs["instrument_key"],
                    "symbol": kwargs["symbol"],
                    "fetched_at": stage3.time.time(),
                    "derivatives": {"status": "NOT_FNO"},
                    "news_and_consensus": {"status": "OK"},
                }

            def load():
                return stage3.get_stage3_enrichment_cached(
                    db_path, "NSE_EQ|ABC", "ABC", "token"
                )

            with (
                patch.object(stage3, "fetch_single_stock_enrichment", side_effect=fetch),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                first = executor.submit(load)
                second = executor.submit(load)
                results = [first.result(), second.result()]

        self.assertEqual(call_count, 1)
        self.assertEqual(
            {result["cache_source"] for result in results}, {"LIVE_API", "CACHE"}
        )


if __name__ == "__main__":
    unittest.main()

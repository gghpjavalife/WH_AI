import asyncio
import threading
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import requests

import services.stock_context as stock_context
from services.stock_context import (
    _bounded_call, _fetch_with_retries, async_bounded_network_call, async_fetch_and_build_market_context,
)
from services.stock_context_models import InvestmentHorizon
from services.stock_context_upstox import fetch_upstox_quote


KEY = "NSE_EQ|INE002A01018"
HORIZON = InvestmentHorizon.LONG_TERM_EQUITY


class FakeTicker:
    missing_price = False
    missing_volume = False
    balance_timeout = False

    def __init__(self, symbol, **kwargs):
        self.symbol = symbol

    def get_modules(self, _modules):
        return {self.symbol: {
            "price": {"currency": "INR"},
            "financialData": {"currentPrice": None if self.missing_price else 100,
                              "financialCurrency": "INR", "targetMeanPrice": 150},
            "defaultKeyStatistics": {"floatShares": 20},
        }}

    def history(self, **kwargs):
        data = pd.DataFrame({
            "high": [102.0] * 65, "low": [98.0] * 65,
            "close": [100.0] * 65, "volume": [1000.0] * 65,
        }, index=pd.date_range("2025-01-01", periods=65))
        if self.missing_volume and self.symbol == "TEST":
            data.iloc[-1, data.columns.get_loc("volume")] = float("nan")
        return data

    def balance_sheet(self, **kwargs):
        if self.balance_timeout:
            raise TimeoutError("timed out")
        return None

    def income_statement(self, **kwargs):
        return None

    def cash_flow(self, **kwargs):
        return None

    @property
    def option_chain(self):
        return None


class AsyncContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = [
            patch("services.stock_context.fetch_upstox_quote",
                  return_value={"last_price": 123, "volume": 4500, "lower_circuit_limit": 90}),
            patch("services.stock_context.fetch_upstox_news", return_value=([], [])),
            patch("services.stock_context.fetch_upstox_shareholdings", return_value={"data": []}),
            patch("services.stock_context.fetch_upstox_corporate_actions", return_value={"data": []}),
        ]
        self.mocks = [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    async def build(self, ticker=FakeTicker):
        return await async_fetch_and_build_market_context(
            "test", HORIZON, semaphore=asyncio.Semaphore(20), instrument_key=KEY,
            upstox_token="explicit-test-token", yahoo_factory=ticker,
        )

    async def test_missing_isolated_feed_does_not_replace_yahoo_quote(self):
        class MissingBalance(FakeTicker):
            balance_timeout = True

        with patch("services.stock_context.asyncio.sleep", new=MockAsync()):
            context = await self.build(MissingBalance)
        self.assertEqual(context.current_price, 100)
        self.assertEqual(context.technicals.current_volume, 1000)
        self.assertEqual(context.fundamentals.float_market_cap, 2000)
        self.assertIsNone(context.source_data["yahoo_balance_sheet"])
        self.assertTrue(any("balance sheet unavailable" in w for w in context.data_warnings))
        self.assertEqual(context.source_data["field_provenance"]["current_price"], "yahoo_quote_modules")
        self.assertEqual(context.intraday.lower_circuit_limit, 90)
        self.assertEqual(self.mocks[0].call_args.kwargs["token"], "explicit-test-token")

    async def test_selective_fallback_and_no_mixed_derived_values(self):
        class MissingBoth(FakeTicker):
            missing_price = True
            missing_volume = True

        context = await self.build(MissingBoth)
        self.assertEqual(context.current_price, 123)
        self.assertEqual(context.technicals.current_volume, 4500)
        self.assertIsNone(context.fundamentals.float_market_cap)
        self.assertIsNone(context.analyst.upside_percentage)
        self.assertEqual(context.source_data["field_provenance"]["current_price"], "upstox_quote")
        self.assertEqual(context.source_data["field_provenance"]["technicals.current_volume"], "upstox_quote")
        self.assertIsNone(context.source_data["yahoo_daily_history"]["rows"][-1][-1])

    async def test_upstox_volume_describes_session_and_retains_timestamps(self):
        class MissingVolume(FakeTicker):
            missing_volume = True
        self.mocks[0].return_value = {
            "last_price": 123, "volume": 4500,
            "timestamp": "2023-10-19T05:21:51.099+05:30",
            "last_trade_time": "1697624972130",
        }
        context = await self.build(MissingVolume)
        metadata = context.source_data["current_volume_metadata"]
        self.assertEqual(metadata["meaning"], "upstox_volume_traded_today")
        self.assertEqual(metadata["feed_update_timestamp"], "2023-10-19T05:21:51.099+05:30")
        self.assertEqual(metadata["last_trade_time_epoch_ms"], "1697624972130")
        self.assertEqual(context.technicals.avg_volume_3m, 1000)
        warning = next(w for w in context.data_warnings if "volume traded today" in w)
        for text in ("not Yahoo historical", "feed update time", "last trade time",
                     "freshness", "Historical volume indicators remain Yahoo-derived"):
            self.assertIn(text, warning)

    async def test_yahoo_volume_identifies_historical_candle_and_freshness(self):
        context = await self.build()
        metadata = context.source_data["current_volume_metadata"]
        self.assertEqual(metadata["meaning"], "latest_available_yahoo_daily_candle_volume")
        self.assertEqual(metadata["candle_index"], "2025-03-06T00:00:00")
        self.assertEqual(context.technicals.current_volume, 1000)
        self.assertTrue(any("not necessarily today's volume or a live observation" in w
                            for w in context.data_warnings))

    async def test_missing_upstox_timestamps_remain_unknown(self):
        class MissingVolume(FakeTicker):
            missing_volume = True
        context = await self.build(MissingVolume)
        metadata = context.source_data["current_volume_metadata"]
        self.assertIsNone(metadata["feed_update_timestamp"])
        self.assertIsNone(metadata["last_trade_time_epoch_ms"])
        self.assertTrue(any("timestamps may be missing or stale" in w for w in context.data_warnings))

    async def test_fallback_is_per_field_and_rejects_invalid_values(self):
        class MissingVolume(FakeTicker):
            missing_volume = True

        self.mocks[0].return_value = {"last_price": 222, "volume": 2.5}
        context = await self.build(MissingVolume)
        self.assertEqual(context.current_price, 100)
        self.assertIsNone(context.technicals.current_volume)
        self.mocks[0].return_value = {"last_price": True, "volume": -1}
        class MissingBoth(FakeTicker):
            missing_price = True
            missing_volume = True
        context = await self.build(MissingBoth)
        self.assertIsNone(context.current_price)
        self.assertIsNone(context.technicals.current_volume)

    async def test_only_missing_price_is_replaced(self):
        class MissingPrice(FakeTicker):
            missing_price = True
        context = await self.build(MissingPrice)
        self.assertEqual(context.current_price, 123)
        self.assertEqual(context.technicals.current_volume, 1000)
        self.assertEqual(context.source_data["field_provenance"]["technicals.current_volume"],
                         "yahoo_daily_history")

    async def test_yahoo_string_rate_limit_is_retried(self):
        class RateLimited(FakeTicker):
            calls = 0
            def get_modules(self, modules):
                type(self).calls += 1
                if type(self).calls == 1:
                    return {self.symbol: "Too Many Requests (429)"}
                return super().get_modules(modules)
        with patch("services.stock_context.asyncio.sleep", new=MockAsync()):
            context = await self.build(RateLimited)
        self.assertEqual(RateLimited.calls, 2)
        self.assertEqual(context.current_price, 100)

    async def test_yahoo_factory_forced_to_use_serial_session(self):
        options = []
        class Observed(FakeTicker):
            def __init__(self, symbol, **kwargs):
                options.append(kwargs)
                super().__init__(symbol, **kwargs)
        await self.build(Observed)
        self.assertTrue(options)
        self.assertTrue(all(kwargs.get("asynchronous") is False for kwargs in options))

    async def test_rejects_factory_with_concurrent_yahoo_session(self):
        class Concurrent(FakeTicker):
            def __init__(self, symbol, **kwargs):
                super().__init__(symbol, **kwargs)
                self.session = object.__new__(stock_context.FuturesSession)
        context = await self.build(Concurrent)
        self.assertEqual(context.current_price, 123)
        self.assertTrue(any("daily history unavailable" in w for w in context.data_warnings))

    async def test_unique_history_failures_do_not_substitute_other_history(self):
        class HistoryFailure(FakeTicker):
            def history(self, **kwargs):
                if self.symbol in ("^GSPC", "SECTOR"):
                    raise requests.Timeout()
                return super().history(**kwargs)
        with patch("services.stock_context.asyncio.sleep", new=MockAsync()):
            context = await async_fetch_and_build_market_context(
                "test", HORIZON, semaphore=asyncio.Semaphore(20), sector_symbol="sector",
                yahoo_factory=HistoryFailure,
            )
        self.assertIsNotNone(context.technicals.sma_50)
        self.assertIsNone(context.macro.beta_to_sp500)
        self.assertIsNone(context.macro.sector_return_5d)
        self.assertIsNone(context.source_data["yahoo_sp500_history"])
        self.assertIsNone(context.source_data["yahoo_sector_history"])
        self.assertTrue(any("S&P 500 history unavailable" in w for w in context.data_warnings))
        self.assertTrue(any("sector history unavailable" in w for w in context.data_warnings))

    async def test_wrong_symbol_history_is_warned_and_not_substituted(self):
        class WrongHistory(FakeTicker):
            def history(self, **kwargs):
                data = super().history(**kwargs)
                data["symbol"] = "OTHER"
                return data.set_index("symbol", append=True)
        context = await self.build(WrongHistory)
        self.assertIsNone(context.technicals.sma_50)
        self.assertTrue(any("does not contain TEST" in w for w in context.data_warnings))

    async def test_missing_history_and_modules_remain_unknown(self):
        class NoData(FakeTicker):
            def get_modules(self, _modules):
                raise requests.Timeout()
            def history(self, **kwargs):
                raise requests.Timeout()
        with patch("services.stock_context.asyncio.sleep", new=MockAsync()):
            context = await self.build(NoData)
        self.assertEqual(context.current_price, 123)
        self.assertEqual(context.technicals.current_volume, 4500)
        self.assertIsNone(context.technicals.sma_200)
        self.assertIsNone(context.macro.beta_to_sp500)
        self.assertIsNone(context.source_data["yahoo_daily_history"])

    async def test_upstox_timeouts_do_not_abort_context(self):
        for mock in self.mocks:
            mock.side_effect = requests.Timeout()
        with patch("services.stock_context.asyncio.sleep", new=MockAsync()):
            context = await self.build()
        self.assertEqual(context.current_price, 100)
        self.assertIsNone(context.intraday)
        self.assertTrue(any("full quote unavailable" in w for w in context.data_warnings))
        self.assertTrue(any("corporate actions unavailable" in w for w in context.data_warnings))

    async def test_empty_token_skips_all_upstox_requests(self):
        for token in ("", "  "):
            context = await async_fetch_and_build_market_context(
                "test", HORIZON, semaphore=asyncio.Semaphore(20), instrument_key=KEY,
                upstox_token=token, yahoo_factory=FakeTicker,
            )
            self.assertEqual(context.current_price, 100)
            self.assertIsNone(context.intraday)
            self.assertTrue(any("token was not supplied" in w for w in context.data_warnings))
        for mock in self.mocks:
            mock.assert_not_called()

    async def test_cancellation_keeps_permit_until_thread_finishes(self):
        started, release = threading.Event(), threading.Event()
        semaphore = asyncio.Semaphore(1)
        def blocking():
            started.set()
            release.wait(5)
        task = asyncio.create_task(_bounded_call(semaphore, blocking))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(semaphore.locked())
            waiter = asyncio.create_task(_bounded_call(semaphore, lambda: "done"))
            await asyncio.sleep(.02)
            self.assertFalse(waiter.done())
            release.set()
            self.assertEqual(await asyncio.wait_for(waiter, 2), "done")
        finally:
            release.set()

    async def test_cancelled_waiter_never_starts_network(self):
        semaphore = asyncio.Semaphore(0)
        operation = Mock()
        task = asyncio.create_task(_bounded_call(semaphore, operation))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        semaphore.release()
        await asyncio.sleep(.01)
        operation.assert_not_called()

    async def test_retry_releases_permit_and_retries_only_transient_errors(self):
        semaphore = asyncio.Semaphore(1)
        calls = []
        def operation():
            calls.append(1)
            if len(calls) == 1:
                response = requests.Response()
                response.status_code = 429
                raise requests.HTTPError(response=response)
            if len(calls) == 2:
                raise requests.Timeout()
            return "ok"
        sleeps = []
        async def sleep(delay):
            self.assertFalse(semaphore.locked())
            self.assertTrue(stock_context._PROCESS_NETWORK_LIMIT.acquire(blocking=False))
            stock_context._PROCESS_NETWORK_LIMIT.release()
            sleeps.append(delay)
        with patch("services.stock_context.asyncio.sleep", new=sleep), \
             patch("services.stock_context.random.uniform", return_value=.2):
            self.assertEqual(await _fetch_with_retries(semaphore, operation), "ok")
        self.assertEqual(sleeps, [.2, .4])
        self.assertEqual(len(calls), 3)
        with self.assertRaises(ValueError):
            await _fetch_with_retries(semaphore, lambda: (_ for _ in ()).throw(ValueError()))

    async def test_shared_semaphore_bounds_all_workers(self):
        semaphore = asyncio.Semaphore(2)
        lock = threading.Lock()
        release = threading.Event()
        two_started = threading.Event()
        active = maximum = 0
        def work():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    two_started.set()
            release.wait(3)
            with lock:
                active -= 1
        tasks = [asyncio.create_task(_bounded_call(semaphore, work)) for _ in range(8)]
        try:
            self.assertTrue(await asyncio.to_thread(two_started.wait, 2))
            await asyncio.sleep(.02)
            self.assertEqual(maximum, 2)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(maximum, 2)

    async def test_cancelled_process_cap_waiter_does_not_start(self):
        started, release = threading.Event(), threading.Event()
        operation = Mock()
        limit = threading.BoundedSemaphore(1)
        with patch.object(stock_context, "_PROCESS_NETWORK_LIMIT", limit):
            def blocking():
                started.set()
                release.wait(5)
            first = asyncio.create_task(_bounded_call(asyncio.Semaphore(1), blocking))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                waiter = asyncio.create_task(_bounded_call(asyncio.Semaphore(1), operation))
                await asyncio.sleep(.05)
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
                release.set()
                await first
                for _ in range(100):
                    if limit.acquire(blocking=False):
                        limit.release()
                        break
                    await asyncio.sleep(.01)
                operation.assert_not_called()
            finally:
                release.set()

    async def test_public_network_helper_uses_process_limit(self):
        limit = threading.BoundedSemaphore(1)
        with patch.object(stock_context, "_PROCESS_NETWORK_LIMIT", limit):
            def work():
                self.assertFalse(limit.acquire(blocking=False))
                return "ok"
            self.assertEqual(await async_bounded_network_call(asyncio.Semaphore(20), work), "ok")
            self.assertTrue(limit.acquire(blocking=False))
            limit.release()

    async def test_cancelled_worker_keeps_process_permit(self):
        started, release = threading.Event(), threading.Event()
        limit = threading.BoundedSemaphore(1)
        with patch.object(stock_context, "_PROCESS_NETWORK_LIMIT", limit):
            def blocking():
                started.set()
                release.wait(5)
            task = asyncio.create_task(_bounded_call(asyncio.Semaphore(1), blocking))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(limit.acquire(blocking=False))
                second = asyncio.create_task(_bounded_call(asyncio.Semaphore(1), lambda: "done"))
                await asyncio.sleep(.02)
                self.assertFalse(second.done())
                release.set()
                self.assertEqual(await asyncio.wait_for(second, 2), "done")
            finally:
                release.set()

    async def test_process_cap_shared_by_separate_event_loops(self):
        lock = threading.Lock()
        release = threading.Event()
        full = threading.Event()
        active = maximum = 0
        errors = []
        def work():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 20:
                    full.set()
            release.wait(5)
            with lock:
                active -= 1
        def runner():
            async def run():
                sem = asyncio.Semaphore(20)
                await asyncio.gather(*(_bounded_call(sem, work) for _ in range(20)))
            try:
                asyncio.run(run())
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=runner) for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(await asyncio.to_thread(full.wait, 3))
            await asyncio.sleep(.1)
            self.assertEqual(maximum, 20)
        finally:
            release.set()
            for thread in threads:
                await asyncio.to_thread(thread.join, 8)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(maximum, 20)

    async def test_explicit_token_is_used_and_never_uses_configuration(self):
        session = Mock()
        session.get.return_value.json.return_value = {
            "data": {"item": {"instrument_token": KEY, "last_price": 1}}
        }
        with patch("services.stock_context_upstox.configured_value", side_effect=AssertionError()):
            fetch_upstox_quote(KEY, session=session, token="specific-token")
        self.assertTrue(session.get.call_args.kwargs["headers"]["Authorization"].endswith("specific-token"))


class MockAsync:
    async def __call__(self, *_args, **_kwargs):
        pass

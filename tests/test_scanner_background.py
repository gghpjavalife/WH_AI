import asyncio
import threading
import time
import unittest
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from unittest.mock import Mock

import pandas as pd
from streamlit.testing.v1 import AppTest

from features.equities import scanner


_APP = """
import streamlit as st
from features.equities.scanner import _render_scanner
st.session_state["_run_seq"] = st.session_state.get("_run_seq", 0) + 1
if st.checkbox("Show scanner", value=True):
    _render_scanner(scope="Equities", suggested_symbols=["ABC"])
"""
_MASTER = pd.DataFrame([{"Symbol": "ABC", "Ticker": "ABC", "Company": "ABC",
                         "ISIN": "", "Exchange": "NSE"}])
_ROWS = [{"Ticker": "ABC", "Price": 100, "Recommendation": "BUY",
          "Passes_filters": True, "Signal_score": 1}]


def _universe_mock(load):
    async def scan(stocks, token, *, on_progress, cancel):
        semaphore = asyncio.Semaphore(8)
        done = 0

        async def one(stock):
            nonlocal done
            async with semaphore:
                identity = (stock["symbol"], stock["instrument_key"], stock["horizon"])
                try:
                    result = await asyncio.to_thread(load, identity, analytics_available=bool(token))
                    record = {"symbol": identity[0], "status": "ok",
                              "context": result.get("context"),
                              "local_gatekeeper": result.get("gate"), "error": result.get("error")}
                except RuntimeError as error:
                    record = {"symbol": identity[0], "status": "error",
                              "error": scanner.log_failure("Stock context ingestion", error)}
                done += 1
                on_progress(done, len(stocks))
                return record

        return await asyncio.gather(*(one(stock) for stock in stocks))
    return scan


class ScannerBackgroundTests(unittest.TestCase):
    def test_scan_and_context_survive_switching_and_collect_on_return(self):
        scan_started, scan_release = threading.Event(), threading.Event()
        context_started, context_release = threading.Event(), threading.Event()

        def scan(symbols, filters, progress, cancel):
            scan_started.set()
            if not scan_release.wait(5):
                raise RuntimeError("scan timeout")
            progress(1, 1)
            return _ROWS, {"MISSING": scanner._FAILURE_NO_DATA}

        def refresh(identity, *, analytics_available):
            context_started.set()
            if not context_release.wait(5):
                raise RuntimeError("context timeout")
            return {"context": {"profile": {"ticker": identity[0]}}}

        app = AppTest.from_string(_APP)
        with patch.object(scanner, "_load_indian_equity_master", return_value=_MASTER), \
             patch.object(scanner, "_scan_stocks", side_effect=scan) as scan_mock, \
             patch.object(scanner, "_scan_universe", side_effect=_universe_mock(refresh)), \
             patch.object(scanner, "configured_value", return_value=""), \
             patch.object(scanner, "show_scan_table") as table:
            try:
                app.run(timeout=3)
                self.assertEqual(scan_mock.call_count, 0)
                app.button(key="market_equities_scan_button").click().run(timeout=3)
                self.assertTrue(scan_started.wait(2))
                app.run(timeout=3)
                self.assertTrue(app.button(key="market_equities_scan_button").disabled)
                app.checkbox[0].uncheck().run(timeout=3)
                app.checkbox[0].check().run(timeout=3)
                self.assertEqual(scan_mock.call_count, 1)
                scan_release.set()
                self.assertTrue(context_started.wait(2))
                app.run(timeout=3)
                self.assertTrue(any("Updating local market context: 0 of 1" in x.proto.text
                                    for x in app.get("progress")))
                app.checkbox[0].uncheck().run(timeout=3)
                future = app.session_state["_market_scan_jobs"]["equities"]["future"]
                context_release.set()
                future.result(timeout=3)
                app.checkbox[0].check().run(timeout=3)
                self.assertFalse(app.exception)
                self.assertEqual(app.session_state["_market_scan_jobs"], {})
                self.assertEqual(app.session_state["market_equities_scan_results"], _ROWS)
                self.assertEqual(app.session_state.market_ai_candidates[0]["Ticker"], "ABC")
                self.assertEqual(len(app.session_state["stock_context_cache"]), 1)
                self.assertEqual(len(table.call_args.args[0]), 1)
                self.assertEqual(app.session_state["market_equities_scan_failures"],
                                 {"MISSING": scanner._FAILURE_NO_DATA})
            finally:
                scan_release.set()
                context_release.set()

    def test_sessions_are_isolated_and_failures_are_reported(self):
        started = threading.Event()
        release = threading.Event()

        def scan(symbols, filters, progress, cancel):
            started.set()
            if not release.wait(5):
                raise RuntimeError("timeout")
            raise RuntimeError("private provider details")

        first, second = AppTest.from_string(_APP), AppTest.from_string(_APP)
        with patch.object(scanner, "_load_indian_equity_master", return_value=_MASTER), \
             patch.object(scanner, "_scan_stocks", side_effect=scan) as fetch, \
             patch.object(scanner, "configured_value", return_value=""):
            try:
                first.run(timeout=3)
                second.run(timeout=3)
                first.button(key="market_equities_scan_button").click().run(timeout=3)
                self.assertTrue(started.wait(2))
                second.run(timeout=3)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(second.session_state["_market_scan_jobs"], {})
                future = first.session_state["_market_scan_jobs"]["equities"]["future"]
                release.set()
                with self.assertRaises(RuntimeError):
                    future.result(timeout=3)
                first.run(timeout=3)
                self.assertFalse(first.exception)
                self.assertTrue(any("Market scan failed" in e.value for e in first.error))
                self.assertFalse(any("private provider details" in e.value for e in first.error))
                self.assertEqual(first.session_state["_market_scan_jobs"], {})
            finally:
                release.set()

    def test_context_exception_preserves_rows_and_reports_error(self):
        progress = scanner._ScanProgress()
        with patch.object(scanner, "_scan_stocks", return_value=(_ROWS, {})), \
             patch.object(scanner, "_scan_universe", side_effect=_universe_mock(
                 lambda identity, **kwargs: (_ for _ in ()).throw(RuntimeError("bad")))):
            rows, failures, contexts, entries = scanner._run_scan_job(
                ["ABC"], scanner.OPEN_FILTERS.copy(), _MASTER, "Equities", "", {}, progress
            )
        self.assertEqual(rows, _ROWS)
        self.assertEqual(failures, {})
        self.assertIn("failed", entries[contexts["ABC"]][1]["error"])

    def test_async_service_receives_token_deduplicates_and_preserves_cache(self):
        horizon = scanner.InvestmentHorizon.LONG_TERM_EQUITY
        first = ("ABC.NS", "NSE_EQ|FIRST", horizon)
        second = ("DEF.NS", None, horizon)
        identities = {"ABC": first, "ALIAS": first, "DEF": second}
        now = datetime.now(timezone.utc)
        cache = {(*first, True): (now, {"context": {"profile": {"ticker": "cached"}}})}
        progress = scanner._ScanProgress()
        calls = []

        async def universe(stocks, token, *, on_progress, cancel):
            calls.append((stocks, token))
            on_progress(1, 1)
            return [{"symbol": "DEF.NS", "status": "ok", "early_exit": True,
                     "context": {"profile": {"ticker": "DEF.NS"}},
                     "local_gatekeeper": {"is_eligible_for_llm": False}, "error": None}]

        with patch.object(scanner, "_scan_universe", side_effect=universe):
            scanner._refresh_scan_contexts(cache, identities, "private-token", progress,
                                           threading.Event())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ([{"symbol": "DEF.NS", "instrument_key": None,
                                     "horizon": horizon}], "private-token"))
        self.assertEqual(cache[(*first, True)][1]["context"]["profile"]["ticker"], "cached")
        entry = cache[(*second, True)][1]
        self.assertFalse(entry["gate"]["is_eligible_for_llm"])
        self.assertIn("updated_at", entry)
        self.assertIn("analytics token", entry["notice"])
        self.assertEqual(progress.get(), ("context", 2, 2))
        self.assertNotIn("private-token", repr(cache))

    def test_scanner_calls_universe_service_with_real_async_runner(self):
        from services import universe_scan

        async def fetch(symbol, horizon, *, instrument_key, upstox_token, semaphore):
            self.assertEqual(upstox_token, "token-value")
            return Mock(model_dump=Mock(return_value={"profile": {"ticker": symbol}}))

        gate = Mock(is_eligible_for_llm=False,
                    model_dump=Mock(return_value={"is_eligible_for_llm": False}))
        identity = ("ABC.NS", None, scanner.InvestmentHorizon.LONG_TERM_EQUITY)
        cache = {}
        with patch.object(universe_scan, "async_fetch_and_build_market_context", fetch), \
             patch.object(universe_scan, "local_gatekeeper", return_value=gate):
            scanner._refresh_scan_contexts(cache, {"ABC": identity}, "token-value",
                                           scanner._ScanProgress(), threading.Event())
        self.assertEqual(cache[(*identity, True)][1]["gate"]["is_eligible_for_llm"], False)
        self.assertIn("context", cache[(*identity, True)][1])

    def test_large_context_phase_is_parallel_bounded_and_keeps_every_result(self):
        size = 2328
        identities = {f"S{i}": (f"S{i}.NS", None, "horizon") for i in range(size)}
        rows = [{"Ticker": symbol} for symbol in identities]
        cache_key = (*identities["S0"], False)
        cache = {cache_key: (datetime.now(timezone.utc), {"context": "cached"})}
        progress = scanner._ScanProgress()
        all_started = threading.Event()
        release_one = threading.Event()
        release_rest = threading.Event()
        lock = threading.Lock()
        active = 0
        maximum = 0
        called = []

        def load(identity, *, analytics_available):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                called.append(identity)
                if active == 8:
                    all_started.set()
            try:
                if identity == identities["S1"]:
                    if not release_one.wait(5):
                        raise RuntimeError("timeout")
                elif not release_rest.wait(5):
                    raise RuntimeError("timeout")
                if identity == identities["S2"]:
                    raise RuntimeError("provider problem")
                return {"context": identity[0]}
            finally:
                with lock:
                    active -= 1

        with patch.object(scanner, "_scan_stocks", return_value=(rows, {})), \
             patch.object(scanner, "_scanner_identities", return_value=identities), \
             patch.object(scanner, "_scan_universe", side_effect=_universe_mock(load)), \
             ThreadPoolExecutor(max_workers=1) as executor:
            try:
                job = executor.submit(scanner._run_scan_job, list(identities), {}, _MASTER,
                                      "Equities", "", cache, progress)
                self.assertTrue(all_started.wait(2))
                self.assertEqual(progress.get(), ("context", 1, size))
                release_one.set()
                deadline = time.monotonic() + 2
                while progress.get()[1] < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(progress.get(), ("context", 2, size))
                release_rest.set()
                results, failures, contexts, entries = job.result(timeout=10)
            finally:
                release_one.set()
                release_rest.set()
        self.assertEqual(maximum, 8)
        self.assertEqual(len(called), size - 1)
        self.assertEqual(len(results), size)
        self.assertEqual(len(contexts), size)
        self.assertEqual(len(entries), size)
        self.assertEqual(failures, {})
        self.assertEqual(entries[cache_key][1]["context"], "cached")
        self.assertIn("failed", entries[contexts["S2"]][1]["error"])
        self.assertEqual(progress.get(), ("context", size, size))
        future = Future()
        future.set_result((results, failures, contexts, entries))
        state = {"_market_scan_jobs": {"trading": {
            "future": future, "symbols": list(identities), "filters": {},
        }}}
        with patch.object(scanner.st, "session_state", state):
            scanner._collect_scan_job("trading", "Trading")
        self.assertEqual(len(state["market_trading_scan_results"]), size)
        self.assertEqual(len(state["market_trading_scan_context_snapshot"]), size)

    def test_context_progress_updates_on_streamlit_rerun(self):
        release = threading.Event()
        one_finished = threading.Event()
        rest_started = threading.Event()
        rows = [dict(_ROWS[0], Ticker=ticker) for ticker in ("ABC", "DEF", "GHI")]
        master = pd.DataFrame([{"Symbol": ticker, "Ticker": ticker, "Company": ticker,
                                "ISIN": "", "Exchange": "NSE"}
                               for ticker in ("ABC", "DEF", "GHI")])
        app = AppTest.from_string("""
from features.equities.scanner import _render_scanner
_render_scanner(scope="Equities", suggested_symbols=["ABC", "DEF", "GHI"])
""")

        def load(identity, *, analytics_available):
            if identity[0] == "ABC.NS":
                one_finished.set()
            else:
                rest_started.set()
                if not release.wait(5):
                    raise RuntimeError("timeout")
            return {"context": {"profile": {"ticker": identity[0]}}}

        with patch.object(scanner, "_load_indian_equity_master", return_value=master), \
             patch.object(scanner, "_scan_stocks", return_value=(rows, {})), \
             patch.object(scanner, "_scan_universe", side_effect=_universe_mock(load)), \
             patch.object(scanner, "configured_value", return_value=""), \
             patch.object(scanner, "show_scan_table"):
            try:
                app.run(timeout=3)
                app.button(key="market_equities_scan_button").click().run(timeout=3)
                self.assertTrue(one_finished.wait(2))
                self.assertTrue(rest_started.wait(2))
                job = app.session_state["_market_scan_jobs"]["equities"]
                deadline = time.monotonic() + 2
                while job["progress"].get()[1] != 1 and time.monotonic() < deadline:
                    time.sleep(0.01)
                app.run(timeout=3)
                self.assertTrue(any("1 of 3" in p.proto.text for p in app.get("progress")))
                release.set()
                job["future"].result(timeout=3)
                app.run(timeout=3)
                self.assertFalse(app.exception)
                self.assertEqual(len(app.session_state["market_equities_scan_results"]), 3)
                self.assertEqual(len(app.session_state["market_equities_scan_context_snapshot"]), 3)
            finally:
                release.set()

    def test_two_sessions_run_independent_scans(self):
        release = threading.Event()
        both_started = threading.Event()
        calls = []
        lock = threading.Lock()

        def scan(symbols, filters, progress, cancel):
            with lock:
                calls.append(threading.current_thread().name)
                if len(calls) == 2:
                    both_started.set()
            if not release.wait(5):
                raise RuntimeError("timeout")
            return _ROWS, {}

        def refresh(identity, *, analytics_available):
            return {"context": {"profile": {"ticker": identity[0]}}}

        first, second = AppTest.from_string(_APP), AppTest.from_string(_APP)
        with patch.object(scanner, "_load_indian_equity_master", return_value=_MASTER), \
             patch.object(scanner, "_scan_stocks", side_effect=scan), \
             patch.object(scanner, "_scan_universe", side_effect=_universe_mock(refresh)), \
             patch.object(scanner, "configured_value", return_value=""), \
             patch.object(scanner, "show_scan_table"):
            try:
                first.run(timeout=3)
                second.run(timeout=3)
                first.button(key="market_equities_scan_button").click().run(timeout=3)
                second.button(key="market_equities_scan_button").click().run(timeout=3)
                self.assertTrue(both_started.wait(2))
                one = first.session_state["_market_scan_jobs"]["equities"]["future"]
                two = second.session_state["_market_scan_jobs"]["equities"]["future"]
                self.assertIsNot(one, two)
                release.set()
                one.result(timeout=3)
                two.result(timeout=3)
                first.run(timeout=3)
                self.assertNotIn("stock_context_cache", second.session_state)
                second.run(timeout=3)
                self.assertEqual(len(first.session_state["stock_context_cache"]), 1)
                self.assertEqual(len(second.session_state["stock_context_cache"]), 1)
                self.assertIsNot(first.session_state["stock_context_cache"],
                                 second.session_state["stock_context_cache"])
                self.assertFalse(first.exception or second.exception)
            finally:
                release.set()

    def test_collection_does_not_replace_newer_context_from_another_scope(self):
        now = datetime.now(timezone.utc)
        key = ("ABC.NS", None, "horizon", False)
        future = Future()
        future.set_result((_ROWS, {}, {"ABC": key}, {
            key: (now - timedelta(minutes=1), {"context": "older"})
        }))
        state = {
            "_market_scan_jobs": {"trading": {
                "future": future, "symbols": ["ABC"], "filters": {},
            }},
            "stock_context_cache": {
                key: (now, {"context": "newer"}),
                ("expired",): (now - timedelta(hours=1), {}),
            },
        }
        with patch.object(scanner.st, "session_state", state):
            self.assertTrue(scanner._collect_scan_job("trading", "Trading"))
        self.assertEqual(state["stock_context_cache"][key][1]["context"], "newer")
        self.assertNotIn(("expired",), state["stock_context_cache"])
        self.assertEqual(state["market_trading_scan_results"], _ROWS)

    def test_saturated_scanner_reports_busy_without_starting_work(self):
        app = AppTest.from_string(_APP)
        with patch.object(scanner, "_load_indian_equity_master", return_value=_MASTER), \
             patch.object(scanner, "_SCAN_SLOTS") as slots, \
             patch.object(scanner, "_scan_stocks") as scan:
            slots.acquire.return_value = False
            app.run(timeout=3)
            app.button(key="market_equities_scan_button").click().run(timeout=3)
            self.assertFalse(app.exception)
            self.assertTrue(any("Scanner is busy" in error.value for error in app.error))
            self.assertEqual(app.session_state["_market_scan_jobs"], {})
            scan.assert_not_called()

    def test_cancellation_stops_both_phases_without_waiting_for_network_calls(self):
        for phase in ("scan", "context"):
            with self.subTest(phase=phase):
                started = threading.Event()
                release = threading.Event()
                calls = []
                lock = threading.Lock()
                cancel = threading.Event()
                symbols = [f"S{i}" for i in range(1000)]
                identities = {s: (f"{s}.NS", None, "horizon") for s in symbols}

                def blocked(value, **kwargs):
                    with lock:
                        calls.append(value)
                        if len(calls) == 8:
                            started.set()
                    if not release.wait(5):
                        raise RuntimeError("timeout")
                    return {"context": "done"}

                def quick_scan(symbols, filters, progress, cancel):
                    return ([{"Ticker": symbol} for symbol in symbols], {})

                async def blocked_universe(stocks, token, *, on_progress, cancel):
                    calls.extend(stocks)
                    started.set()
                    while not cancel.is_set():
                        await asyncio.sleep(0.01)
                    raise asyncio.CancelledError()

                scan_patch = (patch.object(scanner, "_scan_stocks", side_effect=quick_scan)
                              if phase == "context" else patch.object(scanner, "fetch_daily_history", side_effect=blocked))
                with scan_patch, \
                     patch.object(scanner, "_scanner_identities", return_value=identities), \
                     patch.object(scanner, "_scan_universe", side_effect=blocked_universe), \
                     patch.object(scanner, "log_failure") as log, \
                     ThreadPoolExecutor(max_workers=1) as executor:
                    try:
                        future = executor.submit(scanner._run_scan_job, symbols, {}, _MASTER,
                                                 "Trading", "", {}, scanner._ScanProgress(), cancel)
                        self.assertTrue(started.wait(3))
                        state = {"_market_scan_jobs": {"trading": {
                            "future": future, "cancel": cancel,
                        }}}
                        with patch.object(scanner.st, "session_state", state):
                            scanner.cancel_scanner_jobs()
                        self.assertNotIn("_market_scan_jobs", state)
                        with self.assertRaises(CancelledError):
                            future.result(timeout=2)
                        self.assertEqual(len(calls), 8 if phase == "scan" else scanner.SCAN_CONTEXT_BATCH_SIZE)
                        log.assert_not_called()
                    finally:
                        release.set()

    def test_cancellation_cancels_queued_jobs_without_touching_other_session(self):
        queued = Future()
        other = Future()
        state = {"_market_scan_jobs": {"equities": {
            "future": queued, "cancel": threading.Event(),
        }}}
        other_state = {"_market_scan_jobs": {"equities": {
            "future": other, "cancel": threading.Event(),
        }}}
        with patch.object(scanner.st, "session_state", state):
            scanner.cancel_scanner_jobs()
        self.assertTrue(queued.cancelled())
        self.assertFalse(other.cancelled())
        self.assertFalse(other_state["_market_scan_jobs"]["equities"]["cancel"].is_set())

    def test_unexpected_cancellation_is_reported_but_requested_is_silent(self):
        for requested in (False, True):
            with self.subTest(requested=requested):
                future = Future()
                future.cancel()
                cancel = threading.Event()
                if requested:
                    cancel.set()
                state = {"_market_scan_jobs": {"trading": {
                    "future": future, "cancel": cancel,
                }}}
                with patch.object(scanner.st, "session_state", state), \
                     patch.object(scanner, "log_failure", return_value="cancelled") as log:
                    self.assertTrue(scanner._collect_scan_job("trading", "Trading"))
                if requested:
                    log.assert_not_called()
                    self.assertNotIn("market_trading_scan_error", state)
                else:
                    log.assert_called_once()
                    self.assertEqual(state["market_trading_scan_error"], "cancelled")


if __name__ == "__main__":
    unittest.main()

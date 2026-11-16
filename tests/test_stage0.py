import gzip
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from services import stage0_bootstrap as stage0


class Stage0Tests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "stage0.sqlite3"
        self.path_patch = patch.object(stage0, "STAGE0_DB_PATH", self.database)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.addCleanup(self.temporary_directory.cleanup)

    def test_upstox_master_is_gunzipped_filtered_and_normalized(self):
        rows = [
            {
                "segment": "NSE_EQ",
                "instrument_type": "EQ",
                "instrument_key": "NSE_EQ|INE000A01001",
                "trading_symbol": "ABC",
                "name": "ABC Limited",
                "isin": "INE000A01001",
            },
            {
                "segment": "BSE_EQ",
                "instrument_type": "EQ",
                "instrument_key": "BSE_EQ|INE000A01001",
                "trading_symbol": "ABC",
                "name": "ABC Limited",
                "isin": "INE000A01001",
            },
            {
                "segment": "NSE_FO",
                "instrument_type": "EQ",
                "instrument_key": "NSE_FO|ABC",
                "trading_symbol": "ABC",
            },
            {
                "segment": "NSE_EQ",
                "instrument_type": "FUT",
                "instrument_key": "NSE_EQ|ABC-FUT",
                "trading_symbol": "ABC",
            },
        ]
        response = SimpleNamespace(
            content=gzip.compress(json.dumps(rows).encode("utf-8")),
            raise_for_status=lambda: None,
        )
        with patch.object(stage0, "_retry_after_rate_limit", side_effect=lambda op, **_: op()):
            actual = stage0.fetch_upstox_universe(get=lambda *args, **kwargs: response)

        self.assertEqual([row["symbol"] for row in actual], ["ABC", "ABC.BO"])
        self.assertEqual(actual[0]["instrument_key"], "NSE_EQ|INE000A01001")

    def test_bse_master_augments_the_shared_universe_without_duplicate_isins(self):
        upstream = [
            {
                "symbol": "ABC.BO",
                "segment": "BSE_EQ",
                "instrument_key": "BSE_EQ|INE000A01001",
                "isin": "INE000A01001",
            },
            {
                "symbol": "ABC",
                "segment": "NSE_EQ",
                "instrument_key": "NSE_EQ|INE000A01001",
                "isin": "INE000A01001",
            },
        ]
        bse_master = pd.DataFrame(
            [
                {
                    "Symbol": "ABC.BO",
                    "Ticker": "ABC",
                    "Company": "ABC Limited",
                    "ISIN": "INE000A01001",
                    "Code": "500001",
                    "Exchange": "BSE",
                },
                {
                    "Symbol": "NEWCO.BO",
                    "Ticker": "NEWCO",
                    "Company": "NewCo Limited",
                    "ISIN": "INE000A01002",
                    "Code": "500002",
                    "Exchange": "BSE",
                },
            ]
        )

        result = stage0._augment_bse_listings(upstream, bse_master)

        self.assertEqual(len(result), 3)
        added = result[-1]
        self.assertEqual(added["symbol"], "NEWCO.BO")
        self.assertEqual(added["instrument_key"], "BSE_EQ|INE000A01002")
        self.assertEqual(added["exchange_token"], "500002")
        self.assertEqual(added["segment"], "BSE_EQ")

    def test_bootstrap_persists_bse_fallback_listings(self):
        bse_master = pd.DataFrame(
            [
                {
                    "Symbol": "NEWCO.BO",
                    "Ticker": "NEWCO",
                    "Company": "NewCo Limited",
                    "ISIN": "INE000A01002",
                    "Code": "500002",
                    "Exchange": "BSE",
                }
            ]
        )
        with (
            patch.object(
                stage0,
                "fetch_upstox_universe",
                side_effect=OSError("Upstox master unavailable"),
            ),
            patch.object(stage0, "fetch_bse_equity_master", return_value=bse_master),
            patch.object(stage0, "_renew_bootstrap_lease"),
            patch.object(stage0, "fetch_macro_anchors", return_value={}),
            patch.object(stage0, "_save_bootstrap") as save,
            patch.object(stage0, "_release_bootstrap_lease"),
        ):
            stage0._refresh_bootstrap("owner")

        saved_universe = save.call_args.args[0]
        self.assertEqual(saved_universe[0]["instrument_key"], "BSE_EQ|INE000A01002")

    def test_macro_extraction_keeps_all_anchor_keys(self):
        columns = pd.MultiIndex.from_product(
            [stage0.MACRO_SYMBOLS, ["Close"]], names=["Ticker", "Price"]
        )
        frame = pd.DataFrame(
            [[100.0, 200.0, 83.0, 70.0, 2200.0]],
            columns=columns,
            index=pd.to_datetime(["2026-11-14"]),
        )
        fake_yfinance = SimpleNamespace(download=lambda **_: frame)
        with patch.dict("sys.modules", {"yfinance": fake_yfinance}):
            macro = stage0._download_macro()

        self.assertEqual(set(macro), set(stage0.MACRO_SYMBOLS))
        self.assertEqual(macro["^NSEI"]["close"], 200.0)
        self.assertEqual(macro["GC=F"]["observed_at"], "2026-11-14T00:00:00")

    def test_server_cache_persists_universe_macro_and_ttl(self):
        universe = [{"symbol": "ABC", "segment": "NSE_EQ"}]
        macro = {"^NSEI": {"close": 200.0, "observed_at": "2026-11-14"}}
        with patch.object(stage0, "enqueue_fundamentals"), patch.object(
            stage0, "start_fundamentals_worker"
        ):
            stage0._save_bootstrap(universe, macro)
        cached = stage0.read_stage0_cache()

        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached["universe"], universe)
        self.assertEqual(cached["macro"], macro)
        self.assertTrue(stage0.stage0_cache_is_fresh(cached))
        self.assertFalse(
            stage0.stage0_cache_is_fresh(cached, now=cached["updated_at"] + 43_201)
        )
        self.assertFalse(
            stage0.stage0_cache_is_fresh(cached, now=cached["updated_at"] - 1)
        )

    def test_sqlite_lease_allows_only_one_concurrent_bootstrap_refresh(self):
        first_owner = stage0._acquire_bootstrap_lease()

        self.assertIsNotNone(first_owner)
        self.assertIsNone(stage0._acquire_bootstrap_lease())
        stage0._release_bootstrap_lease(first_owner)
        self.assertIsNotNone(stage0._acquire_bootstrap_lease())

    def test_fundamentals_queue_batches_and_missing_values_are_pending(self):
        universe = [
            {"symbol": f"STOCK{i}", "instrument_key": f"NSE_EQ|ISIN{i}"}
            for i in range(20)
        ]
        self.assertEqual(stage0.enqueue_fundamentals(universe), 20)
        self.assertIsNone(stage0.get_fundamental_snapshot("STOCK0"))
        self.assertEqual(stage0.get_fundamentals_status("STOCK0"), "PENDING")

        batch = stage0._next_fundamentals_batch()
        self.assertEqual(len(batch), stage0.FUNDAMENTALS_BATCH_SIZE)
        stage0._save_fundamentals_batch(
            batch,
            {
                "STOCK0.NS": {
                    "financialData": {
                        "returnOnEquity": {"raw": 0.12},
                        "freeCashflow": {"raw": 1234},
                    },
                    "defaultKeyStatistics": {"mostRecentQuarter": 123},
                    "summaryProfile": {
                        "sector": "Financial Services",
                        "industry": "Commercial Banks",
                    },
                    "calendarEvents": {},
                }
            },
        )

        self.assertEqual(stage0.get_fundamentals_status("STOCK0"), "COMPLETED")
        self.assertEqual(
            stage0.get_fundamental_snapshot("STOCK0")["financialData"][
                "returnOnEquity"
            ]["raw"],
            0.12,
        )
        self.assertEqual(
            stage0.get_fundamental_snapshot("STOCK0")["summaryProfile"]["industry"],
            "Commercial Banks",
        )
        self.assertEqual(stage0.get_fundamentals_status("STOCK9"), "PENDING")

    def test_completed_fundamentals_are_requeued_after_their_refresh_ttl(self):
        stage0.enqueue_fundamentals([{"symbol": "ABC"}])
        batch = stage0._next_fundamentals_batch()
        stage0._save_fundamentals_batch(batch, {"ABC.NS": {}})
        connection = stage0._connect()
        try:
            connection.execute(
                "UPDATE fundamentals_queue SET last_updated = ? WHERE symbol = 'ABC'",
                (stage0.time.time() - stage0.FUNDAMENTALS_TTL_SECONDS - 1,),
            )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(stage0.enqueue_fundamentals([{"symbol": "ABC"}]), 1)
        self.assertEqual(stage0.get_fundamentals_status("ABC"), "PENDING")
        self.assertIsNone(stage0.get_fundamental_snapshot("ABC"))

    def test_rate_limited_batch_is_returned_to_resumable_queue(self):
        stage0.enqueue_fundamentals([{"symbol": "ABC"}])
        sleeps = []

        def rate_limited(_batch):
            raise RuntimeError("HTTP 429 Too Many Requests")

        with patch.object(stage0, "_fundamentals_next_wake_at", return_value=None):
            stage0._run_fundamentals_queue(
                fetch_batch=rate_limited,
                sleep=sleeps.append,
            )

        self.assertEqual(stage0.get_fundamentals_status("ABC"), "PENDING")
        self.assertIsNone(stage0.get_fundamental_snapshot("ABC"))
        self.assertEqual(sleeps, [stage0.RATE_LIMIT_PAUSE_SECONDS])
        connection = stage0._connect()
        try:
            row = connection.execute(
                "SELECT retry_after FROM fundamentals_queue WHERE symbol = 'ABC'"
            ).fetchone()
        finally:
            connection.close()
        self.assertGreater(row["retry_after"], stage0.time.time())

    def test_scanner_master_deduplicates_isin_and_prefers_nse(self):
        frame = stage0.universe_frame(
            [
                {
                    "symbol": "ABC.BO",
                    "name": "ABC Ltd",
                    "isin": "INE000A01001",
                    "segment": "BSE_EQ",
                    "instrument_key": "BSE_EQ|INE000A01001",
                },
                {
                    "symbol": "ABC",
                    "name": "ABC Ltd",
                    "isin": "INE000A01001",
                    "segment": "NSE_EQ",
                    "instrument_key": "NSE_EQ|INE000A01001",
                },
            ]
        )

        self.assertEqual(frame["Symbol"].tolist(), ["ABC"])
        self.assertEqual(frame["Exchange"].tolist(), ["NSE"])
        self.assertEqual(
            frame["Instrument_Key"].tolist(), ["NSE_EQ|INE000A01001"]
        )

    def test_browser_bridge_uses_separate_indexeddb_and_small_localstorage_keys(self):
        component_path = (
            Path(__file__).parents[1]
            / "src"
            / "ui"
            / "components"
            / "indexeddb_storage"
            / "index.html"
        )
        component = component_path.read_text(encoding="utf-8")

        self.assertIn('const STAGE0_DB_NAME = "GGHP_LocalStore"', component)
        self.assertIn('const STAGE0_STORE = "stage0_cache"', component)
        self.assertIn('localStorage.setItem("gghp_stage0_epoch"', component)
        self.assertIn('localStorage.setItem("gghp_macro_anchors"', component)
        self.assertIn("key: \"universe_master\"", component)


if __name__ == "__main__":
    unittest.main()

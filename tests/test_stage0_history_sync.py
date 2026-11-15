from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from unittest.mock import patch
from urllib.parse import quote

import pandas as pd
import pytest

from services import stage0_history_sync as history_sync


FIXED_NOW = datetime.fromisoformat("2026-11-13T18:00:00+05:30")
TARGET = date(2026, 11, 13)
NSE_KEY = "NSE_EQ|INE002A01018"
BSE_KEY = "BSE_EQ|INE002A01018"


class FakeResponse:
    def __init__(self, status_code=200, candles=None, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._candles = candles or []

    def json(self):
        return {"data": {"candles": self._candles}}


class FakeAsyncClient:
    responses = []
    requests = []
    headers_seen = None

    def __init__(self, *, headers, timeout):
        self.headers_seen = headers
        type(self).headers_seen = headers

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url):
        type(self).requests.append(url)
        if not type(self).responses:
            raise AssertionError("Unexpected history request.")
        response = type(self).responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _candle(day: date, close: int = 100):
    return [f"{day.isoformat()}T00:00:00+05:30", close - 1, close + 2, close - 2, close, 1000, 0]


def _reset_client(responses):
    FakeAsyncClient.responses = list(responses)
    FakeAsyncClient.requests = []
    FakeAsyncClient.headers_seen = None


def _noop_sleep(_delay):
    async def noop(_duration):
        return None
    return noop


def test_backfill_encodes_instrument_key_and_stores_sorted_ohlcv(tmp_path):
    candles = [_candle(TARGET - timedelta(days=1), 102), _candle(TARGET - timedelta(days=2), 101)]
    _reset_client([FakeResponse(candles=candles)])
    db_path = tmp_path / "history.sqlite3"

    with patch.object(history_sync.httpx, "AsyncClient", FakeAsyncClient):
        result = asyncio.run(
            history_sync.sync_daily_history(
                [NSE_KEY],
                "access-token",
                db_path,
                now=FIXED_NOW,
                stagger_seconds=0,
                sleep=_noop_sleep(0),
            )
        )

    assert result["synced"] == 1
    assert FakeAsyncClient.headers_seen["Authorization"] == "Bearer access-token"
    assert f"/historical-candle/{quote(NSE_KEY, safe='')}/day/" in FakeAsyncClient.requests[0]
    assert f"/{(TARGET - timedelta(days=380)).isoformat()}" in FakeAsyncClient.requests[0]
    connection = history_sync._connect(db_path)
    try:
        rows = connection.execute(
            "SELECT trade_date, close FROM daily_ohlcv_history "
            "WHERE instrument_key = ? ORDER BY trade_date",
            (NSE_KEY,),
        ).fetchall()
        meta = connection.execute(
            "SELECT last_synced_date, bar_count FROM history_sync_meta WHERE instrument_key = ?",
            (NSE_KEY,),
        ).fetchone()
    finally:
        connection.close()
    assert rows == [("2026-11-11", 101.0), ("2026-11-12", 102.0)]
    assert tuple(meta) == ("2026-11-13", 2)


def test_latest_completed_date_uses_ist_close_and_skips_weekends():
    assert history_sync.latest_completed_ist_trading_date(
        datetime.fromisoformat("2026-11-16T10:00:00+05:30")
    ) == date(2026, 11, 13)
    assert history_sync.latest_completed_ist_trading_date(
        datetime.fromisoformat("2026-11-16T15:30:00+05:30")
    ) == date(2026, 11, 16)
    assert history_sync.latest_completed_ist_trading_date(
        datetime.fromisoformat("2026-11-15T20:00:00+05:30")
    ) == date(2026, 11, 13)


def test_incremental_delta_starts_three_days_before_checkpoint(tmp_path):
    db_path = tmp_path / "history.sqlite3"
    history_sync.init_history_sync(db_path)
    connection = history_sync._connect(db_path)
    try:
        connection.executemany(
            "INSERT INTO daily_ohlcv_history VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (NSE_KEY, (TARGET - timedelta(days=index)).isoformat(), 100, 102, 99, 101, 1000)
                for index in range(history_sync.MINIMUM_HISTORY_BARS)
            ),
        )
        connection.execute(
            "INSERT INTO history_sync_meta VALUES (?, ?, ?, ?)",
            (NSE_KEY, "2026-11-10", 260, 1.0),
        )
        connection.commit()
    finally:
        connection.close()
    _reset_client([FakeResponse(candles=[_candle(TARGET, 103)])])

    with patch.object(history_sync.httpx, "AsyncClient", FakeAsyncClient):
        asyncio.run(
            history_sync.sync_daily_history(
                [NSE_KEY],
                "token",
                db_path,
                now=FIXED_NOW,
                stagger_seconds=0,
                sleep=_noop_sleep(0),
            )
        )
    assert FakeAsyncClient.requests[0].endswith("/2026-11-13/2026-11-07")


def test_fresh_checkpoint_skips_network_request(tmp_path):
    db_path = tmp_path / "history.sqlite3"
    history_sync.init_history_sync(db_path)
    connection = history_sync._connect(db_path)
    try:
        connection.execute(
            "INSERT INTO history_sync_meta VALUES (?, ?, ?, ?)",
            (NSE_KEY, TARGET.isoformat(), 1, 1.0),
        )
        connection.commit()
    finally:
        connection.close()
    _reset_client([])

    with patch.object(history_sync.httpx, "AsyncClient", FakeAsyncClient):
        result = asyncio.run(
            history_sync.sync_daily_history(
                [NSE_KEY], "token", db_path, now=FIXED_NOW
            )
        )
    assert result["skipped"] == 1
    assert FakeAsyncClient.requests == []


def test_429_honors_retry_after_and_then_succeeds(tmp_path):
    _reset_client(
        [
            FakeResponse(status_code=429, headers={"Retry-After": "0"}),
            FakeResponse(candles=[_candle(TARGET)]),
        ]
    )
    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    with patch.object(history_sync.httpx, "AsyncClient", FakeAsyncClient):
        result = asyncio.run(
            history_sync.sync_daily_history(
                [NSE_KEY],
                "token",
                tmp_path / "history.sqlite3",
                now=FIXED_NOW,
                stagger_seconds=0,
                sleep=record_sleep,
            )
        )
    assert result["synced"] == 1
    assert len(FakeAsyncClient.requests) == 2
    assert sleeps == [0.0]


def test_successful_symbol_checkpoint_is_skipped_when_resuming_partial_run(tmp_path):
    _reset_client(
        [
            FakeResponse(candles=[_candle(TARGET)]),
            FakeResponse(status_code=500),
        ]
    )
    db_path = tmp_path / "history.sqlite3"
    with (
        patch.object(history_sync.httpx, "AsyncClient", FakeAsyncClient),
        pytest.raises(history_sync.HistorySyncError) as error,
    ):
        asyncio.run(
            history_sync.sync_daily_history(
                [NSE_KEY, BSE_KEY],
                "token",
                db_path,
                now=FIXED_NOW,
                max_retries=0,
                stagger_seconds=0,
                sleep=_noop_sleep(0),
            )
        )
    assert error.value.completed == 1
    assert BSE_KEY in error.value.failures

    _reset_client([FakeResponse(candles=[_candle(TARGET, 105)])])
    with patch.object(history_sync.httpx, "AsyncClient", FakeAsyncClient):
        result = asyncio.run(
            history_sync.sync_daily_history(
                [NSE_KEY, BSE_KEY],
                "token",
                db_path,
                now=FIXED_NOW,
                max_retries=0,
                stagger_seconds=0,
                sleep=_noop_sleep(0),
            )
        )
    assert result["skipped_instrument_keys"] == [NSE_KEY]
    assert result["synced_instrument_keys"] == [BSE_KEY]
    assert len(FakeAsyncClient.requests) == 1


def test_stage2_loader_returns_latest_bars_ascending_and_expected_columns(tmp_path):
    db_path = tmp_path / "history.sqlite3"
    history_sync.init_history_sync(db_path)
    candles = [
        {
            "trade_date": (TARGET - timedelta(days=index)).isoformat(),
            "open": 100 + index,
            "high": 105 + index,
            "low": 99 + index,
            "close": 104 + index,
            "volume": 1000 + index,
        }
        for index in range(5)
    ]
    history_sync._store_symbol_history(db_path, NSE_KEY, candles, TARGET)

    frames = history_sync.load_cached_history_frames(
        str(db_path), [NSE_KEY], limit_bars=3
    )

    assert list(frames) == [NSE_KEY]
    assert isinstance(frames[NSE_KEY], pd.DataFrame)
    assert list(frames[NSE_KEY].columns) == ["open", "high", "low", "close", "volume"]
    assert frames[NSE_KEY]["close"].tolist() == [106, 105, 104]

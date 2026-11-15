"""Incremental, rate-limited daily OHLCV history synchronization for Stage 2."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from core.config import settings
from services.stage0_bootstrap import STAGE0_DB_PATH

IST = ZoneInfo("Asia/Kolkata")
HISTORY_DAYS = 380
MINIMUM_HISTORY_BARS = 260
MAX_CONCURRENT_REQUESTS = 5
REQUEST_STAGGER_SECONDS = 0.09
MAX_RETRIES = 5
LEASE_SECONDS = 300
LEASE_WAIT_SECONDS = 60
RETRY_BASE_SECONDS = 0.5


class HistorySyncError(RuntimeError):
    """Raised when one or more symbols fail while other checkpoints are retained."""

    def __init__(self, failures: Mapping[str, str], completed: int):
        self.failures = dict(failures)
        self.completed = completed
        summary = "; ".join(
            f"{instrument_key}: {error}"
            for instrument_key, error in list(self.failures.items())[:5]
        )
        super().__init__(
            f"Daily history sync completed {completed} symbol(s), but "
            f"{len(failures)} failed. {summary}"
        )


def _canonical_instrument_key(instrument_key: str) -> str:
    if not isinstance(instrument_key, str) or not instrument_key.strip():
        raise ValueError("A non-empty Upstox instrument key is required.")
    key = instrument_key.strip().upper().replace(":", "|")
    parts = key.split("|", 1)
    if len(parts) != 2 or parts[0] not in {"NSE_EQ", "BSE_EQ"} or not parts[1]:
        raise ValueError(f"Invalid NSE/BSE equity instrument key: {instrument_key!r}.")
    return key


def latest_completed_ist_trading_date(now: datetime | None = None) -> date:
    """Return the latest completed weekday candle boundary in Indian market time."""
    local_now = now or datetime.now(IST)
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=IST)
    else:
        local_now = local_now.astimezone(IST)
    result = local_now.date()
    if local_now.time().replace(tzinfo=None) < datetime_time(15, 30):
        result -= timedelta(days=1)
    while result.weekday() >= 5:
        result -= timedelta(days=1)
    return result


def _parse_trading_date(timestamp: Any) -> str:
    if isinstance(timestamp, datetime):
        parsed = timestamp
    elif isinstance(timestamp, date):
        parsed = datetime.combine(timestamp, datetime_time.min, tzinfo=IST)
    elif isinstance(timestamp, str) and timestamp.strip():
        text = timestamp.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"Invalid Upstox candle timestamp: {timestamp!r}.") from error
    else:
        raise ValueError("Upstox candle is missing a valid timestamp.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    else:
        parsed = parsed.astimezone(IST)
    return parsed.date().isoformat()


def normalize_upstox_candles(candles: Any) -> list[dict[str, Any]]:
    """Normalize newest-first Upstox rows to ascending, validated daily OHLCV."""
    if not isinstance(candles, list):
        raise ValueError("Upstox historical response must contain a candle list.")
    normalized: dict[str, dict[str, Any]] = {}
    for row in candles:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        trade_date = _parse_trading_date(row[0])
        values: list[float] = []
        valid = True
        for value in row[1:6]:
            if isinstance(value, bool):
                valid = False
                break
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                valid = False
                break
            if not math.isfinite(number):
                valid = False
                break
            values.append(number)
        if not valid:
            continue
        open_price, high_price, low_price, close_price, volume_value = values
        if (
            min(open_price, high_price, low_price, close_price) <= 0
            or low_price > min(open_price, close_price)
            or high_price < max(open_price, close_price)
            or volume_value < 0
        ):
            continue
        volume = int(volume_value)
        if volume != volume_value:
            continue
        normalized[trade_date] = {
            "trade_date": trade_date,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
        }
    return [normalized[trade_date] for trade_date in sorted(normalized)]


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_ohlcv_history (
            instrument_key TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume INTEGER NOT NULL,
            PRIMARY KEY (instrument_key, trade_date)
        );
        CREATE INDEX IF NOT EXISTS daily_ohlcv_history_date_idx
            ON daily_ohlcv_history(instrument_key, trade_date);
        CREATE TABLE IF NOT EXISTS history_sync_meta (
            instrument_key TEXT PRIMARY KEY,
            last_synced_date TEXT NOT NULL,
            bar_count INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS history_sync_leases (
            lease_key TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            expires_at REAL NOT NULL
        );
        """
    )
    return connection


def init_history_sync(db_path: str | Path = STAGE0_DB_PATH) -> None:
    """Create local OHLCV, checkpoint, and single-flight tables."""
    connection = _connect(db_path)
    connection.close()


def _read_symbol_state(
    db_path: str | Path, instrument_key: str
) -> tuple[str | None, int]:
    connection = _connect(db_path)
    try:
        meta = connection.execute(
            "SELECT last_synced_date FROM history_sync_meta WHERE instrument_key = ?",
            (instrument_key,),
        ).fetchone()
        history_state = connection.execute(
            "SELECT COUNT(*), MAX(trade_date) FROM daily_ohlcv_history "
            "WHERE instrument_key = ?",
            (instrument_key,),
        ).fetchone()
        checkpoint = str(meta[0]) if meta else (
            str(history_state[1]) if history_state[1] else None
        )
        return checkpoint, int(history_state[0])
    finally:
        connection.close()


def _acquire_lease(db_path: str | Path) -> str | None:
    owner = uuid4().hex
    now = time.time()
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM history_sync_leases WHERE lease_key = 'daily-history' "
            "AND expires_at <= ?",
            (now,),
        )
        try:
            connection.execute(
                "INSERT INTO history_sync_leases(lease_key, owner, expires_at) "
                "VALUES('daily-history', ?, ?)",
                (owner, now + LEASE_SECONDS),
            )
        except sqlite3.IntegrityError:
            connection.rollback()
            return None
        connection.commit()
        return owner
    finally:
        connection.close()


def _renew_lease(db_path: str | Path, owner: str) -> None:
    connection = _connect(db_path)
    try:
        connection.execute(
            "UPDATE history_sync_leases SET expires_at = ? "
            "WHERE lease_key = 'daily-history' AND owner = ?",
            (time.time() + LEASE_SECONDS, owner),
        )
        connection.commit()
    finally:
        connection.close()


def _release_lease(db_path: str | Path, owner: str) -> None:
    connection = _connect(db_path)
    try:
        connection.execute(
            "DELETE FROM history_sync_leases "
            "WHERE lease_key = 'daily-history' AND owner = ?",
            (owner,),
        )
        connection.commit()
    finally:
        connection.close()


def _store_symbol_history(
    db_path: str | Path,
    instrument_key: str,
    candles: Sequence[Mapping[str, Any]],
    last_synced_date: date,
) -> int:
    connection = _connect(db_path)
    try:
        with connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO daily_ohlcv_history
                    (instrument_key, trade_date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        instrument_key,
                        candle["trade_date"],
                        candle["open"],
                        candle["high"],
                        candle["low"],
                        candle["close"],
                        candle["volume"],
                    )
                    for candle in candles
                ),
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM daily_ohlcv_history WHERE instrument_key = ?",
                (instrument_key,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT OR REPLACE INTO history_sync_meta
                    (instrument_key, last_synced_date, bar_count, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (instrument_key, last_synced_date.isoformat(), int(count), time.time()),
            )
        return int(count)
    finally:
        connection.close()


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, retry_at.timestamp() - time.time())


async def _fetch_candles(
    client: httpx.AsyncClient,
    instrument_key: str,
    from_date: date,
    to_date: date,
    *,
    request_limiter: asyncio.Semaphore,
    pacing_lock: asyncio.Lock,
    next_request_at: list[float],
    stagger_seconds: float,
    max_retries: int,
    sleep: Any,
) -> list[dict[str, Any]]:
    encoded_key = quote(instrument_key, safe="")
    url = (
        f"{settings.upstox_api_base.rstrip('/')}/v2/historical-candle/"
        f"{encoded_key}/day/{to_date.isoformat()}/{from_date.isoformat()}"
    )
    for attempt in range(max_retries + 1):
        async with request_limiter:
            async with pacing_lock:
                now = time.monotonic()
                delay = max(0.0, next_request_at[0] - now)
                if delay:
                    await sleep(delay)
                next_request_at[0] = time.monotonic() + stagger_seconds
            response = await client.get(url)
        status = response.status_code
        if status == 429 or 500 <= status <= 599:
            if attempt >= max_retries:
                raise RuntimeError(
                    f"Upstox historical candle request exhausted retries "
                    f"after HTTP {status}."
                )
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            delay = retry_after if retry_after is not None else (
                RETRY_BASE_SECONDS * (2**attempt)
            )
            await sleep(delay)
            continue
        if status < 200 or status >= 300:
            raise RuntimeError(f"Upstox historical candle request failed with HTTP {status}.")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("Upstox returned invalid historical candle JSON.") from error
        if not isinstance(payload, Mapping):
            raise ValueError("Upstox historical response must be a JSON object.")
        data = payload.get("data")
        candles = data.get("candles") if isinstance(data, Mapping) else None
        if candles is None and isinstance(payload.get("candles"), list):
            candles = payload["candles"]
        return normalize_upstox_candles(candles)
    raise RuntimeError("Historical candle request ended without a response.")


async def sync_daily_history(
    instrument_keys: Sequence[str],
    access_token: str,
    db_path: str | Path = STAGE0_DB_PATH,
    *,
    now: datetime | None = None,
    max_concurrent: int = MAX_CONCURRENT_REQUESTS,
    stagger_seconds: float = REQUEST_STAGGER_SECONDS,
    max_retries: int = MAX_RETRIES,
    lease_wait_seconds: float = LEASE_WAIT_SECONDS,
    sleep: Any = asyncio.sleep,
) -> dict[str, Any]:
    """Backfill or delta-sync selected equities, checkpointing each symbol."""
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError("An Upstox access token is required for history sync.")
    if max_concurrent < 1:
        raise ValueError("Maximum concurrent history requests must be positive.")
    if stagger_seconds < 0 or max_retries < 0 or lease_wait_seconds < 0:
        raise ValueError("Pacing, retries, and lease wait must be nonnegative.")
    keys = list(dict.fromkeys(_canonical_instrument_key(key) for key in instrument_keys))
    target_date = latest_completed_ist_trading_date(now)
    if not keys:
        return {
            "target_date": target_date.isoformat(),
            "requested": 0,
            "synced": 0,
            "skipped": 0,
            "bar_counts": {},
        }
    init_history_sync(db_path)
    owner = _acquire_lease(db_path)
    if owner is None:
        deadline = time.monotonic() + lease_wait_seconds
        while time.monotonic() < deadline:
            await sleep(0.1)
            owner = _acquire_lease(db_path)
            if owner is not None:
                break
        if owner is None:
            raise TimeoutError("Timed out waiting for the daily history sync lease.")

    latest = target_date.isoformat()
    to_sync: list[tuple[str, date]] = []
    skipped: list[str] = []
    bar_counts: dict[str, int] = {}
    try:
        for key in keys:
            last_synced, count = _read_symbol_state(db_path, key)
            bar_counts[key] = count
            if last_synced == latest:
                skipped.append(key)
                continue
            if count < MINIMUM_HISTORY_BARS or last_synced is None:
                from_date = target_date - timedelta(days=HISTORY_DAYS)
            else:
                try:
                    checkpoint = date.fromisoformat(last_synced)
                except ValueError as error:
                    raise ValueError(
                        f"Invalid history checkpoint for {key}: {last_synced!r}."
                    ) from error
                from_date = checkpoint - timedelta(days=3)
            to_sync.append((key, from_date))
    except Exception:
        _release_lease(db_path, owner)
        raise

    semaphore = asyncio.Semaphore(max_concurrent)
    pacing_lock = asyncio.Lock()
    next_request_at = [0.0]
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token.strip()}",
    }
    failures: dict[str, str] = {}
    completed: list[str] = []
    try:
        async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:

            async def sync_one(key: str, from_date: date) -> None:
                try:
                    candles = await _fetch_candles(
                        client,
                        key,
                        from_date,
                        target_date,
                        request_limiter=semaphore,
                        pacing_lock=pacing_lock,
                        next_request_at=next_request_at,
                        stagger_seconds=stagger_seconds,
                        max_retries=max_retries,
                        sleep=sleep,
                    )
                    bar_counts[key] = _store_symbol_history(
                        db_path, key, candles, target_date
                    )
                    completed.append(key)
                except (httpx.HTTPError, OSError, RuntimeError, ValueError) as error:
                    failures[key] = str(error)
                finally:
                    _renew_lease(db_path, owner)

            await asyncio.gather(
                *(sync_one(key, start_date) for key, start_date in to_sync)
            )
    finally:
        _release_lease(db_path, owner)

    result = {
        "target_date": latest,
        "requested": len(keys),
        "synced": len(completed),
        "skipped": len(skipped),
        "bar_counts": bar_counts,
        "synced_instrument_keys": completed,
        "skipped_instrument_keys": skipped,
    }
    if failures:
        raise HistorySyncError(failures, len(completed))
    return result


def sync_daily_history_sync(
    instrument_keys: Sequence[str],
    access_token: str,
    db_path: str | Path = STAGE0_DB_PATH,
    **kwargs: Any,
) -> dict[str, Any]:
    """Synchronous entrypoint for explicit UI actions outside event loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            sync_daily_history(
                instrument_keys,
                access_token,
                db_path,
                **kwargs,
            )
        )
    raise RuntimeError(
        "sync_daily_history_sync cannot run inside an active event loop; await "
        "sync_daily_history instead."
    )


def load_cached_history_frames(
    db_path: str | Path,
    instrument_keys: Sequence[str],
    limit_bars: int = MINIMUM_HISTORY_BARS,
) -> dict[str, pd.DataFrame]:
    """Load latest local bars in ascending date order for the network-free Stage 2."""
    if limit_bars < 1:
        raise ValueError("History bar limit must be positive.")
    keys = list(dict.fromkeys(_canonical_instrument_key(key) for key in instrument_keys))
    if not keys:
        return {}
    connection = _connect(db_path)
    try:
        result: dict[str, pd.DataFrame] = {}
        for key in keys:
            rows = connection.execute(
                """
                SELECT open, high, low, close, volume
                FROM (
                    SELECT trade_date, open, high, low, close, volume
                    FROM daily_ohlcv_history
                    WHERE instrument_key = ?
                    ORDER BY trade_date DESC
                    LIMIT ?
                )
                ORDER BY trade_date ASC
                """,
                (key, limit_bars),
            ).fetchall()
            if rows:
                result[key] = pd.DataFrame(
                    rows,
                    columns=["open", "high", "low", "close", "volume"],
                )
        return result
    finally:
        connection.close()

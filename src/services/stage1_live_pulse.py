"""Batched Upstox live quotes with a poll-safe SQLite snapshot cache."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, MutableMapping, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from core.config import settings
from services.stage0_bootstrap import STAGE0_DB_PATH

UPSTOX_QUOTES_URL = (
    f"{settings.upstox_api_base.rstrip('/')}/v2/market-quote/quotes"
)
BATCH_SIZE = 500
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_TTL_SECONDS = 180
MARKET_CLOSED_TTL_SECONDS = 3600
LEASE_SECONDS = 300
LEASE_WAIT_SECONDS = 60


def _canonical_instrument_key(key: str) -> str:
    return key.strip().upper().replace(":", "|")


def is_indian_market_open(now_ist: datetime | None = None) -> bool:
    """Return whether the Indian cash-market session is currently open."""
    now = now_ist or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


def chunk_instrument_keys(
    instrument_keys: Sequence[str], size: int = BATCH_SIZE
) -> list[list[str]]:
    """Split unique, non-empty instrument keys into bounded request batches."""
    if size < 1:
        raise ValueError("Batch size must be positive.")
    unique_keys = list(
        dict.fromkeys(
            _canonical_instrument_key(key)
            for key in instrument_keys
            if isinstance(key, str) and key.strip()
        )
    )
    return [
        unique_keys[index : index + size]
        for index in range(0, len(unique_keys), size)
    ]


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int:
    number = _finite_float(value)
    return int(number) if number is not None else 0


def _top_level(levels: Any) -> dict[str, Any]:
    if isinstance(levels, list):
        return levels[0] if levels and isinstance(levels[0], dict) else {}
    if isinstance(levels, dict):
        return levels
    return {}


def normalize_upstox_quote(
    instrument_key: str, raw: Mapping[str, Any]
) -> dict[str, Any]:
    """Normalize one Upstox full-quote record without failing on absent fields."""
    ohlc = raw.get("ohlc")
    ohlc = ohlc if isinstance(ohlc, Mapping) else {}
    depth = raw.get("depth")
    depth = depth if isinstance(depth, Mapping) else {}
    bid = _top_level(depth.get("buy"))
    ask = _top_level(depth.get("sell"))

    best_bid = _finite_float(bid.get("price"))
    best_ask = _finite_float(ask.get("price"))
    last_price = _finite_float(raw.get("last_price"))
    spread_pct = None
    if best_bid is not None and best_ask is not None and best_bid > 0:
        spread_pct = round((best_ask - best_bid) / best_bid * 100, 4)

    lower_circuit = _finite_float(raw.get("lower_circuit_limit"))
    upper_circuit = _finite_float(raw.get("upper_circuit_limit"))
    is_circuit_locked = bool(
        last_price is not None
        and (
            (upper_circuit is not None and upper_circuit > 0 and last_price >= upper_circuit)
            or (lower_circuit is not None and lower_circuit > 0 and last_price <= lower_circuit)
        )
    )

    return {
        "instrument_key": instrument_key,
        "symbol": raw.get("symbol"),
        "last_price": last_price,
        "open": _finite_float(ohlc.get("open")),
        "high": _finite_float(ohlc.get("high")),
        "low": _finite_float(ohlc.get("low")),
        "close": _finite_float(ohlc.get("close")),
        "volume": _integer(raw.get("volume")),
        "vwap": _finite_float(raw.get("average_price")),
        "oi": _integer(raw.get("oi")),
        "best_bid": best_bid,
        "best_bid_quantity": _integer(bid.get("quantity")),
        "best_bid_orders": _integer(bid.get("orders")),
        "best_ask": best_ask,
        "best_ask_quantity": _integer(ask.get("quantity")),
        "best_ask_orders": _integer(ask.get("orders")),
        "bid_ask_spread_pct": spread_pct,
        "lower_circuit": lower_circuit,
        "upper_circuit": upper_circuit,
        "is_circuit_locked": is_circuit_locked,
        "timestamp": raw.get("timestamp") or raw.get("last_trade_time"),
    }


def _retry_after_seconds(value: str | None, fallback: float) -> float:
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return fallback


async def _fetch_single_batch(
    client: httpx.AsyncClient,
    batch_keys: list[str],
    semaphore: asyncio.Semaphore,
    requested_keys: Mapping[str, str],
    max_retries: int = 3,
) -> dict[str, dict[str, Any]]:
    params = {"instrument_key": ",".join(batch_keys)}
    async with semaphore:
        for attempt in range(max_retries):
            try:
                response = await client.get(UPSTOX_QUOTES_URL, params=params)
                if response.status_code == 429:
                    if attempt == max_retries - 1:
                        raise RuntimeError(
                            f"Upstox Stage 1 remained rate-limited after {max_retries} attempts."
                        )
                    await asyncio.sleep(
                        _retry_after_seconds(
                            response.headers.get("Retry-After"),
                            float(2 ** (attempt + 1)),
                        )
                    )
                    continue
                response.raise_for_status()
                body = response.json()
                data = body.get("data") if isinstance(body, dict) else None
                if not isinstance(data, dict):
                    raise RuntimeError("Upstox returned a malformed full-quote response.")

                normalized = {}
                for quote in data.values():
                    if not isinstance(quote, dict):
                        continue
                    token = quote.get("instrument_token")
                    if not isinstance(token, str):
                        continue
                    canonical_key = token.strip().upper().replace(":", "|")
                    output_key = requested_keys.get(canonical_key)
                    if output_key is not None:
                        normalized[output_key] = normalize_upstox_quote(output_key, quote)
                return normalized
            except httpx.RequestError as error:
                if attempt == max_retries - 1:
                    raise RuntimeError(
                        f"Upstox Stage 1 batch failed after {max_retries} attempts."
                    ) from error
                await asyncio.sleep(1.5 * (2**attempt) + random.uniform(0, 0.25))
            except httpx.HTTPStatusError as error:
                if error.response.status_code < 500 or attempt == max_retries - 1:
                    raise RuntimeError(
                        f"Upstox Stage 1 returned HTTP {error.response.status_code}."
                    ) from error
                await asyncio.sleep(1.5 * (2**attempt) + random.uniform(0, 0.25))
    raise RuntimeError("Upstox Stage 1 batch exhausted its retry attempts.")


async def fetch_live_market_pulse(
    instrument_keys: Sequence[str],
    access_token: str,
    max_concurrent_batches: int = 3,
    stagger_seconds: float = 0.25,
    max_retries: int = 3,
) -> dict[str, dict[str, Any]]:
    """Fetch full quotes in batches, bounded by concurrency and staggered starts."""
    if max_concurrent_batches < 1:
        raise ValueError("Maximum concurrent batches must be positive.")
    if stagger_seconds < 0:
        raise ValueError("Batch stagger must be nonnegative.")
    if max_retries < 1:
        raise ValueError("Maximum retries must be positive.")

    batches = chunk_instrument_keys(instrument_keys)
    if not batches:
        return {}
    token = access_token.strip() if isinstance(access_token, str) else ""
    if not token:
        raise ValueError("An Upstox access token is required for live quotes.")
    requested_keys = {
        _canonical_instrument_key(key): key
        for batch in batches
        for key in batch
    }
    semaphore = asyncio.Semaphore(max_concurrent_batches)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
        tasks = []
        for index, batch in enumerate(batches):
            if index and stagger_seconds:
                await asyncio.sleep(stagger_seconds)
            tasks.append(
                asyncio.create_task(
                    _fetch_single_batch(
                        client,
                        batch,
                        semaphore,
                        requested_keys,
                        max_retries=max_retries,
                    )
                )
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: dict[str, dict[str, Any]] = {}
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise RuntimeError(
            f"Upstox Stage 1 failed for {len(errors)} of {len(batches)} quote batches."
        ) from errors[0]
    for result in results:
        merged.update(result)
    return merged


def _database(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS stage1_pulse_cache (
            cache_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            fetched_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stage1_pulse_leases (
            cache_key TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            expires_at REAL NOT NULL
        );
        """
    )
    return connection


def init_stage1_cache(db_path: str | Path = STAGE0_DB_PATH) -> None:
    """Create the Stage 1 cache tables without fetching live data."""
    connection = _database(db_path)
    connection.close()


def _cache_key(instrument_keys: Sequence[str]) -> tuple[str, list[str]]:
    keys = list(
        dict.fromkeys(
            _canonical_instrument_key(key)
            for key in instrument_keys
            if isinstance(key, str) and key.strip()
        )
    )
    digest = hashlib.sha256("\n".join(sorted(keys)).encode("utf-8")).hexdigest()
    return digest, keys


def _read_snapshot(
    db_path: str | Path, cache_key: str
) -> dict[str, Any] | None:
    connection = _database(db_path)
    try:
        row = connection.execute(
            "SELECT payload_json, fetched_at FROM stage1_pulse_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        try:
            quotes = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(quotes, dict):
            return None
        return {"fetched_at": float(row[1]), "quotes": quotes}
    finally:
        connection.close()


def _snapshot_is_fresh(snapshot: Mapping[str, Any] | None, ttl: int) -> bool:
    if snapshot is None:
        return False
    age = time.time() - float(snapshot["fetched_at"])
    return 0 <= age < ttl


def _snapshot_is_not_future(snapshot: Mapping[str, Any] | None) -> bool:
    if snapshot is None:
        return False
    fetched_at = float(snapshot["fetched_at"])
    return math.isfinite(fetched_at) and fetched_at <= time.time()


def _acquire_lease(db_path: str | Path, cache_key: str) -> str | None:
    now = time.time()
    owner = uuid4().hex
    connection = _database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM stage1_pulse_leases WHERE cache_key = ? AND expires_at <= ?",
            (cache_key, now),
        )
        try:
            connection.execute(
                "INSERT INTO stage1_pulse_leases(cache_key, owner, expires_at) "
                "VALUES(?, ?, ?)",
                (cache_key, owner, now + LEASE_SECONDS),
            )
        except sqlite3.IntegrityError:
            connection.rollback()
            return None
        connection.commit()
        return owner
    finally:
        connection.close()


def _release_lease(db_path: str | Path, cache_key: str, owner: str) -> None:
    connection = _database(db_path)
    try:
        connection.execute(
            "DELETE FROM stage1_pulse_leases WHERE cache_key = ? AND owner = ?",
            (cache_key, owner),
        )
        connection.commit()
    finally:
        connection.close()


def _write_snapshot(
    db_path: str | Path,
    cache_key: str,
    quotes: Mapping[str, Any],
    fetched_at: float,
) -> None:
    connection = _database(db_path)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO stage1_pulse_cache(cache_key, payload_json, fetched_at) "
            "VALUES(?, ?, ?)",
            (cache_key, json.dumps(quotes, separators=(",", ":"), allow_nan=False), fetched_at),
        )
        connection.commit()
    finally:
        connection.close()


def _session_state(
    supplied: MutableMapping[str, Any] | None,
) -> MutableMapping[str, Any] | None:
    if supplied is not None:
        return supplied
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        import streamlit as st

        if get_script_run_ctx(suppress_warning=True) is not None:
            return st.session_state
    except ImportError:
        return None
    return None


def get_stage1_pulse_cached(
    db_path: str | Path = STAGE0_DB_PATH,
    instrument_keys: Sequence[str] = (),
    access_token: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    *,
    session_state: MutableMapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fresh browser-session/SQLite snapshot or fetch a new live pulse."""
    if ttl_seconds < 0:
        raise ValueError("Cache TTL must be nonnegative.")
    cache_key, keys = _cache_key(instrument_keys)
    market_open = is_indian_market_open()
    effective_ttl = (
        ttl_seconds if market_open else max(ttl_seconds, MARKET_CLOSED_TTL_SECONDS)
    )
    state = _session_state(session_state)
    session_key = f"_stage1_live_pulse_{cache_key}"

    if not keys:
        return {
            "source": "EMPTY",
            "fetched_at": time.time(),
            "market_open": market_open,
            "quotes": {},
        }

    if state is not None:
        session_snapshot = state.get(session_key)
        if isinstance(session_snapshot, dict) and _snapshot_is_fresh(
            session_snapshot, effective_ttl
        ):
            return {
                "source": "SESSION_CACHE",
                "fetched_at": session_snapshot["fetched_at"],
                "market_open": market_open,
                "quotes": session_snapshot["quotes"],
            }

    cached = _read_snapshot(db_path, cache_key)
    if _snapshot_is_fresh(cached, effective_ttl):
        if state is not None and cached is not None:
            state[session_key] = cached
        return {
            "source": "CACHE",
            "fetched_at": cached["fetched_at"],
            "market_open": market_open,
            "quotes": cached["quotes"],
        }

    owner = _acquire_lease(db_path, cache_key)
    if owner is None:
        deadline = time.monotonic() + LEASE_WAIT_SECONDS
        while time.monotonic() < deadline:
            cached = _read_snapshot(db_path, cache_key)
            if _snapshot_is_fresh(cached, effective_ttl):
                if state is not None and cached is not None:
                    state[session_key] = cached
                return {
                    "source": "CACHE",
                    "fetched_at": cached["fetched_at"],
                    "market_open": market_open,
                    "quotes": cached["quotes"],
                }
            owner = _acquire_lease(db_path, cache_key)
            if owner is not None:
                break
            time.sleep(0.1)
        if owner is None:
            raise TimeoutError("Timed out waiting for the Stage 1 quote refresh.")

    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "get_stage1_pulse_cached must be called outside an active event loop."
            )
        try:
            quotes = asyncio.run(fetch_live_market_pulse(keys, access_token))
        except (RuntimeError, ValueError) as error:
            if (
                not market_open
                and cached is not None
                and _snapshot_is_not_future(cached)
            ):
                fallback = {
                    **cached,
                    "source": "STALE_CACHE",
                    "market_open": False,
                    "refresh_error": str(error),
                }
                if state is not None:
                    state[session_key] = cached
                return fallback
            raise
        fetched_at = time.time()
        _write_snapshot(db_path, cache_key, quotes, fetched_at)
        snapshot = {"fetched_at": fetched_at, "quotes": quotes}
        if state is not None:
            state[session_key] = snapshot
        return {
            "source": "LIVE_API",
            "fetched_at": fetched_at,
            "market_open": market_open,
            "quotes": quotes,
        }
    finally:
        _release_lease(db_path, cache_key, owner)

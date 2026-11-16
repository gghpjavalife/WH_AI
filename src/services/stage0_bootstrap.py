"""Local-first Stage 0 bootstrap data and resumable fundamentals hydration."""

from __future__ import annotations

import gzip
import json
import logging
import os
import random
import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pandas as pd
import requests

from core.constants import ProjectPath
from features.equities.market_data import MarketDataError, fetch_bse_equity_master
from services.stock_context_sources import json_value

logger = logging.getLogger(__name__)

UPSTOX_MASTER_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)
MACRO_SYMBOLS = ("^INDIAVIX", "^NSEI", "INR=X", "BZ=F", "GC=F")
STAGE0_TTL_SECONDS = 12 * 60 * 60
FUNDAMENTALS_TTL_SECONDS = 7 * 24 * 60 * 60
FUNDAMENTALS_BATCH_SIZE = 15
FUNDAMENTALS_BATCH_DELAY = (2.5, 4.5)
RATE_LIMIT_PAUSE_SECONDS = 60
BOOTSTRAP_LEASE_SECONDS = 600
STAGE0_DB_PATH = Path(
    os.environ.get(
        "STAGE0_CACHE_PATH",
        str(ProjectPath.ROOT.value / ".stage0-cache.sqlite3"),
    )
).expanduser()

_bootstrap_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stage0-bootstrap")
_fundamentals_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stage0-fundamentals")
_bootstrap_lock = threading.Lock()
_bootstrap_future: Future | None = None
_fundamentals_lock = threading.Lock()
_fundamentals_future: Future | None = None


def _connect() -> sqlite3.Connection:
    STAGE0_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STAGE0_DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS session_kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stage0_cache (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fundamentals_queue (
            symbol TEXT PRIMARY KEY,
            instrument_key TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            retry_after REAL,
            last_updated REAL,
            data_json TEXT,
            error TEXT,
            lease_until REAL
        );
        CREATE INDEX IF NOT EXISTS fundamentals_pending_idx
            ON fundamentals_queue(status, retry_after, symbol);
        """
    )
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(fundamentals_queue)")
    }
    if "lease_until" not in columns:
        connection.execute("ALTER TABLE fundamentals_queue ADD COLUMN lease_until REAL")
    connection.commit()
    return connection


def _read_cache(connection: sqlite3.Connection, key: str) -> tuple[Any, float] | None:
    row = connection.execute(
        "SELECT payload, updated_at FROM stage0_cache WHERE cache_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["payload"]), float(row["updated_at"])


def read_stage0_cache() -> dict[str, Any] | None:
    """Return the shared server snapshot when both required bootstrap datasets exist."""
    connection = _connect()
    try:
        universe = _read_cache(connection, "universe_master")
        macro = _read_cache(connection, "macro_anchors")
        if universe is None or macro is None:
            return None
        epoch_row = connection.execute(
            "SELECT value FROM session_kv WHERE key = 'stage0_epoch'"
        ).fetchone()
        epoch = int(epoch_row["value"]) if epoch_row else int(min(universe[1], macro[1]))
        return {
            "server_epoch": epoch,
            "updated_at": min(universe[1], macro[1]),
            "universe_updated_at": universe[1],
            "macro_updated_at": macro[1],
            "universe": universe[0],
            "macro": macro[0],
        }
    finally:
        connection.close()


def stage0_cache_is_fresh(snapshot: dict[str, Any] | None, *, now: float | None = None) -> bool:
    if not snapshot:
        return False
    age = (time.time() if now is None else now) - float(snapshot["updated_at"])
    return 0 <= age < STAGE0_TTL_SECONDS


def _acquire_bootstrap_lease() -> str | None:
    owner = uuid4().hex
    now = time.time()
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT value FROM session_kv WHERE key = 'stage0_bootstrap_lease'"
        ).fetchone()
        if row:
            lease = json.loads(row["value"])
            if float(lease.get("expires_at", 0)) > now:
                connection.rollback()
                return None
        connection.execute(
            "INSERT OR REPLACE INTO session_kv(key, value) VALUES(?, ?)",
            (
                "stage0_bootstrap_lease",
                json.dumps({"owner": owner, "expires_at": now + BOOTSTRAP_LEASE_SECONDS}),
            ),
        )
        connection.commit()
        return owner
    finally:
        connection.close()


def _renew_bootstrap_lease(owner: str) -> None:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT value FROM session_kv WHERE key = 'stage0_bootstrap_lease'"
        ).fetchone()
        if row and json.loads(row["value"]).get("owner") == owner:
            connection.execute(
                "UPDATE session_kv SET value = ? WHERE key = 'stage0_bootstrap_lease'",
                (
                    json.dumps(
                        {
                            "owner": owner,
                            "expires_at": time.time() + BOOTSTRAP_LEASE_SECONDS,
                        }
                    ),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _release_bootstrap_lease(owner: str) -> None:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT value FROM session_kv WHERE key = 'stage0_bootstrap_lease'"
        ).fetchone()
        if row and json.loads(row["value"]).get("owner") == owner:
            connection.execute(
                "DELETE FROM session_kv WHERE key = 'stage0_bootstrap_lease'"
            )
            connection.commit()
    finally:
        connection.close()


def _http_status(error: BaseException) -> int | None:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return int(status)
    for arg in getattr(error, "args", ()):
        status = getattr(arg, "status_code", None)
        if status is not None:
            return int(status)
    if "429" in str(error) or "too many requests" in str(error).lower():
        return 429
    return None


def _retry_after_rate_limit(operation: Callable[[], Any], *, label: str) -> Any:
    for attempt in range(3):
        try:
            return operation()
        except Exception as error:
            if _http_status(error) != 429 or attempt == 2:
                raise
            logger.warning("%s was rate limited; retrying after 60 seconds.", label)
            time.sleep(RATE_LIMIT_PAUSE_SECONDS)


def fetch_upstox_universe(
    *,
    get: Callable[..., Any] = requests.get,
) -> list[dict[str, Any]]:
    """Download and normalize the compressed Upstox EQ instrument master."""
    response = _retry_after_rate_limit(
        lambda: get(
            UPSTOX_MASTER_URL,
            headers={"Accept": "application/gzip, application/json"},
            timeout=(5, 30),
        ),
        label="Upstox instrument master",
    )
    response.raise_for_status()
    content = response.content
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    try:
        rows = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Upstox returned an invalid instrument master.") from error
    if not isinstance(rows, list):
        raise ValueError("Upstox instrument master must be a JSON array.")

    universe = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        segment = str(row.get("segment", "")).strip().upper()
        if segment not in {"NSE_EQ", "BSE_EQ"}:
            continue
        if str(row.get("instrument_type", "")).strip().upper() != "EQ":
            continue
        instrument_key = row.get("instrument_key")
        trading_symbol = row.get("trading_symbol")
        if not isinstance(instrument_key, str) or not instrument_key.strip():
            continue
        if not isinstance(trading_symbol, str) or not trading_symbol.strip():
            continue
        symbol = trading_symbol.strip().upper()
        if segment == "BSE_EQ" and not symbol.endswith(".BO"):
            symbol += ".BO"
        universe.append(
            {
                "symbol": symbol,
                "trading_symbol": trading_symbol.strip().upper(),
                "name": str(row.get("name") or trading_symbol).strip(),
                "isin": str(row.get("isin") or "").strip().upper(),
                "segment": segment,
                "instrument_key": instrument_key.strip(),
                "exchange_token": str(row.get("exchange_token") or ""),
                "lot_size": row.get("lot_size"),
                "tick_size": row.get("tick_size"),
            }
        )
    if not universe:
        raise ValueError("Upstox instrument master contains no NSE/BSE equity rows.")
    return universe


def _augment_bse_listings(
    universe: list[dict[str, Any]],
    bse_master: pd.DataFrame,
) -> list[dict[str, Any]]:
    """Include active BSE equities absent from the broker instrument master."""
    known_bse_isins = {
        str(item.get("isin") or "").strip().upper()
        for item in universe
        if str(item.get("segment") or "").strip().upper() == "BSE_EQ"
    }
    known_bse_keys = {
        str(item.get("instrument_key") or "").strip().upper()
        for item in universe
        if str(item.get("segment") or "").strip().upper() == "BSE_EQ"
    }
    additions = []
    for row in bse_master.to_dict(orient="records"):
        if str(row.get("Exchange") or "").strip().upper() != "BSE":
            continue
        isin = str(row.get("ISIN") or "").strip().upper()
        ticker = str(row.get("Ticker") or row.get("Symbol") or "").strip().upper()
        ticker = ticker.removesuffix(".BO")
        if not isin or not ticker or ticker in {"NAN", "NONE"}:
            continue
        instrument_key = f"BSE_EQ|{isin}"
        if isin in known_bse_isins or instrument_key in known_bse_keys:
            continue
        additions.append(
            {
                "symbol": f"{ticker}.BO",
                "trading_symbol": ticker,
                "name": str(row.get("Company") or ticker).strip(),
                "isin": isin,
                "segment": "BSE_EQ",
                "instrument_type": "EQ",
                "instrument_key": instrument_key,
                "exchange_token": str(row.get("Code") or ""),
                "lot_size": 1,
                "tick_size": None,
            }
        )
        known_bse_isins.add(isin)
        known_bse_keys.add(instrument_key)
    return [*universe, *additions]


def _download_macro() -> dict[str, dict[str, Any]]:
    import yfinance as yf

    frame = yf.download(
        tickers=list(MACRO_SYMBOLS),
        period="1y",
        interval="1d",
        group_by="ticker",
        progress=False,
        threads=False,
        auto_adjust=False,
    )
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("Yahoo Finance returned no macro anchor history.")
    output: dict[str, dict[str, Any]] = {}
    for symbol in MACRO_SYMBOLS:
        try:
            if isinstance(frame.columns, pd.MultiIndex):
                symbol_frame = frame[symbol]
            else:
                symbol_frame = frame
            if "Close" not in symbol_frame:
                continue
            close = pd.to_numeric(symbol_frame["Close"], errors="coerce").dropna()
            if close.empty:
                continue
            observed_at = close.index[-1]
            if hasattr(observed_at, "isoformat"):
                observed_at = observed_at.isoformat()
            anchor = {
                "close": float(close.iloc[-1]),
                "observed_at": str(observed_at),
            }
            if symbol == "^NSEI":
                dma_200 = close.rolling(window=200, min_periods=200).mean().iloc[-1]
                anchor["dma_200"] = (
                    float(dma_200) if pd.notna(dma_200) else None
                )
            output[symbol] = anchor
        except (KeyError, TypeError, ValueError, IndexError):
            output[symbol] = {"close": None, "observed_at": None}
    if not any(anchor["close"] is not None for anchor in output.values()):
        raise ValueError("Yahoo Finance returned no valid macro anchor closes.")
    for symbol in MACRO_SYMBOLS:
        output.setdefault(
            symbol,
            {
                "close": None,
                "observed_at": None,
                **({"dma_200": None} if symbol == "^NSEI" else {}),
            },
        )
    return output


def fetch_macro_anchors() -> dict[str, dict[str, Any]]:
    return _retry_after_rate_limit(_download_macro, label="Yahoo macro anchors")


def _save_bootstrap(
    universe: list[dict[str, Any]],
    macro: dict[str, Any],
    *,
    macro_updated_at: float | None = None,
) -> None:
    now = time.time()
    macro_updated_at = macro_updated_at if macro_updated_at is not None else now
    server_epoch = int(min(now, macro_updated_at))
    connection = _connect()
    try:
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO stage0_cache(cache_key, payload, updated_at) VALUES(?, ?, ?)",
                ("universe_master", json.dumps(universe, separators=(",", ":")), now),
            )
            connection.execute(
                "INSERT OR REPLACE INTO stage0_cache(cache_key, payload, updated_at) VALUES(?, ?, ?)",
                (
                    "macro_anchors",
                    json.dumps(macro, separators=(",", ":")),
                    macro_updated_at,
                ),
            )
            connection.execute(
                "INSERT OR REPLACE INTO session_kv(key, value) VALUES('stage0_epoch', ?)",
                (str(server_epoch),),
            )
    finally:
        connection.close()
    enqueue_fundamentals(universe)
    start_fundamentals_worker()


def _refresh_bootstrap(owner: str) -> None:
    try:
        try:
            universe = fetch_upstox_universe()
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            logger.warning(
                "Upstox instrument master unavailable; trying the BSE listing feed: %s",
                error,
            )
            universe = []
        try:
            bse_master = fetch_bse_equity_master()
        except MarketDataError as error:
            if not universe:
                raise RuntimeError(
                    "Neither the Upstox instrument master nor the BSE listing feed "
                    "is available."
                ) from error
            logger.warning(
                "BSE listing feed unavailable; retaining Upstox BSE listings: %s",
                error,
            )
        else:
            universe = _augment_bse_listings(universe, bse_master)
        if not universe:
            raise RuntimeError("The exchange feeds returned no equity listings.")
        _renew_bootstrap_lease(owner)
        macro_updated_at = None
        try:
            macro = fetch_macro_anchors()
        except Exception:
            logger.exception("Stage 0 macro-anchor refresh failed.")
            cached = read_stage0_cache()
            if not cached:
                raise
            macro = cached["macro"]
            macro_updated_at = cached["macro_updated_at"]
        _save_bootstrap(universe, macro, macro_updated_at=macro_updated_at)
    finally:
        _release_bootstrap_lease(owner)


def request_stage0_refresh(*, force: bool = False) -> Future | None:
    """Schedule one shared refresh; stale cached data remains available to sessions."""
    global _bootstrap_future
    snapshot = read_stage0_cache()
    if not force and stage0_cache_is_fresh(snapshot):
        return None
    with _bootstrap_lock:
        if _bootstrap_future is not None and not _bootstrap_future.done():
            return _bootstrap_future
        owner = _acquire_bootstrap_lease()
        if owner is None:
            return _bootstrap_future
        _bootstrap_future = _bootstrap_executor.submit(_refresh_bootstrap, owner)
        _bootstrap_future.add_done_callback(_log_bootstrap_result)
        return _bootstrap_future


def _log_bootstrap_result(future: Future) -> None:
    error = future.exception()
    if error is not None:
        logger.error(
            "Stage 0 bootstrap refresh failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


def _extract_fundamental_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    fields = {
        "financialData": (
            "currentPrice",
            "targetMeanPrice",
            "totalRevenue",
            "grossMargins",
            "operatingMargins",
            "profitMargins",
            "returnOnEquity",
            "returnOnAssets",
            "revenueGrowth",
            "earningsGrowth",
            "operatingCashflow",
            "capitalAdequacyRatio",
            "freeCashflow",
            "totalCash",
            "totalDebt",
            "debtToEquity",
        ),
        "defaultKeyStatistics": (
            "trailingEps",
            "trailingPE",
            "forwardEps",
            "forwardPE",
            "pegRatio",
            "priceToBook",
            "enterpriseToEbitda",
            "mostRecentQuarter",
            "sharesOutstanding",
            "floatShares",
            "shortPercentOfFloat",
            "piotroskiFScore",
        ),
        "summaryProfile": ("sector", "industry", "longBusinessSummary"),
        "calendarEvents": ("exDividendDate", "earnings"),
    }
    extracted: dict[str, Any] = {}
    for module_name, names in fields.items():
        module = value.get(module_name)
        if not isinstance(module, dict):
            extracted[module_name] = {}
            continue
        extracted[module_name] = {
            name: module[name]
            for name in names
            if name in module
        }
    return extracted


def enqueue_fundamentals(universe: list[dict[str, Any]]) -> int:
    """Persist symbols to the resumable queue without materializing another symbol list."""
    now = time.time()
    connection = _connect()
    try:
        with connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO fundamentals_queue(symbol, instrument_key)
                VALUES(?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    instrument_key = excluded.instrument_key,
                    status = 'PENDING',
                    retry_after = NULL,
                    error = NULL
                WHERE fundamentals_queue.status = 'COMPLETED'
                  AND (fundamentals_queue.last_updated IS NULL
                       OR fundamentals_queue.last_updated <= ?)
                """,
                (
                    (row["symbol"], row.get("instrument_key"), now - FUNDAMENTALS_TTL_SECONDS)
                    for row in universe
                    if isinstance(row, dict) and isinstance(row.get("symbol"), str)
                ),
            )
            return connection.total_changes - before
    finally:
        connection.close()


def _next_fundamentals_batch() -> list[tuple[str, str | None]]:
    now = time.time()
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT symbol, instrument_key FROM fundamentals_queue
            WHERE (status = 'PENDING' AND (retry_after IS NULL OR retry_after <= ?))
               OR (status = 'IN_PROGRESS' AND lease_until <= ?)
            ORDER BY symbol LIMIT ?
            """,
            (now, now, FUNDAMENTALS_BATCH_SIZE),
        ).fetchall()
        if rows:
            connection.executemany(
                """
                UPDATE fundamentals_queue SET status = 'IN_PROGRESS', lease_until = ?
                WHERE symbol = ?
                """,
                ((now + 300, row["symbol"]) for row in rows),
            )
        connection.commit()
        return [(row["symbol"], row["instrument_key"]) for row in rows]
    finally:
        connection.close()


def _fundamentals_next_wake_at() -> float | None:
    connection = _connect()
    try:
        row = connection.execute(
            """
            SELECT MIN(next_at) AS next_at FROM (
                SELECT lease_until AS next_at FROM fundamentals_queue
                WHERE status = 'IN_PROGRESS' AND lease_until IS NOT NULL
                UNION ALL
                SELECT retry_after AS next_at FROM fundamentals_queue
                WHERE status = 'PENDING' AND retry_after IS NOT NULL
            )
            """
        ).fetchone()
        return float(row["next_at"]) if row and row["next_at"] is not None else None
    finally:
        connection.close()


def _mark_fundamentals_retry(batch: list[tuple[str, str | None]], error: str, delay: float) -> None:
    connection = _connect()
    try:
        with connection:
            connection.executemany(
                """
                UPDATE fundamentals_queue SET status = 'PENDING',
                    attempts = attempts + 1, retry_after = ?, error = ?, lease_until = NULL
                WHERE symbol = ?
                """,
                ((time.time() + delay, error[:500], symbol) for symbol, _ in batch),
            )
    finally:
        connection.close()


def _save_fundamentals_batch(
    batch: list[tuple[str, str | None]], results: Any
) -> None:
    now = time.time()
    connection = _connect()
    try:
        with connection:
            for symbol, _ in batch:
                yahoo_symbol = (
                    symbol if symbol.endswith((".NS", ".BO")) else f"{symbol}.NS"
                )
                value = results.get(yahoo_symbol) if isinstance(results, dict) else None
                if value is None and isinstance(results, dict):
                    value = results.get(symbol.removesuffix(".NS"))
                payload = _extract_fundamental_payload(value)
                connection.execute(
                    """
                    UPDATE fundamentals_queue SET status = 'COMPLETED',
                        attempts = attempts + 1, retry_after = NULL,
                        last_updated = ?, data_json = ?, error = NULL
                    WHERE symbol = ?
                    """,
                    (
                        now,
                        json.dumps(json_value(payload), separators=(",", ":")),
                        symbol,
                    ),
                )
    finally:
        connection.close()


def _fetch_yahooquery_batch(batch: list[tuple[str, str | None]]) -> Any:
    from yahooquery import Ticker

    symbols = [
        symbol if symbol.endswith((".NS", ".BO")) else f"{symbol}.NS"
        for symbol, _ in batch
    ]
    results = Ticker(" ".join(symbols), asynchronous=False, progress=False).get_modules(
        [
            "financialData",
            "defaultKeyStatistics",
            "summaryProfile",
            "calendarEvents",
        ]
    )
    if isinstance(results, dict):
        error = next(
            (
                value
                for value in results.values()
                if isinstance(value, str)
                and ("429" in value or "too many requests" in value.lower())
            ),
            None,
        )
        if error:
            raise requests.HTTPError(error, response=requests.Response())
    return results


def _run_fundamentals_queue(
    *,
    fetch_batch: Callable[[list[tuple[str, str | None]]], Any] = _fetch_yahooquery_batch,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    delay = FUNDAMENTALS_BATCH_DELAY
    while True:
        batch = _next_fundamentals_batch()
        if not batch:
            next_wake_at = _fundamentals_next_wake_at()
            if next_wake_at is None:
                return
            sleep(max(1, min(30, next_wake_at - time.time())))
            continue
        try:
            results = fetch_batch(batch)
            _save_fundamentals_batch(batch, results)
        except Exception as error:
            if _http_status(error) == 429:
                _mark_fundamentals_retry(batch, "HTTP 429; waiting before retry.", RATE_LIMIT_PAUSE_SECONDS)
                sleep(RATE_LIMIT_PAUSE_SECONDS)
                continue
            logger.exception("Fundamentals batch failed.")
            _mark_fundamentals_retry(batch, str(error), 60)
            sleep(60)
            continue
        sleep(random.uniform(*delay))


def start_fundamentals_worker() -> Future | None:
    """Resume the SQLite queue in one background worker for this app process."""
    global _fundamentals_future
    with _fundamentals_lock:
        if _fundamentals_future is not None and not _fundamentals_future.done():
            return _fundamentals_future
        _fundamentals_future = _fundamentals_executor.submit(_run_fundamentals_queue)
        return _fundamentals_future


def get_fundamental_snapshot(symbol: str) -> dict[str, Any] | None:
    """Return completed cached data, or None while it is pending/unavailable."""
    normalized = symbol.strip().upper()
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT status, data_json FROM fundamentals_queue WHERE symbol = ?",
            (normalized,),
        ).fetchone()
        if row is None or row["status"] != "COMPLETED" or not row["data_json"]:
            return None
        return json.loads(row["data_json"])
    finally:
        connection.close()


def get_fundamental_snapshots(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Read completed fundamentals for several scanner symbols in one SQLite pass."""
    normalized = list(
        dict.fromkeys(
            symbol.strip().upper()
            for symbol in symbols
            if isinstance(symbol, str) and symbol.strip()
        )
    )
    if not normalized:
        return {}
    connection = _connect()
    try:
        results = {}
        for start in range(0, len(normalized), 500):
            batch = normalized[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT symbol, data_json FROM fundamentals_queue
                WHERE status = 'COMPLETED' AND data_json IS NOT NULL
                  AND symbol IN ({placeholders})
                """,
                batch,
            ).fetchall()
            results.update(
                {
                    str(row["symbol"]): json.loads(row["data_json"])
                    for row in rows
                    if row["data_json"]
                }
            )
        return results
    finally:
        connection.close()


def get_fundamentals_status(symbol: str) -> str:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT status FROM fundamentals_queue WHERE symbol = ?",
            (symbol.strip().upper(),),
        ).fetchone()
        return str(row["status"]) if row else "PENDING"
    finally:
        connection.close()


def universe_frame(universe: list[dict[str, Any]] | None = None) -> pd.DataFrame:
    """Adapt Upstox master records to the equity scanner's existing table contract."""
    if universe is None:
        snapshot = read_stage0_cache()
        universe = snapshot["universe"] if snapshot else []
    by_isin: dict[str, dict[str, Any]] = {}
    without_isin = []
    for item in universe:
        if not isinstance(item, dict):
            continue
        isin = str(item.get("isin") or "")
        if not isin:
            without_isin.append(item)
            continue
        existing = by_isin.get(isin)
        if (
            existing is None
            or item.get("segment") == "NSE_EQ"
            and existing.get("segment") != "NSE_EQ"
        ):
            by_isin[isin] = item
    rows = []
    for item in (*by_isin.values(), *without_isin):
        isin = str(item.get("isin") or "")
        rows.append(
            {
                "Symbol": item.get("symbol", ""),
                "Company": item.get("name", item.get("symbol", "")),
                "ISIN": isin,
                "Exchange": "NSE" if item.get("segment") == "NSE_EQ" else "BSE",
                "Instrument_Key": item.get("instrument_key", ""),
            }
        )
    return pd.DataFrame(
        rows,
        columns=["Symbol", "Company", "ISIN", "Exchange", "Instrument_Key"],
    )


def fundamentals_queue_counts() -> dict[str, int]:
    connection = _connect()
    try:
        return {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM fundamentals_queue GROUP BY status"
            )
        }
    finally:
        connection.close()

"""On-demand option-chain, news, and analyst enrichment for one stock."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from core.config import settings
from services.stage0_bootstrap import STAGE0_DB_PATH
from curl_cffi.requests.exceptions import RequestException as YahooRequestError
from yahooquery import Ticker

UPSTOX_OPTION_CONTRACT_URL = (
    f"{settings.upstox_api_base.rstrip('/')}/v2/option/contract"
)
UPSTOX_OPTION_CHAIN_URL = (
    f"{settings.upstox_api_base.rstrip('/')}/v2/option/chain"
)
DEFAULT_TTL_SECONDS = 15 * 60
RATE_LIMIT_CACHE_MIN_SECONDS = 30
UPSTOX_RETRY_BASE_SECONDS = 1.0
UPSTOX_MAX_RETRIES = 2
UPSTOX_MAX_AUTOMATIC_RETRY_WAIT_SECONDS = 10.0
LEASE_SECONDS = 180
LEASE_WAIT_SECONDS = 60
IST = ZoneInfo("Asia/Kolkata")
_UPSTOX_REQUEST_INTERVAL_SECONDS = 0.5
_UPSTOX_PACING_LOCK = threading.Lock()
_UPSTOX_LAST_REQUEST_STARTED = 0.0


def _pace_upstox_request() -> None:
    """Keep concurrent Stage 3 option requests to a conservative two per second."""
    global _UPSTOX_LAST_REQUEST_STARTED
    with _UPSTOX_PACING_LOCK:
        elapsed = time.monotonic() - _UPSTOX_LAST_REQUEST_STARTED
        if elapsed < _UPSTOX_REQUEST_INTERVAL_SECONDS:
            time.sleep(_UPSTOX_REQUEST_INTERVAL_SECONDS - elapsed)
        _UPSTOX_LAST_REQUEST_STARTED = time.monotonic()


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
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


async def _upstox_get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: Mapping[str, str],
) -> httpx.Response:
    """Pace Upstox options calls and retry bounded 429/5xx responses."""
    for attempt in range(UPSTOX_MAX_RETRIES + 1):
        _pace_upstox_request()
        response = await client.get(url, params=params)
        status = response.status_code
        if status != 429 and not 500 <= status <= 599:
            return response
        if attempt >= UPSTOX_MAX_RETRIES:
            return response

        retry_after = (
            _retry_after_seconds(response.headers.get("Retry-After"))
            if status == 429
            else None
        )
        delay = (
            retry_after
            if retry_after is not None
            else UPSTOX_RETRY_BASE_SECONDS * (2**attempt)
        )
        if delay > UPSTOX_MAX_AUTOMATIC_RETRY_WAIT_SECONDS:
            return response
        await asyncio.sleep(delay)
    raise RuntimeError("Upstox options retry loop ended unexpectedly.")


def _to_yahoo_symbol(symbol: str, exchange: str = "NSE") -> str:
    """Convert a trading symbol into its Yahoo Finance listing symbol."""
    clean = symbol.strip().upper()
    if not clean:
        raise ValueError("A stock symbol is required.")
    if clean.endswith((".NS", ".BO")):
        return clean
    suffix = ".BO" if "BSE" in str(exchange).upper() else ".NS"
    return f"{clean}{suffix}"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Mapping):
        value = value.get("raw")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value: Any) -> int:
    parsed = _number(value)
    return max(0, int(parsed)) if parsed is not None else 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "to_dict"):
        try:
            return _json_safe(value.to_dict(orient="records"))
        except TypeError:
            return _json_safe(value.to_dict())
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _expiry_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _option_side(row: Mapping[str, Any], side: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    options = _mapping(row.get(f"{side}_options"))
    return _mapping(options.get("market_data")), _mapping(options.get("option_greeks"))


def _first_number(*values: Any) -> float | None:
    for value in values:
        parsed = _number(value)
        if parsed is not None:
            return parsed
    return None


def compute_option_chain_metrics(
    chain_rows: list[dict[str, Any]], spot_price: float | None = None
) -> dict[str, Any]:
    """Aggregate OI/volume PCR, max pain and nearest-to-spot option Greeks."""
    if not chain_rows:
        return {"status": "EMPTY_CHAIN"}

    calls_oi = puts_oi = calls_volume = puts_volume = 0
    strikes: dict[float, dict[str, int]] = {}
    normalized_rows: list[tuple[float, Mapping[str, Any], Mapping[str, Any]]] = []
    for row in chain_rows:
        if not isinstance(row, dict):
            continue
        strike = _number(row.get("strike_price"))
        if strike is None or strike <= 0:
            continue
        call_market, call_greeks = _option_side(row, "call")
        put_market, put_greeks = _option_side(row, "put")
        call_oi, put_oi = _integer(call_market.get("oi")), _integer(put_market.get("oi"))
        call_volume = _integer(call_market.get("volume"))
        put_volume = _integer(put_market.get("volume"))
        calls_oi += call_oi
        puts_oi += put_oi
        calls_volume += call_volume
        puts_volume += put_volume
        current = strikes.setdefault(strike, {"call_oi": 0, "put_oi": 0})
        current["call_oi"] += call_oi
        current["put_oi"] += put_oi
        normalized_rows.append((strike, call_greeks, put_greeks))

    pcr_oi = round(puts_oi / calls_oi, 3) if calls_oi else None
    pcr_volume = round(puts_volume / calls_volume, 3) if calls_volume else None
    max_pain_strike = None
    if strikes:
        def payout(candidate_strike: float) -> float:
            return sum(
                max(candidate_strike - strike, 0) * values["call_oi"]
                + max(strike - candidate_strike, 0) * values["put_oi"]
                for strike, values in strikes.items()
            )

        max_pain_strike = min(sorted(strikes), key=payout)

    spot = _number(spot_price)
    if spot is None or spot <= 0:
        spot = next(
            (
                parsed
                for row in chain_rows
                if isinstance(row, dict)
                and (parsed := _number(row.get("underlying_spot_price"))) is not None
                and parsed > 0
            ),
            None,
        )
    atm = min(normalized_rows, key=lambda row: abs(row[0] - spot)) if spot else None
    greeks: dict[str, Any] = {}
    if atm:
        strike, call, put = atm
        call_iv = _number(call.get("iv"))
        put_iv = _number(put.get("iv"))
        greeks = {
            "atm_strike": strike,
            "call_iv": call_iv,
            "put_iv": put_iv,
            "atm_iv": (
                round((call_iv + put_iv) / 2, 4)
                if call_iv is not None and put_iv is not None
                else call_iv if call_iv is not None else put_iv
            ),
            "call_delta": _number(call.get("delta")),
            "put_delta": _number(put.get("delta")),
            "gamma": _first_number(call.get("gamma"), put.get("gamma")),
            "theta": _first_number(call.get("theta"), put.get("theta")),
            "vega": _first_number(call.get("vega"), put.get("vega")),
        }

    return {
        "status": "OK" if strikes else "EMPTY_CHAIN",
        "total_call_oi": calls_oi,
        "total_put_oi": puts_oi,
        "total_call_volume": calls_volume,
        "total_put_volume": puts_volume,
        "pcr_oi": pcr_oi,
        "pcr_volume": pcr_volume,
        "max_pain_strike": max_pain_strike,
        "atm_greeks": greeks,
    }


def _status_for_http_error(status_code: int) -> str:
    if status_code == 429:
        return "RATE_LIMITED"
    if status_code in (400, 404):
        return "NOT_FNO"
    return "UNAVAILABLE"


async def fetch_upstox_option_context(
    instrument_key: str,
    access_token: str,
    spot_price: float | None = None,
) -> dict[str, Any]:
    """Resolve the nearest active expiry and retrieve one option chain."""
    if not isinstance(instrument_key, str) or "|" not in instrument_key:
        return {"is_fno_eligible": None, "status": "UNAVAILABLE", "error": "Invalid instrument key."}
    if not isinstance(access_token, str) or not access_token.strip():
        return {
            "is_fno_eligible": None,
            "status": "UNAVAILABLE",
            "error": "An Upstox access token is required.",
        }
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token.strip()}",
    }

    async with httpx.AsyncClient(headers=headers, timeout=12.0) as client:
        try:
            contracts_response = await _upstox_get_with_retries(
                client,
                UPSTOX_OPTION_CONTRACT_URL,
                params={"instrument_key": instrument_key},
            )
        except httpx.RequestError as error:
            return {
                "is_fno_eligible": None,
                "status": "UNAVAILABLE",
                "error": f"Option contract request failed: {error}",
            }
        if contracts_response.status_code in (400, 404):
            return {"is_fno_eligible": False, "status": "NOT_FNO"}
        if contracts_response.status_code == 429:
            return {
                "is_fno_eligible": None,
                "status": "RATE_LIMITED",
                "endpoint": "option_contract",
                "retry_after_seconds": _retry_after_seconds(
                    contracts_response.headers.get("Retry-After")
                ),
            }
        try:
            contracts_response.raise_for_status()
            contracts_payload = contracts_response.json()
        except (httpx.HTTPStatusError, ValueError) as error:
            status = (
                _status_for_http_error(contracts_response.status_code)
                if isinstance(error, httpx.HTTPStatusError)
                else "UNAVAILABLE"
            )
            return {
                "is_fno_eligible": None,
                "status": status,
                "endpoint": "option_contract",
                "error": f"Option contract response was unavailable: {error}",
            }
        contracts = contracts_payload.get("data") if isinstance(contracts_payload, dict) else None
        if not isinstance(contracts, list):
            return {
                "is_fno_eligible": None,
                "status": "UNAVAILABLE",
                "error": "Upstox returned a malformed option-contract response.",
            }

        today = datetime.now(IST).date()
        expiry_dates = sorted(
            {
                expiry
                for contract in contracts
                if isinstance(contract, dict)
                and (expiry := _expiry_date(contract.get("expiry"))) is not None
                and expiry >= today
            }
        )
        if not expiry_dates:
            return {"is_fno_eligible": False, "status": "NOT_FNO"}
        nearest_expiry = expiry_dates[0].isoformat()

        try:
            chain_response = await _upstox_get_with_retries(
                client,
                UPSTOX_OPTION_CHAIN_URL,
                params={
                    "instrument_key": instrument_key,
                    "expiry_date": nearest_expiry,
                },
            )
        except httpx.RequestError as error:
            return {
                "is_fno_eligible": True,
                "expiry": nearest_expiry,
                "available_expiries": [item.isoformat() for item in expiry_dates[:3]],
                "status": "UNAVAILABLE",
                "error": f"Option-chain request failed: {error}",
            }
        if chain_response.status_code == 429:
            return {
                "is_fno_eligible": True,
                "expiry": nearest_expiry,
                "available_expiries": [item.isoformat() for item in expiry_dates[:3]],
                "status": "RATE_LIMITED",
                "endpoint": "option_chain",
                "retry_after_seconds": _retry_after_seconds(
                    chain_response.headers.get("Retry-After")
                ),
            }
        try:
            chain_response.raise_for_status()
            chain_payload = chain_response.json()
        except (httpx.HTTPStatusError, ValueError) as error:
            status = (
                _status_for_http_error(chain_response.status_code)
                if isinstance(error, httpx.HTTPStatusError)
                else "UNAVAILABLE"
            )
            return {
                "is_fno_eligible": True,
                "expiry": nearest_expiry,
                "available_expiries": [item.isoformat() for item in expiry_dates[:3]],
                "status": status,
                "endpoint": "option_chain",
                "error": f"Option-chain response was unavailable: {error}",
            }
        chain_rows = chain_payload.get("data") if isinstance(chain_payload, dict) else None
        if not isinstance(chain_rows, list):
            return {
                "is_fno_eligible": True,
                "expiry": nearest_expiry,
                "available_expiries": [item.isoformat() for item in expiry_dates[:3]],
                "status": "UNAVAILABLE",
                "error": "Upstox returned a malformed option-chain response.",
            }
        metrics = compute_option_chain_metrics(chain_rows, spot_price=spot_price)
        return {
            "is_fno_eligible": True,
            "expiry": nearest_expiry,
            "available_expiries": [item.isoformat() for item in expiry_dates[:3]],
            **metrics,
        }


def _fetch_yahoo_news_and_consensus_sync(yahoo_ticker: str) -> dict[str, Any]:
    """Fetch one ticker's public analyst, company-profile and recent-news data."""
    try:
        ticker = Ticker(yahoo_ticker, asynchronous=False)
        modules = ticker.get_modules(
            ["financialData", "summaryProfile", "recommendationTrend"]
        )
        ticker_modules = (
            modules.get(yahoo_ticker) if isinstance(modules, dict) else None
        )
        if not isinstance(ticker_modules, dict):
            ticker_modules = {}
        financial = ticker_modules.get("financialData")
        financial = financial if isinstance(financial, dict) else {}
        profile = ticker_modules.get("summaryProfile")
        profile = profile if isinstance(profile, dict) else {}
        raw_news = ticker.news(count=5) if hasattr(ticker, "news") else []
        headlines = []
        if isinstance(raw_news, list):
            for item in raw_news[:5]:
                if isinstance(item, dict) and item.get("title"):
                    headlines.append(
                        {
                            "title": str(item["title"])[:500],
                            "publisher": item.get("publisher"),
                            "published_at": item.get("providerPublishTime"),
                        }
                    )

        return {
            "status": "OK",
            "yahoo_ticker": yahoo_ticker,
            "sector": profile.get("sector"),
            "industry": profile.get("industry"),
            "business_summary": (
                str(profile.get("longBusinessSummary") or "")[:600] or None
            ),
            "target_mean_price": _number(financial.get("targetMeanPrice")),
            "target_high_price": _number(financial.get("targetHighPrice")),
            "target_low_price": _number(financial.get("targetLowPrice")),
            "recommendation_key": financial.get("recommendationKey"),
            "analyst_count": _integer(financial.get("numberOfAnalystOpinions")),
            "recommendation_trend": _json_safe(
                ticker_modules.get("recommendationTrend")
            ),
            "recent_headlines": headlines,
        }
    except (
        YahooRequestError,
        AttributeError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        return {
            "status": "UNAVAILABLE",
            "yahoo_ticker": yahoo_ticker,
            "error": f"Yahoo enrichment unavailable: {error}",
            "recent_headlines": [],
        }


async def fetch_single_stock_enrichment(
    instrument_key: str,
    symbol: str,
    access_token: str,
    exchange: str = "NSE",
    spot_price: float | None = None,
) -> dict[str, Any]:
    """Fetch option-chain and Yahoo enrichment concurrently for one listing."""
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("A stock symbol is required.")
    yahoo_ticker = _to_yahoo_symbol(symbol, exchange)
    options_task = asyncio.create_task(
        fetch_upstox_option_context(
            instrument_key, access_token, spot_price=spot_price
        )
    )
    yahoo_task = asyncio.to_thread(_fetch_yahoo_news_and_consensus_sync, yahoo_ticker)
    options_data, news_consensus = await asyncio.gather(
        options_task, yahoo_task, return_exceptions=True
    )
    if isinstance(options_data, Exception):
        options_data = {
            "is_fno_eligible": None,
            "status": "UNAVAILABLE",
            "error": f"Options enrichment failed: {options_data}",
        }
    if isinstance(news_consensus, Exception):
        news_consensus = {
            "status": "UNAVAILABLE",
            "yahoo_ticker": yahoo_ticker,
            "error": f"Yahoo enrichment failed: {news_consensus}",
            "recent_headlines": [],
        }
    return {
        "instrument_key": instrument_key,
        "symbol": symbol.strip().upper(),
        "fetched_at": time.time(),
        "derivatives": options_data,
        "news_and_consensus": news_consensus,
    }


def _database(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS stage3_stock_cache (
            cache_key TEXT PRIMARY KEY,
            instrument_key TEXT NOT NULL,
            symbol TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            fetched_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stage3_stock_leases (
            cache_key TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            expires_at REAL NOT NULL
        );
        """
    )
    return connection


def init_stage3_cache(db_path: str | Path = STAGE0_DB_PATH) -> None:
    """Create the Stage 3 cache and lease tables."""
    connection = _database(db_path)
    connection.close()


def _cache_key(
    instrument_key: str,
    symbol: str,
    exchange: str,
    access_token: str,
) -> str:
    # Bind cache entries to the authenticated account without persisting its token.
    token_fingerprint = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    identity = "\n".join(
        (
            instrument_key.strip().upper().replace(":", "|"),
            symbol.strip().upper(),
            exchange.strip().upper(),
            token_fingerprint,
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _read_cache(db_path: str | Path, cache_key: str) -> dict[str, Any] | None:
    connection = _database(db_path)
    try:
        row = connection.execute(
            "SELECT payload_json, fetched_at FROM stage3_stock_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        fetched_at = float(row[1])
        age = time.time() - fetched_at
        if age < 0:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
    finally:
        connection.close()


def _write_cache(
    db_path: str | Path,
    cache_key: str,
    instrument_key: str,
    symbol: str,
    payload: Mapping[str, Any],
    fetched_at: float,
) -> None:
    connection = _database(db_path)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO stage3_stock_cache"
            "(cache_key, instrument_key, symbol, payload_json, fetched_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                cache_key,
                instrument_key,
                symbol,
                json.dumps(
                    _json_safe(payload), separators=(",", ":"), allow_nan=False
                ),
                fetched_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _cache_ttl_for_payload(payload: Mapping[str, Any], ttl_seconds: int) -> int:
    derivatives = payload.get("derivatives")
    if not isinstance(derivatives, Mapping) or derivatives.get("status") != "RATE_LIMITED":
        return ttl_seconds
    retry_after = _number(derivatives.get("retry_after_seconds"))
    transient_ttl = max(
        RATE_LIMIT_CACHE_MIN_SECONDS,
        int(math.ceil(retry_after)) if retry_after is not None else 0,
    )
    return min(ttl_seconds, transient_ttl)


def _acquire_lease(db_path: str | Path, cache_key: str) -> str | None:
    now = time.time()
    owner = uuid4().hex
    connection = _database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM stage3_stock_leases WHERE cache_key = ? AND expires_at <= ?",
            (cache_key, now),
        )
        try:
            connection.execute(
                "INSERT INTO stage3_stock_leases(cache_key, owner, expires_at) "
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
            "DELETE FROM stage3_stock_leases WHERE cache_key = ? AND owner = ?",
            (cache_key, owner),
        )
        connection.commit()
    finally:
        connection.close()


def get_stage3_enrichment_cached(
    db_path: str | Path = STAGE0_DB_PATH,
    instrument_key: str = "",
    symbol: str = "",
    access_token: str = "",
    exchange: str = "NSE",
    spot_price: float | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Return one 15-minute SQLite-cached stock enrichment or fetch it lazily."""
    if ttl_seconds < 0:
        raise ValueError("Cache TTL must be nonnegative.")
    if not isinstance(instrument_key, str):
        raise ValueError("Instrument key must be a string.")
    if instrument_key.strip() and "|" not in instrument_key:
        raise ValueError("A valid Upstox equity instrument key is required.")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("A stock symbol is required.")
    token = access_token.strip() if isinstance(access_token, str) else ""
    cache_key = _cache_key(instrument_key, symbol, exchange, token)
    cached = _read_cache(db_path, cache_key)
    if (
        cached is not None
        and 0 <= time.time() - float(cached["fetched_at"])
        < _cache_ttl_for_payload(cached, ttl_seconds)
    ):
        return {**cached, "cache_source": "CACHE"}

    owner = _acquire_lease(db_path, cache_key)
    if owner is None:
        deadline = time.monotonic() + LEASE_WAIT_SECONDS
        while time.monotonic() < deadline:
            cached = _read_cache(db_path, cache_key)
            if (
                cached is not None
                and 0 <= time.time() - float(cached["fetched_at"])
                < _cache_ttl_for_payload(cached, ttl_seconds)
            ):
                return {**cached, "cache_source": "CACHE"}
            owner = _acquire_lease(db_path, cache_key)
            if owner is not None:
                break
            time.sleep(0.1)
        if owner is None:
            raise TimeoutError("Timed out waiting for the Stage 3 enrichment refresh.")

    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "get_stage3_enrichment_cached must be called outside an active event loop."
            )
        fresh = asyncio.run(
            fetch_single_stock_enrichment(
                instrument_key=instrument_key,
                symbol=symbol,
                access_token=token,
                exchange=exchange,
                spot_price=spot_price,
            )
        )
        fetched_at = float(fresh["fetched_at"])
        _write_cache(
            db_path,
            cache_key,
            instrument_key,
            symbol,
            fresh,
            fetched_at,
        )
        return {**fresh, "cache_source": "LIVE_API"}
    finally:
        _release_lease(db_path, cache_key, owner)

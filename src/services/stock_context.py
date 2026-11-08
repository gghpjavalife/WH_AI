"""Two-phase market context intake, independent of the application's AI workflows.

All monetary amounts are in the instrument's quote currency. Ownership and
yield fields are fractions; fields named ``*_pct`` or ``*_percentage`` are
percentage points unless their documented source is a fractional holding.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from yahooquery import Ticker
from yahooquery.base import FuturesSession

from core.config import settings
from core.logging import logger
from .stock_context_models import (
    AnalystConsensus, CompanyProfile, FundamentalRatios, FuturesMarketMetrics,
    GrowthAndEarningsQuality, IntradayMicrostructureMetrics, InvestmentHorizon,
    LocalGatekeeperResponse, MacroContext, NewsHeadline, NewsHeadlines,
    OptionsChainMetrics, ShareholderDistributionMetrics, StockSignalContext,
    TechnicalIndicators, TradeSignalResponse, local_gatekeeper, run_triggered_analysis,
)
from .stock_context_sources import (
    first_number, json_value, map_yahoo_modules, option_ratios, returns_from_history, yahoo_modules,
)
from .stock_context_stream import UpstoxMicrostructureStream
from .stock_context_technical import TechnicalDataError, calculate_technical_indicators
from .stock_context_upstox import (
    equity_isin, fetch_upstox_corporate_actions,
    fetch_upstox_news, fetch_upstox_quote, fetch_upstox_shareholdings,
    fetch_upstox_volume_baseline, parse_promoter_pledge, quote_fields, upcoming_corporate_action,
)

_MODULES = [
    "summaryProfile", "defaultKeyStatistics", "financialData", "price",
    "summaryDetail", "majorHoldersBreakdown", "institutionOwnership",
    "insiderTransactions", "netSharePurchaseActivity", "earningsHistory",
]

# This cap is shared by every async context runner in this Python process,
# including runners on different threads and event loops.
_PROCESS_NETWORK_LIMIT = threading.BoundedSemaphore(20)


def _daily_history(raw: Any, symbol: str) -> pd.DataFrame:
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        raise ValueError(f"Yahoo returned no daily candle data for {symbol}.")
    frame = raw
    if isinstance(frame.index, pd.MultiIndex) and "symbol" in frame.index.names:
        try:
            frame = frame.xs(symbol, level="symbol")
        except KeyError as exc:
            raise ValueError(f"Yahoo daily candles do not include {symbol}.") from exc
    if not isinstance(frame.index, pd.DatetimeIndex):
        try:
            dates = pd.to_datetime(frame.index, errors="raise", utc=True)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"Yahoo daily candles contain invalid dates for {symbol}."
            ) from exc
        if not isinstance(dates, pd.DatetimeIndex):
            raise ValueError(f"Yahoo daily candles contain invalid dates for {symbol}.")
        frame = frame.copy()
        frame.index = dates
    return frame


def _supplement_components(payload: dict[str, Any], supplemental: dict[str, Any]) -> None:
    """Accept separately sourced data only in the documented supplemental layers."""
    allowed = {"fundamentals", "shareholders", "growth", "options", "futures", "analyst", "macro", "news"}
    unknown = set(supplemental) - allowed
    if unknown:
        raise ValueError(f"Unsupported supplemental sections: {', '.join(sorted(unknown))}.")
    for section, values in supplemental.items():
        if not isinstance(values, dict):
            raise ValueError(f"Supplemental {section} must be an object.")
        if section == "fundamentals" and set(values) - {"altman_model_applicable"}:
            raise ValueError("Only Altman model applicability can supplement fundamental ratios.")
        if payload.get(section) is None:
            payload[section] = {}
        if section == "news" and "headlines" in values:
            if not isinstance(values["headlines"], list):
                raise ValueError("Supplemental news headlines must be a list.")
            values = {**values, "headlines": payload[section]["headlines"] + values["headlines"]}
        payload[section].update(values)


def _quote_matches_stream_session(quote: dict[str, Any], traded_at: str) -> bool:
    try:
        raw = quote["timestamp"]
        if isinstance(raw, bool):
            return False
        if isinstance(raw, (int, float)) or isinstance(raw, str) and raw.isdigit():
            quoted = datetime.fromtimestamp(float(raw) / 1000, tz=timezone.utc)
        else:
            quoted = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        traded = datetime.fromisoformat(traded_at.replace("Z", "+00:00"))
        if quoted.utcoffset() is None or traded.utcoffset() is None:
            return False
        zone = ZoneInfo("Asia/Kolkata")
        return quoted.astimezone(zone).date() == traded.astimezone(zone).date()
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def fetch_and_build_market_context(
    symbol: str,
    horizon: InvestmentHorizon,
    *,
    instrument_key: str | None = None,
    sector_symbol: str | None = None,
    supplemental: dict[str, Any] | None = None,
    intraday_stream: UpstoxMicrostructureStream | None = None,
    yahoo_factory: Callable[..., Any] = Ticker,
    http_session: requests.Session | None = None,
) -> StockSignalContext:
    """Fetch a validated context; this function never calls an LLM.

    An optional caller-supplied ``supplemental`` supplies externally verified
    data not exposed by Yahoo or the documented Upstox quote APIs. The caller
    is responsible for its provenance, currency, units, and freshness.
    Intraday contexts require a fresh, complete D30 streaming snapshot.
    """
    symbol, horizon, _ = _validate_request(symbol, horizon, instrument_key, None, intraday_stream)

    ticker = yahoo_factory(symbol, timeout=settings.http_read_timeout_seconds)
    raw_modules = ticker.get_modules(_MODULES)
    yahoo_modules(raw_modules, symbol)
    history_raw = ticker.history(period="1y", interval="1d")
    _daily_history(history_raw, symbol)
    balance = ticker.balance_sheet(frequency="a")
    income = ticker.income_statement(frequency="a", trailing=False)
    cash = ticker.cash_flow(frequency="a", trailing=False)
    chain = ticker.option_chain
    def historical_benchmarks():
        benchmark_raw = yahoo_factory("^GSPC", timeout=settings.http_read_timeout_seconds).history(period="1y", interval="1d")
        benchmark = _daily_history(benchmark_raw, "^GSPC")
        sector_raw, sector, normalized = None, None, sector_symbol
        if sector_symbol:
            normalized = sector_symbol.strip().upper()
            if not normalized:
                raise ValueError("Sector ticker must not be blank.")
            sector_raw = yahoo_factory(normalized, timeout=settings.http_read_timeout_seconds).history(period="1y", interval="1d")
            sector = _daily_history(sector_raw, normalized)
        return benchmark_raw, benchmark, sector_raw, sector, normalized

    def upstox(fetch):
        return fetch(instrument_key, session=http_session)

    return _assemble_market_context(
        symbol, horizon, instrument_key, sector_symbol, supplemental, intraday_stream,
        raw_modules, history_raw, balance, income, cash, chain, None, None, upstox,
        historical_benchmarks=historical_benchmarks,
    )


def _validate_request(symbol, horizon, instrument_key, sector_symbol, intraday_stream):
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("A Yahoo ticker symbol is required.")
    horizon = InvestmentHorizon(horizon)
    if horizon == InvestmentHorizon.INTRADAY_TRADING and intraday_stream is None:
        raise ValueError("Intraday context requires a fresh Upstox streaming snapshot.")
    if intraday_stream is not None and instrument_key != intraday_stream.instrument_key:
        raise ValueError("The streaming and requested Upstox instrument keys must match.")
    if sector_symbol:
        sector_symbol = sector_symbol.strip().upper()
        if not sector_symbol:
            raise ValueError("Sector ticker must not be blank.")
    return symbol, horizon, sector_symbol


def _assemble_market_context(
    symbol, horizon, instrument_key, sector_symbol, supplemental, intraday_stream,
    raw_modules, history_raw, balance, income, cash, chain, benchmark_raw, sector_raw,
    upstox, *, tolerant=False, intake_warnings=None, historical_benchmarks=None,
):
    from .stock_context_financials import calculate_financial_metrics, verify_altman_inputs

    history_warnings = []
    if tolerant:
        try:
            modules = yahoo_modules(raw_modules, symbol)
        except ValueError:
            modules = {}
        def history_or_empty(raw, ticker_symbol, label):
            try:
                return _daily_history(raw, ticker_symbol)
            except ValueError:
                history_warnings.append(
                    f"Yahoo {label} unavailable or does not contain {ticker_symbol}; "
                    "historical values remain unknown."
                )
                return pd.DataFrame()
        history = history_or_empty(history_raw, symbol, "daily history")
        benchmark = history_or_empty(benchmark_raw, "^GSPC", "S&P 500 history")
        sector = history_or_empty(sector_raw, sector_symbol, "sector history") if sector_symbol else None
    else:
        modules = yahoo_modules(raw_modules, symbol)
        history = _daily_history(history_raw, symbol)
    mapped = map_yahoo_modules(modules, symbol)
    if tolerant and (mapped["current_price"] is not None and mapped["current_price"] <= 0):
        mapped["current_price"] = None
        mapped["fundamentals"]["float_market_cap"] = None
    if instrument_key:
        mapped["profile"]["isin"] = equity_isin(instrument_key)
    financial = calculate_financial_metrics(balance, income, cash, mapped["fundamentals"]["market_cap"])
    financial_module = modules.get("financialData")
    price_module = modules.get("price")
    statement_currency = financial_module.get("financialCurrency") if isinstance(financial_module, dict) else None
    quote_currency = price_module.get("currency") if isinstance(price_module, dict) else None
    statement_currencies = {statement_currency} if statement_currency else set()
    for frame in (balance, income):
        if isinstance(frame, pd.DataFrame) and "currencyCode" in frame:
            statement_currencies.update(str(value) for value in frame["currencyCode"].dropna() if str(value))
    currency_mismatch = (
        not quote_currency or len(statement_currencies) != 1
        or quote_currency not in statement_currencies
    )
    altman_matrix = verify_altman_inputs(balance, income, cash, mapped["fundamentals"]["market_cap"])
    altman_matrix["currency_consistent"] = not currency_mismatch
    altman_matrix["applicability"] = "unverified"
    mapped["fundamentals"].update({
        "piotroski_f_score": financial["piotroski_f_score"],
        "altman_z_score": None if currency_mismatch else financial["altman_z_score"],
    })
    if historical_benchmarks is not None:
        benchmark_raw, benchmark, sector_raw, sector, sector_symbol = historical_benchmarks()
    warnings = [
        "Yahoo option_chain replaces missing cells with zero; reported option totals may be incomplete.",
        "Promoter pledging and other unavailable fields require a separately verified source.",
    ]
    warnings.extend(intake_warnings or [])
    warnings.extend(history_warnings)
    if currency_mismatch:
        warnings.append("Financial-statement and quote currencies are missing or inconsistent; Altman Z cannot be verified.")
    if not sector_symbol:
        warnings.append("Sector returns require an explicitly selected sector benchmark.")
    if not isinstance(chain, pd.DataFrame):
        warnings.append("Yahoo option-chain data is unavailable.")
    if not instrument_key and (not supplemental or "news" not in supplemental):
        warnings.append("No news feed was supplied; the empty list does not imply there were no headlines.")
    try:
        technicals = calculate_technical_indicators(history, warnings=warnings)
    except TechnicalDataError as exc:
        technicals = {}
        warnings.append(f"Technical indicators unavailable: {exc}")
    if tolerant:
        # Never treat a forward/back-filled final candle as a live observation.
        raw_volume = None
        if not history.empty:
            column = next((c for c in history.columns if str(c).lower() == "volume"), None)
            if column is not None:
                raw_volume = first_number(history.sort_index()[column].iloc[-1])
        if raw_volume is None or raw_volume < 0 or not raw_volume.is_integer():
            technicals["current_volume"] = None
            warnings.append("Yahoo current volume is unavailable; a repaired candle is not a current observation.")
        elif technicals.get("current_volume") is None:
            technicals["current_volume"] = int(raw_volume)
        if mapped["current_price"] is None:
            warnings.append("Yahoo current price is unavailable.")
    source_data = {
        "yahoo_quote_modules": json_value(raw_modules),
        "yahoo_daily_history": json_value(history_raw),
        "yahoo_balance_sheet": json_value(balance),
        "yahoo_income_statement": json_value(income),
        "yahoo_cash_flow": json_value(cash),
        "yahoo_option_chain": json_value(chain),
        "yahoo_sp500_history": json_value(benchmark_raw),
        "yahoo_sector_history": json_value(sector_raw),
        "altman_verification": altman_matrix,
    }
    payload: dict[str, Any] = {
        **mapped, "horizon": horizon,
        "technicals": technicals,
        "growth": {name: financial[name] for name in (
            "revenue_growth_yoy", "earnings_growth_yoy", "earnings_quality_score")},
        "options": option_ratios(chain),
        "futures": None,
        "macro": returns_from_history(history, sector, benchmark),
        "news": {"headlines": []},
        "intraday": None,
        "source_data": source_data,
        "data_warnings": warnings,
    }
    if tolerant:
        source_data["field_provenance"] = {}
        if payload["current_price"] is not None:
            source_data["field_provenance"]["current_price"] = "yahoo_quote_modules"
        if technicals.get("current_volume") is not None:
            source_data["field_provenance"]["technicals.current_volume"] = "yahoo_daily_history"
            source_data["current_volume_metadata"] = {
                "meaning": "latest_available_yahoo_daily_candle_volume",
                "candle_index": json_value(history.sort_index().index[-1]),
            }
            warnings.append(
                "Current volume is the latest available Yahoo daily candle volume, not necessarily today's "
                "volume or a live observation; verify its date and freshness."
            )
    quote = None
    if instrument_key:
        try:
            quote = upstox(fetch_upstox_quote)
        except (requests.RequestException, ValueError):
            message = "Upstox full quote unavailable; circuit proximity cannot be assessed."
            logger.warning("%s", message)
            warnings.append(message)
        if quote is not None:
            source_data["upstox_quote"] = json_value(quote)
            fields = quote_fields(quote)
            if tolerant and intraday_stream is None:
                if payload["current_price"] is None and fields["current_price"] is not None:
                    payload["current_price"] = fields["current_price"]
                    source_data.setdefault("field_provenance", {})["current_price"] = "upstox_quote"
                    warnings.append("Current price uses the Upstox quote; verify instrument identity and freshness.")
                volume = first_number(quote.get("volume"))
                if (payload["technicals"].get("current_volume") is None and volume is not None
                        and volume >= 0 and volume.is_integer()):
                    payload["technicals"]["current_volume"] = int(volume)
                    source_data.setdefault("field_provenance", {})["technicals.current_volume"] = "upstox_quote"
                    source_data["current_volume_metadata"] = {
                        "meaning": "upstox_volume_traded_today",
                        "feed_update_timestamp": source_data["upstox_quote"].get("timestamp"),
                        "last_trade_time_epoch_ms": source_data["upstox_quote"].get("last_trade_time"),
                    }
                    warnings.append(
                        "Current volume uses Upstox quote volume traded today, not Yahoo historical daily "
                        "candle volume. Feed timestamp is the feed update time; last_trade_time is the last "
                        "trade time in epoch milliseconds. Verify instrument identity, session and freshness; "
                        "timestamps may be missing or stale. Historical volume indicators remain Yahoo-derived."
                    )
            if fields["current_price"] is None:
                warnings.append("Upstox returned no usable quoted price.")
            if intraday_stream is None:
                payload["intraday"] = {name: fields[name] for name in (
                    "upper_circuit_limit", "lower_circuit_limit", "distance_to_lower_circuit_pct")}
                warnings.append("Circuit distance uses the Upstox quote price; quote freshness must be verified.")
            if fields["lower_circuit_limit"] is None:
                warnings.append("Upstox lower circuit limit is unavailable; circuit proximity cannot be assessed.")
    if instrument_key:
        try:
            headlines, news_pages = upstox(fetch_upstox_news)
            payload["news"]["headlines"] = headlines
            source_data["upstox_news_pages"] = json_value(news_pages)
        except (requests.RequestException, ValueError):
            message = "Upstox news unavailable; an empty headline list does not imply no news."
            logger.warning("%s", message)
            warnings.append(message)
        if instrument_key.startswith(("NSE_EQ|", "BSE_EQ|")):
            try:
                holdings = upstox(fetch_upstox_shareholdings)
                source_data["upstox_shareholdings"] = json_value(holdings)
                pledge, verified = parse_promoter_pledge(holdings)
                if pledge is not None:
                    payload["shareholders"]["promoter_pledged_shares_pct"] = pledge
                    payload["shareholders"]["promoter_pledge_verified"] = verified
                    if not verified:
                        message = "Upstox promoter pledge is unverified; any malformed value uses a zero fallback and the local gate remains blocked."
                        logger.warning("%s", message)
                        warnings.append(message)
            except (requests.RequestException, ValueError):
                message = "Upstox shareholdings unavailable; promoter pledge remains unverified."
                logger.warning("%s", message)
                warnings.append(message)
            try:
                actions = upstox(fetch_upstox_corporate_actions)
                source_data["upstox_corporate_actions"] = json_value(actions)
                event = upcoming_corporate_action(actions)
                payload["news"]["upcoming_corporate_action_event"] = event
                if event is None:
                    warnings.append("Corporate action status is unavailable due to incomplete event data.")
            except (requests.RequestException, ValueError):
                message = "Upstox corporate actions unavailable; upcoming event status is unknown."
                logger.warning("%s", message)
                warnings.append(message)
        warnings.append("Upstox news covers only the last seven days.")
    if intraday_stream is not None:
        snapshot = intraday_stream.snapshot()
        payload["current_price"] = snapshot.pop("current_price")
        if tolerant:
            source_data["field_provenance"]["current_price"] = "upstox_stream"
        if quote is not None and _quote_matches_stream_session(quote, snapshot["traded_at"]):
            circuit_quote = {**quote, "last_price": payload["current_price"]}
            circuit = quote_fields(circuit_quote)
            snapshot.update({name: circuit[name] for name in (
                "upper_circuit_limit", "lower_circuit_limit", "distance_to_lower_circuit_pct")})
        elif quote is not None:
            warnings.append("Upstox quote session does not match the live tick; circuit proximity is unavailable.")
        source_data["upstox_stream_timestamp"] = {
            "observed_at": snapshot.pop("observed_at"),
            "traded_at": snapshot.pop("traded_at"),
        }
        source_data["upstox_latest_stream_message"] = json_value(snapshot.pop("raw_message"))
        source_data["upstox_completed_minute_bars"] = json_value(snapshot.pop("completed_minute_bars"))
        source_data["intraday_volume_baseline"] = json_value(intraday_stream.baseline)
        source_data["intraday_volume_baseline_source"] = json_value(intraday_stream.baseline_source)
        warnings.extend(intraday_stream.baseline_warnings)
        if intraday_stream.baseline_source is None:
            warnings.append("Intraday volume baseline was supplied without historical source data.")
        payload["intraday"] = snapshot
    if supplemental:
        _supplement_components(payload, supplemental)
        if "promoter_pledged_shares_pct" in supplemental.get("shareholders", {}):
            payload["shareholders"]["promoter_pledge_verified"] = True
        source_data["supplemental"] = json_value(supplemental)
        warnings.append("Supplemental values are supplied by the caller and require independent verification.")
    if payload["fundamentals"].get("altman_model_applicable") is True:
        altman_matrix["applicability"] = "caller_confirmed"
    target, current = first_number(payload["analyst"].get("mean_price_target")), first_number(payload.get("current_price"))
    if source_data.get("field_provenance", {}).get("current_price") in {"upstox_quote", "upstox_stream"}:
        target = None
        warnings.append("Yahoo target upside is unavailable with an Upstox price; currencies are not verified.")
    payload["analyst"]["upside_percentage"] = (
        (target / current - 1) * 100 if target is not None and current is not None and current > 0 else None
    )
    return StockSignalContext.model_validate(payload)


class ProviderRateLimitError(RuntimeError):
    """A provider rejected requests with HTTP 429; the current batch must stop."""


def _retryable(error: Exception) -> bool:
    response = getattr(error, "response", None)
    return (isinstance(error, (TimeoutError, requests.Timeout))
            or "timeout" in type(error).__name__.lower()
            or "timed out" in str(error).lower())


async def _bounded_call(
    semaphore: asyncio.Semaphore,
    operation: Callable[[], Any],
    *,
    abort_if: asyncio.Event | None = None,
) -> Any:
    # Shield the worker, not just to_thread: cancellation must not release its
    # permit while its uninterruptible network thread is still running.
    await semaphore.acquire()
    if abort_if is not None and abort_if.is_set():
        semaphore.release()
        raise ProviderRateLimitError(
            "A market-data provider returned HTTP 429; context ingestion stopped."
        )
    cancelled = threading.Event()

    def run():
        with _PROCESS_NETWORK_LIMIT:
            if cancelled.is_set():
                return None
            if abort_if is not None and abort_if.is_set():
                raise ProviderRateLimitError(
                    "A market-data provider returned HTTP 429; context ingestion stopped."
                )
            return operation()

    try:
        task = asyncio.create_task(asyncio.to_thread(run))
    except BaseException:
        semaphore.release()
        raise
    def finished(done):
        semaphore.release()
        if not done.cancelled():
            done.exception()
    task.add_done_callback(finished)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        raise


async def _fetch_with_retries(semaphore, operation, *, attempts=3, abort_if=None):
    for attempt in range(attempts):
        try:
            return await _bounded_call(semaphore, operation, abort_if=abort_if)
        except Exception as exc:
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) == 429:
                if abort_if is not None:
                    abort_if.set()
                raise ProviderRateLimitError(
                    "A market-data provider returned HTTP 429; context ingestion stopped."
                ) from exc
            if attempt + 1 == attempts or not _retryable(exc):
                raise
            await asyncio.sleep(random.uniform(0.1, 0.3) * (2 ** attempt))


async def async_bounded_network_call(
    semaphore: asyncio.Semaphore,
    operation: Callable[[], Any],
    *,
    abort_if: asyncio.Event | None = None,
) -> Any:
    """Run one serial blocking provider operation under both request caps.

    Share a semaphore per runner/event loop; this helper shares the process cap
    with async context ingestion. The callable must not spawn parallel requests.
    Retryable timeouts use up to three attempts with async jitter. HTTP 429
    aborts context ingestion so a large scan does not continue against a
    provider that is actively throttling requests.
    """
    return await _fetch_with_retries(semaphore, operation, abort_if=abort_if)


async def async_fetch_and_build_market_context(
    symbol: str,
    horizon: InvestmentHorizon,
    *,
    semaphore: asyncio.Semaphore,
    instrument_key: str | None = None,
    upstox_token: str = "",
    sector_symbol: str | None = None,
    supplemental: dict[str, Any] | None = None,
    intraday_stream: UpstoxMicrostructureStream | None = None,
    rate_limit_event: asyncio.Event | None = None,
    yahoo_factory: Callable[..., Any] = Ticker,
    http_session: requests.Session | None = None,
) -> StockSignalContext:
    """Fetch concurrently using the runner's shared semaphore; never invoke the gate.

    Pass one ``asyncio.Semaphore(20)`` per runner/event loop. All calls also
    share a process-wide 20-operation cap across loops and threads. Blocking
    provider operations retain that cap until their thread finishes, even if
    cancelled. Provider timeouts bound requests; nothing is cached or written
    to disk. A supplied HTTP session must support concurrent requests, and
    custom provider factories must not spawn parallel network requests.
    """
    symbol, horizon, sector_symbol = _validate_request(symbol, horizon, instrument_key, sector_symbol, intraday_stream)

    def yahoo(ticker_symbol, method, *args, **kwargs):
        ticker = yahoo_factory(ticker_symbol, timeout=settings.http_read_timeout_seconds, asynchronous=False)
        if isinstance(getattr(ticker, "session", None), FuturesSession):
            raise ValueError("Asynchronous Yahoo sessions are not supported.")
        value = getattr(ticker, method)
        result = value(*args, **kwargs) if callable(value) else value
        errors = [result] if isinstance(result, str) else (
            [result.get(ticker_symbol), result.get("error")] if isinstance(result, dict) else [])
        for error in errors:
            if isinstance(error, str):
                if "429" in error or "too many requests" in error.lower():
                    response = requests.Response()
                    response.status_code = 429
                    raise requests.HTTPError("Yahoo rate limited", response=response)
                if "timeout" in error.lower() or "timed out" in error.lower():
                    raise TimeoutError("Yahoo request timed out")
        return result

    jobs = {
        "quote modules": lambda: yahoo(symbol, "get_modules", _MODULES),
        "daily history": lambda: yahoo(symbol, "history", period="1y", interval="1d"),
        "balance sheet": lambda: yahoo(symbol, "balance_sheet", frequency="a"),
        "income statement": lambda: yahoo(symbol, "income_statement", frequency="a", trailing=False),
        "cash flow": lambda: yahoo(symbol, "cash_flow", frequency="a", trailing=False),
        "option chain": lambda: yahoo(symbol, "option_chain"),
        "S&P 500 history": lambda: yahoo("^GSPC", "history", period="1y", interval="1d"),
    }
    if sector_symbol:
        jobs["sector history"] = lambda: yahoo(sector_symbol, "history", period="1y", interval="1d")
    yahoo_results = await asyncio.gather(*(
        async_bounded_network_call(semaphore, operation, abort_if=rate_limit_event)
        for operation in jobs.values()
    ), return_exceptions=True)
    rate_limit_error = next(
        (result for result in yahoo_results if isinstance(result, ProviderRateLimitError)),
        None,
    )
    if rate_limit_error is not None:
        if rate_limit_event is not None:
            rate_limit_event.set()
        raise rate_limit_error
    warnings = []
    values = {}
    for name, result in zip(jobs, yahoo_results):
        if isinstance(result, Exception):
            warnings.append(f"Yahoo {name} unavailable; affected fields remain unknown.")
            result = None
        elif (result is None or isinstance(result, str)
              or (isinstance(result, pd.DataFrame) and result.empty)
              or (name == "quote modules" and
                  (not isinstance(result, dict) or not isinstance(result.get(symbol), dict) or not result[symbol]))):
            warnings.append(f"Yahoo {name} unavailable or empty; affected fields remain unknown.")
        values[name] = result

    upstox_results = {}
    if instrument_key:
        calls = [fetch_upstox_quote, fetch_upstox_news]
        if instrument_key.startswith(("NSE_EQ|", "BSE_EQ|")):
            calls.extend([fetch_upstox_shareholdings, fetch_upstox_corporate_actions])
        if upstox_token.strip():
            results = await asyncio.gather(*(
                async_bounded_network_call(semaphore, lambda fetch=fetch: fetch(
                    instrument_key, session=http_session, token=upstox_token),
                    abort_if=rate_limit_event)
                for fetch in calls
            ), return_exceptions=True)
            rate_limit_error = next(
                (result for result in results if isinstance(result, ProviderRateLimitError)),
                None,
            )
            if rate_limit_error is not None:
                if rate_limit_event is not None:
                    rate_limit_event.set()
                raise rate_limit_error
            upstox_results = dict(zip(calls, results))
        else:
            warnings.append("Upstox token was not supplied; authenticated data is unavailable.")
            upstox_results = {fetch: ValueError("Upstox token unavailable.") for fetch in calls}

    def upstox(fetch):
        result = upstox_results[fetch]
        if isinstance(result, Exception):
            raise ValueError("Upstox request unavailable.") from result
        return result

    return await asyncio.to_thread(
        _assemble_market_context, symbol, horizon, instrument_key, sector_symbol, supplemental,
        intraday_stream, values["quote modules"], values["daily history"], values["balance sheet"],
        values["income statement"], values["cash flow"], values["option chain"],
        values["S&P 500 history"], values.get("sector history"), upstox,
        tolerant=True, intake_warnings=warnings,
    )


def main() -> None:
    """Run phase one only; interactive intraday users should use the stream API."""
    parser = argparse.ArgumentParser(description="Build and locally assess a stock context without invoking an LLM.")
    parser.add_argument("symbol", help="Yahoo symbol, e.g. RELIANCE.NS")
    parser.add_argument("--horizon", choices=[InvestmentHorizon.LONG_TERM_EQUITY.value,
                                              InvestmentHorizon.SHORT_TERM_TRADING.value],
                        default=InvestmentHorizon.LONG_TERM_EQUITY.value)
    parser.add_argument("--instrument-key", help="Optional Upstox instrument key")
    parser.add_argument("--sector-symbol", help="Optional Yahoo sector benchmark")
    parser.add_argument("--supplemental", type=Path, help="JSON file of independently verified supplemental sections")
    args = parser.parse_args()
    supplemental = None
    if args.supplemental is not None:
        supplemental = json.loads(args.supplemental.read_text(encoding="utf-8"))
        if not isinstance(supplemental, dict):
            raise ValueError("Supplemental JSON must contain an object.")
    context = fetch_and_build_market_context(
        args.symbol, InvestmentHorizon(args.horizon), instrument_key=args.instrument_key,
        sector_symbol=args.sector_symbol, supplemental=supplemental,
    )
    print(json.dumps({"context": context.model_dump(mode="json"),
                      "gatekeeper": local_gatekeeper(context).model_dump(mode="json")},
                     allow_nan=False))


if __name__ == "__main__":
    main()

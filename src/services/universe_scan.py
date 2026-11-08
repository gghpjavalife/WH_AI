"""Asynchronous, in-memory Phase 1 scans; no LLM calls or persistent storage."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any

from core.logging import log_failure
from services.stock_context import ProviderRateLimitError, async_fetch_and_build_market_context
from services.stock_context_models import InvestmentHorizon, local_gatekeeper


@dataclass(frozen=True)
class _Stock:
    symbol: str
    horizon: InvestmentHorizon
    instrument_key: str | None


def _validate_universe(stock_universe: list[dict]) -> list[_Stock]:
    if not isinstance(stock_universe, list):
        raise TypeError("stock_universe must be a list.")
    stocks = []
    for index, item in enumerate(stock_universe):
        if not isinstance(item, dict):
            raise ValueError(f"Universe item {index} must be an object.")
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError(f"Universe item {index} requires a nonempty symbol.")
        key = item.get("instrument_key")
        if key is not None and (not isinstance(key, str) or not key.strip()):
            raise ValueError(f"Universe item {index} has an invalid instrument key.")
        try:
            horizon = InvestmentHorizon(
                item.get("horizon", InvestmentHorizon.LONG_TERM_EQUITY)
            )
        except (ValueError, TypeError) as error:
            raise ValueError(f"Universe item {index} has an invalid horizon.") from error
        if horizon == InvestmentHorizon.INTRADAY_TRADING:
            raise ValueError(
                f"Universe item {index}: intraday requires a live D30 stream, "
                "which this snapshot runner does not supply."
            )
        stocks.append(_Stock(symbol.strip().upper(), horizon, key.strip() if key else None))
    return stocks


async def _scan_universe(
    stock_universe: list[dict],
    upstox_token: str,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    cancel: Event | None = None,
) -> list[dict[str, Any]]:
    """Return one result per input, preserving order and failures."""
    stocks = _validate_universe(stock_universe)
    if not isinstance(upstox_token, str):
        raise TypeError("upstox_token must be a string.")
    semaphore = asyncio.Semaphore(20)
    rate_limit_event = asyncio.Event()
    started = time.monotonic()
    completed = 0
    total = len(stocks)
    last_log = started
    print(f"Universe scan | stocks: {total:,} | HTTP concurrency cap: 20", flush=True)

    async def scan(stock: _Stock) -> dict[str, Any]:
        nonlocal completed, last_log
        try:
            context = await async_fetch_and_build_market_context(
                stock.symbol, stock.horizon, instrument_key=stock.instrument_key,
                upstox_token=upstox_token, semaphore=semaphore,
                rate_limit_event=rate_limit_event,
            )
            gate = await asyncio.to_thread(local_gatekeeper, context)
            result = {
                "symbol": stock.symbol,
                "status": "ok",
                "early_exit": not gate.is_eligible_for_llm,
                "context": context.model_dump(mode="json"),
                "local_gatekeeper": gate.model_dump(mode="json"),
                "error": None,
            }
        except ProviderRateLimitError:
            rate_limit_event.set()
            raise
        except (OSError, RuntimeError, TypeError, ValueError, KeyError, IndexError) as error:
            result = {
                "symbol": stock.symbol, "status": "error", "early_exit": True,
                "context": None, "local_gatekeeper": None,
                "error": log_failure(f"Stock context ingestion {stock.symbol}", error),
            }
        completed += 1
        if on_progress is not None:
            on_progress(completed, total)
        now = time.monotonic()
        if completed == total or completed % 100 == 0 or now - last_log >= 10:
            elapsed = max(now - started, 1e-9)
            print(
                f"Universe scan | {completed:,}/{total:,} complete | "
                f"{completed / elapsed:.1f} stocks/s | {elapsed:.1f}s elapsed",
                flush=True,
            )
            last_log = now
        return result

    tasks = [asyncio.create_task(scan(stock)) for stock in stocks]
    gathered = asyncio.gather(*tasks)
    try:
        if cancel is None:
            return await gathered
        while not gathered.done():
            if cancel.is_set():
                gathered.cancel()
                try:
                    await gathered
                except asyncio.CancelledError:
                    pass
                raise asyncio.CancelledError("Universe scan cancelled.")
            await asyncio.wait((gathered,), timeout=0.1)
        return await gathered
    finally:
        if not gathered.done():
            gathered.cancel()
            try:
                await gathered
            except asyncio.CancelledError:
                pass
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def async_execute_universe_scan(
    stock_universe: list[dict], upstox_token: str
) -> list[str]:
    """Return ordered JSON records with context, gate and failures for every input.

    Inputs use ``symbol``, optional ``instrument_key``, and optional ``horizon``.
    ``early_exit`` is true for failed or locally blocked stocks. This function
    neither calls an LLM nor persists its inputs, outputs, or progress.
    """
    results = await _scan_universe(stock_universe, upstox_token)
    return [json.dumps(result, ensure_ascii=False, allow_nan=False) for result in results]

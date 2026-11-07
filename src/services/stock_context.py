"""Two-phase market context intake, independent of the application's AI workflows.

All monetary amounts are in the instrument's quote currency. Ownership and
yield fields are fractions; fields named ``*_pct`` or ``*_percentage`` are
percentage points unless their documented source is a fractional holding.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from yahooquery import Ticker

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


def _daily_history(raw: Any, symbol: str) -> pd.DataFrame:
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        raise ValueError(f"Yahoo returned no daily candle data for {symbol}.")
    frame = raw
    if isinstance(frame.index, pd.MultiIndex) and "symbol" in frame.index.names:
        try:
            frame = frame.xs(symbol, level="symbol")
        except KeyError as exc:
            raise ValueError(f"Yahoo daily candles do not include {symbol}.") from exc
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
    from .stock_context_financials import calculate_financial_metrics, verify_altman_inputs

    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("A Yahoo ticker symbol is required.")
    horizon = InvestmentHorizon(horizon)
    if horizon == InvestmentHorizon.INTRADAY_TRADING and intraday_stream is None:
        raise ValueError("Intraday context requires a fresh Upstox streaming snapshot.")
    if intraday_stream is not None and instrument_key != intraday_stream.instrument_key:
        raise ValueError("The streaming and requested Upstox instrument keys must match.")

    ticker = yahoo_factory(symbol, timeout=settings.http_read_timeout_seconds)
    raw_modules = ticker.get_modules(_MODULES)
    modules = yahoo_modules(raw_modules, symbol)
    history_raw = ticker.history(period="1y", interval="1d")
    history = _daily_history(history_raw, symbol)
    balance = ticker.balance_sheet(frequency="a")
    income = ticker.income_statement(frequency="a", trailing=False)
    cash = ticker.cash_flow(frequency="a", trailing=False)
    chain = ticker.option_chain
    mapped = map_yahoo_modules(modules, symbol)
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
    benchmark_raw = yahoo_factory("^GSPC", timeout=settings.http_read_timeout_seconds).history(period="1y", interval="1d")
    benchmark = _daily_history(benchmark_raw, "^GSPC")
    sector_raw = None
    sector = None
    if sector_symbol:
        sector_symbol = sector_symbol.strip().upper()
        if not sector_symbol:
            raise ValueError("Sector ticker must not be blank.")
        sector_raw = yahoo_factory(sector_symbol, timeout=settings.http_read_timeout_seconds).history(period="1y", interval="1d")
        sector = _daily_history(sector_raw, sector_symbol)

    warnings = [
        "Yahoo option_chain replaces missing cells with zero; reported option totals may be incomplete.",
        "Promoter pledging and other unavailable fields require a separately verified source.",
    ]
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
    quote = None
    if instrument_key:
        try:
            quote = fetch_upstox_quote(instrument_key, session=http_session)
        except (requests.RequestException, ValueError):
            message = "Upstox full quote unavailable; circuit proximity cannot be assessed."
            logger.warning("%s", message)
            warnings.append(message)
        if quote is not None:
            source_data["upstox_quote"] = json_value(quote)
            fields = quote_fields(quote)
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
            headlines, news_pages = fetch_upstox_news(instrument_key, session=http_session)
            payload["news"]["headlines"] = headlines
            source_data["upstox_news_pages"] = json_value(news_pages)
        except (requests.RequestException, ValueError):
            message = "Upstox news unavailable; an empty headline list does not imply no news."
            logger.warning("%s", message)
            warnings.append(message)
        if instrument_key.startswith(("NSE_EQ|", "BSE_EQ|")):
            try:
                holdings = fetch_upstox_shareholdings(instrument_key, session=http_session)
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
                actions = fetch_upstox_corporate_actions(instrument_key, session=http_session)
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
    payload["analyst"]["upside_percentage"] = (
        (target / current - 1) * 100 if target is not None and current is not None and current > 0 else None
    )
    return StockSignalContext.model_validate(payload)


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

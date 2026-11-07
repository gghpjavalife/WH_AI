"""Provider normalization for the stock context engine."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import pandas as pd


def json_value(value: Any) -> Any:
    """Preserve provider rows and indices in JSON-compatible form."""
    if isinstance(value, pd.DataFrame):
        return {
            "index_names": list(value.index.names),
            "index": [json_value(x) for x in value.index.tolist()],
            "columns": [str(x) for x in value.columns],
            "rows": [[json_value(x) for x in row] for row in value.itertuples(index=False, name=None)],
        }
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if value is pd.NA or value is pd.NaT or value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Unsupported provider value type: {type(value).__name__}")


def number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("raw")
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def integer(value: Any) -> int | None:
    result = number(value)
    return int(result) if result is not None and result.is_integer() else None


def first_number(*values: Any) -> float | None:
    for value in values:
        result = number(value)
        if result is not None:
            return result
    return None


def yahoo_modules(payload: Any, symbol: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get(symbol), dict):
        raise ValueError(f"Yahoo quote summary is unavailable for {symbol}.")
    modules = payload[symbol]
    if not modules:
        raise ValueError(f"Yahoo quote summary is empty for {symbol}.")
    return modules


def map_yahoo_modules(modules: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Map Yahoo quoteSummary modules without treating absent values as zero."""
    def module(name: str) -> dict[str, Any]:
        value = modules.get(name)
        return value if isinstance(value, dict) else {}

    profile, stats = module("summaryProfile"), module("defaultKeyStatistics")
    financial, price = module("financialData"), module("price")
    detail, holders = module("summaryDetail"), module("majorHoldersBreakdown")
    market_cap = first_number(price.get("marketCap"), detail.get("marketCap"))
    current_price = first_number(financial.get("currentPrice"), price.get("regularMarketPrice"))
    float_shares = first_number(stats.get("floatShares"))
    institution = first_number(holders.get("institutionsPercentHeld"), stats.get("heldPercentInstitutions"))
    insider = first_number(holders.get("insidersPercentHeld"), stats.get("heldPercentInsiders"))
    retail = 1 - institution - insider if institution is not None and insider is not None and institution + insider <= 1 else None
    target = first_number(financial.get("targetMeanPrice"))
    dividend = first_number(detail.get("dividendYield"))
    debt_equity_pct = first_number(financial.get("debtToEquity"))
    return {
        "current_price": current_price,
        "profile": {
            "official_name": price.get("longName") or price.get("shortName"),
            "ticker": symbol,
            "sector": profile.get("sector"), "industry": profile.get("industry"),
            "full_summary": profile.get("longBusinessSummary"), "country": profile.get("country"),
            "full_time_employees": integer(profile.get("fullTimeEmployees")),
        },
        "fundamentals": {
            "market_cap": market_cap,
            "float_market_cap": float_shares * current_price if float_shares is not None and current_price is not None else None,
            "trailing_pe": number(detail.get("trailingPE")), "forward_pe": number(stats.get("forwardPE")),
            "peg_ratio": number(stats.get("pegRatio")), "price_to_book": number(stats.get("priceToBook")),
            "price_to_sales": number(detail.get("priceToSalesTrailing12Months")),
            "enterprise_value_to_ebitda": number(stats.get("enterpriseToEbitda")),
            "gross_margin": number(financial.get("grossMargins")),
            "operating_margin": number(financial.get("operatingMargins")),
            "net_margin": first_number(financial.get("profitMargins"), stats.get("profitMargins")),
            "roe": number(financial.get("returnOnEquity")), "roa": number(financial.get("returnOnAssets")),
            "debt_to_equity": debt_equity_pct / 100 if debt_equity_pct is not None else None,
            "free_cash_flow": number(financial.get("freeCashflow")),
            "dividend_yield": dividend if dividend is None or 0 <= dividend <= 1 else None,
            "shares_outstanding": integer(stats.get("sharesOutstanding")),
        },
        "shareholders": {
            "pct_held_by_institutions": institution, "pct_held_by_insiders": insider,
            "pct_held_by_retail": retail,
            "total_institutional_holders": integer(holders.get("institutionsCount")),
            "short_interest_pct_float": number(stats.get("shortPercentOfFloat")),
        },
        "analyst": {
            "consensus_rating": financial.get("recommendationKey"),
            "mean_price_target": target, "high_price_target": number(financial.get("targetHighPrice")),
            "low_price_target": number(financial.get("targetLowPrice")),
            "upside_percentage": (target / current_price - 1) * 100
            if target is not None and current_price is not None and current_price > 0 else None,
            # A calendar quarter is not necessarily the company's fiscal Q4.
            "earnings_surprise_pct_q4": None,
        },
    }


def option_ratios(chain: Any) -> dict[str, float | None]:
    """Aggregate the entire returned chain, retaining unavailable ratios as None."""
    result = {"put_call_volume_ratio": None, "put_call_oi_ratio": None}
    if not isinstance(chain, pd.DataFrame) or chain.empty or "optionType" not in chain.index.names:
        return result
    types = chain.index.get_level_values("optionType").astype(str).str.lower()
    for field, column in (("put_call_volume_ratio", "volume"), ("put_call_oi_ratio", "openInterest")):
        if column not in chain:
            continue
        values = pd.to_numeric(chain[column], errors="coerce")
        # yahooquery fills missing cells with zero before returning the frame,
        # so a zero cannot safely be distinguished from an absent value.
        if values.isna().any() or (values <= 0).any():
            continue
        puts = values[types.isin(["put", "puts"])]
        calls = values[types.isin(["call", "calls"])]
        if not puts.empty and not calls.empty and calls.sum() > 0:
            result[field] = float(puts.sum() / calls.sum())
    return result


def returns_from_history(stock: Any, sector: Any, benchmark: Any) -> dict[str, Any]:
    """Five/30 session sector returns and aligned daily S&P 500 beta."""
    result = {"sector_return_5d": None, "sector_return_30d": None,
              "beta_to_sp500": None, "market_regime_bias": None}

    def closes(frame: Any) -> pd.Series:
        if not isinstance(frame, pd.DataFrame) or "close" not in frame:
            return pd.Series(dtype=float)
        series = pd.to_numeric(frame["close"], errors="coerce")
        if isinstance(series.index, pd.MultiIndex) and "date" in series.index.names:
            series.index = series.index.get_level_values("date")
        return series.dropna().sort_index()

    sector_close, benchmark_close, stock_close = map(closes, (sector, benchmark, stock))
    for days in (5, 30):
        if len(sector_close) > days and sector_close.iloc[-days - 1] > 0:
            result[f"sector_return_{days}d"] = float(
                (sector_close.iloc[-1] / sector_close.iloc[-days - 1] - 1) * 100
            )
    if len(benchmark_close) >= 200:
        latest, average = benchmark_close.iloc[-1], benchmark_close.tail(200).mean()
        result["market_regime_bias"] = "Risk-On" if latest > average else "Risk-Off" if latest < average else "Neutral"
    aligned = pd.concat(
        [stock_close.pct_change(fill_method=None), benchmark_close.pct_change(fill_method=None)],
        axis=1, join="inner",
    ).dropna()
    if len(aligned) >= 30:
        variance = aligned.iloc[:, 1].var()
        if variance > 0:
            result["beta_to_sp500"] = float(aligned.iloc[:, 0].cov(aligned.iloc[:, 1]) / variance)
    return result

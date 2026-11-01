"""Company fundamentals retrieved from Yahoo Finance's public quote-summary feed."""

from __future__ import annotations

from math import isfinite
from urllib.parse import quote

import requests
import streamlit as st

from core.constants import RuntimeDefault
from features.equities.market_data import normalize_symbol

_FUNDAMENTALS_TIMEOUT = RuntimeDefault.MARKET_DATA_TIMEOUT.value
_FUNDAMENTALS_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary"

_FUNDAMENTAL_FIELDS = (
    ("marketCap", "Market capitalization", "price", "marketCap", "currency"),
    ("trailingPE", "Trailing P/E", "summaryDetail", "trailingPE", ""),
    ("forwardPE", "Forward P/E", "summaryDetail", "forwardPE", ""),
    ("priceToBook", "Price / book", "defaultKeyStatistics", "priceToBook", ""),
    ("pegRatio", "PEG ratio", "defaultKeyStatistics", "pegRatio", ""),
    ("returnOnEquity", "Return on equity", "financialData", "returnOnEquity", "percent"),
    ("profitMargins", "Profit margin", "financialData", "profitMargins", "percent"),
    ("revenueGrowth", "Revenue growth", "financialData", "revenueGrowth", "percent"),
    ("earningsGrowth", "Earnings growth", "financialData", "earningsGrowth", "percent"),
    ("debtToEquity", "Debt / equity", "financialData", "debtToEquity", ""),
    ("dividendYield", "Dividend yield", "summaryDetail", "dividendYield", "percent"),
    ("freeCashflow", "Free cash flow", "financialData", "freeCashflow", "currency"),
)


class FundamentalsDataError(RuntimeError):
    """Company fundamentals could not be retrieved or validated."""


def _raw_value(module: object, key: str) -> float | None:
    if not isinstance(module, dict):
        return None
    metric = module.get(key)
    if isinstance(metric, dict):
        metric = metric.get("raw")
    if isinstance(metric, bool) or not isinstance(metric, (float, int)):
        return None
    value = float(metric)
    return value if isfinite(value) else None


@st.cache_data(ttl=6 * 3600, max_entries=500, show_spinner=False)
def fetch_company_fundamentals(symbol: str) -> dict[str, object]:
    """Fetch available public company ratios for one NSE/BSE ticker."""
    normalized = normalize_symbol(symbol)
    yahoo_symbol = (
        normalized if normalized.endswith(".BO") else f"{normalized}.NS"
    )
    modules = "price,summaryDetail,defaultKeyStatistics,financialData"
    try:
        response = requests.get(
            f"{_FUNDAMENTALS_URL}/{quote(yahoo_symbol, safe='')}",
            params={"modules": modules},
            headers={"User-Agent": "Mozilla/5.0 WealthHomeAI/1.0"},
            timeout=_FUNDAMENTALS_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise FundamentalsDataError(
            f"Yahoo Finance fundamentals are temporarily unavailable for {normalized}."
        ) from error

    quote_summary = payload.get("quoteSummary") if isinstance(payload, dict) else None
    results = quote_summary.get("result") if isinstance(quote_summary, dict) else None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise FundamentalsDataError(
            f"Yahoo Finance returned no company fundamentals for {normalized}."
        )
    result = results[0]
    metrics: list[dict[str, object]] = []
    for _, label, module_name, field_name, unit in _FUNDAMENTAL_FIELDS:
        value = _raw_value(result.get(module_name), field_name)
        if value is None:
            continue
        if unit == "percent":
            formatted = f"{value * 100:.2f}%"
        elif unit == "currency":
            price_module = result.get("price")
            currency = (
                price_module.get("currency")
                if isinstance(price_module, dict)
                and isinstance(price_module.get("currency"), str)
                else ""
            )
            formatted = (
                f"{currency} {value:,.2f}" if currency else f"{value:,.2f}"
            )
        else:
            formatted = f"{value:,.2f}"
        metrics.append(
            {
                "Fundamental": label,
                "Value": formatted,
                "raw": value,
            }
        )

    if not metrics:
        raise FundamentalsDataError(
            f"Yahoo Finance returned no usable company fundamentals for {normalized}."
        )
    return {
        "symbol": normalized,
        "source": "Yahoo Finance public quote-summary data",
        "metrics": metrics,
        "values": {
            key: value
            for key, label, module_name, field_name, _ in _FUNDAMENTAL_FIELDS
            if (value := _raw_value(result.get(module_name), field_name)) is not None
        },
        "available_count": len(metrics),
        "expected_count": len(_FUNDAMENTAL_FIELDS),
    }

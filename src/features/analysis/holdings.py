"""Technical, fundamental, and news research for portfolio stock holdings."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pandas as pd
import requests
import streamlit as st

from features.equities.market_data import (
    MarketDataError,
    assess_stock,
    calculate_indicators,
    fetch_daily_history,
)
from .fundamentals import FundamentalsDataError, fetch_company_fundamentals
from .news import CompanyNewsError, fetch_company_news

_FUNDAMENTAL_RULES = (
    ("trailingPE", "Trailing P/E", lambda value: 1 if 0 < value <= 25 else -1 if value <= 0 or value > 40 else 0),
    ("forwardPE", "Forward P/E", lambda value: 1 if 0 < value <= 25 else -1 if value <= 0 or value > 40 else 0),
    ("priceToBook", "Price / book", lambda value: 1 if 0 < value <= 3 else -1 if value >= 8 else 0),
    ("pegRatio", "PEG ratio", lambda value: 1 if 0 < value <= 1.5 else -1 if value >= 3 else 0),
    ("returnOnEquity", "Return on equity", lambda value: 1 if value >= 0.15 else -1 if value < 0.05 else 0),
    ("profitMargins", "Profit margin", lambda value: 1 if value > 0.10 else -1 if value <= 0 else 0),
    ("revenueGrowth", "Revenue growth", lambda value: 1 if value > 0 else -1 if value < 0 else 0),
    ("earningsGrowth", "Earnings growth", lambda value: 1 if value > 0 else -1 if value < 0 else 0),
    ("debtToEquity", "Debt / equity", lambda value: 1 if value <= 100 else -1 if value >= 200 else 0),
    ("freeCashflow", "Free cash flow", lambda value: 1 if value > 0 else -1 if value < 0 else 0),
)


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _ticker_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip().upper()


def _text(value: Any, fallback: str = "") -> str:
    if value is None or pd.isna(value):
        return fallback
    text = str(value).strip()
    return text if text else fallback


def build_portfolio_holding_rows(
    equities: pd.DataFrame,
    trading_positions: pd.DataFrame,
    mutual_funds: pd.DataFrame,
) -> list[dict[str, Any]]:
    """Keep a small normalized view for summaries and tests."""
    rows = []
    for asset_type, frame, name_column, quantity_column, price_column in (
        ("Equity", equities, "Company", "Qty", "LTP"),
        ("Trading", trading_positions, "Company", "Qty", "LTP"),
        ("Mutual fund", mutual_funds, "Fund", "Units", "Latest_NAV"),
    ):
        for _, holding in frame.iterrows():
            ticker = _ticker_value(holding.get("Ticker"))
            quantity = _number(holding.get(quantity_column))
            price = _number(holding.get(price_column))
            cost = _number(
                holding.get("Avg_NAV")
                if asset_type == "Mutual fund"
                else holding.get("Avg_Price")
            )
            investment = abs(quantity) * cost
            profit_loss = (
                quantity * (price - cost)
                if asset_type in {"Equity", "Trading"}
                else abs(quantity) * (price - cost)
            )
            rows.append(
                {
                    "type": asset_type,
                    "name": _text(
                        holding.get(name_column), ticker or "Unnamed holding"
                    ),
                    "ticker": ticker,
                    "quantity": quantity,
                    "current_value": abs(quantity) * price,
                    "return_pct": profit_loss / investment * 100 if investment else 0.0,
                    "signal_key": ticker,
                    "nav_date": _text(holding.get("NAV_Date")),
                    "isin": _text(holding.get("ISIN")),
                }
            )
    return rows


def _fundamental_component(
    fundamentals: dict[str, Any] | None,
) -> tuple[float | None, list[dict[str, object]]]:
    if not fundamentals:
        return None, []
    values = fundamentals.get("values")
    if not isinstance(values, dict):
        return None, []
    scored: list[dict[str, object]] = []
    for key, label, rule in _FUNDAMENTAL_RULES:
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        score = rule(float(value))
        scored.append(
            {
                "Fundamental": label,
                "Value": value,
                "Assessment": (
                    "Positive" if score > 0 else "Negative" if score < 0 else "Neutral"
                ),
                "Score": score,
            }
        )
    if len(scored) < 3:
        return None, scored
    return (
        sum(int(metric["Score"]) for metric in scored) / len(scored),
        scored,
    )


def combine_stock_signal(
    technical: dict[str, Any] | None,
    fundamentals: dict[str, Any] | None,
    news: dict[str, Any] | None,
) -> dict[str, Any]:
    """Blend available technical, fundamental, and headline evidence."""
    components: list[tuple[str, float, float]] = []
    if technical:
        evaluated = int(technical.get("evaluated_checks", 0))
        bullish = int(technical.get("bullish_checks", 0))
        bearish = int(technical.get("bearish_checks", 0))
        if evaluated >= 10:
            components.append(
                ("Technical", (bullish - bearish) / evaluated, 0.5)
            )

    fundamental_score, fundamental_checks = _fundamental_component(fundamentals)
    if fundamental_score is not None:
        components.append(("Fundamentals", fundamental_score, 0.3))

    articles = news.get("headlines", []) if news else []
    scored_news = [
        int(article["sentiment"])
        for article in articles
        if isinstance(article, dict) and article.get("sentiment") in {-1, 0, 1}
    ]
    if scored_news:
        components.append(
            ("News headlines", sum(scored_news) / len(scored_news), 0.2)
        )

    if not components:
        return {
            "signal": "Not rated",
            "score": None,
            "reason": (
                "Not enough technical checks, company fundamentals, or recent "
                "headlines are available to calculate a signal."
            ),
            "fundamental_checks": fundamental_checks,
        }

    total_weight = sum(weight for _, _, weight in components)
    score = sum(value * weight for _, value, weight in components) / total_weight
    if score >= 0.6:
        signal = "Strong Buy"
    elif score >= 0.2:
        signal = "Buy"
    elif score <= -0.6:
        signal = "Strong Sell"
    elif score <= -0.2:
        signal = "Sell"
    else:
        signal = "Hold"
    evidence = ", ".join(
        f"{name} {value:+.2f}" for name, value, _ in components
    )
    return {
        "signal": signal,
        "score": score,
        "reason": f"{signal} · combined evidence score {score:+.2f} ({evidence}).",
        "fundamental_checks": fundamental_checks,
    }


def _analyze_selected_stock(ticker: str) -> dict[str, Any]:
    technical = None
    fundamentals = None
    news = None
    errors: dict[str, str] = {}
    try:
        indicators = calculate_indicators(fetch_daily_history(ticker))
        technical = assess_stock(indicators)
        technical["raw_indicators"] = indicators
    except (MarketDataError, requests.RequestException, ValueError) as error:
        errors["technical"] = str(error)
    try:
        fundamentals = fetch_company_fundamentals(ticker)
    except FundamentalsDataError as error:
        errors["fundamentals"] = str(error)
    try:
        news = fetch_company_news(ticker)
    except CompanyNewsError as error:
        errors["news"] = str(error)
    combined = combine_stock_signal(technical, fundamentals, news)
    return {
        **combined,
        "technical": technical,
        "fundamentals": fundamentals,
        "news": news,
        "errors": errors,
    }


def _build_holding_scan_row(
    holding: pd.Series,
    research: dict[str, Any],
) -> tuple[dict[str, object], dict[str, object]]:
    from features.equities.scanner import build_scan_table_payload

    ticker = _ticker_value(holding.get("Ticker"))
    company = _text(holding.get("Company"), ticker)
    technical = research.get("technical")
    indicators = (
        technical.get("raw_indicators", {}) if isinstance(technical, dict) else {}
    )
    assessment = technical or {}
    market_keys = {
        "SMA_20": "sma20",
        "SMA_50": "sma50",
        "SMA_200": "sma200",
        "EMA_12": "ema12",
        "EMA_26": "ema26",
        "RSI_14": "rsi14",
        "MACD": "macd",
        "MACD_signal": "macd_signal",
        "MACD_histogram": "macd_histogram",
        "ATR_14": "atr14",
        "ATR_%": "atr_pct",
        "ADX_14": "adx14",
        "Plus_DI_14": "plus_di14",
        "Minus_DI_14": "minus_di14",
        "CCI_20": "cci20",
        "MFI_14": "mfi14",
        "OBV_change_20d": "obv_change20",
        "Bollinger_lower": "bollinger_lower",
        "Bollinger_middle": "bollinger_middle",
        "Bollinger_upper": "bollinger_upper",
        "Bollinger_width_%": "bollinger_width_pct",
        "Stochastic_%K": "stochastic_k",
        "Stochastic_%D": "stochastic_d",
        "Volatility_20d_annualized_%": "volatility20_annualized_pct",
        "Return_1m_%": "return_1m_pct",
        "Return_3m_%": "return_3m_pct",
        "Average_volume_20d": "average_volume_20",
        "Volume_vs_average": "relative_volume",
        "52w_high": "high_52w",
        "52w_low": "low_52w",
        "52w_range_position_%": "price_52w_position_pct",
        "Daily_change_%": "daily_change_pct",
        "Price": "price",
    }
    scanner_row: dict[str, object] = {
        key: indicators.get(source)
        for key, source in market_keys.items()
        if source in indicators
    }
    scanner_row.update(
        {
            "Ticker": ticker,
            "Price": _number(holding.get("LTP")),
            "Recommendation": str(research.get("signal", "Not rated")).upper(),
            "Signal_score": _number(research.get("score")),
            "Positive_signals": int(assessment.get("bullish_checks", 0)),
            "Total_signals": int(assessment.get("evaluated_checks", 0)),
            "Reason": str(research.get("reason", "No research is available.")),
            "Indicator_checks": [
                (
                    item.get("Indicator", ""),
                    item.get("Value", ""),
                    item.get("Signal", "Neutral"),
                )
                for item in assessment.get("indicators", [])
                if isinstance(item, dict)
            ],
        }
    )
    payload, config = build_scan_table_payload([scanner_row], {ticker: f"{ticker} · {company}"})
    item = payload[0]
    market_value = holding.get("Market_Value", holding.get("Gross_Exposure"))
    item.update(
        {
            "ticker": ticker,
            "quantity": _number(holding.get("Qty")),
            "average": _number(holding.get("Avg_Price")),
            "investment": _number(holding.get("Investment")),
            "holdingValue": _number(market_value),
            "pnl": _number(holding.get("P&L")),
            "returnPct": _number(holding.get("Returns_%")),
            "allocationPct": _number(holding.get("Allocation_%")),
            "extraGroups": [
                [
                    "Holding details",
                    [
                        ["holding_quantity", "Quantity"],
                        ["holding_average", "Average cost"],
                        ["holding_value", "Market value"],
                        ["holding_pnl", "P&L"],
                        ["holding_return", "Return"],
                        ["holding_allocation", "Portfolio share"],
                    ],
                ]
            ],
            "extraSections": [],
            "summary": (
                f"Quantity {holding.get('Qty', 0)} · Average cost "
                f"₹{_number(holding.get('Avg_Price')):,.2f} · Portfolio share "
                f"{_number(holding.get('Allocation_%')):.2f}%"
            ),
        }
    )
    holding_values = {
        "holding_quantity": f"{_number(holding.get('Qty')):,.2f}",
        "holding_average": f"₹{_number(holding.get('Avg_Price')):,.2f}",
        "holding_value": f"₹{_number(market_value):,.2f}",
        "holding_pnl": f"₹{_number(holding.get('P&L')):,.2f}",
        "holding_return": f"{_number(holding.get('Returns_%')):+.2f}%",
        "holding_allocation": f"{_number(holding.get('Allocation_%')):.2f}%",
    }
    holding_tones = {
        "holding_quantity": "neu",
        "holding_average": "neu",
        "holding_value": "neu",
        "holding_pnl": "pos" if _number(holding.get("P&L")) >= 0 else "neg",
        "holding_return": "pos" if _number(holding.get("Returns_%")) >= 0 else "neg",
        "holding_allocation": "neu",
    }
    item["ind"].update(
        {
            key: [value, holding_tones[key]]
            for key, value in holding_values.items()
        }
    )
    fundamental_checks = {
        str(check.get("Fundamental")): str(check.get("Assessment", "Neutral")).lower()
        for check in research.get("fundamental_checks", [])
        if isinstance(check, dict)
    }
    fundamentals = research.get("fundamentals")
    if isinstance(fundamentals, dict):
        fundamental_items = []
        for index, metric in enumerate(fundamentals.get("metrics", [])):
            if not isinstance(metric, dict):
                continue
            label = str(metric.get("Fundamental", "Fundamental"))
            key = f"fundamental_{index}"
            assessment_name = fundamental_checks.get(label, "neutral")
            tone = "pos" if assessment_name == "positive" else "neg" if assessment_name == "negative" else "neu"
            item["ind"][key] = [str(metric.get("Value", "—")), tone]
            fundamental_items.append([key, label])
        if fundamental_items:
            item["extraGroups"].append(["Fundamentals", fundamental_items])
            item["extraSections"].append(
                {
                    "title": "Fundamental screen",
                    "note": (
                        f"{fundamentals.get('available_count', 0)} of "
                        f"{fundamentals.get('expected_count', 0)} company metrics "
                        f"available from {fundamentals.get('source', 'public data')}. "
                        "Assessments use general screening heuristics, not "
                        "sector-adjusted valuation rules."
                    ),
                }
            )
    elif research.get("errors", {}).get("fundamentals"):
        item["extraSections"].append(
            {
                "title": "Fundamentals",
                "note": str(research["errors"]["fundamentals"]),
            }
        )

    news = research.get("news")
    if isinstance(news, dict):
        headlines = []
        for article in news.get("headlines", []):
            if not isinstance(article, dict):
                continue
            sentiment = article.get("sentiment", 0)
            headlines.append(
                {
                    "title": str(article.get("title", "")),
                    "url": str(article.get("url", "")),
                    "publisher": str(article.get("publisher", "")),
                    "published": str(article.get("published", "")),
                    "sentimentLabel": {
                        1: "Positive terms",
                        0: "Neutral / mixed",
                        -1: "Negative terms",
                    }.get(sentiment, "Neutral / mixed"),
                }
            )
        item["extraSections"].append(
            {
                "title": "Recent company headlines",
                "note": (
                    f"Source: {news.get('source', 'Public headlines')}. "
                    + (
                        "Headline sentiment is a keyword-only context signal."
                        if headlines
                        else "No recent headlines were returned."
                    )
                ),
                "items": headlines,
            }
        )
    elif research.get("errors", {}).get("news"):
        item["extraSections"].append(
            {
                "title": "Recent company headlines",
                "note": str(research["errors"]["news"]),
            }
        )

    if research.get("errors", {}).get("technical"):
        item["extraSections"].insert(
            0,
            {
                "title": "Technical indicators",
                "note": str(research["errors"]["technical"]),
            },
        )

    config["signals"] = [
        "STRONG BUY",
        "BUY",
        "HOLD",
        "SELL",
        "STRONG SELL",
        "NOT RATED",
    ]
    config["showFilters"] = False
    config["exportColumns"] = [
        ["label", "Stock"],
        ["ticker", "Ticker"],
        ["signal", "Signal"],
        ["score", "Signal score"],
        ["price", "Price"],
        ["chg", "Day change %"],
        ["m1", "1 month %"],
        ["rsi", "RSI 14"],
        ["vol", "Volume vs average"],
        ["quantity", "Quantity"],
        ["average", "Average cost"],
        ["holdingValue", "Market value"],
        ["pnl", "P&L"],
        ["returnPct", "Return %"],
        ["allocationPct", "Portfolio share %"],
        ["reason", "Signal reason"],
    ]
    return item, config


def _holding_table_columns(layout: str) -> list[list[object]]:
    equity_columns: list[list[object]] = [
        ["ticker", "Ticker", 0],
        ["quantity", "Qty", 1, "number"],
        ["average", "Avg_Price", 1, "currency"],
        ["price", "LTP", 1, "currency"],
        ["investment", "Investment", 1, "currency"],
        ["holdingValue", "Market_Value", 1, "currency"],
        ["pnl", "P&L", 1, "currency-change"],
        ["returnPct", "Returns_%", 1, "percent"],
        ["allocationPct", "Allocation_%", 1, "percent"],
        ["signal", "Signal", 0],
    ]
    trading_columns: list[list[object]] = [
        ["stock", "Stock", 0],
        ["price", "Price", 1, "currency"],
        ["chg", "Day change", 1, "percent"],
        ["m1", "1 month", 1, "percent"],
        ["rsi", "RSI 14", 1, "number"],
        ["vol", "Volume", 1, "multiplier"],
        ["signal", "Signal", 0],
        ["strength", "Strength", 0],
        ["quantity", "Open quantity", 1, "number"],
        ["average", "Average price", 1, "currency"],
        ["holdingValue", "Gross exposure", 1, "currency"],
        ["pnl", "Open P&L", 1, "currency-change"],
        ["returnPct", "Return", 1, "percent"],
    ]
    layouts = {
        "home_equity": equity_columns,
        "equity": equity_columns,
        "trading": trading_columns,
    }
    return layouts.get(layout, equity_columns)


def render_stock_holdings_table(
    display: pd.DataFrame,
    *,
    key: str,
    theme: str,
    layout: str = "equity",
) -> None:
    """Render holdings using the shared market-scanner table and detail panels."""
    cache = st.session_state.setdefault("stock_research_cache", {})
    if not isinstance(cache, dict):
        cache = {}
        st.session_state.stock_research_cache = cache
    from features.equities.scan_table import show_scan_table

    if st.button("Refresh holdings research", key=f"refresh_{key}"):
        for ticker in {
            _ticker_value(value)
            for value in display.get("Ticker", pd.Series(dtype=str))
            if _ticker_value(value)
        }:
            cache.pop(f"selected_stock_research_{ticker}", None)
            fetch_daily_history.clear(ticker)
            fetch_company_fundamentals.clear(ticker)
            fetch_company_news.clear(ticker)
        st.rerun()

    payload = []
    config: dict[str, object] = {}
    missing = {
        _ticker_value(holding.get("Ticker"))
        for _, holding in display.iterrows()
        if _ticker_value(holding.get("Ticker"))
        and not isinstance(
            cache.get(
                f"selected_stock_research_{_ticker_value(holding.get('Ticker'))}"
            ),
            dict,
        )
    }
    if missing:
        with st.spinner(f"Loading indicators, fundamentals, and news for {len(missing)} holding(s)…"):
            with ThreadPoolExecutor(
                max_workers=min(4, len(missing)),
                thread_name_prefix="holdings-research",
            ) as executor:
                pending = {
                    executor.submit(_analyze_selected_stock, ticker): ticker
                    for ticker in missing
                }
                for future in as_completed(pending):
                    ticker = pending[future]
                    research_key = f"selected_stock_research_{ticker}"
                    cache[research_key] = future.result()
    for _, holding in display.iterrows():
        ticker = _ticker_value(holding.get("Ticker"))
        if not ticker:
            continue
        research = cache.get(f"selected_stock_research_{ticker}")
        if not isinstance(research, dict):
            continue
        row, row_config = _build_holding_scan_row(holding, research)
        payload.append(row)
        config = row_config
    if not payload:
        st.info("No holdings with a mapped stock ticker are available for research.")
        return
    config["columns"] = _holding_table_columns(layout)
    config["tableKey"] = key
    config["showColumnFilters"] = False
    show_scan_table(payload, config)

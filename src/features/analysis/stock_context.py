"""Broker-holding identity and local stock-context integration."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, get_args, get_origin

import pandas as pd
import requests

from core.logging import log_failure
from features.equities.market_data import normalize_symbol
from services.stock_context import fetch_and_build_market_context
from services.stock_context_models import (
    InvestmentHorizon, StockSignalContext, TechnicalIndicators, local_gatekeeper,
)

CONTEXT_TTL = timedelta(minutes=15)
CONTEXT_SECTIONS = (
    "profile", "fundamentals", "technicals", "shareholders", "intraday",
    "growth", "options", "futures", "analyst", "macro", "news",
)


def holding_identity(holding: pd.Series, layout: str, broker: str) -> tuple[str, str | None, InvestmentHorizon] | None:
    """Only equity keys are eligible for stock math; never substitute a derivative."""
    raw_ticker = holding.get("Ticker")
    if not isinstance(raw_ticker, str) or not raw_ticker.strip():
        return None
    try:
        ticker = normalize_symbol(raw_ticker)
    except ValueError:
        return None
    raw_key = holding.get("Instrument_Key")
    key = raw_key.strip().upper() if isinstance(raw_key, str) and raw_key.strip() else None
    if key and not key.startswith(("NSE_EQ|", "BSE_EQ|")):
        return None
    if layout == "trading" and key is None:
        return None
    if broker.casefold() == "upstox" and key is None:
        return None
    if key and key.startswith("BSE_EQ|"):
        symbol = ticker.removesuffix(".BO") + ".BO"
    else:
        symbol = ticker if ticker.endswith(".BO") else ticker + ".NS"
    horizon = (InvestmentHorizon.SHORT_TERM_TRADING if layout == "trading"
               else InvestmentHorizon.LONG_TERM_EQUITY)
    return symbol, key, horizon


def load_holding_context(identity: tuple[str, str | None, InvestmentHorizon],
                         *, analytics_available: bool) -> dict[str, Any]:
    symbol, key, horizon = identity
    if not analytics_available:
        key = None
    try:
        context = fetch_and_build_market_context(symbol, horizon, instrument_key=key)
        gate = local_gatekeeper(context)
    except (requests.RequestException, ValueError, TypeError, RuntimeError, OSError) as exc:
        return {"error": log_failure("Stock context ingestion", exc)}
    return {
        "context": context.model_dump(mode="json"),
        "gate": gate.model_dump(mode="json"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "notice": (
            None if key else "A usable Upstox instrument key and analytics token are required for "
            "quote, circuit and corporate-action data."
        ),
    }


def refresh_holding_contexts(cache: dict, identities: set[tuple[str, str | None, InvestmentHorizon]],
                             *, analytics_available: bool, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    for key, entry in list(cache.items()):
        if not isinstance(entry, tuple) or len(entry) != 2 or now - entry[0] >= CONTEXT_TTL:
            cache.pop(key, None)
    missing = [identity for identity in identities if (*identity, analytics_available) not in cache]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=min(4, len(missing)), thread_name_prefix="stock-context") as executor:
        pending = {
            executor.submit(load_holding_context, identity, analytics_available=analytics_available): identity
            for identity in missing
        }
        for future in as_completed(pending):
            identity = pending[future]
            try:
                result = future.result()
            except (requests.RequestException, ValueError, TypeError, RuntimeError, OSError,
                    KeyError, IndexError) as exc:
                result = {"error": log_failure(f"Stock context ingestion {identity[0]}", exc)}
            cache[(*identity, analytics_available)] = (now, result)


def _numeric_annotation(annotation: Any) -> bool:
    if annotation in (int, float):
        return True
    origin = get_origin(annotation)
    if origin is Annotated:
        return _numeric_annotation(get_args(annotation)[0])
    return any(_numeric_annotation(arg) for arg in get_args(annotation) if isinstance(arg, type)
               or get_origin(arg) is not None)


def context_filter_fields() -> list[tuple[str, str, str, str]]:
    fields = []
    for section in CONTEXT_SECTIONS:
        annotation = StockSignalContext.model_fields[section].annotation
        args = getattr(annotation, "__args__", ())
        model = next((arg for arg in args if hasattr(arg, "model_fields")), annotation)
        if not hasattr(model, "model_fields"):
            continue
        for field, info in model.model_fields.items():
            if field == "headlines":
                continue
            label = _LABELS.get(field, field.replace("_", " "))
            fields.append((section, field, f"{section.replace('_', ' ').title()} · {label}",
                           "number" if _numeric_annotation(info.annotation) else "text"))
    return fields


# Only identical periods and definitions are merged. In particular the
# statement-derived annual growth rates and 30-day volatility stay separate.
_TECHNICAL_ROWS = {
    "sma_20": "SMA_20", "sma_50": "SMA_50", "sma_200": "SMA_200",
    "rsi_14": "RSI_14", "macd_line": "MACD", "macd_signal": "MACD_signal",
    "macd_hist": "MACD_histogram", "bb_lower": "Bollinger_lower",
    "bb_middle": "Bollinger_middle", "bb_upper": "Bollinger_upper",
    "adx_14": "ADX_14", "money_flow_index_14": "MFI_14",
}
_TECHNICAL_GROUPS = {
    "ema_20": "Trend", "ema_50": "Trend", "ema_200": "Trend",
    "atr_percentile_14": "Risk and range", "chop_index_14": "Risk and range",
    "squeeze_alert": "Risk and range", "historical_volatility_30": "Risk and range",
    "current_volume": "Performance and volume", "avg_volume_3m": "Performance and volume",
    "volume_price_trend": "Performance and volume",
}
_FUNDAMENTAL_ROWS = {
    "market_cap": "Market capitalization", "trailing_pe": "Trailing P/E",
    "forward_pe": "Forward P/E", "peg_ratio": "PEG ratio",
    "price_to_book": "Price / book", "net_margin": "Profit margin",
    "roe": "Return on equity", "debt_to_equity": "Debt / equity",
    "free_cash_flow": "Free cash flow", "dividend_yield": "Dividend yield",
}
_GROUP_TITLES = {
    "profile": "Company profile", "fundamentals": "Fundamentals",
    "shareholders": "Ownership and trading activity", "intraday": "Intraday market",
    "growth": "Annual growth and earnings quality", "options": "Options",
    "futures": "Futures", "analyst": "Analyst consensus",
    "macro": "Market and sector", "news": "Corporate actions",
}
_LABELS = {
    "historical_volatility_30": "30-day annualized volatility",
    "avg_volume_3m": "Average volume (3 months)",
    "current_volume": "Current volume", "atr_percentile_14": "ATR percentile (14)",
    "chop_index_14": "Choppiness index (14)",
    "revenue_growth_yoy": "Annual revenue growth (year over year)",
    "earnings_growth_yoy": "Annual earnings growth (year over year)",
    "short_interest_pct_float": "Short interest / float",
    "promoter_pledged_shares_pct": "Promoter shares pledged",
    "pct_held_by_institutions": "Institutional ownership",
    "pct_held_by_insiders": "Insider ownership",
    "pct_held_by_retail": "Retail ownership",
}
_FRACTIONS = {
    ("fundamentals", name) for name in
    ("gross_margin", "operating_margin", "net_margin", "roe", "roa", "dividend_yield")
} | {
    ("shareholders", name) for name in
    ("pct_held_by_institutions", "pct_held_by_insiders", "pct_held_by_retail",
     "short_interest_pct_float", "promoter_pledged_shares_pct")
} | {("growth", "revenue_growth_yoy"), ("growth", "earnings_growth_yoy")}
_PERCENT_POINTS = {
    ("technicals", "historical_volatility_30"),
    ("intraday", "distance_from_vwap_pct"), ("intraday", "distance_to_lower_circuit_pct"),
    ("options", "implied_move_percentage"), ("futures", "front_month_premium_pct"),
    ("futures", "open_interest_change_pct"), ("analyst", "upside_percentage"),
    ("analyst", "earnings_surprise_pct_q4"), ("macro", "sector_return_5d"),
    ("macro", "sector_return_30d"),
}
_RAW_LABELS = {
    "SMA_20": "SMA 20", "SMA_50": "SMA 50", "SMA_200": "SMA 200",
    "RSI_14": "RSI 14", "MACD": "MACD", "MACD_signal": "MACD signal",
    "MACD_histogram": "MACD histogram", "Bollinger_lower": "Bollinger lower",
    "Bollinger_middle": "Bollinger middle", "Bollinger_upper": "Bollinger upper",
    "ADX_14": "ADX 14", "MFI_14": "MFI 14",
}

# Descriptions describe the context value, not an investment recommendation.
# Provider-supplied values can differ in freshness, currency and methodology.
_CONTEXT_INFO = {
    "profile": {
        "official_name": "Company name reported by the provider.",
        "ticker": "Provider ticker identifying the security and exchange suffix.",
        "isin": "International Securities Identification Number for the equity instrument.",
        "sector": "Provider's broad sector classification of the company.",
        "industry": "Provider's industry classification within the sector.",
        "full_summary": "Provider's description of the company's business.",
        "country": "Country reported in the company profile, not necessarily its listing exchange.",
        "full_time_employees": "Number of full-time employees reported by the provider.",
    },
    "fundamentals": {
        "market_cap": "Market value of outstanding shares, in the provider's quote currency.",
        "float_market_cap": "Estimated value of floating shares: float shares multiplied by current price, in quote currency.",
        "trailing_pe": "Share price divided by trailing earnings per share; a valuation multiple.",
        "forward_pe": "Share price divided by estimated future earnings per share; estimates can change.",
        "peg_ratio": "Price-to-earnings multiple relative to estimated earnings growth; provider methodology may vary.",
        "price_to_book": "Share price relative to book value per share; a valuation multiple.",
        "price_to_sales": "Share price relative to trailing sales per share; a valuation multiple.",
        "enterprise_value_to_ebitda": "Enterprise value divided by EBITDA; a valuation multiple before interest, tax, depreciation and amortization.",
        "gross_margin": "Gross profit as a percentage of revenue; the stored provider value is a fraction.",
        "operating_margin": "Operating income as a percentage of revenue; the stored provider value is a fraction.",
        "net_margin": "Net income as a percentage of revenue; the stored provider value is a fraction.",
        "roe": "Return on equity: income relative to shareholders' equity, displayed as a percentage.",
        "roa": "Return on assets: income relative to assets, displayed as a percentage.",
        "debt_to_equity": "Provider-reported debt relative to equity, expressed as a ratio; definitions can vary.",
        "free_cash_flow": "Provider-reported free cash flow, in its reported currency; negative values indicate an outflow.",
        "dividend_yield": "Provider-reported dividend yield as a percentage of share price; not a guaranteed future payout.",
        "shares_outstanding": "Number of shares outstanding reported by the provider.",
        "piotroski_f_score": "Statement-based financial strength score from 0 to 9; availability depends on sufficient comparable data.",
        "altman_z_score": "Statement-based Altman Z-score; below 1.81 is a risk flag only when the public-manufacturing model is applicable.",
        "altman_model_applicable": "Whether applicability of the public-manufacturing Altman model is independently confirmed; unknown is not confirmation.",
    },
    "technicals": {
        "sma_20": "20-session simple average of daily closing prices, in price units.",
        "sma_50": "50-session simple average of daily closing prices, in price units.",
        "sma_200": "200-session simple average of daily closing prices, in price units.",
        "ema_20": "20-session exponential average of daily closes, giving more weight to recent prices.",
        "ema_50": "50-session exponential average of daily closes, giving more weight to recent prices.",
        "ema_200": "200-session exponential average of daily closes, giving more weight to recent prices.",
        "rsi_14": "14-session relative strength index of closing-price momentum, on a 0–100 scale.",
        "macd_line": "MACD line: 12-session exponential moving average minus the 26-session average, in price units.",
        "macd_signal": "Nine-session exponential moving average of the MACD line, in price units.",
        "macd_hist": "MACD line minus its signal line, in price units.",
        "bb_upper": "Upper Bollinger band: 20-session closing average plus two standard deviations, in price units.",
        "bb_lower": "Lower Bollinger band: 20-session closing average minus two standard deviations, in price units.",
        "bb_middle": "Middle Bollinger band: 20-session simple average of daily closes, in price units.",
        "adx_14": "14-session average directional index measuring trend strength, not direction, on a 0–100 scale.",
        "current_volume": "Volume in the latest daily candle, in shares; it may represent an incomplete session.",
        "avg_volume_3m": "Average daily share volume over 63 sessions, approximately three months.",
        "money_flow_index_14": "14-session money flow index combining price and volume, on a 0–100 scale.",
        "atr_percentile_14": "Percentile rank of the latest 14-session average true range among available observations, on a 0–100 scale.",
        "chop_index_14": "14-session choppiness index comparing accumulated true range with the price range, on a 0–100 scale.",
        "squeeze_alert": "Whether Bollinger bands are inside the 20-session, 1.5-ATR Keltner channel; not a direction forecast.",
        "volume_price_trend": "Cumulative volume weighted by daily closing-price percentage changes; its level depends on the history used.",
        "historical_volatility_30": "Standard deviation of 30 daily returns, annualized using 252 sessions and displayed as a percentage.",
    },
    "shareholders": {
        "pct_held_by_institutions": "Provider-reported institutional ownership as a percentage of shares outstanding.",
        "pct_held_by_insiders": "Provider-reported insider ownership as a percentage of shares outstanding.",
        "pct_held_by_retail": "Residual ownership estimate: 100% minus reported institutional and insider ownership; not a direct retail count.",
        "total_institutional_holders": "Number of institutional holders reported by the provider.",
        "net_shares_bought_quarterly": "Net shares bought during the quarter: purchases minus sales, as reported by the source.",
        "top_10_holders_trend": "Reported accumulation, distribution or neutral trend among the top ten holders.",
        "institutional_block_trade_bias": "Reported institutional block-trade directional bias on a -1 to 1 scale; source methodology may vary.",
        "short_interest_pct_float": "Short interest relative to floating shares, normally a fraction displayed as a percentage; values above 1 are unverified.",
        "insider_transactions_bias": "Reported directional bias of insider transactions; source methodology and scale may vary.",
        "promoter_pledged_shares_pct": "Promoter shares pledged as a percentage; a verified value above 25% is flagged, while missing or unverified data is not rated.",
        "promoter_pledge_verified": "Whether the promoter pledge figure was independently verified; unknown is not verification.",
    },
    "intraday": {
        "vwap": "Intraday volume-weighted average price, in price units; depends on the available trading data.",
        "distance_from_vwap_pct": "Current price's percentage distance from intraday VWAP; positive means above VWAP.",
        "opening_range_breakout": "Reported price position or breakout relative to the opening range; not a guaranteed trend.",
        "time_of_day_segment": "Trading-session segment assigned to the intraday snapshot.",
        "relative_volume_intraday": "Intraday volume relative to a reference volume, expressed as a multiple; source methodology may vary.",
        "bid_ask_spread": "Difference between best ask and best bid, in price units.",
        "upper_circuit_limit": "Exchange-reported upper price limit, in price units, when available.",
        "lower_circuit_limit": "Exchange-reported lower price limit, in price units, when available.",
        "distance_to_lower_circuit_pct": "Percentage distance above the lower circuit limit; less than 1.5% is a local risk flag.",
    },
    "growth": {
        "revenue_growth_yoy": "Year-over-year change in annual revenue, displayed as a percentage.",
        "earnings_growth_yoy": "Year-over-year change in annual earnings, displayed as a percentage; comparisons can be sensitive to losses.",
        "earnings_quality_score": "Annual operating cash flow divided by net income; interpretation is sensitive to negative or near-zero earnings.",
    },
    "options": {
        "put_call_volume_ratio": "Total put volume divided by call volume across the returned option chain; incomplete data can prevent calculation.",
        "put_call_oi_ratio": "Total put open interest divided by call open interest across the returned option chain; incomplete data can prevent calculation.",
        "implied_volatility_percentile": "Reported percentile of implied volatility relative to its reference history, on a 0–100 scale.",
        "implied_move_percentage": "Option-implied price move as a percentage; its horizon and method depend on the source, not a guarantee.",
        "gamma_exposure_bias": "Reported net long, net short or neutral gamma exposure; source methodology may vary.",
        "unusual_options_activity": "Whether the source identified unusual options activity; missing data is not evidence of no activity.",
    },
    "futures": {
        "front_month_premium_pct": "Front-month futures premium or discount to the reference spot price, as a percentage.",
        "open_interest_change_pct": "Percentage change in futures open interest over the source's comparison period.",
        "term_structure_regime": "Reported futures term structure: contango or backwardation; comparison contracts depend on the source.",
        "commercial_net_position": "Reported net position of commercial futures participants; units and reporting period depend on the source.",
    },
    "analyst": {
        "consensus_rating": "Provider's summary of analyst recommendations; it is not an order or guarantee.",
        "mean_price_target": "Mean analyst price target, in the provider's quote currency; estimates can change.",
        "high_price_target": "Highest analyst price target reported by the provider, in quote currency.",
        "low_price_target": "Lowest analyst price target reported by the provider, in quote currency.",
        "upside_percentage": "Mean analyst target relative to current price: (target / price − 1) × 100; may be negative.",
        "earnings_surprise_pct_q4": "Reported fiscal fourth-quarter earnings surprise as a percentage; the source determines the comparison.",
    },
    "macro": {
        "sector_return_5d": "Percentage return of the selected sector benchmark over five trading sessions.",
        "sector_return_30d": "Percentage return of the selected sector benchmark over 30 trading sessions.",
        "beta_to_sp500": "Sensitivity of aligned daily stock returns to S&P 500 returns: covariance divided by benchmark variance.",
        "market_regime_bias": "S&P 500 close relative to its 200-session average: above is Risk-On, below Risk-Off, equal Neutral.",
    },
    "news": {
        "headlines": "Reported news items; an empty list can also mean no feed was supplied.",
        "upcoming_corporate_action_event": "Reported upcoming split, bonus or ex-dividend event; unknown is not the same as none.",
        "headline": "Headline text as reported by the news source.",
        "timestamp": "Publication timestamp reported for the news item.",
        "source": "Publisher or source reported for the news item.",
        "url": "Source URL reported for the news item.",
        "raw_sentiment_score": "Source-provided sentiment score; scale and methodology depend on the source.",
    },
}
_OVERVIEW_INFO = {
    "context_horizon": "Investment horizon selected for this context and its local risk checks.",
    "context_current_price": "Current price reported by the context provider, in quote currency; it may be delayed or differ from the broker price.",
    "context_profile": "Company identity and provider-reported business profile.",
    "context_fundamentals": "Provider and statement-derived financial and valuation measures.",
    "context_technicals": "Indicators calculated from daily price and volume history.",
    "context_shareholders": "Reported ownership, transactions and promoter pledge information.",
    "context_intraday": "Intraday price, volume and market microstructure measures when available.",
    "context_growth": "Annual statement-derived growth and earnings quality measures.",
    "context_options": "Option-chain ratios and other reported options measures.",
    "context_futures": "Reported futures market measures when available.",
    "context_analyst": "Provider-reported analyst recommendations and price targets.",
    "context_macro": "Benchmark and sector market measures.",
    "context_news": "Reported headlines and upcoming corporate action information.",
    "context_divergence": "Local check for price below SMA 200 and RSI above 70; false can also mean the check was skipped.",
    "context_penalty": "Local gate's confidence-scale penalty: 0.1 for yield above 5% with negative free cash flow; not a market forecast.",
    "context_eligibility": "Whether local checks permit the separate triggered analysis; blocked can include missing critical data.",
    "context_warnings": "Local gate warnings explain risk flags and checks that could not be completed.",
    "context_data_warnings": "Data warnings describe missing, estimated or inconsistent source inputs; absence of a warning is not verification.",
    "context_source_data": "Underlying provider inputs retained in the context, subject to source availability and freshness.",
}
_OVERVIEW_INFO.update({
    "context_gate_is_eligible_for_llm": _OVERVIEW_INFO["context_eligibility"],
    "context_gate_local_warnings": _OVERVIEW_INFO["context_warnings"],
    "context_gate_technical_divergence_detected": _OVERVIEW_INFO["context_divergence"],
    "context_gate_macro_risk_penalty": _OVERVIEW_INFO["context_penalty"],
})


def _context_info(section: str, name: str) -> str:
    return _CONTEXT_INFO.get(section, {}).get(
        name, f"{name.replace('_', ' ').capitalize()}: reported {section.replace('_', ' ')} information; "
              "source, units and interpretation may vary."
    )


def _add_info(info: dict[str, str], key: str, description: str) -> None:
    existing = info.get(key)
    if not existing:
        info[key] = description
    elif description not in existing:
        info[key] = f"{existing} {description}"


def _available(value: Any) -> bool:
    return value is not None and value != "" and not (
        isinstance(value, float) and not math.isfinite(value)
    )


def _context_display(section: str, name: str, value: Any) -> str:
    if not _available(value):
        return "Unavailable"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        if (section, name) in _FRACTIONS:
            if name == "short_interest_pct_float" and value > 1:
                return f"{value} (fraction; unverified, {value * 100:g}% if fractional)"
            return f"{value * 100:g}%"
        if (section, name) in _PERCENT_POINTS:
            return f"{value:g}%"
        if section == "technicals" and isinstance(value, float):
            return str(round(value, 2)) if abs(value) >= 1 or value == 0 else f"{value:.4g}"
    return str(value)


def _legacy_number(text: str, *, fraction: bool = False, debt_equity: bool = False) -> float | None:
    try:
        value = float(text.replace(",", "").replace("₹", "").replace("%", "").strip())
    except (ValueError, AttributeError):
        return None
    return value / 100 if fraction or debt_equity else value


def _missing_display(value: Any) -> bool:
    return str(value).strip().lower() in ("", "n/a", "none", "nan", "unavailable", "—")


def _context_tone(section: str, name: str, value: Any, context: dict[str, Any]) -> str:
    if not _available(value):
        return "unknown"
    if section == "fundamentals" and name == "altman_z_score":
        if (context.get("fundamentals") or {}).get("altman_model_applicable") is True and value < 1.81:
            return "neg"
    if section == "shareholders" and name == "promoter_pledged_shares_pct":
        if (context.get("shareholders") or {}).get("promoter_pledge_verified") is True and value > 0.25:
            return "neg"
    if section == "intraday" and name == "distance_to_lower_circuit_pct" and value < 1.5:
        return "neg"
    return "neu"


def _group_ratings(item: dict[str, Any], config: dict[str, Any], context_checks: dict[str, str]) -> None:
    """Only explicit directional checks contribute; tones alone are not checks."""
    groups = [*config.get("groups", []), *item.get("extraGroups", [])]
    ratings = {}
    check_info = config.get("checkInfo", {})
    for title, rows in groups:
        keys = {key for key, _ in rows if key in item["ind"] and not _missing_display(item["ind"][key][0])}
        checks = []
        for name, value, state in item.get("checks", []):
            key = check_info.get(name)
            if (state not in ("p", "n") or key not in keys or _missing_display(value)
                    or key in item.get("contextReplacedChecks", [])):
                continue
            if key in _RAW_LABELS:
                index = config["rawCols"].index(_RAW_LABELS[key])
                if not _available(item["raw"][index]):
                    continue
            checks.append((key, state))
        checked_keys = {key for key, _ in checks}
        checks.extend((key, "p" if tone == "pos" else "n")
                      for key, tone in context_checks.items() if key in keys and key not in checked_keys)
        positive = sum(state == "p" for _, state in checks)
        rated = len(checks)
        ratings[title] = {
            "positive": positive, "rated": rated,
            "label": f"{positive} / {rated} positive / directional checks" if rated else "Not rated",
        }
    item["groupRatings"] = ratings


def _normalize_legacy_tones(item: dict[str, Any], config: dict[str, Any]) -> None:
    checks = {}
    for name, value, state in item.get("checks", []):
        key = config.get("checkInfo", {}).get(name)
        if key and state in ("p", "n") and not _missing_display(value):
            checks[key] = "pos" if state == "p" else "neg"
    for _, rows in config.get("groups", []):
        for key, _ in rows:
            if key not in item["ind"]:
                continue
            display = item["ind"][key][0]
            if _missing_display(display):
                item["ind"][key][1] = "unknown"
                continue
            if key in _RAW_LABELS:
                index = config["rawCols"].index(_RAW_LABELS[key])
                if not _available(item["raw"][index]):
                    item["ind"][key][1] = "unknown"
                    continue
            item["ind"][key][1] = checks.get(key, "neu")


def append_context_to_row(item: dict[str, Any], config: dict[str, Any], result: dict[str, Any] | None) -> None:
    item.setdefault("extraGroups", [])
    item.setdefault("extraSections", [])
    fields = context_filter_fields()
    info = item.setdefault("contextInfo", {})
    field_info = config.setdefault("fieldInfo", {})
    for section, descriptions in _CONTEXT_INFO.items():
        for name, description in descriptions.items():
            _add_info(field_info, f"context_{section}_{name}", description)
    for key, description in _OVERVIEW_INFO.items():
        if key != "context_source_data":
            _add_info(field_info, key, description)
    for name, legacy_key in _TECHNICAL_ROWS.items():
        _add_info(info, legacy_key, _context_info("technicals", name))
    for title, rows in item["extraGroups"]:
        if title == "Fundamentals":
            for key, label in rows:
                for name, legacy_label in _FUNDAMENTAL_ROWS.items():
                    if label == legacy_label:
                        _add_info(info, key, _context_info("fundamentals", name))
    raw_info = config.setdefault("rawInfo", {})
    raw_indicator_keys = config.setdefault("rawIndicatorKeys", {})
    for section, field, label, _ in fields:
        description = _context_info(section, field)
        _add_info(field_info, f"context_{section}_{field}", description)
        _add_info(raw_info, label, description)
        raw_indicator_keys[label] = f"context_{section}_{field}"
    for label, key in (
        ("Context · horizon", "context_horizon"), ("Context · current price", "context_current_price"),
        ("Gate · eligibility", "context_eligibility"), ("Gate · technical divergence", "context_divergence"),
        ("Gate · macro risk penalty", "context_penalty"),
    ):
        _add_info(raw_info, label, _OVERVIEW_INFO[key])
    config.setdefault("rawTypes", ["number"] * len(config["rawCols"]))
    config["hiddenRawCols"] = [
        label for section, name, label, _ in fields
        if section == "technicals" and _RAW_LABELS.get(_TECHNICAL_ROWS.get(name)) in config["rawCols"]
    ]
    config["rawCols"].extend(label for _, _, label, _ in fields)
    config["rawTypes"].extend(kind for _, _, _, kind in fields)
    context = result.get("context") if isinstance(result, dict) else None
    technical_values = context.get("technicals") if isinstance(context, dict) else None
    item["technicalCoverage"] = {
        "available": sum(_available(technical_values.get(name))
                         for name in TechnicalIndicators.model_fields)
        if isinstance(technical_values, dict) else 0,
        "total": len(TechnicalIndicators.model_fields),
        "contextAvailable": isinstance(technical_values, dict),
    }
    item["raw"].extend(
        value if isinstance(value, (str, bool)) or (
            isinstance(value, (int, float)) and math.isfinite(value)) else ""
        for section, field, _, _ in fields
        for value in [(context.get(section) or {}).get(field) if context else None]
    )
    config.setdefault("rawUnits", {}).update({
        label: "fraction" if (section, field) in _FRACTIONS else "percentage points"
        for section, field, label, _ in fields
        if (section, field) in _FRACTIONS or (section, field) in _PERCENT_POINTS
    })
    config["rawCols"].extend(["Context · horizon", "Context · current price"])
    config["rawTypes"].extend(["text", "number"])
    item["raw"].extend([
        context.get("horizon", "") if context else "",
        context.get("current_price") if context and context.get("current_price") is not None else "",
    ])
    config["rawCols"].extend(["Gate · eligibility", "Gate · technical divergence", "Gate · macro risk penalty"])
    config["rawTypes"].extend(["text", "text", "number"])
    gate = result.get("gate") if isinstance(result, dict) else None
    item["raw"].extend([
        ("Eligible" if gate["is_eligible_for_llm"] else "Blocked")
        if gate and "is_eligible_for_llm" in gate else "",
        str(gate["technical_divergence_detected"])
        if gate and gate.get("technical_divergence_detected") is not None else "",
        gate.get("macro_risk_penalty", "") if gate else "",
    ])
    fixed_groups = {title: rows for title, rows in config.get("groups", [])}
    fixed_keys = {key for rows in fixed_groups.values() for key, _ in rows}
    for key in _TECHNICAL_ROWS.values():
        if key in fixed_keys:
            item["ind"].setdefault(key, ["Unavailable", "unknown"])
    for name in _TECHNICAL_GROUPS:
        key = f"context_technicals_{name}"
        rows = fixed_groups.get(_TECHNICAL_GROUPS[name])
        if rows is not None and not any(existing == key for existing, _ in rows):
            rows.append([key, _LABELS.get(name, name.replace("_", " ").capitalize())])
        if rows is not None:
            item["ind"].setdefault(key, ["Unavailable", "unknown"])
    for row in fixed_groups.get("Risk and range", []):
        if row[0] == "Volatility_20d_annualized_%":
            row[1] = "20-day annualized volatility"
    _normalize_legacy_tones(item, config)
    context_checks: dict[str, str] = {}
    # Fundamental tones are produced from explicit legacy fundamental checks.
    for title, rows in item.get("extraGroups", []):
        if title == "Fundamentals":
            for key, _ in rows:
                tone = item["ind"].get(key, ["", "neu"])[1]
                if tone in ("pos", "neg"):
                    context_checks[key] = tone
    if not result:
        item["extraSections"].append({"title": "Local stock context",
                                     "note": "Unavailable: no verified stock instrument mapping."})
        _group_ratings(item, config, context_checks)
        return
    if not context:
        item["extraSections"].append({"title": "Local stock context", "note": result.get("error", "Unavailable.")})
        _group_ratings(item, config, context_checks)
        return
    fundamentals = context.get("fundamentals")
    if isinstance(fundamentals, dict) and any(
        name != "altman_model_applicable" and _available(value)
        for name, value in fundamentals.items()
    ):
        for section in item["extraSections"]:
            availability = section.get("availability")
            if isinstance(availability, dict) and "fundamentals" in availability:
                availability["fundamentals"] = "Research signal excludes unavailable research fundamentals."
                section["note"] = " ".join(availability.values())
    gate = gate or {}
    status = ("Eligible" if gate["is_eligible_for_llm"] else "Blocked") if "is_eligible_for_llm" in gate else "Unavailable"
    notes = [
        f"Phase 1: {status}. Updated {result.get('updated_at', 'at an unknown time')}. "
        "This is a snapshot, not an intraday stream. Existing portfolio AI analysis uses a separate workflow."
    ]
    if result.get("notice"):
        notes.append(result["notice"])
    notes.extend(gate.get("local_warnings", []))
    notes.extend(context.get("data_warnings", []))
    item["extraSections"].append({"title": "Local stock context gate", "note": " ".join(notes)})
    item["contextModel"] = {key: value for key, value in context.items() if key != "source_data"}
    overview = [("context_horizon", "Horizon", context.get("horizon")),
                ("context_current_price", "Context provider price", context.get("current_price")),
                ("context_divergence", "Technical divergence", gate.get("technical_divergence_detected")),
                ("context_penalty", "Macro risk penalty", gate.get("macro_risk_penalty"))]
    for key, _, value in overview:
        item["ind"][key] = [_context_display("", key, value), "neu" if _available(value) else "unknown"]
    item["extraGroups"].append(["Context overview", [[key, label] for key, label, _ in overview]])
    missing = [section.replace("_", " ").title() for section in CONTEXT_SECTIONS
               if not isinstance(context.get(section), dict)
               or not any(_available(value) and value != [] for value in context[section].values())]
    if missing:
        item["extraSections"].append({"title": "Unavailable context sections", "note": ", ".join(missing)})

    groups = {title: rows for title, rows in item["extraGroups"]}
    fundamental_rows = groups.get("Fundamentals", [])
    profile = context.get("profile")
    context_ticker = profile.get("ticker") if isinstance(profile, dict) else None
    inr_context = isinstance(context_ticker, str) and context_ticker.upper().endswith((".NS", ".BO"))
    for section in CONTEXT_SECTIONS:
        values = context.get(section)
        if not isinstance(values, dict):
            continue
        for name, value in values.items():
            if name == "headlines":
                continue
            key = f"context_{section}_{name}"
            label = _LABELS.get(name, name.replace("_", " ").capitalize())
            legacy_key = _TECHNICAL_ROWS.get(name) if section == "technicals" else None
            if section == "fundamentals" and name in _FUNDAMENTAL_ROWS:
                legacy_key = next((k for k, label_ in fundamental_rows
                                   if label_ == _FUNDAMENTAL_ROWS[name]), None)
            if (inr_context and section == "fundamentals"
                    and name in {"market_cap", "float_market_cap"} and _available(value)):
                display_key = legacy_key if legacy_key in item["ind"] else key
                item.setdefault("panelCurrencies", {})[display_key] = "INR"
            description = _context_info(section, name)
            if key not in field_info:
                _add_info(info, key, description)
            if legacy_key:
                _add_info(info, legacy_key, description)
            display = _context_display(section, name, value)
            tone = _context_tone(section, name, value, context)
            if legacy_key and section == "technicals" and legacy_key not in item["ind"]:
                item["ind"][legacy_key] = [display, tone]
                continue
            if legacy_key and legacy_key in item["ind"]:
                legacy, legacy_tone = item["ind"][legacy_key]
                legacy_raw = None
                if legacy_key in _RAW_LABELS:
                    index = config["rawCols"].index(_RAW_LABELS[legacy_key])
                    legacy_raw = item["raw"][index]
                elif section == "fundamentals":
                    legacy_raw = _legacy_number(legacy, fraction=(section, name) in _FRACTIONS,
                                                debt_equity=name == "debt_to_equity")
                if _available(value):
                    same = _available(legacy_raw) and legacy_raw == value
                    item["ind"][legacy_key] = [display, legacy_tone if same else tone]
                    _add_info(info, legacy_key, "Showing the newer context value.")
                    if not same:
                        item.setdefault("contextReplacedChecks", []).append(legacy_key)
                        context_checks.pop(legacy_key, None)
                    if legacy_key in _RAW_LABELS:
                        item["raw"][index] = value
                    if legacy_key == "RSI_14":
                        item["rsi"] = value
                elif _missing_display(legacy):
                    item["ind"][legacy_key] = ["Unavailable", "unknown"]
                else:
                    _add_info(info, legacy_key, "Context value unavailable; showing the research value.")
                if tone in ("pos", "neg") and legacy_key not in context_checks:
                    context_checks[legacy_key] = tone
                continue
            item["ind"][key] = [display, tone]
            if tone in ("pos", "neg"):
                context_checks[key] = tone
            if section == "technicals" and name in _TECHNICAL_GROUPS and _TECHNICAL_GROUPS[name] in fixed_groups:
                continue
            title = _GROUP_TITLES.get(section, "Momentum")
            if title not in groups:
                groups[title] = []
                item["extraGroups"].append([title, groups[title]])
            groups[title].append([key, label])
    headlines = (context.get("news") or {}).get("headlines", [])
    if headlines:
        existing = {
            (str(article.get("url") or "").strip(), str(article.get("title") or "").strip().casefold()): article
            for section in item["extraSections"]
            for article in section.get("items", [])
            if isinstance(article, dict) and article.get("url") and article.get("title")
        }
        additional = []
        for article in headlines:
            if not isinstance(article, dict):
                continue
            url = str(article.get("url") or "").strip()
            title = str(article.get("headline") or "").strip()
            match = existing.get((url, title.casefold())) if url and title else None
            if match is not None:
                match["sentimentLabel"] = (
                    f"{match.get('sentimentLabel', '')} · Context source: {article.get('source') or 'Unknown source'}"
                    f" · Context time: {article.get('timestamp') or 'Unknown time'}"
                    f" · Context sentiment: {article.get('raw_sentiment_score')}"
                )
            else:
                additional.append({
                    "title": title, "url": url,
                    "publisher": str(article.get("source") or "Unknown source"),
                    "published": str(article.get("timestamp") or "Unknown time"),
                    "sentiment": None,
                    "sentimentLabel": f"Context sentiment: {article.get('raw_sentiment_score')}",
                })
        if additional:
            item["extraSections"].append({"title": "Context headlines", "items": additional})
    _group_ratings(item, config, context_checks)

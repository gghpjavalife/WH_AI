"""Public NSE market scanner and single-stock research workspace."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import escape
import re

import pandas as pd
import requests
import streamlit as st

from brokers.factory import TICKER_MAP
from core.logging import log_failure
from .scan_table import show_scan_table
from .market_data import (
    MarketDataError,
    assess_stock,
    calculate_indicators,
    fetch_bse_equity_master,
    fetch_daily_history,
    fetch_indian_equity_master,
    fetch_nse_equity_master,
    matches_scanner_filters,
    normalize_symbol,
    style_recommendations,
)

RECOMMENDED_FILTERS = {
    "minimum_price": 20.0,
    "maximum_price": 5000.0,
    "minimum_rsi": 35.0,
    "maximum_rsi": 70.0,
    "minimum_average_volume": 100_000.0,
    "require_above_sma50": True,
}
TRADING_FILTERS = {
    "minimum_price": 10.0,
    "maximum_price": 10_000.0,
    "minimum_rsi": 25.0,
    "maximum_rsi": 75.0,
    "minimum_average_volume": 200_000.0,
    "require_above_sma50": False,
    "minimum_relative_volume": 1.0,
    "minimum_atr_pct": 1.0,
}
OPEN_FILTERS = {
    "minimum_price": 0.0,
    "maximum_price": 1_000_000.0,
    "minimum_rsi": 0.0,
    "maximum_rsi": 100.0,
    "minimum_average_volume": 0.0,
    "require_above_sma50": False,
}


def _numeric_filter(value: float | int | None, fallback: float) -> float:
    return float(fallback if value is None else value)


SCAN_METHOD = (
    "Every stock is scored on all of its indicators and each one is checked "
    "for a positive reading: price against the 20, 50 and 200-day averages, "
    "12/26-day EMAs, MACD and its signal line, RSI, ADX with +DI/-DI, "
    "Stochastic, CCI, money flow (MFI), OBV, Bollinger position, volume "
    "against its 20-day average, 1 and 3-month returns, and the position in "
    "the 52-week range. The more positive checks a stock has, the stronger "
    "its signal: Strong Buy, Buy, Hold or Sell. No price, RSI or volume "
    "filter is applied by default."
)
DEFAULT_SCAN_SYMBOLS = [
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
    "TCS",
    "INFY",
    "BHARTIARTL",
    "ITC",
]


def _load_indian_equity_master(*, refresh: bool = False) -> pd.DataFrame:
    if refresh:
        fetch_indian_equity_master.clear()
        fetch_nse_equity_master.clear()
        fetch_bse_equity_master.clear()
    try:
        master = fetch_indian_equity_master()
    except MarketDataError as error:
        diagnostic = log_failure("Indian equity lists", error)
        st.warning(
            f"{diagnostic} Only the app's broker-mapped symbols are available "
            "until an exchange list can be refreshed."
        )
        master = pd.DataFrame(
            [
                {
                    "Symbol": symbol,
                    "Company": symbol,
                    "ISIN": "",
                    "Exchange": "Broker map",
                }
                for symbol in sorted(TICKER_MAP)
            ]
        )
    source_warning = master.attrs.get("source_warning")
    if source_warning:
        st.warning(str(source_warning))
    return master


def _equity_labels(master: pd.DataFrame) -> dict[str, str]:
    labels = {}
    for row in master.itertuples(index=False):
        ticker = getattr(row, "Ticker", row.Symbol)
        labels[str(row.Symbol)] = (
            f"{ticker} · {row.Company} ({row.Exchange})"
        )
    return labels


def _render_stock_result(
    symbol: str,
    history: pd.DataFrame,
    *,
    label: str | None = None,
) -> None:
    indicators = calculate_indicators(history)
    assessment = assess_stock(indicators)
    theme = getattr(st.context.theme, "type", "light")
    recommendation = str(assessment["recommendation"])
    notice = {
        "STRONG BUY": st.success,
        "BUY": st.success,
        "HOLD": st.warning,
        "SELL": st.error,
    }[recommendation]
    notice(
        f"**{recommendation} — {label or symbol}**  \n{assessment['reason']}",
        icon={
            "STRONG BUY": ":material/rocket_launch:",
            "BUY": ":material/trending_up:",
            "HOLD": ":material/pause_circle:",
            "SELL": ":material/trending_down:",
        }[recommendation],
    )
    first, second, third = st.columns(3)
    with first:
        st.metric(
            "Last close",
            f"₹{indicators['price']:,.2f}",
            f"{indicators['daily_change_pct']:+.2f}%",
            border=True,
        )
    with second:
        st.metric(
            "RSI (14)",
            f"{indicators['rsi14']:.1f}",
            border=True,
        )
    with third:
        st.metric(
            "Volume vs 20-day average",
            f"{indicators['relative_volume']:.2f}×",
            border=True,
        )

    signal_frame = pd.DataFrame(assessment["indicators"])
    st.dataframe(
        style_recommendations(signal_frame, theme=theme),
        hide_index=True,
        width="stretch",
    )
    chart_data = history.tail(120).set_index("Date")[["Close"]].copy()
    chart_data["20-day average"] = (
        history["Close"].rolling(20).mean().tail(120).to_numpy()
    )
    chart_data["50-day average"] = (
        history["Close"].rolling(50).mean().tail(120).to_numpy()
    )
    st.line_chart(chart_data, width="stretch")
    st.caption(
        f"Last daily candle: {history['Date'].iloc[-1]} · 20-day average "
        f"₹{indicators['sma20']:,.2f} · 50-day average ₹{indicators['sma50']:,.2f} · "
        f"52-week range ₹{indicators['low_52w']:,.2f}–"
        f"₹{indicators['high_52w']:,.2f}"
    )


def _load_stock(symbol: str) -> tuple[pd.DataFrame | None, str | None]:
    try:
        history = fetch_daily_history(symbol)
        calculate_indicators(history)
    except (MarketDataError, requests.RequestException, ValueError) as error:
        log_failure(f"Public market data for {symbol}", error)
        return None, (
            str(error)
            if isinstance(error, (MarketDataError, ValueError))
            else "Public market data is temporarily unavailable. Try again shortly."
        )
    return history, None


def _render_single_stock_search(
    *,
    scope: str,
    symbols: list[str] | None = None,
) -> None:
    safe_scope = scope.lower().replace(" ", "_")
    search_key = f"market_research_symbol_{safe_scope}"
    st.subheader("Research a stock")
    master = _load_indian_equity_master()
    labels = _equity_labels(master)
    option_symbols = set(labels) | set(TICKER_MAP)
    for candidate in symbols or []:
        try:
            normalized_candidate = normalize_symbol(candidate)
        except ValueError:
            continue
        option_symbols.add(normalized_candidate)
        labels.setdefault(normalized_candidate, normalized_candidate)
    options = sorted(option_symbols)
    if len(master):
        st.caption(
            f"{len(master):,} active Indian equities loaded from the NSE and "
            "BSE exchange lists."
        )
        st.markdown(
            "[BSE listed-equity feed ↗](https://www.bseindia.com/) · "
            "[NSE equity security master ↗]"
            "(https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv)"
        )
    suggested = next(
        (symbol for symbol in symbols or [] if symbol in option_symbols),
        "",
    )
    st.session_state.setdefault(search_key, suggested or "RELIANCE")
    with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
        symbol = st.selectbox(
            f"Indian stock · {scope}",
            options=options,
            accept_new_options=True,
            format_func=lambda ticker: labels.get(ticker, ticker),
            key=search_key,
            width=400,
            help=(
                "Select an NSE- or BSE-listed stock or type a ticker symbol. "
                "This research uses public data, not a broker connection."
            ),
        )
        search_clicked = st.button(
            "Research stock",
            key=f"market_research_button_{safe_scope}",
            type="primary",
            icon=":material/search:",
        )
    normalized = symbol.strip().upper().removesuffix(".NS")
    if search_clicked:
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as error:
            st.error(str(error))
            st.session_state[f"market_research_result_{safe_scope}"] = None
        else:
            history, error = _load_stock(normalized)
            if error:
                st.error(error)
                st.session_state[f"market_research_result_{safe_scope}"] = None
            else:
                st.session_state[f"market_research_result_{safe_scope}"] = (
                    normalized,
                    history,
                )

    result = st.session_state.get(f"market_research_result_{safe_scope}")
    if result and result[0] == normalized and normalized:
        _render_stock_result(result[0], result[1], label=labels.get(result[0]))


RESULTS_PAGE_SIZE = 10
_SIGNAL_RANK = {"STRONG BUY": 0, "BUY": 1, "HOLD": 2, "SELL": 3}
_TOOLTIP_FORMATS = (
    ("As_of", "As of", "{}"),
    ("Daily_change_%", "Daily change", "{:+.2f}%"),
    ("SMA_20", "SMA 20", "₹{:,.2f}"),
    ("SMA_50", "SMA 50", "₹{:,.2f}"),
    ("SMA_200", "SMA 200", "₹{:,.2f}"),
    ("EMA_12", "EMA 12", "₹{:,.2f}"),
    ("EMA_26", "EMA 26", "₹{:,.2f}"),
    ("RSI_14", "RSI 14", "{:.1f}"),
    ("MACD", "MACD", "{:.3f}"),
    ("MACD_signal", "MACD signal", "{:.3f}"),
    ("MACD_histogram", "MACD histogram", "{:.3f}"),
    ("ATR_14", "ATR 14", "₹{:,.2f}"),
    ("ATR_%", "ATR %", "{:.2f}%"),
    ("ADX_14", "ADX 14", "{:.1f}"),
    ("Plus_DI_14", "+DI 14", "{:.1f}"),
    ("Minus_DI_14", "-DI 14", "{:.1f}"),
    ("CCI_20", "CCI 20", "{:.1f}"),
    ("MFI_14", "MFI 14", "{:.1f}"),
    ("OBV_change_20d", "OBV change 20d", "{:,.0f}"),
    ("Bollinger_lower", "Bollinger lower", "₹{:,.2f}"),
    ("Bollinger_middle", "Bollinger middle", "₹{:,.2f}"),
    ("Bollinger_upper", "Bollinger upper", "₹{:,.2f}"),
    ("Bollinger_width_%", "Bollinger width", "{:.2f}%"),
    ("Stochastic_%K", "Stochastic %K", "{:.1f}"),
    ("Stochastic_%D", "Stochastic %D", "{:.1f}"),
    ("Volatility_20d_annualized_%", "Volatility 20d", "{:.2f}%"),
    ("Return_1m_%", "Return 1m", "{:+.2f}%"),
    ("Return_3m_%", "Return 3m", "{:+.2f}%"),
    ("Average_volume_20d", "Avg volume 20d", "{:,.0f}"),
    ("Volume_vs_average", "Volume / average", "{:.2f}×"),
    ("52w_high", "52w high", "₹{:,.2f}"),
    ("52w_low", "52w low", "₹{:,.2f}"),
    ("52w_range_position_%", "52w range position", "{:.0f}%"),
)


def _format_tooltip_value(value: object, template: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:
        return "n/a"
    return template.format(number if template != "{}" else value)


def _scan_row_tooltip(row: dict[str, object]) -> str:
    lines = [f"{row['Recommendation']}: {row['Reason']}", ""]
    for key, label, template in _TOOLTIP_FORMATS:
        if key in row:
            lines.append(f"{label}: {_format_tooltip_value(row[key], template)}")
    lines += ["", "Indicator signals:"]
    lines += [part for part in str(row["Indicator_signals"]).split("; ") if part]
    return "\n".join(lines)


def _why_tags(row: dict[str, object]) -> list[str]:
    """Short, human-readable reasons behind a Buy / Strong Buy signal."""

    def number(key: str) -> float:
        try:
            return float(row.get(key, float("nan")))
        except (TypeError, ValueError):
            return float("nan")

    tags: list[str] = []
    price = number("Price")
    averages = [
        label
        for key, label in (("SMA_20", "20"), ("SMA_50", "50"), ("SMA_200", "200"))
        if price > number(key)
    ]
    if averages:
        tags.append(f"Above {'/'.join(averages)}-day avg")
    if number("MACD") > number("MACD_signal"):
        tags.append("MACD bullish")
    rsi = number("RSI_14")
    if 50 <= rsi <= 70:
        tags.append(f"RSI {rsi:.0f} healthy")
    if number("ADX_14") >= 25 and number("Plus_DI_14") > number("Minus_DI_14"):
        tags.append("Strong uptrend")
    if number("Volume_vs_average") >= 1.2:
        tags.append("Volume surge")
    if number("MFI_14") >= 60:
        tags.append("Money inflow")
    if number("Return_3m_%") > 0:
        tags.append(f"3m {number('Return_3m_%'):+.0f}%")
    return tags


_POS, _NEG, _NEUTRAL = "#22C55E", "#F87171", "#E5E7EB"
def _num(row: dict[str, object], key: str) -> float:
    try:
        return float(row.get(key, float("nan")))
    except (TypeError, ValueError):
        return float("nan")


def _tone(row: dict[str, object], key: str) -> str:
    """Colour for an indicator value: green when bullish, red when bearish."""
    value = _num(row, key)
    if value != value:
        return _NEUTRAL
    price = _num(row, "Price")
    if key in {"Daily_change_%", "Return_1m_%", "Return_3m_%", "MACD_histogram",
               "CCI_20", "OBV_change_20d"}:
        good = value > 0
    elif key in {"SMA_20", "SMA_50", "SMA_200", "EMA_12", "EMA_26"}:
        good = price > value
    elif key == "RSI_14":
        good = 50 <= value <= 70
        if value > 70 or value < 40:
            good = False
        elif not good:
            return _NEUTRAL
    elif key == "MACD":
        good = value > _num(row, "MACD_signal")
    elif key == "Plus_DI_14":
        good = value > _num(row, "Minus_DI_14")
    elif key == "Minus_DI_14":
        good = value < _num(row, "Plus_DI_14")
    elif key == "MFI_14":
        good = value >= 50
    elif key == "Volume_vs_average":
        good = value >= 1
    elif key == "52w_range_position_%":
        good = value >= 50
    elif key == "Stochastic_%K":
        good = value > _num(row, "Stochastic_%D")
    else:
        return _NEUTRAL
    return _POS if good else _NEG


_INDICATOR_LINE = re.compile(
    r"^(.*?): (.*) \((Positive|Negative|Neutral|Unavailable)\)$"
)
_INDICATOR_GROUPS = (
    ("Trend", (("SMA_20", "20-day average"), ("SMA_50", "50-day average"),
               ("SMA_200", "200-day average"), ("EMA_12", "12-day EMA"),
               ("EMA_26", "26-day EMA"), ("ADX_14", "Trend strength (ADX)"),
               ("Plus_DI_14", "Buyers (+DI)"), ("Minus_DI_14", "Sellers (-DI)"))),
    ("Momentum", (("RSI_14", "RSI"), ("MACD", "MACD"), ("MACD_signal", "MACD signal"),
                  ("MACD_histogram", "MACD histogram"), ("Stochastic_%K", "Stochastic %K"),
                  ("Stochastic_%D", "Stochastic %D"), ("CCI_20", "CCI"),
                  ("MFI_14", "Money flow (MFI)"))),
    ("Risk and range", (("ATR_14", "Daily range (ATR)"), ("ATR_%", "ATR %"),
                        ("Volatility_20d_annualized_%", "Volatility 20d"),
                        ("Bollinger_lower", "Bollinger low"),
                        ("Bollinger_middle", "Bollinger mid"),
                        ("Bollinger_upper", "Bollinger high"),
                        ("Bollinger_width_%", "Band width"),
                        ("52w_low", "52-week low"), ("52w_high", "52-week high"),
                        ("52w_range_position_%", "Position in 52w range"))),
    ("Performance and volume", (("Daily_change_%", "Today"), ("Return_1m_%", "1 month"),
                                ("Return_3m_%", "3 months"),
                                ("Average_volume_20d", "Avg volume 20d"),
                                ("Volume_vs_average", "Volume vs average"),
                                ("OBV_change_20d", "OBV change 20d"))),
)
_WIKI = "https://en.wikipedia.org/wiki/"
_INV = "https://www.investopedia.com/terms/"
# key: (one-line meaning, learn-more link)
_INDICATOR_INFO = {
    "SMA_20": ("Average close of the last 20 days. Price above it = short-term strength.", _INV + "s/sma.asp"),
    "SMA_50": ("Average close of the last 50 days. A common medium-term trend line.", _INV + "s/sma.asp"),
    "SMA_200": ("Average close of the last 200 days. The classic long-term trend line.", _INV + "s/sma.asp"),
    "EMA_12": ("Exponential average that weights recent days more; fast trend.", _INV + "e/ema.asp"),
    "EMA_26": ("Slower exponential average; with the 12-day EMA it builds MACD.", _INV + "e/ema.asp"),
    "ADX_14": ("Trend strength, not direction. Above 25 means a strong trend.", _INV + "a/adx.asp"),
    "Plus_DI_14": ("+DI: buying pressure. Above -DI means buyers lead.", _INV + "a/adx.asp"),
    "Minus_DI_14": ("-DI: selling pressure. Above +DI means sellers lead.", _INV + "a/adx.asp"),
    "RSI_14": ("Momentum from 0-100. 50-70 is healthy; above 70 overheated, below 30 oversold.", _INV + "r/rsi.asp"),
    "MACD": ("Gap between 12 and 26-day EMAs. Rising means momentum is building.", _INV + "m/macd.asp"),
    "MACD_signal": ("9-day average of MACD. MACD above it is bullish.", _INV + "m/macd.asp"),
    "MACD_histogram": ("MACD minus its signal line. Positive and growing = rising momentum.", _INV + "m/macd.asp"),
    "Stochastic_%K": ("Where price closed in its recent range (0-100). Above 80 overbought.", _INV + "s/stochasticoscillator.asp"),
    "Stochastic_%D": ("Smoothed %K. Crossovers hint at momentum shifts.", _INV + "s/stochasticoscillator.asp"),
    "CCI_20": ("Distance of price from its average. Above +100 strong, below -100 weak.", _INV + "c/commoditychannelindex.asp"),
    "MFI_14": ("RSI that includes volume. Above 80 overbought, below 20 oversold.", _INV + "m/mfi.asp"),
    "ATR_14": ("Average daily price range in rupees; a measure of volatility.", _INV + "a/atr.asp"),
    "ATR_%": ("ATR as a share of price; higher means a more volatile stock.", _INV + "a/atr.asp"),
    "Volatility_20d_annualized_%": ("How much the price swings, annualised, over 20 days.", _INV + "v/volatility.asp"),
    "Bollinger_lower": ("Lower band: 2 standard deviations under the 20-day average.", _INV + "b/bollingerbands.asp"),
    "Bollinger_middle": ("Middle band: the 20-day average.", _INV + "b/bollingerbands.asp"),
    "Bollinger_upper": ("Upper band: 2 standard deviations over the 20-day average.", _INV + "b/bollingerbands.asp"),
    "Bollinger_width_%": ("Band width; narrow bands often precede big moves.", _INV + "b/bollingerbands.asp"),
    "52w_low": ("Lowest price in the last 52 weeks.", _INV + "1/52weeklow.asp"),
    "52w_high": ("Highest price in the last 52 weeks.", _INV + "1/52weekhigh.asp"),
    "52w_range_position_%": ("Where price sits between the 52-week low (0%) and high (100%).", _INV + "1/52weekhigh.asp"),
    "Daily_change_%": ("Price change versus the previous close.", _INV + "p/percentage-change.asp"),
    "Return_1m_%": ("Price change over about one month.", _INV + "r/rateofreturn.asp"),
    "Return_3m_%": ("Price change over about three months.", _INV + "r/rateofreturn.asp"),
    "Average_volume_20d": ("Average shares traded per day over 20 days; a liquidity gauge.", _INV + "v/volume.asp"),
    "Volume_vs_average": ("Today's volume divided by the 20-day average; above 1x is active.", _INV + "v/volume.asp"),
    "OBV_change_20d": ("On-balance volume change; positive suggests accumulation.", _INV + "o/onbalancevolume.asp"),
}
_CHECK_INFO_KEY = {
    "Price vs 20-day average": "SMA_20",
    "Price vs 12-day EMA": "EMA_12",
    "Price vs 26-day EMA": "EMA_26",
    "20-day vs 50-day trend": "SMA_50",
    "50-day vs 200-day trend": "SMA_200",
    "MACD momentum": "MACD",
    "MACD histogram": "MACD_histogram",
    "14-day RSI": "RSI_14",
    "ATR (14)": "ATR_14",
    "Bollinger bands (20, 2\u03c3)": "Bollinger_middle",
    "Stochastic (%K / %D)": "Stochastic_%K",
    "20-day annualized volatility": "Volatility_20d_annualized_%",
    "ADX / directional index (14)": "ADX_14",
    "CCI (20)": "CCI_20",
    "Money flow index (14)": "MFI_14",
    "20-day OBV change": "OBV_change_20d",
    "1-month return": "Return_1m_%",
    "3-month return": "Return_3m_%",
    "52-week range position": "52w_range_position_%",
    "Average volume confirmation": "Volume_vs_average",
}
_TONE_CLASS = {_POS: "pos", _NEG: "neg"}


def _scan_table_payload(
    rows: list[dict[str, object]], labels: dict[str, str]
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Rows and settings for the interactive, browser-side results table."""
    raw_keys = [key for key, _, _ in _TOOLTIP_FORMATS]
    payload = []
    for row in rows:
        symbol = str(row["Ticker"])
        checks = []
        structured = row.get("Indicator_checks")
        if structured is None:
            structured = [
                match.groups()
                for part in str(row.get("Indicator_signals", "")).split("; ")
                if (match := _INDICATOR_LINE.match(part))
            ]
        for name, value, state in structured:
            checks.append(
                [name, str(value), {"Positive": "p", "Negative": "n"}.get(state, "u")]
            )
        indicators = {
            key: [
                _format_tooltip_value(row[key], template),
                _TONE_CLASS.get(_tone(row, key), "neu"),
            ]
            for key, _, template in _TOOLTIP_FORMATS
            if key in row
        }
        payload.append(
            {
                "ticker": symbol,
                "label": labels.get(symbol, symbol),
                "price": _num(row, "Price"),
                "chg": _num(row, "Daily_change_%"),
                "rsi": _num(row, "RSI_14"),
                "rsiTone": _TONE_CLASS.get(_tone(row, "RSI_14"), "neu"),
                "signal": str(row["Recommendation"]),
                "score": int(row.get("Signal_score", 0) or 0),
                "pos": int(row.get("Positive_signals", 0) or 0),
                "total": int(row.get("Total_signals", 0) or 0),
                "pass": bool(row.get("Passes_filters")),
                "m1": _num(row, "Return_1m_%"),
                "vol": _num(row, "Volume_vs_average"),
                "reason": str(row.get("Reason", "")),
                "checks": checks,
                "ind": indicators,
                "raw": [
                    "" if row.get(key) is None else row.get(key) for key in raw_keys
                ],
            }
        )
    present = {row["signal"] for row in payload}
    config = {
        "signals": [s for s in _SIGNAL_RANK if s in present],
        "groups": [[title, [list(item) for item in items]] for title, items in _INDICATOR_GROUPS],
        "rawCols": [label for _, label, _ in _TOOLTIP_FORMATS],
        "info": {key: list(value) for key, value in _INDICATOR_INFO.items()},
        "checkInfo": _CHECK_INFO_KEY,
    }
    return payload, config


_FAILURE_NO_DATA = "Not available on Yahoo Finance"
_FAILURE_SHORT_HISTORY = "Fewer than 50 trading days of history"
_FAILURE_TEMPORARY = "Temporary data-provider error or rate limit"


def _failure_reason(error: Exception) -> str:
    message = str(error)
    if "No public" in message and "was found" in message:
        return _FAILURE_NO_DATA
    if "At least 50 daily prices" in message:
        return _FAILURE_SHORT_HISTORY
    if isinstance(error, MarketDataError) and "temporarily unavailable" not in message:
        return _FAILURE_NO_DATA
    return _FAILURE_TEMPORARY


def _render_scan_failures(failures: object) -> None:
    reasons = dict(failures) if isinstance(failures, dict) else {
        symbol: _FAILURE_NO_DATA for symbol in failures or []
    }
    if not reasons:
        return
    explanations = {
        _FAILURE_NO_DATA: (
            "Yahoo Finance does not publish a daily chart for these tickers. "
            "Many BSE-only, suspended, delisted or very thinly traded scrips "
            "are missing from the free feed."
        ),
        _FAILURE_SHORT_HISTORY: (
            "Newly listed, suspended or rarely traded stocks lack the 50 daily "
            "prices needed for stable indicators."
        ),
        _FAILURE_TEMPORARY: (
            "The data provider throttled or timed out. Scan these again later "
            "or in smaller batches."
        ),
    }
    with st.expander(
        f"{len(reasons):,} stock(s) skipped \u2014 no usable market data",
        icon=":material/info:",
    ):
        for reason, explanation in explanations.items():
            group = sorted(
                symbol for symbol, value in reasons.items() if value == reason
            )
            if group:
                st.markdown(f"**{reason} ({len(group):,})**  \n{explanation}")
                st.caption(", ".join(group[:200]) + (" \u2026" if len(group) > 200 else ""))


SCAN_MAX_WORKERS = 32


def _scan_stocks(
    symbols: list[str],
    filters: dict[str, float | bool],
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    results: list[dict[str, object]] = []
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(
        max_workers=max(1, min(SCAN_MAX_WORKERS, len(symbols))), thread_name_prefix="market-scan"
    ) as executor:
        pending = {
            executor.submit(fetch_daily_history, symbol): symbol
            for symbol in symbols
        }
        for done, future in enumerate(as_completed(pending), start=1):
            symbol = pending[future]
            if on_progress and (done % 25 == 0 or done == len(pending)):
                on_progress(done, len(pending))
            try:
                history = future.result()
                indicators = calculate_indicators(history)
            except (
                MarketDataError,
                requests.RequestException,
                ValueError,
            ) as error:
                reason = _failure_reason(error)
                if reason == _FAILURE_TEMPORARY:
                    log_failure(f"Public market scan for {symbol}", error)
                failures[symbol] = reason
                continue
            assessment = assess_stock(indicators)
            indicator_values = {
                "Daily_change_%": indicators["daily_change_pct"],
                "SMA_20": indicators["sma20"],
                "SMA_50": indicators["sma50"],
                "SMA_200": indicators["sma200"],
                "EMA_12": indicators["ema12"],
                "EMA_26": indicators["ema26"],
                "RSI_14": indicators["rsi14"],
                "MACD": indicators["macd"],
                "MACD_signal": indicators["macd_signal"],
                "MACD_histogram": indicators["macd_histogram"],
                "ATR_14": indicators["atr14"],
                "ATR_%": indicators["atr_pct"],
                "ADX_14": indicators["adx14"],
                "Plus_DI_14": indicators["plus_di14"],
                "Minus_DI_14": indicators["minus_di14"],
                "CCI_20": indicators["cci20"],
                "MFI_14": indicators["mfi14"],
                "OBV_change_20d": indicators["obv_change20"],
                "Bollinger_lower": indicators["bollinger_lower"],
                "Bollinger_middle": indicators["bollinger_middle"],
                "Bollinger_upper": indicators["bollinger_upper"],
                "Bollinger_width_%": indicators["bollinger_width_pct"],
                "Stochastic_%K": indicators["stochastic_k"],
                "Stochastic_%D": indicators["stochastic_d"],
                "Volatility_20d_annualized_%": indicators[
                    "volatility20_annualized_pct"
                ],
                "Return_1m_%": indicators["return_1m_pct"],
                "Return_3m_%": indicators["return_3m_pct"],
                "Average_volume_20d": indicators["average_volume_20"],
                "Volume_vs_average": indicators["relative_volume"],
                "52w_high": indicators["high_52w"],
                "52w_low": indicators["low_52w"],
                "52w_range_position_%": indicators["price_52w_position_pct"],
            }
            indicator_signals = assessment["indicators"]
            results.append(
                {
                    "Ticker": symbol,
                    "As_of": str(history["Date"].iloc[-1]),
                    "Price": indicators["price"],
                    "Recommendation": assessment["recommendation"],
                    "Positive_signals": int(assessment["bullish_checks"]),
                    "Total_signals": int(assessment["evaluated_checks"]),
                    "Signal_score": assessment["score"],
                    "Reason": assessment["reason"],
                    "Indicator_checks": [
                        [signal["Indicator"], signal["Value"], signal["Signal"]]
                        for signal in indicator_signals
                    ],
                    "Indicator_signals": "; ".join(
                        f"{signal['Indicator']}: {signal['Value']} "
                        f"({signal['Signal']})"
                        for signal in indicator_signals
                    ),
                    **indicator_values,
                    "Passes_filters": matches_scanner_filters(
                        indicators,
                        minimum_price=float(filters["minimum_price"]),
                        maximum_price=float(filters["maximum_price"]),
                        minimum_rsi=float(filters["minimum_rsi"]),
                        maximum_rsi=float(filters["maximum_rsi"]),
                        minimum_average_volume=float(
                            filters["minimum_average_volume"]
                        ),
                        require_above_sma50=bool(filters["require_above_sma50"]),
                        minimum_relative_volume=float(
                            filters.get("minimum_relative_volume", 0.0)
                        ),
                        minimum_atr_pct=float(
                            filters.get("minimum_atr_pct", 0.0)
                        ),
                    ),
                }
            )
    results.sort(
        key=lambda row: (
            not bool(row["Passes_filters"]),
            {"STRONG BUY": 0, "BUY": 1, "HOLD": 2, "SELL": 3}[
                str(row["Recommendation"])
            ],
            -int(row.get("Positive_signals", 0)),
            -int(row.get("Signal_score", 0)),
            str(row["Ticker"]),
        )
    )
    return results, failures


def _ai_recommendation_candidates(
    rows: list[dict[str, object]], *, use_custom_filters: bool = False
) -> list[dict[str, object]]:
    """Keep only screened NSE Strong Buy/Buy rows for broker-priced LLM input."""
    return [
        {
            "Ticker": str(row["Ticker"]).upper(),
            "Recommendation": str(row["Recommendation"]),
            "Positive_signals": int(row.get("Positive_signals", 0)),
            "Total_signals": int(row.get("Total_signals", 0)),
            "Reason": str(row.get("Reason", "")),
        }
        for row in rows
        if row["Recommendation"] in {"STRONG BUY", "BUY"}
        and (not use_custom_filters or bool(row.get("Passes_filters")))
        and not str(row["Ticker"]).upper().endswith(".BO")
    ]


_TRANSIENT_KEYS = ("_scan_button", "_scan_nse_button", "_refresh_universe")


def _restore_scanner_state(prefix: str) -> None:
    """Streamlit drops widget values when their page is not rendered; bring them back."""
    saved = st.session_state.get("_scanner_saved", {}).get(prefix, {})
    run_seq = st.session_state.get("_run_seq", 0)
    seen = st.session_state.setdefault("_scanner_seen_seq", {})
    returning = seen.get(prefix) != run_seq - 1
    seen[prefix] = run_seq
    for key, value in saved.items():
        if returning or key not in st.session_state:
            st.session_state[key] = value


def _save_scanner_state(prefix: str) -> None:
    start = f"market_{prefix}_"
    st.session_state.setdefault("_scanner_saved", {})[prefix] = {
        key: value
        for key, value in st.session_state.items()
        if isinstance(key, str)
        and key.startswith(start)
        and not key.endswith(_TRANSIENT_KEYS)
    }


def _render_scanner(
    *, scope: str, suggested_symbols: list[str] | None = None
) -> None:
    if scope == "Trading":
        st.caption(
            "Screen active trade candidates using price, RSI, average liquidity, "
            "relative volume, and ATR-normalized movement. Full indicator values "
            "remain visible for stocks that do not pass every filter."
        )
    else:
        st.caption(
            "Screen longer-term equity candidates using price, RSI, average "
            "liquidity, and trend filters. Full indicator values remain visible "
            "for stocks that do not pass every filter."
        )
    prefix = scope.lower().replace(" ", "_")
    _restore_scanner_state(prefix)
    recommended_filters = (
        TRADING_FILTERS if scope == "Trading" else RECOMMENDED_FILTERS
    )
    with st.container(horizontal=True, vertical_alignment="center"):
        refresh_symbols = st.button(
            "Refresh stock list",
            key=f"market_{prefix}_refresh_universe",
            icon=":material/refresh:",
            help="Reload the latest active equity lists published by NSE and BSE.",
        )
    master = _load_indian_equity_master(refresh=refresh_symbols)
    labels = _equity_labels(master)
    available_symbols = set(labels) | set(TICKER_MAP)
    for candidate in suggested_symbols or []:
        try:
            normalized_candidate = normalize_symbol(candidate)
        except ValueError:
            continue
        available_symbols.add(normalized_candidate)
        labels.setdefault(normalized_candidate, normalized_candidate)
    if len(master):
        st.caption(
            f"{len(master):,} active Indian equities available across NSE "
            "and BSE · lists refresh automatically every 24 hours."
        )
        with st.expander("What do the stock counts mean?", icon=":material/help:"):
            st.markdown(
                "- **Listed stocks**: securities in the combined active NSE/BSE "
                "exchange list. The scan universe may also include broker-mapped "
                "or portfolio tickers.\n"
                "- **Fetched**: selected symbols for which enough daily price history "
                "was retrieved to calculate indicators. Unavailable symbols are "
                "reported separately as scan failures.\n"
                "- **Buy / Strong Buy signals**: fetched stocks whose technical "
                "indicator score is classified as Buy or Strong Buy. With custom "
                "filters enabled, this count includes only stocks that pass them.\n"
                "- **NSE candidates for portfolio analysis**: the latest Equities "
                "scan's Buy/Strong Buy stocks that pass custom filters (when enabled) "
                "and are NSE tickers. BSE `.BO` listings are excluded. The LLM can "
                "recommend only candidates with usable broker quotes."
            )
        st.markdown(
            "[BSE listed-equity feed ↗](https://www.bseindia.com/) · "
            "[NSE equity security master ↗]"
            "(https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv)"
        )
    for filter_name, default_value in recommended_filters.items():
        st.session_state.setdefault(
            f"market_{prefix}_{filter_name}", default_value
        )
    minimum_price: float
    maximum_price: float
    minimum_rsi: float
    maximum_rsi: float
    minimum_average_volume: float
    require_above_sma50: bool
    requested_defaults = [
        symbol
        for symbol in (suggested_symbols or DEFAULT_SCAN_SYMBOLS)
        if symbol in available_symbols
    ]
    if not requested_defaults:
        requested_defaults = [
            symbol
            for symbol in DEFAULT_SCAN_SYMBOLS
            if symbol in available_symbols
        ]
    watchlist_key = f"market_{prefix}_scan_symbols"
    st.session_state.setdefault(watchlist_key, requested_defaults)
    st.session_state[watchlist_key] = [
        symbol
        for symbol in st.session_state[watchlist_key]
        if symbol in available_symbols
    ]
    exchange_symbols = {
        exchange: sorted(
            set(master.loc[master["Exchange"].eq(exchange), "Symbol"].astype(str))
            if "Exchange" in master and "Symbol" in master
            else set()
        )
        for exchange in ("NSE", "BSE")
    }
    universe_options = [
        "Choose stock universe",
        "All listed stocks (NSE + BSE)",
        "NSE-listed stocks",
        "BSE-listed stocks",
        "Portfolio holdings",
    ]
    universe_key = f"market_{prefix}_scan_universe"
    if universe_key not in st.session_state:
        st.session_state[universe_key] = universe_options[0]
    st.selectbox(
        "Choose which stocks to scan",
        options=universe_options,
        key=universe_key,
        help=(
            "Choose a listing universe or your portfolio. Use the stock picker "
            "below to scan a hand-picked watchlist."
        ),
    )
    selected_universe = st.session_state[universe_key]
    select_all = selected_universe == "All listed stocks (NSE + BSE)"
    if select_all:
        symbols = sorted(available_symbols)
        st.caption(
            f"All {len(symbols):,} listed stocks selected. Historical prices are "
            "fetched individually; provider rate limits may apply."
        )
    elif selected_universe == "NSE-listed stocks":
        symbols = exchange_symbols["NSE"]
        st.caption(f"{len(symbols):,} NSE-listed stocks selected.")
    elif selected_universe == "BSE-listed stocks":
        symbols = exchange_symbols["BSE"]
        st.caption(f"{len(symbols):,} BSE-listed stocks selected.")
    elif selected_universe == "Portfolio holdings":
        symbols = []
        for state_key in ("equity_holdings", "portfolio"):
            portfolio_frame = st.session_state.get(state_key)
            if not isinstance(portfolio_frame, pd.DataFrame) or "Ticker" not in portfolio_frame.columns:
                continue
            for ticker in portfolio_frame["Ticker"].dropna():
                normalized = str(ticker).strip().upper()
                if normalized:
                    try:
                        symbols.append(normalize_symbol(normalized))
                    except ValueError:
                        continue
        symbols = sorted(set(symbols) & available_symbols)
        st.caption(f"{len(symbols):,} portfolio holdings available to scan.")
    else:
        symbols = st.multiselect(
            "Pick individual stocks",
            options=sorted(available_symbols),
            format_func=lambda ticker: labels.get(ticker, ticker),
            key=watchlist_key,
            help=(
                "Select any active BSE or NSE equity. Stocks held in this "
                "workspace are included in the suggested list."
            ),
        )
    use_custom = st.toggle(
        "Use custom filters",
        key=f"market_{prefix}_use_custom",
        help="Narrow the scan by price, RSI, volume and trend. Suggested "
        "values are pre-filled.",
    )
    st.caption(SCAN_METHOD)
    extra_filters: dict[str, float] = {}
    if use_custom:
        left, middle, right = st.columns(3)
        with left:
            minimum_price = _numeric_filter(st.number_input(
                "Minimum price (₹)",
                min_value=0.0,
                max_value=1_000_000.0,
                step=5.0,
                key=f"market_{prefix}_minimum_price",
            ), 0.0)
            minimum_rsi = _numeric_filter(st.number_input(
                "Minimum RSI",
                min_value=0.0,
                max_value=100.0,
                step=1.0,
                key=f"market_{prefix}_minimum_rsi",
            ), 0.0)
        with middle:
            maximum_price = _numeric_filter(st.number_input(
                "Maximum price (₹)",
                min_value=1.0,
                max_value=1_000_000.0,
                step=100.0,
                key=f"market_{prefix}_maximum_price",
            ), 1_000_000.0)
            maximum_rsi = _numeric_filter(st.number_input(
                "Maximum RSI",
                min_value=0.0,
                max_value=100.0,
                step=1.0,
                key=f"market_{prefix}_maximum_rsi",
            ), 100.0)
        with right:
            minimum_average_volume = _numeric_filter(st.number_input(
                "Minimum 20-day average volume",
                min_value=0.0,
                max_value=1_000_000_000.0,
                step=10_000.0,
                key=f"market_{prefix}_minimum_average_volume",
                format="%.0f",
                help=(
                    "Screens out thinly traded stocks; this is a share count, "
                    "not a rupee value."
                ),
            ), 0.0)
            require_above_sma50 = st.checkbox(
                "Require price above the 50-day average",
                key=f"market_{prefix}_require_above_sma50",
            )
        if scope == "Trading":
            relative_volume_column, atr_column = st.columns(2)
            with relative_volume_column:
                extra_filters["minimum_relative_volume"] = _numeric_filter(st.number_input(
                    "Minimum volume / 20-day average",
                    min_value=0.0,
                    max_value=100.0,
                    step=0.1,
                    key=f"market_{prefix}_minimum_relative_volume",
                    help="Targets active sessions with above-average traded volume.",
                ), 0.0)
            with atr_column:
                extra_filters["minimum_atr_pct"] = _numeric_filter(st.number_input(
                    "Minimum ATR / price (%)",
                    min_value=0.0,
                    max_value=100.0,
                    step=0.1,
                    key=f"market_{prefix}_minimum_atr_pct",
                    help="Targets stocks with a minimum daily true-range movement.",
                ), 0.0)

    else:
        minimum_price = 0.0
        maximum_price = 1_000_000.0
        minimum_rsi = 0.0
        maximum_rsi = 100.0
        minimum_average_volume = 0.0
        require_above_sma50 = False

    current_filters: dict[str, float | bool] = {
        "minimum_price": minimum_price,
        "maximum_price": maximum_price,
        "minimum_rsi": minimum_rsi,
        "maximum_rsi": maximum_rsi,
        "minimum_average_volume": minimum_average_volume,
        "require_above_sma50": require_above_sma50,
        **extra_filters,
    }
    custom_filters = use_custom and current_filters != recommended_filters
    if not use_custom:
        pass
    elif custom_filters:
        st.warning(
            "These filters differ from the suggested defaults. Review them before "
            "scanning; custom filters may return weaker or fewer matches."
        )
    else:
        st.caption(
            (
                "Suggested trade filters favor active volume and sufficient "
                "daily price movement."
                if scope == "Trading"
                else "Suggested equity filters favor established prices, liquid "
                "trading, non-extreme RSI, and positive medium-term trend."
            )
        )
    if minimum_price > maximum_price:
        st.error("Minimum price must not exceed maximum price.")
    if minimum_rsi > maximum_rsi:
        st.error("Minimum RSI must not exceed maximum RSI.")

    if custom_filters:
        st.html(
            f"<style>.st-key-market_{prefix}_scan_button button"
            "{background:#b42318!important;"
            "border-color:#b42318!important;color:#fff!important}</style>"
        )
    can_scan = (
        bool(symbols)
        and minimum_price <= maximum_price
        and minimum_rsi <= maximum_rsi
    )
    with st.container(horizontal=True, vertical_alignment="center"):
        scan_clicked = st.button(
            "Scan selected stocks",
            key=f"market_{prefix}_scan_button",
            type="primary",
            icon=":material/troubleshoot:",
            disabled=not can_scan,
            help=(
                "The button turns red when filters differ from the suggested defaults."
                if custom_filters
                else "Fetch delayed public daily candles and evaluate the selected stocks."
            ),
        )
        
    scan_requested = scan_clicked
    results: list[dict[str, object]] = []
    failures: dict[str, str] = {}
    if scan_requested:
        progress = st.progress(0.0, text=f"Reviewing {len(symbols):,} stock(s)…")

        def _update(done: int, total: int) -> None:
            progress.progress(
                done / total, text=f"Reviewed {done:,} of {total:,} stocks…"
            )

        results, failures = _scan_stocks(symbols, current_filters, _update)
        progress.empty()

    results_key = f"market_{prefix}_scan_results"
    failures_key = f"market_{prefix}_scan_failures"
    filters_key = f"market_{prefix}_scan_filters"
    symbols_key = f"market_{prefix}_scan_symbols_used"
    custom_key = f"market_{prefix}_scan_custom"
    if scan_requested:
        st.session_state[results_key] = results
        st.session_state[failures_key] = failures
        st.session_state[filters_key] = current_filters.copy()
        st.session_state[symbols_key] = symbols.copy()
        st.session_state[custom_key] = use_custom
        if scope == "Equities":
            st.session_state.market_ai_candidates = _ai_recommendation_candidates(
                results, use_custom_filters=use_custom
            )
            st.session_state.market_ai_candidates_updated_at = datetime.now()

    previous_results = st.session_state.get(results_key, [])
    previous_filters = st.session_state.get(filters_key)
    previous_symbols = st.session_state.get(symbols_key)
    used_custom = bool(st.session_state.get(custom_key))
    if previous_results and (
        previous_filters != current_filters or previous_symbols != symbols
    ):
        st.info("Scanner settings changed. Run the scan again to refresh results.")
    elif previous_results:
        matches = list(previous_results)
        # Always present the full fetched dataset in the interactive table.
        # When "Select all" was used to scan the entire universe, keep the
        # old behaviour of surfacing a dedicated metric for Buy / Strong Buy
        # counts so tests and the UX remain consistent, but still show all
        # scanned rows in the table (regardless of Recommendation).
        if select_all:
            buy_count = sum(
                1
                for row in previous_results
                if row["Recommendation"] in {"BUY", "STRONG BUY"}
                and (not used_custom or row["Passes_filters"])
            )
            metric_label = "Buy / Strong Buy signals"
            metric_value = buy_count
            empty_message = "No Buy or Strong Buy signals were found in this scan."
        else:
            metric_label = "Stocks scanned"
            metric_value = len(matches)
            empty_message = "No market data was available for the selected stocks."
        st.metric(
            metric_label,
            metric_value,
            f"of {len(previous_results)} fetched",
            border=True,
        )
        if scope == "Equities":
            candidate_count = len(st.session_state.get("market_ai_candidates", []))
            updated_at = st.session_state.get("market_ai_candidates_updated_at")
            update_note = (
                f" Last updated {updated_at.strftime('%d %b %Y, %H:%M:%S')}."
                if isinstance(updated_at, datetime)
                else " Run an Equities scan to create this candidate list."
            )
            st.caption(
                f"{candidate_count:,} NSE Buy/Strong Buy candidates from the latest "
                "Equities scan are saved for portfolio analysis."
                + update_note
            )
        if matches:
            payload, config = _scan_table_payload(matches, labels)
            config["showFilters"] = used_custom and not select_all
            show_scan_table(payload, config)
        else:
            st.info(empty_message)
        _render_scan_failures(st.session_state.get(failures_key))
    elif st.session_state.get(failures_key):
        _render_scan_failures(st.session_state[failures_key])
    _save_scanner_state(prefix)


def render_market_research(
    *,
    broker_connected: bool,
    scope: str,
    symbols: list[str] | None = None,
) -> None:
    if broker_connected:
        st.caption(
            f"Research uses public market history and is independent of your "
            f"broker session. Suggested tickers come from {scope.lower()} where "
            "a supported ticker is available."
        )
    else:
        st.info(
            "No broker is connected. Public stock research is still available."
        )
    st.caption(
        "Public daily prices may be delayed. Indicator values describe historical "
        "prices and are not personalized investment advice."
    )
    st.markdown("[Market data source: Yahoo Finance ↗](https://finance.yahoo.com/)")
    if scope not in {"Equities", "Trading", "Market watchlist"}:
        st.info(
            f"Public NSE stock indicators are not applicable to {scope.lower()}. "
            "This asset class is shown in its own investment workspace; no proxy "
            "stock or estimated data is substituted."
        )
        return
    _render_single_stock_search(scope=scope, symbols=symbols)


def render_market_scanner(
    *,
    broker_connected: bool,
    scope: str,
    symbols: list[str] | None = None,
) -> None:
    if not broker_connected:
        st.info(
            "No broker is connected. The public NSE watchlist scanner remains "
            "available without account credentials."
        )
    st.caption(
        "Public data can be delayed. Signals are rules-based historical research, "
        "not personalized investment advice or order instructions."
    )
    st.markdown("[Market data source: Yahoo Finance ↗](https://finance.yahoo.com/)")
    if scope not in {"Equities", "Trading", "Market watchlist"}:
        st.info(
            f"The NSE equity scanner cannot provide valid {scope.lower()} contract "
            "or NAV data. Instrument-specific feeds are not currently configured."
        )
        return
    _render_scanner(scope=scope, suggested_symbols=symbols)

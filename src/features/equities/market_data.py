"""Broker-independent equity research using delayed public chart data."""

from __future__ import annotations

import threading
import time
import io
import re
from math import isfinite
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

from core.constants import MarketDataEndpoint, MarketDataPattern, RuntimeDefault

_SYMBOL_PATTERN = re.compile(MarketDataPattern.SYMBOL.value)
_MARKET_DATA_TIMEOUT = RuntimeDefault.MARKET_DATA_TIMEOUT.value


class MarketDataError(RuntimeError):
    """Public market data could not be retrieved or interpreted."""


def normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper().removesuffix(".NS")
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError("Enter a valid NSE ticker symbol, such as RELIANCE.")
    return normalized


@st.cache_data(ttl=86_400, max_entries=1, show_spinner=False)
def fetch_nse_equity_master() -> pd.DataFrame:
    """Fetch and normalize the NSE's daily list of active EQ-series equities."""
    try:
        response = requests.get(
            MarketDataEndpoint.NSE_EQUITY_MASTER.value,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
                ),
                "Accept": "text/csv,*/*",
                "Referer": "https://www.nseindia.com/",
            },
            timeout=_MARKET_DATA_TIMEOUT,
        )
        response.raise_for_status()
        source = pd.read_csv(
            io.StringIO(response.content.decode("utf-8-sig")),
            skipinitialspace=True,
        )
    except (
        requests.RequestException,
        UnicodeDecodeError,
        pd.errors.ParserError,
        ValueError,
    ) as error:
        raise MarketDataError(
            "The NSE equity list is temporarily unavailable."
        ) from error

    source.columns = [str(column).strip().upper() for column in source.columns]
    required_columns = {"SYMBOL", "NAME OF COMPANY", "SERIES", "ISIN NUMBER"}
    if not required_columns.issubset(source.columns):
        raise MarketDataError("The NSE equity list has an unexpected format.")

    equities = source.loc[
        source["SERIES"].astype("string").str.strip().str.upper().eq("EQ"),
        ["SYMBOL", "NAME OF COMPANY", "ISIN NUMBER"],
    ].copy()
    equities.columns = ["Symbol", "Company", "ISIN"]
    equities["Symbol"] = equities["Symbol"].astype("string").str.strip().str.upper()
    equities["Company"] = equities["Company"].astype("string").str.strip()
    equities["ISIN"] = equities["ISIN"].astype("string").str.strip().str.upper()
    equities = equities.dropna(subset=["Symbol", "Company", "ISIN"])
    equities = equities.loc[equities["Company"].ne("") & equities["ISIN"].ne("")]
    valid_symbols: list[str] = []
    for symbol in equities["Symbol"]:
        try:
            valid_symbols.append(normalize_symbol(str(symbol)))
        except ValueError:
            valid_symbols.append("")
    equities["Symbol"] = valid_symbols
    equities = equities.loc[equities["Symbol"].ne("")]
    equities = equities.drop_duplicates(subset=["Symbol"], keep="last")
    equities = equities.sort_values("Symbol").reset_index(drop=True)
    if equities.empty:
        raise MarketDataError(
            "The NSE equity list contains no active EQ-series stocks."
        )
    equities["Exchange"] = "NSE"
    return equities[["Symbol", "Company", "ISIN", "Exchange"]]


@st.cache_data(ttl=86_400, max_entries=1, show_spinner=False)
def fetch_bse_equity_master() -> pd.DataFrame:
    """Fetch active BSE equity listings, retaining BSE codes for price lookup."""
    try:
        response = requests.get(
            MarketDataEndpoint.BSE_EQUITY_MASTER.value,
            params={"segment": "Equity", "status": "Active"},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json,*/*",
                "Referer": "https://www.bseindia.com/",
            },
            timeout=_MARKET_DATA_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise MarketDataError(
            "The BSE equity list is temporarily unavailable."
        ) from error

    if not isinstance(payload, list) or not payload:
        raise MarketDataError("The BSE equity list returned no active securities.")
    source = pd.DataFrame(payload)
    required_columns = {
        "SCRIP_CD",
        "Scrip_Name",
        "Status",
        "ISIN_NUMBER",
        "scrip_id",
        "Segment",
    }
    if not required_columns.issubset(source.columns):
        raise MarketDataError("The BSE equity list has an unexpected format.")

    active = source.loc[
        source["Status"].astype("string").str.strip().str.casefold().eq("active")
        & source["Segment"].astype("string").str.strip().str.casefold().eq("equity"),
        ["SCRIP_CD", "Scrip_Name", "ISIN_NUMBER", "scrip_id"],
    ].copy()
    active.columns = ["Code", "Company", "ISIN", "Ticker"]
    active["Code"] = active["Code"].astype("string").str.strip()
    active["Company"] = active["Company"].astype("string").str.strip()
    active["ISIN"] = active["ISIN"].astype("string").str.strip().str.upper()
    active["Ticker"] = active["Ticker"].astype("string").str.strip().str.upper()
    active = active.dropna(subset=["Code", "Company", "ISIN", "Ticker"])
    active = active.loc[
        active["Company"].ne("")
        & active["ISIN"].ne("")
        & active["Ticker"].ne("")
        & active["Code"].str.fullmatch(r"\d+")
    ]
    active["Symbol"] = active["Ticker"] + ".BO"
    active = active.drop_duplicates(subset=["ISIN"], keep="last")
    active = active.drop_duplicates(subset=["Symbol"], keep="last")
    active = active.sort_values("Symbol").reset_index(drop=True)
    if active.empty:
        raise MarketDataError("The BSE equity list contains no active equity stocks.")
    active["Exchange"] = "BSE"
    return active[["Symbol", "Company", "ISIN", "Exchange", "Ticker"]]


@st.cache_data(ttl=86_400, max_entries=1, show_spinner=False)
def fetch_indian_equity_master() -> pd.DataFrame:
    """Combine active BSE and NSE listings, preferring NSE symbols for shared ISINs."""
    sources: list[pd.DataFrame] = []
    source_errors: list[str] = []
    for fetch_master, exchange in (
        (fetch_bse_equity_master, "BSE"),
        (fetch_nse_equity_master, "NSE"),
    ):
        try:
            sources.append(fetch_master())
        except MarketDataError as error:
            source_errors.append(f"{exchange}: {error}")
    if not sources:
        raise MarketDataError(
            "NSE and BSE listed-stock feeds are unavailable. "
            + "; ".join(source_errors)
        )

    bse = next(
        (frame for frame in sources if "Ticker" in frame.columns),
        pd.DataFrame(columns=["Symbol", "Company", "ISIN", "Exchange"]),
    )
    nse = next(
        (frame for frame in sources if "Ticker" not in frame.columns),
        pd.DataFrame(columns=["Symbol", "Company", "ISIN", "Exchange"]),
    )
    nse_isins = set(nse["ISIN"].astype(str))
    bse_only = bse.loc[~bse["ISIN"].astype(str).isin(nse_isins)]
    combined = pd.concat(
        [
            frame[["Symbol", "Company", "ISIN", "Exchange"]]
            for frame in (nse, bse_only)
            if not frame.empty
        ]
        or [pd.DataFrame(columns=["Symbol", "Company", "ISIN", "Exchange"])],
        ignore_index=True,
    )
    if combined.empty:
        raise MarketDataError("The exchange lists contain no active equity stocks.")
    combined = combined.drop_duplicates(subset=["Symbol"], keep="first")
    combined = combined.sort_values(["Exchange", "Symbol"]).reset_index(drop=True)
    if source_errors:
        combined.attrs["source_warning"] = (
            "One exchange's listing feed could not be loaded: "
            + "; ".join(source_errors)
        )
    return combined


_MISSING_TTL_SECONDS = 24 * 3600
_missing_symbols: dict[str, float] = {}
_thread_local = threading.local()


def _http_session() -> requests.Session:
    """One keep-alive session per worker thread avoids repeated TLS handshakes."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4)
        session.mount("https://", adapter)
        _thread_local.session = session
    return session


def _no_data(symbol: str) -> MarketDataError:
    """Remember symbols Yahoo does not carry so re-scans skip them for a day."""
    _missing_symbols[symbol] = time.monotonic()
    return MarketDataError(f"No public market data was found for {symbol}.")


@st.cache_data(ttl=3 * 3600, max_entries=6000, show_spinner=False)
def fetch_daily_history(symbol: str) -> pd.DataFrame:
    """Fetch up to one year of daily candles from Yahoo Finance's public chart API."""
    normalized = normalize_symbol(symbol)
    yahoo_symbol = (
        normalized if normalized.endswith(".BO") else f"{normalized}.NS"
    )
    missed_at = _missing_symbols.get(normalized)
    if missed_at is not None and time.monotonic() - missed_at < _MISSING_TTL_SECONDS:
        raise MarketDataError(f"No public market data was found for {normalized}.")
    instrument = quote(yahoo_symbol, safe="")
    payload = None
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = _http_session().get(
                f"{MarketDataEndpoint.YAHOO_CHART.value}/{instrument}",
                params={"range": "1y", "interval": "1d", "events": "div,splits"},
                headers={"User-Agent": "Mozilla/5.0 WealthHomeAI/1.0"},
                timeout=_MARKET_DATA_TIMEOUT,
            )
            if response.status_code == 404:
                raise _no_data(normalized)
            if response.status_code == 429 or response.status_code >= 500:
                last_error = requests.HTTPError(
                    f"HTTP {response.status_code}", response=response
                )
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            payload = response.json()
            break
        except (requests.RequestException, ValueError) as error:
            last_error = error
            time.sleep(0.5 * (attempt + 1))
    if payload is None:
        raise MarketDataError(
            "Public market data is temporarily unavailable or rate limited. "
            "Try again shortly."
        ) from last_error

    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        raise MarketDataError("Public market data returned an invalid chart response.")
    if chart.get("error"):
        raise _no_data(normalized)
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise _no_data(normalized)

    result = results[0]
    raw_timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    if (
        not isinstance(raw_timestamps, list)
        or not isinstance(indicators, dict)
        or any(
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not isfinite(timestamp)
            for timestamp in raw_timestamps
        )
    ):
        raise MarketDataError(
            f"Public market data for {normalized} contains an invalid daily history."
        )
    timestamps = raw_timestamps
    quote_sets = indicators.get("quote")
    if (
        not timestamps
        or not isinstance(quote_sets, list)
        or not quote_sets
        or not isinstance(quote_sets[0], dict)
    ):
        raise MarketDataError(
            f"Public market data for {normalized} contains no daily prices."
        )
    quote_data = quote_sets[0]

    def values(name: str) -> list[object]:
        column = quote_data.get(name)
        if not isinstance(column, list) or len(column) != len(timestamps):
            return [None] * len(timestamps)
        return column

    try:
        dates = pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(
            "Asia/Kolkata"
        ).date
    except (TypeError, ValueError, OverflowError) as error:
        raise MarketDataError(
            f"Public market data for {normalized} contains invalid dates."
        ) from error
    frame = pd.DataFrame(
        {
            "Date": dates,
            "Open": values("open"),
            "High": values("high"),
            "Low": values("low"),
            "Close": values("close"),
            "Volume": values("volume"),
        }
    )
    frame = frame.dropna(subset=["Close"]).reset_index(drop=True)
    if frame.empty:
        raise MarketDataError(
            f"Public market data for {normalized} contains no usable prices."
        )
    for column in ("Open", "High", "Low", "Close", "Volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["Close"]).reset_index(drop=True)


def calculate_indicators(history: pd.DataFrame) -> dict[str, float]:
    """Calculate full-year price, trend, momentum, volatility, and volume signals."""
    if "Close" not in history or len(history) < 50:
        raise MarketDataError(
            "At least 50 daily prices are needed to calculate stable trend indicators."
        )
    close = pd.to_numeric(history["Close"], errors="coerce").dropna()
    if (
        len(close) < 50
        or not close.gt(0).all()
        or not all(isfinite(float(value)) for value in close)
    ):
        raise MarketDataError("Market history contains invalid closing prices.")

    delta = close.diff()
    average_gain = delta.clip(lower=0).ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean()
    average_loss = -delta.clip(upper=0).ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean()
    relative_strength = average_gain / average_loss.replace(0, float("nan"))
    rsi = 100 - 100 / (1 + relative_strength)
    rsi = rsi.mask(average_loss.eq(0) & average_gain.gt(0), 100)
    rsi = rsi.mask(average_loss.eq(0) & average_gain.eq(0), 50)
    rsi = rsi.fillna(50.0).clip(0, 100)

    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    macd = ema_fast - ema_slow
    macd_signal = macd.ewm(span=9, adjust=False).mean()
    volume = (
        pd.to_numeric(history["Volume"], errors="coerce")
        if "Volume" in history
        else pd.Series(dtype="float64")
    )
    if "Volume" in history and volume.dropna().lt(0).any():
        raise MarketDataError("Market history contains invalid traded volume.")
    volume = volume.replace([float("inf"), float("-inf")], float("nan"))
    has_volume_data = "Volume" in history and volume.notna().any()
    average_volume_20 = (
        float(volume.tail(20).mean()) if has_volume_data else float("nan")
    )
    latest_volume = (
        float(volume.iloc[-1])
        if has_volume_data and len(volume) and pd.notna(volume.iloc[-1])
        else float("nan")
    )
    previous_close = float(close.iloc[-2])
    current_price = float(close.iloc[-1])
    sma20 = float(close.tail(20).mean())
    sma50 = float(close.tail(50).mean())
    sma200 = float(close.tail(200).mean()) if len(close) >= 200 else float("nan")
    ema12 = float(ema_fast.iloc[-1])
    ema26 = float(ema_slow.iloc[-1])
    macd_value = float(macd.iloc[-1])
    macd_signal_value = float(macd_signal.iloc[-1])
    macd_histogram = macd_value - macd_signal_value
    recent_returns = close.pct_change().dropna()
    volatility20 = (
        float(recent_returns.tail(20).std(ddof=1) * (252**0.5) * 100)
        if len(recent_returns.tail(20)) > 1
        else 0.0
    )
    bollinger_std = float(close.tail(20).std(ddof=0))
    bollinger_upper = sma20 + 2 * bollinger_std
    bollinger_lower = sma20 - 2 * bollinger_std
    bollinger_width_pct = (
        (bollinger_upper - bollinger_lower) / sma20 * 100 if sma20 else 0.0
    )
    stochastic_k = float("nan")
    stochastic_d = float("nan")
    atr14 = float("nan")
    atr_pct = float("nan")
    adx14 = float("nan")
    plus_di14 = float("nan")
    minus_di14 = float("nan")
    cci20 = float("nan")
    mfi14 = float("nan")
    obv_change20 = float("nan")
    if {"High", "Low"}.issubset(history.columns):
        highs = pd.to_numeric(history["High"], errors="coerce")
        lows = pd.to_numeric(history["Low"], errors="coerce")
        aligned_close = pd.to_numeric(history["Close"], errors="coerce")
        previous = aligned_close.shift(1)
        true_range = pd.concat(
            [
                highs - lows,
                (highs - previous).abs(),
                (lows - previous).abs(),
            ],
            axis=1,
        ).max(axis=1, skipna=True)
        atr_series = true_range.ewm(
            alpha=1 / 14, min_periods=14, adjust=False
        ).mean()
        if pd.notna(atr_series.iloc[-1]):
            atr14 = float(atr_series.iloc[-1])
            atr_pct = atr14 / current_price * 100 if current_price else float("nan")

        upward_move = highs.diff()
        downward_move = -lows.diff()
        plus_dm = upward_move.where(
            (upward_move > downward_move) & upward_move.gt(0), 0.0
        )
        minus_dm = downward_move.where(
            (downward_move > upward_move) & downward_move.gt(0), 0.0
        )
        smoothed_atr = true_range.ewm(
            alpha=1 / 14, min_periods=14, adjust=False
        ).mean()
        plus_di = (
            100 * plus_dm.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
            / smoothed_atr.replace(0, float("nan"))
        )
        minus_di = (
            100 * minus_dm.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
            / smoothed_atr.replace(0, float("nan"))
        )
        di_total = (plus_di + minus_di).replace(0, float("nan"))
        directional_index = 100 * (plus_di - minus_di).abs() / di_total
        adx_series = directional_index.ewm(
            alpha=1 / 14, min_periods=14, adjust=False
        ).mean()
        if pd.notna(adx_series.iloc[-1]):
            adx14 = float(adx_series.iloc[-1])
            plus_di14 = float(plus_di.iloc[-1])
            minus_di14 = float(minus_di.iloc[-1])

        lowest_14 = lows.rolling(14, min_periods=14).min()
        highest_14 = highs.rolling(14, min_periods=14).max()
        range_14 = (highest_14 - lowest_14).replace(0, float("nan"))
        stochastic_series = ((aligned_close - lowest_14) / range_14) * 100
        stochastic_k = (
            float(stochastic_series.iloc[-1])
            if pd.notna(stochastic_series.iloc[-1])
            else float("nan")
        )
        smoothed_stochastic = stochastic_series.rolling(3, min_periods=3).mean()
        stochastic_d = (
            float(smoothed_stochastic.iloc[-1])
            if pd.notna(smoothed_stochastic.iloc[-1])
            else float("nan")
        )
        typical_price = (highs + lows + aligned_close) / 3
        typical_average = typical_price.rolling(20, min_periods=20).mean()
        mean_deviation = typical_price.rolling(20, min_periods=20).apply(
            lambda window: float(abs(window - window.mean()).mean()),
            raw=True,
        )
        cci_series = (typical_price - typical_average) / (
            0.015 * mean_deviation.replace(0, float("nan"))
        )
        if pd.notna(cci_series.iloc[-1]):
            cci20 = float(cci_series.iloc[-1])

        if "Volume" in history:
            aligned_volume = pd.to_numeric(
                history["Volume"], errors="coerce"
            ).fillna(0)
            money_flow = typical_price * aligned_volume
            typical_delta = typical_price.diff()
            positive_flow = money_flow.where(typical_delta > 0, 0.0).rolling(
                14, min_periods=14
            ).sum()
            negative_flow = money_flow.where(typical_delta < 0, 0.0).rolling(
                14, min_periods=14
            ).sum()
            money_ratio = positive_flow / negative_flow.replace(
                0, float("nan")
            )
            mfi_series = 100 - 100 / (1 + money_ratio)
            mfi_series = mfi_series.mask(
                negative_flow.eq(0) & positive_flow.gt(0), 100
            )
            mfi_series = mfi_series.mask(
                negative_flow.eq(0) & positive_flow.eq(0), 50
            )
            if pd.notna(mfi_series.iloc[-1]):
                mfi14 = float(mfi_series.iloc[-1])
            direction = aligned_close.diff().gt(0).astype(int) - aligned_close.diff().lt(
                0
            ).astype(int)
            on_balance_volume = (direction * aligned_volume).cumsum()
            if len(on_balance_volume) >= 21:
                obv_change20 = float(
                    on_balance_volume.iloc[-1] - on_balance_volume.iloc[-21]
                )
        high_52w = float(highs.tail(252).max())
        low_52w = float(lows.tail(252).min())
    else:
        high_52w = float(close.tail(252).max())
        low_52w = float(close.tail(252).min())
    rolling_high = close.rolling(14, min_periods=14).max()
    rolling_low = close.rolling(14, min_periods=14).min()
    price_52w_position = (
        (current_price - low_52w) / (high_52w - low_52w) * 100
        if high_52w > low_52w
        else 50.0
    )
    del rolling_high, rolling_low

    return {
        "price": current_price,
        "daily_change_pct": (
            (current_price / previous_close - 1) * 100 if previous_close else 0.0
        ),
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "ema12": ema12,
        "ema26": ema26,
        "rsi14": float(rsi.iloc[-1]),
        "macd": macd_value,
        "macd_signal": macd_signal_value,
        "macd_histogram": macd_histogram,
        "atr14": atr14,
        "atr_pct": atr_pct,
        "adx14": adx14,
        "plus_di14": plus_di14,
        "minus_di14": minus_di14,
        "cci20": cci20,
        "mfi14": mfi14,
        "obv_change20": obv_change20,
        "bollinger_upper": bollinger_upper,
        "bollinger_middle": sma20,
        "bollinger_lower": bollinger_lower,
        "bollinger_width_pct": bollinger_width_pct,
        "stochastic_k": stochastic_k,
        "stochastic_d": stochastic_d,
        "volatility20_annualized_pct": volatility20,
        "return_1m_pct": (
            (current_price / float(close.iloc[-21]) - 1) * 100
            if len(close) >= 21
            else 0.0
        ),
        "return_3m_pct": (
            (current_price / float(close.iloc[-61]) - 1) * 100
            if len(close) >= 61
            else float("nan")
        ),
        "price_52w_position_pct": price_52w_position,
        "average_volume_20": average_volume_20,
        "relative_volume": (
            latest_volume / average_volume_20
            if isfinite(latest_volume)
            and isfinite(average_volume_20)
            and average_volume_20 > 0
            else float("nan")
        ),
        "volume": latest_volume,
        "high_52w": high_52w,
        "low_52w": low_52w,
    }


def assess_stock(indicators: dict[str, float]) -> dict[str, object]:
    """Return an explainable trend/momentum signal, not personalized advice."""
    indicators = {
        "sma200": float("nan"),
        "ema12": float("nan"),
        "ema26": float("nan"),
        "macd_histogram": float("nan"),
        "atr14": float("nan"),
        "atr_pct": float("nan"),
        "adx14": float("nan"),
        "plus_di14": float("nan"),
        "minus_di14": float("nan"),
        "cci20": float("nan"),
        "mfi14": float("nan"),
        "obv_change20": float("nan"),
        "bollinger_lower": float("nan"),
        "bollinger_middle": float("nan"),
        "bollinger_upper": float("nan"),
        "bollinger_width_pct": float("nan"),
        "stochastic_k": float("nan"),
        "stochastic_d": float("nan"),
        "volatility20_annualized_pct": float("nan"),
        "return_1m_pct": float("nan"),
        "return_3m_pct": float("nan"),
        "price_52w_position_pct": float("nan"),
        "volume": float("nan"),
        "average_volume_20": float("nan"),
        "relative_volume": float("nan"),
        **indicators,
    }
    price = indicators["price"]
    sma20 = indicators["sma20"]
    sma50 = indicators["sma50"]
    rsi = indicators["rsi14"]
    macd = indicators["macd"]
    macd_signal = indicators["macd_signal"]

    def check(
        name: str, value: float, positive: bool, negative: bool
    ) -> tuple[str, bool | None]:
        if not isfinite(value):
            return (name, None)
        return (name, positive if positive else False if negative else None)

    adx_is_valid = all(
        isfinite(indicators[key]) for key in ("adx14", "plus_di14", "minus_di14")
    )
    stochastic_is_valid = all(
        isfinite(indicators[key]) for key in ("stochastic_k", "stochastic_d")
    )
    signal_checks = [
        check("Price vs 20-day average", sma20, price > sma20, price < sma20),
        check("Price vs 50-day average", sma50, price > sma50, price < sma50),
        check(
            "Price vs 200-day average",
            indicators["sma200"],
            price > indicators["sma200"],
            price < indicators["sma200"],
        ),
        check("Price vs 12-day EMA", indicators["ema12"], price > indicators["ema12"], price < indicators["ema12"]),
        check("Price vs 26-day EMA", indicators["ema26"], price > indicators["ema26"], price < indicators["ema26"]),
        check("MACD vs signal", macd_signal, macd > macd_signal, macd < macd_signal),
        check("RSI", rsi, 50 <= rsi <= 70, rsi < 40 or rsi > 80),
        (
            "ADX with directional index",
            (
                indicators["plus_di14"] > indicators["minus_di14"]
                if adx_is_valid and indicators["adx14"] >= 25
                else False
                if adx_is_valid
                and indicators["adx14"] >= 25
                and indicators["plus_di14"] < indicators["minus_di14"]
                else None
            ),
        ),
        (
            "Stochastic",
            (
                indicators["stochastic_k"] > indicators["stochastic_d"]
                if stochastic_is_valid
                and indicators["stochastic_k"] > indicators["stochastic_d"]
                else False
                if stochastic_is_valid
                and indicators["stochastic_k"] < indicators["stochastic_d"]
                else None
            ),
        ),
        check("CCI", indicators["cci20"], indicators["cci20"] > 0, indicators["cci20"] < 0),
        check("MFI", indicators["mfi14"], indicators["mfi14"] > 50, indicators["mfi14"] < 50),
        check("OBV", indicators["obv_change20"], indicators["obv_change20"] > 0, indicators["obv_change20"] < 0),
        check("Bollinger position", indicators["bollinger_middle"], price > indicators["bollinger_middle"], price < indicators["bollinger_middle"]),
        check("Volume vs 20-day average", indicators["relative_volume"], indicators["relative_volume"] >= 1, indicators["relative_volume"] < 1),
        check("1-month return", indicators["return_1m_pct"], indicators["return_1m_pct"] > 0, indicators["return_1m_pct"] < 0),
        check("3-month return", indicators["return_3m_pct"], indicators["return_3m_pct"] > 0, indicators["return_3m_pct"] < 0),
        check("52-week range position", indicators["price_52w_position_pct"], indicators["price_52w_position_pct"] >= 50, indicators["price_52w_position_pct"] < 50),
    ]
    bullish = sum(value is True for _, value in signal_checks)
    bearish = sum(value is False for _, value in signal_checks)
    evaluated_checks = bullish + bearish
    trend_positive_count = sum(
        value is True for name, value in signal_checks[:5]
    )
    trend_negative_count = sum(
        value is False for name, value in signal_checks[:5]
    )

    checks = (
        (
            price > sma20,
            price < sma20,
            "Price vs 20-day average",
            f"₹{price:,.2f} vs ₹{sma20:,.2f}",
            "Above average" if price > sma20 else "Below average",
        ),
        (
            price > indicators["ema12"],
            price < indicators["ema12"],
            "Price vs 12-day EMA",
            f"₹{price:,.2f} vs ₹{indicators['ema12']:,.2f}",
            (
                "Price above short-term EMA"
                if price > indicators["ema12"]
                else "Price below short-term EMA"
            ),
        ),
        (
            price > indicators["ema26"],
            price < indicators["ema26"],
            "Price vs 26-day EMA",
            f"₹{price:,.2f} vs ₹{indicators['ema26']:,.2f}",
            (
                "Price above medium-term EMA"
                if price > indicators["ema26"]
                else "Price below medium-term EMA"
            ),
        ),
        (
            price > sma50,
            price < sma50,
            "Price vs 50-day average",
            f"₹{price:,.2f} vs ₹{sma50:,.2f}",
            "Price above average" if price > sma50 else "Price below average",
        ),
        (
            price > indicators["sma200"],
            price < indicators["sma200"],
            "Price vs 200-day average",
            (
                f"₹{price:,.2f} vs ₹{indicators['sma200']:,.2f}"
                if isfinite(indicators["sma200"])
                else "Unavailable (<200 sessions)"
            ),
            (
                "Price above average"
                if isfinite(indicators["sma200"]) and price > indicators["sma200"]
                else "Price below average"
                if isfinite(indicators["sma200"])
                else "Long-term average unavailable"
            ),
        ),
        (
            sma20 > sma50,
            sma20 < sma50,
            "20-day vs 50-day trend",
            f"₹{sma20:,.2f} vs ₹{sma50:,.2f}",
            "Short-term trend leads" if sma20 > sma50 else "Short-term trend lags",
        ),
        (
            macd > macd_signal,
            macd < macd_signal,
            "MACD momentum",
            f"{macd:.2f} vs signal {macd_signal:.2f}",
            "MACD above signal" if macd > macd_signal else "MACD below signal",
        ),
        (
            indicators["macd_histogram"] > 0,
            indicators["macd_histogram"] < 0,
            "MACD histogram",
            f"{indicators['macd_histogram']:.2f}",
            (
                "Positive momentum expansion"
                if indicators["macd_histogram"] > 0
                else "Negative momentum expansion"
                if indicators["macd_histogram"] < 0
                else "Momentum is flat"
            ),
        ),
        (
            50 <= rsi <= 70,
            rsi < 40 or rsi > 80,
            "14-day RSI",
            f"{rsi:.1f}",
            (
                "Positive momentum without an extreme reading"
                if 50 <= rsi <= 70
                else "Potentially oversold or overheated"
                if rsi < 40 or rsi > 80
                else "Momentum is neutral"
            ),
        ),
    )
    positive_share = bullish / evaluated_checks if evaluated_checks else 0.0
    negative_share = bearish / evaluated_checks if evaluated_checks else 0.0
    if evaluated_checks < 10:
        recommendation = "HOLD"
        recommendation_reason = (
            f"Only {evaluated_checks} indicator checks are available; at least 10 "
            "are needed for a directional signal."
        )
    elif positive_share >= 0.70:
        recommendation = "STRONG BUY"
        recommendation_reason = (
            f"{bullish} of {evaluated_checks} available indicator checks are positive."
        )
    elif positive_share >= 0.55:
        recommendation = "BUY"
        recommendation_reason = (
            f"{bullish} of {evaluated_checks} available indicator checks are positive."
        )
    elif negative_share >= 0.55:
        recommendation = "SELL"
        recommendation_reason = (
            f"{bearish} of {evaluated_checks} available indicator checks are negative."
        )
    else:
        recommendation = "HOLD"
        recommendation_reason = (
            "Trend and momentum signals are mixed; wait for clearer confirmation."
        )

    reasons = []
    scored_signals = dict(signal_checks)
    display_signal_names = {
        "Price vs 20-day average": "Price vs 20-day average",
        "Price vs 12-day EMA": "Price vs 12-day EMA",
        "Price vs 26-day EMA": "Price vs 26-day EMA",
        "Price vs 50-day average": "Price vs 50-day average",
        "Price vs 200-day average": "Price vs 200-day average",
        "MACD momentum": "MACD vs signal",
        "14-day RSI": "RSI",
    }
    for is_bullish, is_bearish, name, value, explanation in checks:
        score_name = display_signal_names.get(name)
        score = scored_signals.get(score_name) if score_name else None
        state = (
            "Positive"
            if score is True
            else "Negative"
            if score is False
            else "Unavailable"
            if score_name and "Unavailable" in value
            else "Neutral"
            if score_name
            else "Positive"
            if is_bullish
            else "Negative"
            if is_bearish
            else "Neutral"
        )
        reasons.append(
            {
                "Indicator": name,
                "Value": value,
                "Signal": state,
                "Reason": explanation,
            }
        )
    for name, value, comparison, reason in (
        (
            "20-day vs 50-day trend",
            f"₹{sma20:,.2f} vs ₹{sma50:,.2f}",
            sma20 > sma50,
            "20-day average above 50-day average",
        ),
        (
            "50-day vs 200-day trend",
            (
                f"₹{sma50:,.2f} vs ₹{indicators['sma200']:,.2f}"
                if isfinite(indicators["sma200"])
                else "Unavailable (<200 sessions)"
            ),
            (
                sma50 > indicators["sma200"]
                if isfinite(indicators["sma200"])
                else None
            ),
            "50-day average above 200-day average",
        ),
        (
            "ATR (14)",
            (
                f"₹{indicators['atr14']:,.2f} "
                f"({indicators['atr_pct']:.2f}% of price)"
                if isfinite(indicators["atr14"])
                else "Unavailable (high/low candles missing)"
            ),
            None,
            "Average true range measures recent absolute price movement.",
        ),
        (
            "Bollinger bands (20, 2σ)",
            (
                f"₹{indicators['bollinger_lower']:,.2f} – "
                f"₹{indicators['bollinger_upper']:,.2f}; "
                f"width {indicators['bollinger_width_pct']:.2f}%"
            ),
            (
                price > indicators["bollinger_middle"]
                if isfinite(indicators["bollinger_middle"])
                else None
            ),
            (
                "Price above the middle band"
                if isfinite(indicators["bollinger_middle"])
                and price > indicators["bollinger_middle"]
                else "Price below the middle band"
                if isfinite(indicators["bollinger_middle"])
                else "Bollinger data unavailable"
            ),
        ),
        (
            "Stochastic (%K / %D)",
            (
                f"{indicators['stochastic_k']:.1f} / {indicators['stochastic_d']:.1f}"
                if isfinite(indicators["stochastic_k"])
                and isfinite(indicators["stochastic_d"])
                else "Unavailable (high/low candles missing)"
            ),
            (
                indicators["stochastic_k"] > indicators["stochastic_d"]
                if isfinite(indicators["stochastic_k"])
                and isfinite(indicators["stochastic_d"])
                else None
            ),
            (
                "%K above %D (positive crossover)"
                if isfinite(indicators["stochastic_k"])
                and isfinite(indicators["stochastic_d"])
                and indicators["stochastic_k"] > indicators["stochastic_d"]
                else "%K below %D (negative crossover)"
                if isfinite(indicators["stochastic_k"])
                and isfinite(indicators["stochastic_d"])
                and indicators["stochastic_k"] < indicators["stochastic_d"]
                else "Flat or unavailable"
                if isfinite(indicators["stochastic_k"])
                else "No OHLC data for stochastic calculation"
            ),
        ),
        (
            "20-day annualized volatility",
            f"{indicators['volatility20_annualized_pct']:.2f}%",
            None,
            "Historical close-to-close volatility; not a forecast.",
        ),
        (
            "ADX / directional index (14)",
            (
                f"ADX {indicators['adx14']:.1f}; "
                f"+DI {indicators['plus_di14']:.1f} / "
                f"-DI {indicators['minus_di14']:.1f}"
                if isfinite(indicators["adx14"])
                else "Unavailable (high/low candles missing)"
            ),
            (
                indicators["plus_di14"] > indicators["minus_di14"]
                if isfinite(indicators["adx14"]) and indicators["adx14"] >= 25
                else False
                if isfinite(indicators["adx14"])
                and indicators["adx14"] >= 25
                and indicators["plus_di14"] < indicators["minus_di14"]
                else None
            ),
            (
                "Directional trend is strong and positive"
                if isfinite(indicators["adx14"])
                and indicators["adx14"] >= 25
                and indicators["plus_di14"] > indicators["minus_di14"]
                else "Directional trend is strong and negative"
                if isfinite(indicators["adx14"])
                and indicators["adx14"] >= 25
                else "Trend strength is weak or data is unavailable"
            ),
        ),
        (
            "CCI (20)",
            (
                f"{indicators['cci20']:+.1f}"
                if isfinite(indicators["cci20"])
                else "Unavailable (high/low candles missing)"
            ),
            (
                indicators["cci20"] > 0
                if isfinite(indicators["cci20"])
                else None
            ),
            (
                "Positive momentum (>0)"
                if isfinite(indicators["cci20"]) and indicators["cci20"] > 0
                else "Negative momentum (<0)"
                if isfinite(indicators["cci20"]) and indicators["cci20"] < 0
                else "Neutral"
                if isfinite(indicators["cci20"])
                else "OHLC history is unavailable"
            ),
        ),
        (
            "Money flow index (14)",
            (
                f"{indicators['mfi14']:.1f}"
                if isfinite(indicators["mfi14"])
                else "Unavailable (OHLCV data missing)"
            ),
            (
                indicators["mfi14"] > 50
                if isfinite(indicators["mfi14"])
                else None
            ),
            (
                "Positive money flow (>50)"
                if isfinite(indicators["mfi14"]) and indicators["mfi14"] > 50
                else "Negative money flow (<50)"
                if isfinite(indicators["mfi14"]) and indicators["mfi14"] < 50
                else "Neutral or unavailable"
                if isfinite(indicators["mfi14"])
                else "OHLCV history is unavailable"
            ),
        ),
        (
            "20-day OBV change",
            (
                f"{indicators['obv_change20']:+,.0f} shares"
                if isfinite(indicators["obv_change20"])
                else "Unavailable (OHLCV data missing)"
            ),
            (
                indicators["obv_change20"] > 0
                if isfinite(indicators["obv_change20"])
                else None
            ),
            (
                "Accumulation bias"
                if isfinite(indicators["obv_change20"])
                and indicators["obv_change20"] > 0
                else "Distribution bias"
                if isfinite(indicators["obv_change20"])
                and indicators["obv_change20"] < 0
                else "Flat or unavailable"
            ),
        ),
        (
            "1-month return",
            f"{indicators['return_1m_pct']:+.2f}%",
            indicators["return_1m_pct"] > 0,
            "Positive" if indicators["return_1m_pct"] > 0 else "Negative/flat",
        ),
        (
            "3-month return",
            f"{indicators['return_3m_pct']:+.2f}%",
            indicators["return_3m_pct"] > 0,
            "Positive" if indicators["return_3m_pct"] > 0 else "Negative/flat",
        ),
        (
            "52-week range position",
            f"{indicators['price_52w_position_pct']:.1f}% of range",
            indicators["price_52w_position_pct"] >= 50,
            "Upper half of range" if indicators["price_52w_position_pct"] >= 50 else (
                "Near the lower end"
                if indicators["price_52w_position_pct"] <= 30
                else "Mid-range"
            ),
        ),
    ):
        scored_name = {
            "Bollinger bands (20, 2σ)": "Bollinger position",
            "Stochastic (%K / %D)": "Stochastic",
            "ADX / directional index (14)": "ADX with directional index",
            "CCI (20)": "CCI",
            "Money flow index (14)": "MFI",
            "20-day OBV change": "OBV",
            "1-month return": "1-month return",
            "3-month return": "3-month return",
            "52-week range position": "52-week range position",
            "Average volume confirmation": "Volume vs 20-day average",
        }.get(name)
        if scored_name:
            score = scored_signals.get(scored_name)
            comparison = score
        state = (
            "Unavailable"
            if comparison is None and "Unavailable" in value
            else "Positive"
            if comparison is True
            else "Negative"
            if comparison is False
            else "Neutral"
        )
        reasons.append(
            {
                "Indicator": name,
                "Value": value,
                "Signal": state,
                "Reason": reason,
            }
        )
    reasons.append(
        {
            "Indicator": "Average volume confirmation",
            "Value": (
                f"{indicators['volume']:,.0f} vs "
                f"{indicators['average_volume_20']:,.0f} "
                f"({indicators['relative_volume']:.2f}×)"
            ),
            "Signal": (
                "Positive"
                if indicators["relative_volume"] >= 1
                else "Neutral"
                if indicators["relative_volume"] > 0
                else "Unavailable"
            ),
            "Reason": (
                "Trading volume is at or above its recent average."
                if indicators["relative_volume"] >= 1
                else "Trading volume is below its recent average."
                if indicators["relative_volume"] > 0
                else "Trading volume is unavailable."
            ),
        }
    )
    return {
        "recommendation": recommendation,
        "score": bullish - bearish,
        "reason": recommendation_reason,
        "bullish_checks": bullish,
        "bearish_checks": bearish,
        "evaluated_checks": evaluated_checks,
        "trend_positive_checks": trend_positive_count,
        "trend_negative_checks": trend_negative_count,
        "indicators": reasons,
    }


def matches_scanner_filters(
    indicators: dict[str, float],
    *,
    minimum_price: float,
    maximum_price: float,
    minimum_rsi: float,
    maximum_rsi: float,
    minimum_average_volume: float,
    require_above_sma50: bool,
    minimum_relative_volume: float = 0.0,
    minimum_atr_pct: float = 0.0,
) -> bool:
    """Apply user-selected liquidity, price, momentum, and trend filters."""
    return (
        minimum_price <= indicators["price"] <= maximum_price
        and minimum_rsi <= indicators["rsi14"] <= maximum_rsi
        and (
            minimum_average_volume <= 0
            or indicators["average_volume_20"] >= minimum_average_volume
        )
        and (
            minimum_relative_volume <= 0
            or (
                isfinite(indicators.get("relative_volume", float("nan")))
                and indicators["relative_volume"] >= minimum_relative_volume
            )
        )
        and (
            minimum_atr_pct <= 0
            or (
                isfinite(indicators.get("atr_pct", float("nan")))
                and indicators["atr_pct"] >= minimum_atr_pct
            )
        )
        and (
            not require_above_sma50
            or indicators["price"] > indicators["sma50"]
        )
    )


def style_recommendations(frame: pd.DataFrame, *, theme: str = "light"):
    """Style recommendation/indicator states using the portfolio return palette."""
    positive = "#216B44" if theme == "dark" else "#DCFCE7"
    negative = "#64282F" if theme == "dark" else "#FEE2E2"
    neutral = "#172334" if theme == "dark" else "#F1F5F9"
    text = "#F8FAFC" if theme == "dark" else "#172033"

    def color(value: object) -> str:
        normalized = str(value).upper()
        if normalized in {"BUY", "STRONG BUY", "POSITIVE"}:
            return f"background-color: {positive}; color: {text}"
        if normalized in {"SELL", "NEGATIVE"}:
            return f"background-color: {negative}; color: {text}"
        return f"background-color: {neutral}; color: {text}"

    columns = [column for column in ("Recommendation", "Signal") if column in frame]
    return frame.style.map(color, subset=columns)

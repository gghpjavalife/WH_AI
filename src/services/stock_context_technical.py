"""Daily technical indicators with explicit warnings for repaired values."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from core.logging import logger


class TechnicalDataError(ValueError):
    """Daily candles cannot support trustworthy technical calculations."""


def _last(series: pd.Series | None) -> float | None:
    if series is None or series.empty:
        return None
    value = series.iloc[-1]
    return float(value) if pd.notna(value) and math.isfinite(float(value)) else None


def simple_moving_average(close: pd.Series, length: int) -> pd.Series:
    return close.rolling(length, min_periods=length).mean()


def exponential_moving_average(close: pd.Series, length: int) -> pd.Series:
    return close.ewm(span=length, adjust=False, min_periods=length).mean()


def relative_strength_index(close: pd.Series, length: int = 14) -> pd.Series:
    change = close.diff()
    gain = change.clip(lower=0).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    loss = -change.clip(upper=0).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    rsi = (100 - 100 / (1 + gain / loss)).mask((loss == 0) & (gain > 0), 100)
    return rsi.mask((loss == 0) & (gain == 0), 50)


def moving_average_convergence_divergence(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = exponential_moving_average(close, 12) - exponential_moving_average(close, 26)
    signal = exponential_moving_average(line, 9)
    return line, signal, line - signal


def true_range_series(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    previous = close.shift()
    return pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)


def average_true_range(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    return true_range_series(high, low, close).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def calculate_technical_indicators(history: pd.DataFrame, *, warnings: list[str] | None = None) -> dict:
    """Compute indicators from unadjusted daily OHLCV, oldest candle first.

    ATR percentile is the rank of the latest ATR among available 14-day ATRs.
    Historical volatility is annualized, in percent. Squeeze means Bollinger
    bands lie inside a 20-day, 1.5-ATR Keltner channel.
    """
    columns = {str(c).lower(): c for c in history.columns}
    if not {"high", "low", "close", "volume"}.issubset(columns):
        raise TechnicalDataError("Daily history must include high, low, close and volume.")
    data = history.rename(columns={v: k for k, v in columns.items()}).copy()
    data = data.sort_index()
    fields = ["high", "low", "close", "volume"]
    for column in fields:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    if data.empty:
        raise TechnicalDataError("Daily history contains no candles.")
    if data[fields].isna().any().any():
        missing = int(data[fields].isna().sum().sum())
        data[fields] = data[fields].ffill().bfill()
        message = f"Technical data warning: forward/back-filled {missing} missing OHLCV values; indicators use estimates."
        logger.warning("%s", message)
        if warnings is not None:
            warnings.append(message)
    if not np.isfinite(data[fields].to_numpy(dtype=float)).all():
        raise TechnicalDataError("Daily history contains missing or invalid OHLCV values.")
    high, low, close, volume = (data[c] for c in ("high", "low", "close", "volume"))
    if ((close <= 0) | (high <= 0) | (low <= 0) | (high < low) |
            (close > high) | (close < low) | (volume < 0)).any():
        raise TechnicalDataError("Daily history contains inconsistent OHLCV values.")

    true_range = true_range_series(high, low, close)
    sma = {n: simple_moving_average(close, n) for n in (20, 50, 200)}
    ema = {n: exponential_moving_average(close, n) for n in (20, 50, 200)}
    rsi = relative_strength_index(close)
    macd_line, macd_signal, macd_hist = moving_average_convergence_divergence(close)
    bb_middle = sma[20]
    std = close.rolling(20).std(ddof=0)
    bb_upper, bb_lower = bb_middle + 2 * std, bb_middle - 2 * std
    atr = average_true_range(high, low, close)
    up, down = high.diff(), -low.diff()
    plus = up.where((up > down) & (up > 0), 0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    minus = down.where((down > up) & (down > 0), 0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    dx = 100 * (plus - minus).abs() / (plus + minus).replace(0, float("nan"))
    adx_value = dx.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    typical = (high + low + close) / 3
    flow = typical * volume
    positive = flow.where(typical.diff() > 0, 0).rolling(14).sum()
    negative = flow.where(typical.diff() < 0, 0).rolling(14).sum()
    mfi = (100 - 100 / (1 + positive / negative)).mask((negative == 0) & (positive > 0), 100)
    spread = (high.rolling(14).max() - low.rolling(14).min()).replace(0, float("nan"))
    ratio = true_range.rolling(14).sum() / spread
    chop = 100 * np.log10(ratio.where(ratio > 0)) / np.log10(14)

    atr_values = atr.dropna() if atr is not None else pd.Series(dtype=float)
    latest_atr = _last(atr)
    upper, lower = _last(bb_upper), _last(bb_lower)
    keltner_mid = _last(close.ewm(span=20, adjust=False, min_periods=20).mean())
    keltner_atr = _last(true_range.ewm(span=20, adjust=False, min_periods=20).mean())
    squeeze = None
    if None not in (upper, lower, keltner_mid, keltner_atr):
        squeeze = upper < keltner_mid + 1.5 * keltner_atr and lower > keltner_mid - 1.5 * keltner_atr
    result = {f"{kind}_{n}": _last(values[n]) for kind, values in (("sma", sma), ("ema", ema)) for n in (20, 50, 200)}
    result.update(
        rsi_14=_last(rsi), macd_line=_last(macd_line), macd_signal=_last(macd_signal),
        macd_hist=_last(macd_hist), bb_upper=upper, bb_lower=lower, bb_middle=_last(bb_middle),
        adx_14=_last(adx_value), current_volume=int(volume.iloc[-1]),
        avg_volume_3m=int(volume.tail(63).mean()) if len(volume) >= 63 else None,
        money_flow_index_14=_last(mfi),
        atr_percentile_14=float((atr_values <= latest_atr).mean() * 100) if latest_atr is not None else None,
        chop_index_14=_last(chop), squeeze_alert=squeeze,
        volume_price_trend=_last((close.pct_change(fill_method=None).fillna(0) * volume).cumsum()),
        historical_volatility_30=_last(close.pct_change(fill_method=None).rolling(30).std() * math.sqrt(252) * 100),
    )
    return result

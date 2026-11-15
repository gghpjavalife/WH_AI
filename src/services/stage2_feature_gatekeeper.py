"""In-memory technical feature calculation and deterministic Jev gatekeeping."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
import math
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class GatekeeperConfig:
    """Deterministic thresholds for the first Jev candidate-filtering phase."""

    enable_min_last_price: bool = True
    min_last_price: float = 20.0
    enable_max_spread_pct: bool = True
    max_spread_pct: float = 0.75
    enable_min_daily_volume: bool = True
    min_daily_volume: int = 50_000
    enable_min_adx_trend: bool = True
    min_adx_trend: float = 18.0
    enable_max_choppiness_index: bool = True
    max_choppiness_index: float = 61.8
    enable_min_piotroski_score: bool = True
    min_piotroski_score: int = 4
    enable_min_altman_z_score: bool = True
    min_altman_z_score: float = 1.81
    enable_max_promoter_pledge_pct: bool = True
    max_promoter_pledge_pct: float = 25.0
    min_history_bars: int = 50

    def __post_init__(self) -> None:
        if self.min_history_bars < 50:
            raise ValueError("At least 50 history bars are required.")
        if self.max_spread_pct < 0 or self.min_daily_volume < 0:
            raise ValueError("Spread and volume thresholds must be nonnegative.")


@dataclass
class GatekeeperDecision:
    instrument_key: str
    symbol: str
    passed: bool
    signal_tier: str
    score: float
    reject_reasons: list[str] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)


@dataclass
class Stage2BatchResult:
    """Outputs for the selected instruments, without any provider/network calls."""

    liquid_instrument_keys: list[str]
    decisions: list[GatekeeperDecision]


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _normalized_key(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().upper().replace(":", "|")


def select_liquid_universe(
    stage0_universe: Sequence[Mapping[str, Any]],
    prior_metrics: Mapping[str, Mapping[str, Any]] | None = None,
    max_instruments: int = 2500,
    min_avg_turnover_inr: float = 5_000_000.0,
) -> list[str]:
    """Select a deterministic capped equity universe, preferring liquid listings.

    Turnover is used as a hard floor and ranking signal when it is available.
    Without turnover history, eligible NSE listings rank before BSE listings.
    Duplicate listings sharing an ISIN resolve to the highest turnover listing,
    or NSE when neither listing has a turnover observation.
    """
    if max_instruments < 0:
        raise ValueError("Maximum instruments must be nonnegative.")
    if min_avg_turnover_inr < 0:
        raise ValueError("Minimum turnover must be nonnegative.")
    prior_metrics = prior_metrics or {}

    by_issuer: dict[str, tuple[float, int, str]] = {}
    for item in stage0_universe:
        if not isinstance(item, Mapping):
            continue
        key = _normalized_key(item.get("instrument_key") or item.get("key"))
        if key is None:
            continue
        segment = str(item.get("segment") or item.get("exchange") or "").strip().upper()
        if segment not in {"NSE_EQ", "BSE_EQ", "NSE", "BSE"}:
            continue
        if str(item.get("instrument_type") or "EQ").strip().upper() != "EQ":
            continue

        metric = prior_metrics.get(key) or prior_metrics.get(
            str(item.get("instrument_key") or item.get("key"))
        ) or {}
        if not isinstance(metric, Mapping):
            metric = {}
        turnover = _finite_number(metric.get("avg_20d_turnover"))
        if turnover is not None and turnover < min_avg_turnover_inr:
            continue
        exchange_priority = 1 if segment in {"NSE_EQ", "NSE"} else 0
        rank_turnover = turnover if turnover is not None else -1.0

        isin = str(item.get("isin") or "").strip().upper()
        issuer_key = f"ISIN:{isin}" if isin else f"KEY:{key}"
        candidate = (rank_turnover, exchange_priority, key)
        current = by_issuer.get(issuer_key)
        if current is None or (
            candidate[0] > current[0]
            or candidate[0] == current[0]
            and candidate[1] > current[1]
            or candidate[:2] == current[:2]
            and candidate[2] < current[2]
        ):
            by_issuer[issuer_key] = candidate

    ranked = sorted(
        by_issuer.values(),
        key=lambda candidate: (-candidate[0], -candidate[1], candidate[2]),
    )
    return [candidate[2] for candidate in ranked[:max_instruments]]


def _history_frame(history_df: pd.DataFrame) -> pd.DataFrame:
    column_lookup = {str(column).strip().lower(): column for column in history_df.columns}
    required = ("open", "high", "low", "close", "volume")
    missing = [column for column in required if column not in column_lookup]
    if missing:
        raise ValueError(f"History is missing required columns: {', '.join(missing)}.")
    frame = history_df.loc[:, [column_lookup[column] for column in required]].copy()
    frame.columns = list(required)
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.replace([np.inf, -np.inf], np.nan)
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    if frame.empty:
        return frame
    valid_prices = (
        frame[["open", "high", "low", "close"]].gt(0).all(axis=1)
        & frame["high"].ge(frame[["open", "low", "close"]].max(axis=1))
        & frame["low"].le(frame[["open", "high", "close"]].min(axis=1))
        & frame["volume"].fillna(0).ge(0)
    )
    return frame.loc[valid_prices]


def _append_or_update_live_candle(
    frame: pd.DataFrame, live_quote: Mapping[str, Any]
) -> pd.DataFrame:
    last_price = _finite_number(live_quote.get("last_price"))
    if last_price is None or last_price <= 0:
        return frame

    open_price = _finite_number(live_quote.get("open")) or last_price
    high_price = _finite_number(live_quote.get("high")) or last_price
    low_price = _finite_number(live_quote.get("low")) or last_price
    volume = _finite_number(live_quote.get("volume"))
    live_row = {
        "open": open_price,
        "high": max(high_price, open_price, last_price),
        "low": min(low_price, open_price, last_price),
        "close": last_price,
        "volume": volume if volume is not None and volume >= 0 else 0.0,
    }

    date_value = live_quote.get("timestamp") or live_quote.get("last_trade_time")
    if date_value is not None and len(frame):
        live_date = pd.to_datetime(date_value, errors="coerce", utc=True)
        index_values = pd.to_datetime(frame.index, errors="coerce", utc=True)
        if not pd.isna(live_date) and len(index_values) and not pd.isna(index_values[-1]):
            if live_date.date() == index_values[-1].date():
                for column, value in live_row.items():
                    frame.iloc[-1, frame.columns.get_loc(column)] = value
                return frame.reset_index(drop=True)
    return pd.concat([frame, pd.DataFrame([live_row])], ignore_index=True)


def _latest_number(series: pd.Series) -> float | None:
    if series.empty:
        return None
    return _finite_number(series.iloc[-1])


def _rounded(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def compute_22_technical_features(
    history_df: pd.DataFrame | None,
    live_quote: Mapping[str, Any] | None = None,
    *,
    min_history_bars: int = 50,
) -> dict[str, Any] | None:
    """Calculate 22 technical features from local candles and an optional quote.

    The live quote replaces today's last daily candle when timestamps identify
    the same date; otherwise it is appended as a new latest candle.
    """
    if min_history_bars < 1:
        raise ValueError("Minimum history bars must be positive.")
    if history_df is None or not isinstance(history_df, pd.DataFrame) or history_df.empty:
        return None
    frame = _history_frame(history_df)
    if frame.empty:
        return None
    if live_quote is not None:
        frame = _append_or_update_live_candle(frame, live_quote)
    if len(frame) < min_history_bars:
        return None

    close, high, low, volume = (
        frame[name].astype(float) for name in ("close", "high", "low", "volume")
    )
    sma_20 = close.rolling(20, min_periods=20).mean()
    sma_50 = close.rolling(50, min_periods=50).mean()
    sma_200 = close.rolling(200, min_periods=50).mean()
    ema_21 = close.ewm(span=21, adjust=False).mean()

    ema_12 = close.ewm(span=12, adjust=False).mean()
    ema_26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema_12 - ema_26
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - macd_signal

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    relative_strength = gain / loss.replace(0, np.nan)
    rsi_14 = (100 - 100 / (1 + relative_strength)).fillna(50.0).clip(0, 100)
    rsi_14 = rsi_14.mask(loss.eq(0) & gain.gt(0), 100.0)
    rsi_14 = rsi_14.mask(loss.eq(0) & gain.eq(0), 50.0)

    bb_std = close.rolling(20, min_periods=20).std()
    bb_upper = sma_20 + 2.0 * bb_std
    bb_lower = sma_20 - 2.0 * bb_std
    bb_width = (bb_upper - bb_lower) / sma_20.replace(0, np.nan) * 100.0

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_14 = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    atr_window = atr_14.tail(100).dropna()
    atr_percentile = (
        float((atr_window <= atr_window.iloc[-1]).mean() * 100)
        if len(atr_window) >= 20
        else None
    )

    keltner_upper = sma_20 + 1.5 * atr_14
    keltner_lower = sma_20 - 1.5 * atr_14
    squeeze_alert = None
    if all(
        value is not None
        for value in (
            _latest_number(bb_upper),
            _latest_number(bb_lower),
            _latest_number(keltner_upper),
            _latest_number(keltner_lower),
        )
    ):
        squeeze_alert = bool(
            bb_upper.iloc[-1] < keltner_upper.iloc[-1]
            and bb_lower.iloc[-1] > keltner_lower.iloc[-1]
        )

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=frame.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=frame.index,
    )
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_14.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_14.replace(0, np.nan)
    directional_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / directional_sum).fillna(0.0)
    adx_14 = dx.ewm(alpha=1 / 14, adjust=False).mean()

    atr_sum_14 = true_range.rolling(14, min_periods=14).sum()
    high_14 = high.rolling(14, min_periods=14).max()
    low_14 = low.rolling(14, min_periods=14).min()
    high_low_range = (high_14 - low_14).replace(0, np.nan)
    choppiness_14 = (
        100 * np.log10(atr_sum_14 / high_low_range) / np.log10(14.0)
    )

    typical_price = (high + low + close) / 3.0
    raw_money_flow = typical_price * volume.fillna(0)
    positive_flow = raw_money_flow.where(typical_price > typical_price.shift(1), 0.0)
    negative_flow = raw_money_flow.where(typical_price < typical_price.shift(1), 0.0)
    positive_sum = positive_flow.rolling(14, min_periods=14).sum()
    negative_sum = negative_flow.rolling(14, min_periods=14).sum()
    money_ratio = positive_sum / negative_sum.replace(0, np.nan)
    mfi_14 = (100 - 100 / (1 + money_ratio)).fillna(50.0)
    mfi_14 = mfi_14.mask(negative_sum.eq(0) & positive_sum.gt(0), 100.0)
    mfi_14 = mfi_14.mask(negative_sum.eq(0) & positive_sum.eq(0), 50.0)

    vpt = (
        (close - previous_close) / previous_close.replace(0, np.nan)
    ).fillna(0.0).mul(volume.fillna(0)).cumsum()
    average_volume_20 = volume.rolling(20, min_periods=20).mean()
    rvol_20 = volume / average_volume_20.replace(0, np.nan)
    stochastic_k = (close - low_14) / high_low_range * 100
    roc_10 = (close - close.shift(10)) / close.shift(10).replace(0, np.nan) * 100
    high_252 = high.rolling(252, min_periods=50).max()
    distance_52w_high = (close - high_252) / high_252.replace(0, np.nan) * 100

    last_close = _latest_number(close)
    vwap = _finite_number(live_quote.get("vwap")) if live_quote is not None else None
    if vwap is None:
        vwap = last_close

    return {
        "last_price": _rounded(last_close),
        "sma_20": _rounded(_latest_number(sma_20)),
        "sma_50": _rounded(_latest_number(sma_50)),
        "sma_200": _rounded(_latest_number(sma_200)),
        "ema_21": _rounded(_latest_number(ema_21)),
        "macd_line": _rounded(_latest_number(macd_line), 4),
        "macd_signal": _rounded(_latest_number(macd_signal), 4),
        "macd_hist": _rounded(_latest_number(macd_hist), 4),
        "rsi_14": _rounded(_latest_number(rsi_14)),
        "bb_upper": _rounded(_latest_number(bb_upper)),
        "bb_lower": _rounded(_latest_number(bb_lower)),
        "bb_width_pct": _rounded(_latest_number(bb_width)),
        "atr_14": _rounded(_latest_number(atr_14)),
        "atr_percentile": _rounded(atr_percentile, 1),
        "squeeze_alert": squeeze_alert,
        "adx_14": _rounded(_latest_number(adx_14)),
        "choppiness_14": _rounded(_latest_number(choppiness_14)),
        "mfi_14": _rounded(_latest_number(mfi_14)),
        "vpt": _rounded(_latest_number(vpt)),
        "rvol_20": _rounded(_latest_number(rvol_20)),
        "stoch_k": _rounded(_latest_number(stochastic_k)),
        "roc_10": _rounded(_latest_number(roc_10)),
        "dist_52w_high_pct": _rounded(_latest_number(distance_52w_high)),
        "vwap": _rounded(vwap),
        "above_vwap": last_close >= vwap if last_close is not None and vwap is not None else None,
    }


def _fundamental_value(fundamentals: Mapping[str, Any], name: str) -> Any:
    if name in fundamentals:
        return fundamentals[name]
    key_statistics = fundamentals.get("defaultKeyStatistics")
    if name == "piotroski_f_score" and isinstance(key_statistics, Mapping):
        return key_statistics.get("piotroskiFScore")
    return None


def evaluate_jev_gatekeeper(
    instrument_key: str,
    symbol: str,
    live_quote: Mapping[str, Any] | None,
    tech_features: Mapping[str, Any] | None,
    fundamentals: Mapping[str, Any] | None = None,
    config: GatekeeperConfig = GatekeeperConfig(),
) -> GatekeeperDecision:
    """Apply deterministic microstructure, trend and available-solvency gates."""
    if not live_quote or _finite_number(live_quote.get("last_price")) is None:
        return GatekeeperDecision(
            instrument_key, symbol, False, "REJECTED", 0.0, ["MISSING_LIVE_QUOTE"]
        )
    if not tech_features:
        return GatekeeperDecision(
            instrument_key, symbol, False, "REJECTED", 0.0, ["INSUFFICIENT_HISTORY"]
        )

    reasons: list[str] = []
    last_price = _finite_number(live_quote.get("last_price"))
    spread = _finite_number(live_quote.get("bid_ask_spread_pct"))
    volume = _finite_number(live_quote.get("volume"))
    if live_quote.get("is_circuit_locked") is True:
        reasons.append("CIRCUIT_LOCKED")
    if (
        config.enable_min_last_price
        and last_price is not None
        and last_price < config.min_last_price
    ):
        reasons.append(f"PENNY_PRICE<{config.min_last_price}")
    if config.enable_max_spread_pct and spread is None:
        reasons.append("MISSING_SPREAD")
    elif config.enable_max_spread_pct and spread > config.max_spread_pct:
        reasons.append(f"WIDE_SPREAD>{config.max_spread_pct}%")
    if (
        config.enable_min_daily_volume
        and (volume is None or volume < config.min_daily_volume)
    ):
        reasons.append(f"LOW_VOLUME<{config.min_daily_volume}")

    adx = _finite_number(tech_features.get("adx_14"))
    choppiness = _finite_number(tech_features.get("choppiness_14"))
    if config.enable_min_adx_trend and adx is None:
        reasons.append("MISSING_ADX_FEATURE")
    elif config.enable_min_adx_trend and adx < config.min_adx_trend:
        reasons.append(f"WEAK_TREND_ADX<{config.min_adx_trend}")
    if config.enable_max_choppiness_index and choppiness is None:
        reasons.append("MISSING_CHOPPINESS_FEATURE")
    elif (
        config.enable_max_choppiness_index
        and choppiness > config.max_choppiness_index
    ):
        reasons.append(f"CHOPPY_REGIME>{config.max_choppiness_index}")

    fundamentals = fundamentals or {}
    f_score = _finite_number(_fundamental_value(fundamentals, "piotroski_f_score"))
    z_score = _finite_number(_fundamental_value(fundamentals, "altman_z_score"))
    pledge = _finite_number(_fundamental_value(fundamentals, "promoter_pledge_pct"))
    if (
        config.enable_min_piotroski_score
        and f_score is not None
        and f_score < config.min_piotroski_score
    ):
        reasons.append(f"WEAK_PIOTROSKI<{config.min_piotroski_score}")
    if (
        config.enable_min_altman_z_score
        and z_score is not None
        and z_score < config.min_altman_z_score
    ):
        reasons.append(f"DISTRESS_ALTMAN_Z<{config.min_altman_z_score}")
    if (
        config.enable_max_promoter_pledge_pct
        and pledge is not None
        and pledge > config.max_promoter_pledge_pct
    ):
        reasons.append(f"HIGH_PLEDGE>{config.max_promoter_pledge_pct}%")

    if reasons:
        return GatekeeperDecision(
            instrument_key=instrument_key,
            symbol=symbol,
            passed=False,
            signal_tier="REJECTED",
            score=0.0,
            reject_reasons=reasons,
            features=dict(tech_features),
        )

    score = 50.0
    tests = (
        (_finite_number(tech_features.get("last_price")), _finite_number(tech_features.get("sma_50")), 10.0),
        (_finite_number(tech_features.get("sma_50")), _finite_number(tech_features.get("sma_200")), 10.0),
        (_finite_number(tech_features.get("macd_hist")), 0.0, 8.0),
        (_finite_number(tech_features.get("rsi_14")), 50.0, 0.0),
    )
    if tests[0][0] is not None and tests[0][1] is not None and tests[0][0] > tests[0][1]:
        score += tests[0][2]
    if tests[1][0] is not None and tests[1][1] is not None and tests[1][0] > tests[1][1]:
        score += tests[1][2]
    if tests[2][0] is not None and tests[2][0] > 0:
        score += tests[2][2]
    rsi = _finite_number(tech_features.get("rsi_14"))
    if rsi is not None and 50 <= rsi <= 70:
        score += 8.0
    if tech_features.get("above_vwap") is True:
        score += 7.0
    relative_volume = _finite_number(tech_features.get("rvol_20"))
    if relative_volume is not None and relative_volume >= 1.2:
        score += 7.0
    score = min(100.0, round(score, 1))
    tier = "STRONG_BUY" if score >= 82 else "BUY" if score >= 68 else "WATCH"
    return GatekeeperDecision(
        instrument_key=instrument_key,
        symbol=symbol,
        passed=True,
        signal_tier=tier,
        score=score,
        features=dict(tech_features),
    )


def run_stage2_batch(
    stage0_universe: Sequence[Mapping[str, Any]],
    live_quotes: Mapping[str, Mapping[str, Any]],
    history_by_instrument: Mapping[str, pd.DataFrame],
    fundamentals_by_symbol: Mapping[str, Mapping[str, Any]] | None = None,
    prior_metrics: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    max_instruments: int = 2500,
    min_avg_turnover_inr: float = 5_000_000.0,
    config: GatekeeperConfig = GatekeeperConfig(),
    session_state: MutableMapping[str, Any] | None = None,
) -> Stage2BatchResult:
    """Run Stage 2 using caller-supplied local snapshots only.

    Histories, live quotes, fundamentals and prior turnover metrics are inputs;
    this function intentionally performs no reads or requests against providers.
    """
    keys = select_liquid_universe(
        stage0_universe,
        prior_metrics,
        max_instruments=max_instruments,
        min_avg_turnover_inr=min_avg_turnover_inr,
    )
    universe_by_key = {
        _normalized_key(item.get("instrument_key") or item.get("key")): item
        for item in stage0_universe
        if isinstance(item, Mapping)
        and _normalized_key(item.get("instrument_key") or item.get("key"))
    }
    quotes_by_key = {
        key: quote
        for raw_key, quote in live_quotes.items()
        if (key := _normalized_key(raw_key)) is not None
    }
    histories_by_key = {
        key: history
        for raw_key, history in history_by_instrument.items()
        if (key := _normalized_key(raw_key)) is not None
    }
    fundamentals_by_symbol = {
        str(symbol).strip().upper(): value
        for symbol, value in (fundamentals_by_symbol or {}).items()
    }

    decisions = []
    for key in keys:
        instrument = universe_by_key.get(key, {})
        symbol = str(
            instrument.get("symbol")
            or instrument.get("trading_symbol")
            or key.rsplit("|", 1)[-1]
        ).strip().upper()
        quote = quotes_by_key.get(key)
        history = histories_by_key.get(key)
        features = compute_22_technical_features(
            history,
            quote,
            min_history_bars=config.min_history_bars,
        )
        decision = evaluate_jev_gatekeeper(
            key,
            symbol,
            quote,
            features,
            fundamentals_by_symbol.get(symbol),
            config,
        )
        decisions.append(decision)
    decisions.sort(key=lambda decision: (-decision.score, decision.instrument_key))
    result = Stage2BatchResult(keys, decisions)
    if session_state is not None:
        session_state["_stage2_gatekeeper_decisions"] = {
            decision.instrument_key: decision for decision in decisions
        }
    return result

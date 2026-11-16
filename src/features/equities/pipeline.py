"""Explicit Stage 0-4 pipeline UI for the equity market scanner."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import hashlib
from html import escape
import json
import math
from queue import Empty, Queue
import sqlite3
from typing import Any

import pandas as pd
import streamlit as st

from core.config import configured_value
from services.stage0_bootstrap import (
    STAGE0_DB_PATH,
    get_fundamental_snapshots,
    read_stage0_cache,
    request_stage0_refresh,
    stage0_cache_is_fresh,
)
from services.stage0_history_sync import (
    HistorySyncError,
    load_cached_history_frames,
    sync_daily_history_sync,
)
from services.stage1_live_pulse import get_stage1_pulse_cached
from services.stage2_feature_gatekeeper import (
    GatekeeperConfig,
    GatekeeperDecision,
    run_stage2_batch,
    select_liquid_universe,
)
from services.stage3_lazy_enrichment import get_stage3_enrichment_cached
from services.stage4_ai_thesis import (
    JevFirewallError,
    build_governed_ai_payload,
    generate_governed_ai_thesis_cached,
)

_PIPELINE_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="equity-pipeline"
)
_STAGE4_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="equity-ai-thesis"
)
_STAGE3_MAX_CONCURRENCY = 10

_STAGE_NAMES = (
    "Universe",
    "Live pulse",
    "Technical screen",
    "Enrichment",
    "AI thesis",
)
_STAGE_DESCRIPTIONS = (
    "INPUT: selected NSE/BSE equity listings. "
    "LOADS: Upstox instrument master and macro anchors from the local cache; refreshes "
    "Stage 0 when stale; syncs daily OHLCV candles into SQLite. A first sync requests "
    "380 calendar days; later syncs request only the checkpoint delta. No live quotes "
    "or AI are requested here.",
    "INPUT: the selected Stage 0 instrument keys. FETCHES: Upstox full-quote batches "
    "with last price, OHLCV, VWAP, top-of-book spread, circuit limits, and circuit "
    "status. Quotes are cached and reused during their freshness window.",
    "INPUT: Stage 1 quotes, up to 260 cached daily candles, and cached fundamentals. "
    "PROCESSES LOCALLY (no network): 22 indicators (trend averages, MACD, RSI, "
    "Bollinger, ATR/squeeze, ADX/choppiness, MFI/VPT/relative volume, stochastic, "
    "ROC, and 52-week distance), then Jev price, spread, volume, trend, solvency, "
    "pledge, and history rules. Missing inputs remain unavailable, not zero.",
    "INPUT: only stocks that pass Stage 2. FETCHES IN PARALLEL (up to 10 stocks): "
    "Upstox option contracts/chain for eligibility, PCR, max pain, ATM IV/Greeks; "
    "Yahoo company profile, analyst targets/recommendations, and up to five recent "
    "headlines. Each feed has its own cached status; non-F&O is a normal outcome.",
    "INPUT: every Stage 2-passed stock with its quote, technicals, macro, "
    "fundamentals, Stage 3 context, and a compact comparison of the full Stage 2 "
    "final list. ON CLICK ONLY: generates one governed Gemini thesis for every "
    "Stage 2-passed stock, sequentially; results appear in the table and assessed "
    "stocks sort highest-first. No automatic AI calls.",
)
_STATUS_LABELS = {
    "success": "Ready",
    "failure": "Failed",
    "refresh_required": "Refresh required",
    "idle": "Not loaded",
    "running": "Running",
}
_STAGE2_SIGNAL = {
    "STRONG_BUY": "STRONG BUY",
    "BUY": "BUY",
    "WATCH": "HOLD",
    "REJECTED": "NOT RATED",
}


def _token() -> str:
    broker_name = str(st.session_state.get("broker_name") or "")
    value = (
        st.session_state.get("token")
        if broker_name.casefold() == "upstox"
        else configured_value("UPSTOX_ANALYTICS_TOKEN", "")
    )
    return value.strip() if isinstance(value, str) else ""


def _stage4_gemini_api_key() -> str:
    configured = configured_value("GEMINI_API_KEY", "")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()

    direct_keys = (
        st.session_state.get("user_gemini_api_key"),
        st.session_state.get("user_settings_ai_key_gemini"),
        (
            st.session_state.get("llm_provider_settings", {})
            .get("Gemini", {})
            .get("api_key")
            if isinstance(st.session_state.get("llm_provider_settings"), dict)
            else None
        ),
    )
    for value in direct_keys:
        if isinstance(value, str) and value.strip():
            return value.strip()

    configurations = st.session_state.get("settings_ai_provider_configs", [])
    keys = st.session_state.get("settings_ai_provider_config_keys", {})
    if not isinstance(configurations, list) or not isinstance(keys, dict):
        return ""
    active_id = str(st.session_state.get("settings_ai_active_config_id") or "")
    gemini_configs = [
        item
        for item in configurations
        if isinstance(item, dict) and str(item.get("provider", "")).casefold() == "gemini"
    ]
    gemini_configs.sort(
        key=lambda item: str(item.get("id") or "") != active_id
    )
    for item in gemini_configs:
        value = keys.get(str(item.get("id") or ""))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _symbol_base(symbol: Any) -> str:
    return str(symbol or "").strip().upper().removesuffix(".NS").removesuffix(".BO")


def _nifty_market_regime(snapshot: dict[str, Any] | None) -> dict[str, float | None]:
    macro = snapshot.get("macro") if isinstance(snapshot, dict) else None
    nifty = macro.get("^NSEI") if isinstance(macro, dict) else None
    nifty = nifty if isinstance(nifty, dict) else {}
    values: dict[str, float | None] = {}
    for output_key, input_key in (
        ("nifty_price", "close"),
        ("nifty_dma_200", "dma_200"),
    ):
        try:
            value = float(nifty.get(input_key))
        except (TypeError, ValueError, OverflowError):
            value = None
        values[output_key] = value if value is not None and math.isfinite(value) else None
    return values


def _symbols_for_exchange(master: pd.DataFrame, exchange: str) -> set[str]:
    if not {"Exchange", "Symbol"}.issubset(master.columns):
        return set()
    exchange = exchange.strip().upper()
    exchange_values = master["Exchange"].astype(str).str.strip().str.upper()
    listings = master
    if exchange == "BSE" and "ISIN" in master.columns:
        nse_isins = {
            str(isin).strip().upper()
            for isin in master.loc[
                exchange_values.eq("NSE"), "ISIN"
            ].dropna()
            if str(isin).strip()
        }
        bse_rows = master.loc[exchange_values.eq("BSE")]
        listings = bse_rows.loc[
            ~bse_rows["ISIN"].astype(str).str.upper().isin(nse_isins)
            | bse_rows["ISIN"].isna()
            | bse_rows["ISIN"].astype(str).str.strip().eq("")
        ]
    else:
        listings = master.loc[exchange_values.eq(exchange)]
    return set(listings["Symbol"].dropna().astype(str))


def _selected_instruments(
    symbols: list[str], stage0_universe: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    listings_by_symbol: dict[str, set[str]] = {}
    for instrument in stage0_universe:
        if not isinstance(instrument, dict):
            continue
        raw_symbol = str(
            instrument.get("symbol") or instrument.get("trading_symbol") or ""
        ).strip().upper()
        if raw_symbol:
            exchange = "BSE" if instrument.get("segment") == "BSE_EQ" else "NSE"
            listings_by_symbol.setdefault(_symbol_base(raw_symbol), set()).add(exchange)

    selected_symbols = set()
    for value in symbols:
        ticker = str(value).strip().upper()
        if not ticker:
            continue
        base = _symbol_base(ticker)
        exchanges = listings_by_symbol.get(base, set())
        if ticker.endswith(".BO"):
            selected_symbols.add((base, "BSE"))
        elif "NSE" in exchanges:
            selected_symbols.add((base, "NSE"))
        elif "BSE" in exchanges:
            selected_symbols.add((base, "BSE"))
    candidates = []
    for instrument in stage0_universe:
        if not isinstance(instrument, dict):
            continue
        raw_symbol = str(
            instrument.get("symbol") or instrument.get("trading_symbol") or ""
        ).strip().upper()
        symbol = _symbol_base(raw_symbol)
        exchange = "BSE" if instrument.get("segment") == "BSE_EQ" else "NSE"
        if (symbol, exchange) in selected_symbols:
            candidates.append(instrument)
    selected_keys = set(
        select_liquid_universe(
            candidates,
            max_instruments=len(candidates),
            min_avg_turnover_inr=0,
        )
    )
    return [
        instrument
        for instrument in candidates
        if str(instrument.get("instrument_key") or instrument.get("key") or "")
        .strip()
        .upper()
        .replace(":", "|")
        in selected_keys
    ]


def _has_bse_listing(universe: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(instrument, dict)
        and str(instrument.get("segment") or "").strip().upper() == "BSE_EQ"
        for instrument in universe
    )


def _friendly_error(index: int, error: Exception, symbol: str | None = None) -> str:
    """Translate provider/SQLite details into actionable scanner messages."""
    if index == 0 and isinstance(error, HistorySyncError):
        return (
            "Daily candles could not be loaded for some selected stocks. Check "
            f"the Upstox connection and retry. {error}"
        )
    messages = {
        0: "Couldn't load the Stage 0 stock universe or daily history. Check "
        "your connection and refresh the universe.",
        1: "Couldn't load live quotes from Upstox. Check the Upstox connection "
        "and try refreshing the live pulse.",
        2: "Couldn't calculate the technical screen from local data. Refresh "
        "Stage 0 history and live quotes, then try again.",
        3: f"Couldn't load stock enrichment for {symbol or 'the selected stock'}. "
        "Check provider availability and try again.",
        4: f"Couldn't generate the AI thesis for {symbol or 'the screened stocks'}. "
        "Check the Gemini configuration and the displayed data requirements.",
    }
    detail = str(error).strip()
    return f"{messages[index]} {detail}" if detail else messages[index]


def _advance_automatic_stages(
    state: dict[str, Any],
    symbols: list[str],
    instruments: list[dict[str, Any]],
    market_regime: dict[str, float | None] | None = None,
) -> None:
    """Run Stages 0–3 automatically for a selected universe."""
    if not symbols:
        return
    if state.get("stage0_result") is None and state.get("stage0_future") is None:
        if state["statuses"][0] != "failure":
            _handle_stage0(state, symbols)
        return

    _consume_stage0_future(state)
    if state.get("stage0_result") is None:
        return

    if not state.get("stage1_attempted"):
        state["stage1_attempted"] = True
        _handle_stage1(state, instruments, st.session_state)

    if (
        state.get("stage1_result") is not None
        and not state.get("stage2_attempted")
        and state["statuses"][1] != "failure"
    ):
        state["stage2_attempted"] = True
        _handle_stage2(
            state,
            instruments,
            GatekeeperConfig(**state.get("gatekeeper_config", {})),
            market_regime,
        )

    if state.get("stage2_attempted"):
        if state["statuses"][2] == "failure":
            _set_stage(
                state,
                3,
                "refresh_required",
                "Stock enrichment is waiting for the technical screen to complete.",
            )
            return
        _consume_stage3_future(state)
        if (
            state.get("stage3_future") is None
            and not state.get("stage3_attempted")
        ):
            _start_stage3_batch(state, instruments)


def _stage0_prepare(symbols: list[str], token: str) -> dict[str, Any]:
    snapshot = read_stage0_cache()
    cached_universe = snapshot.get("universe") or [] if isinstance(snapshot, dict) else []
    selected_instruments = _selected_instruments(symbols, cached_universe)
    missing_selected_bse = bool(symbols) and all(
        str(symbol).strip().upper().endswith(".BO") for symbol in symbols
    ) and not selected_instruments
    regime = _nifty_market_regime(snapshot)
    missing_nifty_dma = (
        regime["nifty_price"] is None or regime["nifty_dma_200"] is None
    )
    if not stage0_cache_is_fresh(snapshot) or missing_selected_bse or missing_nifty_dma:
        refresh_future = request_stage0_refresh(force=True)
        if refresh_future is not None:
            refresh_future.result()
        snapshot = read_stage0_cache()
    if not stage0_cache_is_fresh(snapshot):
        raise RuntimeError("Stage 0 instrument and macro data are unavailable.")
    instruments = _selected_instruments(symbols, snapshot.get("universe") or [])
    if symbols and not instruments:
        if all(str(symbol).strip().upper().endswith(".BO") for symbol in symbols):
            raise RuntimeError(
                "The refreshed Stage 0 master still has no matching BSE listing. "
                "Check whether the BSE listing feed and Upstox instrument service "
                "are available, then retry."
            )
        raise RuntimeError(
            "The selected stocks are not available in the Stage 0 instrument "
            "master. Refresh Stage 0 and try again."
        )
    available_listings = {
        (
            _symbol_base(
                str(
                    instrument.get("symbol")
                    or instrument.get("trading_symbol")
                    or ""
                ).strip().upper()
            ),
            "BSE" if instrument.get("segment") == "BSE_EQ" else "NSE",
        )
        for instrument in snapshot.get("universe") or []
        if isinstance(instrument, dict)
    }
    missing_symbols = [
        symbol
        for symbol in symbols
        if (
            _symbol_base(symbol),
            "BSE" if symbol.strip().upper().endswith(".BO") else "NSE",
        )
        not in available_listings
        and not (
            not symbol.strip().upper().endswith(".BO")
            and (_symbol_base(symbol), "BSE") in available_listings
        )
    ]
    keys = [
        str(instrument.get("instrument_key") or instrument.get("key") or "")
        .strip()
        .upper()
        .replace(":", "|")
        for instrument in instruments
    ]
    history_result = None
    if keys and token:
        history_result = sync_daily_history_sync(
            keys,
            token,
            db_path=STAGE0_DB_PATH,
        )
    return {
        "snapshot": snapshot,
        "instruments": instruments,
        "history_result": history_result,
        "missing_symbols": missing_symbols,
    }


def _key_signature(symbols: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(symbols)).encode("utf-8")).hexdigest()[:16]


def _new_pipeline_state(signature: str) -> dict[str, Any]:
    return {
        "signature": signature,
        "statuses": ["idle"] * 5,
        "messages": [""] * 5,
        "stage0_future": None,
        "stage0_result": None,
        "bse_catalog_future": None,
        "bse_catalog_refresh_failed": False,
        "stage1_result": None,
        "stage1_attempted": False,
        "stage2_attempted": False,
        "stage2_decisions": {},
        "stage2_rows": [],
        "stage2_all_rows": [],
        "stage2_fundamentals": {},
        "stage3_results": {},
        "stage3_errors": {},
        "stage3_future": None,
        "stage3_attempted": False,
        "stage3_progress": {"completed": 0, "total": 0, "current": ""},
        "stage3_progress_events": None,
        "stage4_results": {},
        "stage4_errors": {},
        "stage4_future": None,
        "stage4_progress_events": None,
        "stage4_progress": {"completed": 0, "total": 0, "current": ""},
        "selected_symbol": None,
        "gatekeeper_config": {},
    }


def _set_stage(
    state: dict[str, Any], index: int, status: str, message: str = ""
) -> None:
    state["statuses"][index] = status
    state["messages"][index] = message


def _consume_stage0_future(state: dict[str, Any]) -> None:
    future = state.get("stage0_future")
    if not isinstance(future, Future) or not future.done():
        return
    try:
        result = future.result()
    except (HistorySyncError, OSError, RuntimeError, sqlite3.Error, TimeoutError, ValueError) as error:
        _set_stage(state, 0, "failure", _friendly_error(0, error))
    else:
        state["stage0_result"] = result
        missing_symbols = result.get("missing_symbols") or []
        if missing_symbols:
            missing_bse = sum(
                str(symbol).strip().upper().endswith(".BO")
                for symbol in missing_symbols
            )
            detail = (
                f"BSE data not loaded for {missing_bse} selected listing(s). "
                if missing_bse
                else f"Stage 0 data is missing for {len(missing_symbols)} selected stock(s). "
            )
            _set_stage(state, 0, "refresh_required", detail)
        else:
            _set_stage(state, 0, "success", "Stage 0 universe is ready.")
        history = result.get("history_result")
        if history is not None:
            history_message = (
                f"Daily history synced for {history['synced']} listing(s); "
                f"{history['skipped']} fresh listing(s) skipped."
            )
            status = state["statuses"][0]
            message = state["messages"][0]
            _set_stage(
                state,
                0,
                status,
                f"{message} {history_message}".strip(),
            )
        elif result.get("instruments"):
            status = state["statuses"][0]
            message = state["messages"][0]
            if status == "success":
                message = "Universe ready. Connect Upstox to sync local daily candles."
            else:
                message = (
                    f"{message} Connect Upstox to sync local daily candles."
                )
            _set_stage(state, 0, status, message)
    state["stage0_future"] = None


def _format_feature_value(value: Any) -> str:
    if value is None:
        return "Unavailable"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.2f}" if pd.notna(number) else "Unavailable"


_TECHNICAL_INDICATORS = (
    ("sma_20", "SMA 20", "Current price above average is a positive short-term trend."),
    ("sma_50", "SMA 50", "Current price above average is a positive medium-term trend."),
    ("sma_200", "SMA 200", "Current price above average is a positive long-term trend."),
    ("ema_21", "EMA 21", "Current price above average is a positive recent trend."),
    ("macd_line", "MACD line", "MACD above its signal line indicates positive momentum."),
    ("macd_signal", "MACD signal", "A positive signal-line value supports upward momentum."),
    ("macd_hist", "MACD histogram", "A positive histogram means MACD is above its signal."),
    ("rsi_14", "RSI 14", "50–70 is treated as constructive; above 70 or below 40 as weak/extended."),
    ("bb_upper", "Bollinger upper", "Upper 20-day band; context only, not a directional pass."),
    ("bb_lower", "Bollinger lower", "Price above the lower band indicates it is not below the lower range."),
    ("bb_width_pct", "Bollinger width %", "Band width describes volatility; it is not directional."),
    ("atr_14", "ATR 14", "Average daily price range; risk context, not directional."),
    ("atr_percentile", "ATR percentile", "Relative volatility rank; risk context, not directional."),
    ("squeeze_alert", "Bollinger/Keltner squeeze", "A squeeze flags volatility compression, not direction."),
    ("adx_14", "ADX 14", "ADX at or above the configured trend threshold indicates trend strength."),
    ("choppiness_14", "Choppiness 14", "A lower value indicates a more directional rather than choppy regime."),
    ("mfi_14", "MFI 14", "MFI at or above 50 indicates positive volume-weighted momentum."),
    ("vpt", "Volume-price trend", "Positive cumulative volume-price trend is treated as supportive."),
    ("rvol_20", "Relative volume 20", "At or above 1.0 means volume is at/above its 20-day average."),
    ("stoch_k", "Stochastic %K", "20–80 is treated as a constructive, non-extreme range."),
    ("roc_10", "Rate of change 10", "Positive 10-session rate of change indicates upward momentum."),
    ("dist_52w_high_pct", "Distance from 52-week high %", "Within 10% of the 52-week high indicates relative strength."),
)


def _technical_indicator_checks(
    features: dict[str, Any],
    gatekeeper_config: GatekeeperConfig | None = None,
) -> list[tuple[str, str, str]]:
    config = gatekeeper_config or GatekeeperConfig()
    price = features.get("last_price")
    macd_line = features.get("macd_line")
    macd_signal = features.get("macd_signal")
    checks = []
    for key, label, _description in _TECHNICAL_INDICATORS:
        value = features.get(key)
        if value is None:
            status = "Unavailable"
        elif key in {"sma_20", "sma_50", "sma_200", "ema_21"}:
            status = "Positive" if price is not None and price > value else "Negative"
        elif key == "macd_line":
            status = "Neutral" if macd_signal is None else (
                "Positive" if macd_line > macd_signal else "Negative"
            )
        elif key in {"macd_signal", "macd_hist"}:
            status = "Positive" if value > 0 else "Negative"
        elif key == "rsi_14":
            status = (
                "Positive"
                if 50 <= value <= 70
                else "Negative"
                if value > 70 or value < 40
                else "Neutral"
            )
        elif key == "bb_lower":
            status = "Positive" if price is not None and price >= value else "Negative"
        elif key in {"bb_upper", "bb_width_pct", "atr_14", "atr_percentile", "squeeze_alert"}:
            status = "Neutral"
        elif key == "adx_14":
            status = "Positive" if value >= config.min_adx_trend else "Negative"
        elif key == "choppiness_14":
            status = (
                "Positive"
                if value <= config.max_choppiness_index
                else "Negative"
            )
        elif key == "mfi_14":
            status = "Positive" if value >= 50 else "Negative"
        elif key in {"vpt", "roc_10"}:
            status = "Positive" if value > 0 else "Negative"
        elif key == "rvol_20":
            status = "Positive" if value >= 1 else "Negative"
        elif key == "stoch_k":
            status = "Positive" if 20 <= value <= 80 else "Neutral"
        elif key == "dist_52w_high_pct":
            status = "Positive" if value >= -10 else "Negative"
        else:
            status = "Neutral"
        checks.append((label, _format_feature_value(value), status))
    return checks


def _technical_indicator_table(
    features: dict[str, Any],
    gatekeeper_config: GatekeeperConfig | None = None,
) -> list[dict[str, str]]:
    checks = _technical_indicator_checks(features, gatekeeper_config)
    return [
        {
            "Indicator": label,
            "Value": value,
            "Read": status,
            "What it means": description,
        }
        for (key, label, description), (_, value, status) in zip(
            _TECHNICAL_INDICATORS, checks
        )
    ]


def _stage2_table_rows(
    decisions: list[GatekeeperDecision],
    instruments: list[dict[str, Any]],
    quotes: dict[str, Any],
    gatekeeper_config: GatekeeperConfig | None = None,
    *,
    include_rejected: bool = False,
) -> list[dict[str, Any]]:
    by_key = {
        str(instrument.get("instrument_key") or instrument.get("key") or "")
        .strip()
        .upper()
        .replace(":", "|"): instrument
        for instrument in instruments
    }
    rows = []
    for decision in decisions:
        if not include_rejected and not decision.passed:
            continue
        instrument = by_key.get(decision.instrument_key, {})
        symbol = str(
            instrument.get("symbol")
            or instrument.get("trading_symbol")
            or decision.symbol
        ).strip().upper()
        if instrument.get("segment") == "BSE_EQ" and not symbol.endswith(".BO"):
            symbol += ".BO"
        features = decision.features or {}
        classification = decision.investment_classification or {}
        quote = quotes.get(decision.instrument_key) or {}
        previous_close = quote.get("close")
        current_price = features.get("last_price") or quote.get("last_price")
        daily_change = None
        try:
            if previous_close and current_price:
                daily_change = (float(current_price) / float(previous_close) - 1) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            daily_change = None
        indicator_values = {
            "Price": current_price,
            "Daily_change_%": daily_change,
            "SMA_20": features.get("sma_20"),
            "SMA_50": features.get("sma_50"),
            "SMA_200": features.get("sma_200"),
            "EMA_21": features.get("ema_21"),
            "MACD": features.get("macd_line"),
            "MACD_signal": features.get("macd_signal"),
            "MACD_histogram": features.get("macd_hist"),
            "RSI_14": features.get("rsi_14"),
            "ATR_14": features.get("atr_14"),
            "ATR_%": features.get("atr_percentile"),
            "ADX_14": features.get("adx_14"),
            "CCI_20": None,
            "MFI_14": features.get("mfi_14"),
            "Bollinger_lower": features.get("bb_lower"),
            "Bollinger_upper": features.get("bb_upper"),
            "Bollinger_width_%": features.get("bb_width_pct"),
            "Stochastic_%K": features.get("stoch_k"),
            "Volatility_20d_annualized_%": None,
            "Return_1m_%": None,
            "Return_3m_%": None,
            "Average_volume_20d": None,
            "Volume_vs_average": features.get("rvol_20"),
            "52w_high": None,
            "52w_low": None,
            "52w_range_position_%": None,
            "As_of": quote.get("timestamp"),
        }
        checks = _technical_indicator_checks(features, gatekeeper_config)
        recommendation = _STAGE2_SIGNAL.get(decision.signal_tier, "NOT RATED")
        rows.append(
            {
                "Ticker": symbol,
                "Instrument_key": decision.instrument_key,
                **indicator_values,
                "Recommendation": recommendation,
                "Investment_tag": classification.get("investment_tag", "REJECT"),
                "Pass_short": bool(classification.get("pass_short")),
                "Pass_long": bool(classification.get("pass_long")),
                "Investment_partial_data_warning": bool(
                    classification.get("partial_data_warning")
                ),
                "Market_regime_warning": bool(
                    classification.get("market_regime_warning")
                ),
                "Signal_score": decision.score,
                "Positive_signals": sum(
                    item[2] == "Positive" for item in checks
                ),
                "Total_signals": len(checks),
                "Indicator_checks": checks,
                "Indicator_signals": "",
                "Reason": (
                    "Jev Gatekeeper passed."
                    if decision.passed
                    else "; ".join(decision.reject_reasons)
                    or "Stage 2 data is unavailable."
                ),
                "Passes_filters": decision.passed,
            }
        )
    return rows


def _visible_stage2_rows(
    state: dict[str, Any], selected_universe: str
) -> list[dict[str, Any]]:
    source_key = (
        "stage2_all_rows"
        if selected_universe == "Pick individual stocks"
        else "stage2_rows"
    )
    rows = state.get(source_key)
    if not isinstance(rows, list) and source_key == "stage2_all_rows":
        rows = state.get("stage2_rows")
    if not isinstance(rows, list):
        return []
    if selected_universe == "Pick individual stocks":
        return [row for row in rows if isinstance(row, dict)]
    return [
        row
        for row in rows
        if isinstance(row, dict) and row.get("Investment_tag") != "REJECT"
    ]


def _detail_scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return f"{value:,.4g}"
    if isinstance(value, str):
        clean = value.strip()
        return clean[:600] if clean else None
    return None


def _flatten_detail_values(
    data: dict[str, Any],
    *,
    limit: int = 30,
) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []

    def visit(value: Any, prefix: str, depth: int) -> None:
        if len(rows) >= limit or value is None:
            return
        if isinstance(value, dict) and depth < 2:
            for name, nested in value.items():
                visit(nested, f"{prefix} · {name}" if prefix else str(name), depth + 1)
                if len(rows) >= limit:
                    return
        elif isinstance(value, list):
            return
        else:
            rendered = _detail_scalar(value)
            if rendered is not None:
                rows.append((prefix.replace("_", " ").strip().title(), rendered))

    for key, value in data.items():
        visit(value, str(key), 0)
        if len(rows) >= limit:
            break
    return rows


def _pipeline_details_for_rows(
    rows: list[dict[str, Any]],
    state: dict[str, Any],
    instruments: list[dict[str, Any]],
    macro: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    instruments_by_key = {
        str(instrument.get("instrument_key") or instrument.get("key") or "")
        .strip()
        .upper()
        .replace(":", "|"): instrument
        for instrument in instruments
    }
    stage1 = state.get("stage1_result")
    quotes = stage1.get("quotes", {}) if isinstance(stage1, dict) else {}
    quotes = quotes if isinstance(quotes, dict) else {}
    fundamentals_by_symbol = state.get("stage2_fundamentals", {})
    decisions = state.get("stage2_decisions", {})
    enrichments = state.get("stage3_results", {})
    theses = state.get("stage4_results", {})
    gatekeeper_config = GatekeeperConfig(**state.get("gatekeeper_config", {}))
    result_rows = []

    for original in rows:
        row = dict(original)
        key = str(row.get("Instrument_key") or "").upper().replace(":", "|")
        instrument = instruments_by_key.get(key, {})
        decision = decisions.get(key) if isinstance(decisions, dict) else None
        quote = quotes.get(key, {})
        quote = quote if isinstance(quote, dict) else {}
        feature_data = (decision.features or {}) if decision else {}
        symbol = str(row.get("Ticker") or "")
        fundamentals = (
            fundamentals_by_symbol.get(_symbol_base(symbol), {})
            if isinstance(fundamentals_by_symbol, dict)
            else {}
        )
        fundamentals = fundamentals if isinstance(fundamentals, dict) else {}
        enrichment = enrichments.get(key, {}) if isinstance(enrichments, dict) else {}
        enrichment = enrichment if isinstance(enrichment, dict) else {}
        derivatives = enrichment.get("derivatives")
        derivatives = derivatives if isinstance(derivatives, dict) else {}
        news = enrichment.get("news_and_consensus")
        news = news if isinstance(news, dict) else {}
        thesis_result = theses.get(key, {}) if isinstance(theses, dict) else {}
        thesis = (
            thesis_result.get("thesis")
            if isinstance(thesis_result, dict)
            else None
        )
        confidence = (
            thesis.get("confidence_score")
            if isinstance(thesis, dict)
            else None
        )
        try:
            row["AI_confidence"] = (
                round(float(confidence) * 100, 1)
                if confidence is not None
                else None
            )
        except (TypeError, ValueError, OverflowError):
            row["AI_confidence"] = None
        indicators: dict[str, list[str]] = {}
        groups: list[list[Any]] = []
        sections: list[dict[str, Any]] = []
        context_info: dict[str, str] = {}
        group_index = 0

        def add_group(
            title: str,
            values: list[tuple[str, Any, str, str | None]],
        ) -> None:
            nonlocal group_index
            items = []
            for value_index, (label, value, tone, explanation) in enumerate(values):
                rendered = _detail_scalar(value)
                if rendered is None:
                    continue
                field_key = f"pipeline_{group_index}_{value_index}"
                indicators[field_key] = [rendered, tone]
                items.append([field_key, label])
                if explanation:
                    context_info[field_key] = explanation
            if items:
                groups.append([title, items])
                group_index += 1

        add_group(
            "Company and listing",
            [
                ("Company", instrument.get("name") or instrument.get("company_name") or symbol, "neu", None),
                ("Ticker", symbol, "neu", None),
                ("Exchange", "BSE" if instrument.get("segment") == "BSE_EQ" else "NSE", "neu", None),
                ("ISIN", instrument.get("isin"), "neu", None),
                ("Instrument key", key, "neu", None),
            ],
        )
        quote_labels = (
            ("last_price", "Last traded price"),
            ("open", "Open"),
            ("high", "Day high"),
            ("low", "Day low"),
            ("close", "Previous close"),
            ("volume", "Traded volume"),
            ("vwap", "VWAP"),
            ("bid_ask_spread_pct", "Bid/ask spread %"),
            ("is_circuit_locked", "Circuit locked"),
            ("lower_circuit_limit", "Lower circuit limit"),
            ("upper_circuit_limit", "Upper circuit limit"),
            ("timestamp", "Quote time"),
        )
        quote_values = [
            (label, quote.get(field), "neu", None)
            for field, label in quote_labels
        ]
        if not any(value is not None for _label, value, _tone, _help in quote_values):
            quote_values = [("Status", "Quote not available", "missing", None)]
        add_group("Live quote", quote_values)

        feature_checks = _technical_indicator_checks(
            feature_data, gatekeeper_config
        )
        feature_by_key = {
            key: feature_data.get(key)
            for key, _label, _description in _TECHNICAL_INDICATORS
        }
        feature_status = {
            _TECHNICAL_INDICATORS[index][0]: check[2]
            for index, check in enumerate(feature_checks)
        }
        indicator_items = []
        for field, label, explanation in _TECHNICAL_INDICATORS:
            value = feature_by_key[field]
            if value is None:
                value = "Unavailable"
                tone = "missing"
            else:
                tone = {
                    "Positive": "pos",
                    "Negative": "neg",
                    "Neutral": "neu",
                }.get(feature_status[field], "neu")
            indicator_items.append((label, value, tone, explanation))
        add_group("Technical indicators", indicator_items)

        rule_labels = (
            ("enable_min_last_price", "Minimum price", "min_last_price", "₹"),
            ("enable_max_spread_pct", "Maximum bid/ask spread", "max_spread_pct", "%"),
            ("enable_min_daily_volume", "Minimum daily volume", "min_daily_volume", " shares"),
            ("enable_min_adx_trend", "Minimum ADX", "min_adx_trend", ""),
            ("enable_max_choppiness_index", "Maximum choppiness", "max_choppiness_index", ""),
            ("enable_min_piotroski_score", "Minimum Piotroski score", "min_piotroski_score", ""),
            ("enable_min_altman_z_score", "Minimum Altman Z-score", "min_altman_z_score", ""),
            ("enable_max_promoter_pledge_pct", "Maximum promoter pledge", "max_promoter_pledge_pct", "%"),
        )
        jev_values = [
            (
                "Decision",
                f"{decision.signal_tier} · {decision.score:.1f}/100"
                if decision
                else "Not available",
                "pos" if decision and decision.passed else "neg" if decision else "neu",
                None,
            )
        ]
        for enabled_field, label, value_field, suffix in rule_labels:
            enabled = getattr(gatekeeper_config, enabled_field)
            threshold = getattr(gatekeeper_config, value_field)
            jev_values.append(
                (
                    label,
                    f"{'On' if enabled else 'Off'} · {threshold}{suffix}",
                    "neu",
                    "Configured deterministic gatekeeper threshold.",
                )
            )
        jev_values.append(
            (
                "Rejection reasons",
                "; ".join(decision.reject_reasons)
                if decision and decision.reject_reasons
                else "None",
                "neg" if decision and decision.reject_reasons else "pos",
                None,
            )
        )
        add_group("Jev screening", jev_values)

        classification = (
            decision.investment_classification
            if decision and isinstance(decision.investment_classification, dict)
            else {}
        )
        investment_values = [
            ("Automatic investment tag", classification.get("investment_tag", "REJECT"), "neu", None),
            ("Short-term gate (3–12 months)", classification.get("pass_short"), "neu", None),
            ("Long-term gate (3+ years)", classification.get("pass_long"), "neu", None),
            (
                "Broad market regime",
                "Bear Market Regime: Stagger Entries"
                if classification.get("market_regime_warning")
                else (
                    "Above Nifty 50 200-DMA"
                    if classification.get("nifty_price") is not None
                    and classification.get("nifty_dma_200") is not None
                    else "Unavailable"
                ),
                "missing"
                if classification.get("market_regime_warning")
                else "pos",
                "Nifty 50 price must be above its 200-day moving average for "
                "short-term investment classification.",
            ),
            ("Lender rules", classification.get("is_lender"), "neu", None),
            ("Partial data warning", classification.get("partial_data_warning"), "missing" if classification.get("partial_data_warning") else "pos", None),
            (
                "Missing expected metrics",
                ", ".join(classification.get("missing_metrics", [])) or "None",
                "missing" if classification.get("missing_metrics") else "pos",
                None,
            ),
            (
                "Short-term failed rules",
                ", ".join(classification.get("failed_short", [])) or "None",
                "neg" if classification.get("failed_short") else "pos",
                None,
            ),
            (
                "Long-term failed rules",
                ", ".join(classification.get("failed_long", [])) or "None",
                "neg" if classification.get("failed_long") else "pos",
                None,
            ),
        ]
        add_group("Investment classification", investment_values)

        fundamental_values = _flatten_detail_values(fundamentals)
        if fundamental_values:
            add_group(
                "Fundamentals",
                [
                    (label, value, "neu", None)
                    for label, value in fundamental_values
                ],
            )
        else:
            add_group(
                "Fundamentals",
                [("Status", "No cached fundamentals available", "missing", None)],
            )

        option_values: list[tuple[str, Any, str, str | None]] = [
            ("Options status", derivatives.get("status", "Not loaded"), "neu", None),
            ("F&O eligible", derivatives.get("is_fno_eligible"), "neu", None),
            ("Expiry", derivatives.get("expiry"), "neu", None),
            ("Open-interest PCR", derivatives.get("pcr_oi"), "neu", None),
            ("Volume PCR", derivatives.get("pcr_volume"), "neu", None),
            ("Max pain strike", derivatives.get("max_pain_strike"), "neu", None),
            ("Total call open interest", derivatives.get("total_call_oi"), "neu", None),
            ("Total put open interest", derivatives.get("total_put_oi"), "neu", None),
        ]
        greeks = derivatives.get("atm_greeks")
        if isinstance(greeks, dict):
            option_values.extend(
                (f"ATM {name.replace('_', ' ').title()}", value, "neu", None)
                for name, value in greeks.items()
            )
        add_group("Derivatives and options", option_values)

        analyst_values = [
            ("News/analyst status", news.get("status", "Not loaded"), "neu", None),
            ("Sector", news.get("sector"), "neu", None),
            ("Industry", news.get("industry"), "neu", None),
            ("Recommendation", news.get("recommendation_key"), "neu", None),
            ("Analyst count", news.get("analyst_count"), "neu", None),
            ("Average analyst target", news.get("target_mean_price"), "neu", None),
            ("Low analyst target", news.get("target_low_price"), "neu", None),
            ("High analyst target", news.get("target_high_price"), "neu", None),
            ("Business overview", news.get("business_summary"), "neu", None),
        ]
        add_group("Analyst outlook", analyst_values)
        headlines = news.get("recent_headlines")
        if isinstance(headlines, list) and headlines:
            sections.append(
                {
                    "title": "Recent news",
                    "items": [
                        {
                            "title": str(item.get("title") or "")[:500],
                            "publisher": item.get("publisher") or "Unknown publisher",
                            "published": item.get("published_at") or "Date unavailable",
                            "sentimentLabel": "Headline only; no sentiment assessment",
                            "sentiment": 0,
                        }
                        for item in headlines
                        if isinstance(item, dict) and item.get("title")
                    ],
                }
            )
        else:
            add_group(
                "Recent news",
                [("Status", "No recent headlines available", "missing", None)],
            )

        macro_values = _flatten_detail_values(macro or {}, limit=20)
        add_group(
            "Market context",
            (
                [(label, value, "neu", None) for label, value in macro_values]
                or [("Status", "Macro context not available", "missing", None)]
            ),
        )
        if isinstance(thesis, dict):
            thesis_values = [
                ("AI stance", thesis.get("stance"), "neu", None),
                ("Confidence", thesis.get("confidence_score"), "neu", None),
                ("Time horizon", thesis.get("time_horizon"), "neu", None),
                ("Thesis", thesis.get("executive_thesis"), "neu", None),
            ]
            thesis_warnings = thesis.get("data_gap_warnings")
            if isinstance(thesis_warnings, list) and thesis_warnings:
                thesis_values.append(
                    (
                        "Data caveats",
                        "; ".join(str(item) for item in thesis_warnings),
                        "neu",
                        None,
                    )
                )
            add_group("AI thesis", thesis_values)
        else:
            add_group(
                "AI thesis",
                [("Status", "Run AI thesis to generate a governed view", "neu", None)],
            )

        row["Pipeline_details"] = {
            "indicators": indicators,
            "groups": groups,
            "sections": sections,
            "context_info": context_info,
            "group_ratings": {
                "Technical indicators": {
                    "positive": sum(
                        status == "Positive"
                        for _label, _value, status in feature_checks
                    ),
                    "rated": sum(
                        status != "Unavailable"
                        for _label, _value, status in feature_checks
                    ),
                }
            },
            "replace_groups": True,
        }
        result_rows.append(row)
    return result_rows


def _handle_stage0(state: dict[str, Any], symbols: list[str]) -> None:
    if state.get("stage0_future") is not None:
        return
    _set_stage(state, 0, "running", "Preparing the local universe and daily candles.")
    state["stage0_future"] = _PIPELINE_EXECUTOR.submit(
        _stage0_prepare, symbols, _token()
    )


def _handle_stage1(
    state: dict[str, Any], instruments: list[dict[str, Any]], session_state: Any
) -> None:
    keys = [
        str(instrument.get("instrument_key") or instrument.get("key") or "")
        .strip()
        .upper()
        .replace(":", "|")
        for instrument in instruments
    ]
    if not keys:
        _set_stage(state, 1, "refresh_required", "Load a Stage 0 equity universe first.")
        return
    token = _token()
    if not token:
        _set_stage(state, 1, "failure", "Connect Upstox or configure UPSTOX_ANALYTICS_TOKEN.")
        return
    _set_stage(state, 1, "running", "Loading live quotes from Upstox.")
    try:
        with st.status(
            f"Loading live quotes for {len(keys)} selected stocks…",
            expanded=True,
        ) as progress:
            progress.write("Requesting batched quotes from Upstox.")
            result = get_stage1_pulse_cached(
                instrument_keys=keys,
                access_token=token,
                session_state=session_state,
            )
            progress.update(
                label="Live quote request completed.",
                state="complete",
                expanded=False,
            )
    except (OSError, RuntimeError, sqlite3.Error, TimeoutError, ValueError) as error:
        _set_stage(state, 1, "failure", _friendly_error(1, error))
        return
    state["stage1_result"] = result
    quotes = result.get("quotes") if isinstance(result, dict) else {}
    count = len(quotes) if isinstance(quotes, dict) else 0
    if count == len(keys):
        if result.get("source") == "STALE_CACHE":
            _set_stage(
                state,
                1,
                "refresh_required",
                f"Showing {count} stale cached quote(s); refresh when the market is open.",
            )
        else:
            _set_stage(state, 1, "success", f"Live quote available for {count} listing(s).")
    elif count:
        missing_keys = set(keys) - set(quotes)
        missing_bse = sum(key.startswith("BSE_EQ|") for key in missing_keys)
        missing_nse = len(missing_keys) - missing_bse
        missing_message = []
        if missing_bse:
            missing_message.append(f"BSE data not loaded for {missing_bse} listing(s)")
        if missing_nse:
            missing_message.append(f"NSE data not loaded for {missing_nse} listing(s)")
        _set_stage(
            state,
            1,
            "refresh_required",
            f"Live quotes loaded for {count}/{len(keys)} listings. "
            + "; ".join(missing_message)
            + ".",
        )
    else:
        bse_count = sum(key.startswith("BSE_EQ|") for key in keys)
        bse_message = (
            f" BSE data was not loaded for {bse_count} selected listing(s)."
            if bse_count
            else ""
        )
        _set_stage(
            state,
            1,
            "failure",
            "No live quotes were loaded from Upstox. Check the connection and "
            f"confirm the selected listings are supported.{bse_message}",
        )


def _handle_stage2(
    state: dict[str, Any],
    instruments: list[dict[str, Any]],
    config: GatekeeperConfig | None = None,
    market_regime: dict[str, float | None] | None = None,
) -> None:
    keys = [
        str(instrument.get("instrument_key") or instrument.get("key") or "")
        .strip()
        .upper()
        .replace(":", "|")
        for instrument in instruments
    ]
    stage1 = state.get("stage1_result")
    quotes = (
        stage1.get("quotes")
        if isinstance(stage1, dict) and isinstance(stage1.get("quotes"), dict)
        else {}
    )
    if not keys:
        state["stage2_rows"] = []
        state["stage2_all_rows"] = []
        _set_stage(state, 2, "refresh_required", "Load Stage 0 before screening.")
        return
    try:
        _set_stage(
            state,
            2,
            "running",
            f"Calculating local technical features for {len(keys)} stocks.",
        )
        with st.status(
            f"Screening {len(keys)} stocks using local history…",
            expanded=True,
        ) as progress:
            progress.write("Loading cached OHLCV and fundamentals from SQLite.")
            histories = load_cached_history_frames(
                STAGE0_DB_PATH,
                keys,
                limit_bars=260,
            )
            fundamentals = get_fundamental_snapshots(
                [
                    str(
                        instrument.get("symbol")
                        or instrument.get("trading_symbol")
                        or ""
                    ).strip().upper()
                    for instrument in instruments
                ]
            )
            state["stage2_fundamentals"] = fundamentals
            progress.write("Calculating indicators and applying Jev gatekeeper rules.")
            result = run_stage2_batch(
                instruments,
                quotes,
                histories,
                fundamentals_by_symbol=fundamentals,
                max_instruments=len(keys),
                min_avg_turnover_inr=0,
                config=config or GatekeeperConfig(),
                session_state=st.session_state,
                market_regime=market_regime,
            )
            progress.update(
                label="Technical screening completed.",
                state="complete",
                expanded=False,
            )
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
        _set_stage(state, 2, "failure", _friendly_error(2, error))
        return
    decisions = result.decisions
    state["stage2_decisions"] = {
        decision.instrument_key: decision for decision in decisions
    }
    passed_decisions = [
        decision for decision in decisions if decision.passed and decision.features
    ]
    state["stage2_rows"] = _stage2_table_rows(
        passed_decisions,
        instruments,
        quotes,
        config,
    )
    state["stage2_all_rows"] = _stage2_table_rows(
        decisions,
        instruments,
        quotes,
        config,
        include_rejected=True,
    )
    feature_count = sum(bool(decision.features) for decision in decisions)
    passed_count = len(passed_decisions)
    quote_count = sum(decision.instrument_key in quotes for decision in decisions)
    stale_quotes = isinstance(stage1, dict) and stage1.get("source") == "STALE_CACHE"
    if (
        feature_count == len(decisions)
        and quote_count == len(decisions)
        and not stale_quotes
    ):
        _set_stage(
            state,
            2,
            "success",
            f"Technical screen complete: {passed_count} of {len(decisions)} "
            "stocks passed Jev rules.",
        )
    else:
        _set_stage(
            state,
            2,
            "refresh_required",
            _stage2_coverage_message(
                feature_count,
                quote_count,
                len(decisions),
                stale_quotes=stale_quotes,
            ),
        )


def _stage2_coverage_message(
    feature_count: int,
    quote_count: int,
    total: int,
    *,
    stale_quotes: bool = False,
) -> str:
    missing_history = max(0, total - feature_count)
    missing_quote = max(0, total - quote_count)
    explanations = []
    if missing_history:
        explanations.append(
            f"{missing_history} stock(s) lack sufficient local daily candle "
            "history to calculate indicators; sync history."
        )
    if missing_quote:
        explanations.append(
            f"{missing_quote} stock(s) have no live quote; refresh quotes."
        )
    if stale_quotes:
        explanations.append(
            "The available quotes are stale; refresh quotes when the market is open."
        )
    return (
        f"Features: {feature_count}/{total}; quotes: {quote_count}/{total}. "
        + " ".join(explanations)
    )


def _on_table_select(
    table_key: str,
    state: dict[str, Any],
) -> None:
    component_state = st.session_state.get(table_key)
    event = (
        component_state.get("select_stock")
        if isinstance(component_state, dict)
        else None
    )
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except json.JSONDecodeError:
            return
    if isinstance(event, dict):
        ticker = str(event.get("ticker") or "").strip().upper()
        if ticker:
            if ticker != state.get("selected_symbol"):
                _set_stage(state, 4, "idle")
            state["selected_symbol"] = ticker


def _selected_instrument(
    state: dict[str, Any], instruments: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    symbol = state.get("selected_symbol")
    if not isinstance(symbol, str):
        return None, None
    for instrument in instruments:
        candidate = str(
            instrument.get("symbol") or instrument.get("trading_symbol") or ""
        ).strip().upper()
        if instrument.get("segment") == "BSE_EQ" and not candidate.endswith(".BO"):
            candidate += ".BO"
        if candidate == symbol:
            key = str(
                instrument.get("instrument_key") or instrument.get("key") or ""
            ).strip().upper().replace(":", "|")
            return instrument, key
    return None, None


def _stage3_batch_worker(
    passed_decisions: list[GatekeeperDecision],
    instruments: list[dict[str, Any]],
    quotes: dict[str, Any],
    token: str,
    progress_events: Queue[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    by_key = {
        str(instrument.get("instrument_key") or instrument.get("key") or "")
        .strip()
        .upper()
        .replace(":", "|"): instrument
        for instrument in instruments
    }
    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    ordered = sorted(
        (
            decision
            for decision in passed_decisions
            if decision.passed and decision.features
        ),
        key=lambda decision: (-decision.score, decision.instrument_key),
    )
    progress_events.put(
        {"completed": 0, "enriched": 0, "total": len(ordered), "current": ""}
    )
    def enrich(decision: GatekeeperDecision) -> tuple[str, str, dict[str, Any]]:
        key = decision.instrument_key
        instrument = by_key.get(key)
        if instrument is None:
            raise ValueError("Listing is no longer available in the selected universe.")
        symbol = str(
            instrument.get("symbol")
            or instrument.get("trading_symbol")
            or decision.symbol
        ).strip().upper()
        if instrument.get("segment") == "BSE_EQ" and not symbol.endswith(".BO"):
            symbol += ".BO"
        quote = quotes.get(key)
        enrichment = get_stage3_enrichment_cached(
            db_path=STAGE0_DB_PATH,
            instrument_key=key,
            symbol=symbol,
            access_token=token,
            exchange="BSE" if instrument.get("segment") == "BSE_EQ" else "NSE",
            spot_price=(
                quote.get("last_price")
                if isinstance(quote, dict)
                else None
            ),
        )
        return key, symbol, enrichment

    completed = 0
    with ThreadPoolExecutor(
        max_workers=_STAGE3_MAX_CONCURRENCY,
        thread_name_prefix="equity-stage3",
    ) as executor:
        futures = {executor.submit(enrich, decision): decision for decision in ordered}
        progress_events.put(
            {
                "current": f"up to {_STAGE3_MAX_CONCURRENCY} stocks in parallel",
                "in_flight": min(_STAGE3_MAX_CONCURRENCY, len(futures)),
            }
        )
        for future in as_completed(futures):
            decision = futures[future]
            completed += 1
            try:
                key, symbol, enrichment = future.result()
            except (OSError, RuntimeError, sqlite3.Error, TimeoutError, ValueError) as error:
                symbol = decision.symbol
                errors[symbol] = _friendly_error(3, error, symbol)
                progress_events.put(
                    {
                        "completed": completed,
                        "current": symbol,
                        "in_flight": min(
                            _STAGE3_MAX_CONCURRENCY,
                            len(futures) - completed,
                        ),
                    }
                )
                continue
            results[key] = enrichment
            progress_events.put(
                {
                    "completed": completed,
                    "enriched_delta": 1,
                    "current": symbol,
                    "in_flight": min(
                        _STAGE3_MAX_CONCURRENCY,
                        len(futures) - completed,
                    ),
                }
            )
    return results, errors


def _start_stage3_batch(
    state: dict[str, Any],
    instruments: list[dict[str, Any]],
) -> None:
    state["stage3_attempted"] = True
    passed = [
        decision
        for decision in state.get("stage2_decisions", {}).values()
        if decision.passed and decision.features
    ]
    if not passed:
        _set_stage(state, 3, "success", "No stocks passed Stage 2; no enrichment needed.")
        return
    token = _token()
    if not token:
        _set_stage(
            state,
            3,
            "failure",
            "Connect Upstox or configure UPSTOX_ANALYTICS_TOKEN to load stock "
            "enrichment for Stage 2 passes.",
        )
        return
    stage1 = state.get("stage1_result")
    quotes = stage1.get("quotes") if isinstance(stage1, dict) else {}
    progress_events: Queue[dict[str, Any]] = Queue()
    state["stage3_progress_events"] = progress_events
    state["stage3_progress"] = {
        "completed": 0,
        "enriched": 0,
        "total": len(passed),
        "current": "Starting",
    }
    state["stage3_errors"] = {}
    _set_stage(
        state,
        3,
        "running",
        f"Enriching 0 of {len(passed)} Stage 2-passed stocks.",
    )
    state["stage3_future"] = _PIPELINE_EXECUTOR.submit(
        _stage3_batch_worker,
        passed,
        instruments,
        quotes if isinstance(quotes, dict) else {},
        token,
        progress_events,
    )


def _stage3_coverage_message(
    results: dict[str, dict[str, Any]],
) -> tuple[int, str, bool]:
    feed_statuses: dict[str, dict[str, int]] = {
        "derivatives": {},
        "news/consensus": {},
    }
    rate_limited_endpoints: dict[str, dict[str, Any]] = {}
    available_feeds = 0
    actionable_gaps = False
    for enrichment in results.values():
        for label, section_key in (
            ("derivatives", "derivatives"),
            ("news/consensus", "news_and_consensus"),
        ):
            section = enrichment.get(section_key)
            status = (
                str(section.get("status") or "UNAVAILABLE").upper()
                if isinstance(section, dict)
                else "UNAVAILABLE"
            )
            statuses = feed_statuses[label]
            statuses[status] = statuses.get(status, 0) + 1
            available_feeds += status == "OK"
            if label == "derivatives" and status == "RATE_LIMITED":
                endpoint = str(section.get("endpoint") or "unknown options endpoint")
                endpoint_status = rate_limited_endpoints.setdefault(
                    endpoint, {"count": 0, "retry_after_seconds": None}
                )
                endpoint_status["count"] += 1
                retry_after = section.get("retry_after_seconds")
                if isinstance(retry_after, (int, float)) and retry_after > 0:
                    prior = endpoint_status["retry_after_seconds"]
                    endpoint_status["retry_after_seconds"] = max(
                        float(prior or 0), float(retry_after)
                    )

    explanations = {
        "NOT_FNO": "no active listed options; expected for non-F&O stocks",
        "RATE_LIMITED": "the provider rate-limited this request",
        "UNAVAILABLE": "the provider did not return this feed",
        "NOT_LOADED": "this feed has not been loaded",
    }
    gap_details = []
    for label, statuses in feed_statuses.items():
        for status, count in sorted(statuses.items()):
            if status == "OK":
                continue
            if label == "derivatives" and status == "RATE_LIMITED":
                continue
            actionable_gaps = actionable_gaps or status != "NOT_FNO"
            explanation = explanations.get(status, f"provider status {status}")
            gap_details.append(f"{label}: {status} for {count} ({explanation})")
    for endpoint, endpoint_status in sorted(rate_limited_endpoints.items()):
        retry_after = endpoint_status["retry_after_seconds"]
        retry_note = (
            f"; provider requested retry after {int(retry_after)}s"
            if retry_after is not None
            else ""
        )
        gap_details.append(
            f"derivatives: RATE_LIMITED for {endpoint_status['count']} "
            f"(Upstox {endpoint} endpoint{retry_note})"
        )
        actionable_gaps = True

    total_feeds = 2 * len(results)
    summary = (
        f"{available_feeds}/{total_feeds} feeds available "
        f"(two feeds per stock across {len(results)} stock(s))."
    )
    if gap_details:
        summary += " " + "; ".join(gap_details) + "."
    return available_feeds, summary, actionable_gaps


def _consume_stage3_future(state: dict[str, Any]) -> None:
    future = state.get("stage3_future")
    progress = state.get("stage3_progress") or {}
    events = state.get("stage3_progress_events")
    if isinstance(events, Queue):
        while True:
            try:
                event = events.get_nowait()
            except Empty:
                break
            enriched_delta = int(event.pop("enriched_delta", 0))
            progress["enriched"] = int(progress.get("enriched", 0)) + enriched_delta
            progress.update(event)
        state["stage3_progress"] = progress
    if not isinstance(future, Future):
        return
    if not future.done():
        _set_stage(
            state,
            3,
            "running",
            f"Processed {progress.get('completed', 0)} of "
            f"{progress.get('total', 0)} stocks"
            + (
                f" · {progress.get('in_flight', 0)} requests active; "
                f"last completed {progress['current']}."
                if progress.get("current")
                else "."
            ),
        )
        return
    try:
        results, errors = future.result()
    except (OSError, RuntimeError, sqlite3.Error, TimeoutError, ValueError) as error:
        _set_stage(state, 3, "failure", _friendly_error(3, error))
    else:
        state["stage3_results"] = results
        state["stage3_errors"] = errors
        available_feeds, coverage_message, actionable_feed_gaps = (
            _stage3_coverage_message(results)
        )
        has_feed_gaps = available_feeds < 2 * len(results)
        if errors and not results:
            _set_stage(
                state,
                3,
                "failure",
                f"Enrichment failed for all {len(errors)} Stage 2-passed stocks. "
                f"{next(iter(errors.values()))}",
            )
        elif errors:
            _set_stage(
                state,
                3,
                "refresh_required",
                f"Enrichment loaded for {len(results)} of "
                f"{progress.get('total', len(results) + len(errors))} stocks; "
                f"{len(errors)} could not be loaded. {next(iter(errors.values()))}",
            )
        elif has_feed_gaps:
            _set_stage(
                state,
                3,
                "refresh_required" if actionable_feed_gaps else "success",
                f"Enrichment loaded for {len(results)} stocks; {coverage_message}",
            )
        else:
            _set_stage(
                state,
                3,
                "success",
                f"Enrichment loaded for all {len(results)} Stage 2-passed stocks.",
            )
    state["stage3_future"] = None


def _stage4_batch_worker(
    work_items: list[tuple[str, str, Any]],
    api_key: str,
    progress_events: Queue,
    completed_before_start: int,
    total: int,
    initial_errors: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    results: dict[str, dict[str, Any]] = {}
    errors = dict(initial_errors)
    for index, (key, symbol, governed) in enumerate(work_items, start=1):
        progress_events.put(
            {
                "completed": completed_before_start + index - 1,
                "total": total,
                "current": symbol,
            }
        )
        try:
            result = generate_governed_ai_thesis_cached(
                governed,
                api_key=api_key,
                db_path=STAGE0_DB_PATH,
            )
            results[key] = result
            progress_events.put({"result_key": key, "result": result})
        except (
            JevFirewallError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            TimeoutError,
            ValueError,
        ) as error:
            errors[key] = f"{symbol}: {error}"
            progress_events.put({"error_key": key, "error": errors[key]})
        progress_events.put(
            {
                "completed": completed_before_start + index,
                "total": total,
                "current": symbol,
            }
        )
    return results, errors


def _consume_stage4_future(state: dict[str, Any]) -> None:
    future = state.get("stage4_future")
    progress = state.get("stage4_progress") or {}
    events = state.get("stage4_progress_events")
    if isinstance(events, Queue):
        while True:
            try:
                event = events.get_nowait()
            except Empty:
                break
            result_key = event.pop("result_key", None)
            if result_key is not None:
                state["stage4_results"][result_key] = event.pop("result")
            error_key = event.pop("error_key", None)
            if error_key is not None:
                state.setdefault("stage4_errors", {})[error_key] = event.pop("error")
            progress.update(event)
        state["stage4_progress"] = progress
    if not isinstance(future, Future):
        return
    if not future.done():
        _set_stage(
            state,
            4,
            "running",
            f"Generated theses for {progress.get('completed', 0)} of "
            f"{progress.get('total', 0)} screened stocks"
            + (
                f"; currently processing {progress['current']}."
                if progress.get("current")
                else "."
            ),
        )
        return
    try:
        results, errors = future.result()
    except (
        JevFirewallError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        TimeoutError,
        ValueError,
    ) as error:
        _set_stage(state, 4, "failure", _friendly_error(4, error))
    else:
        state["stage4_results"].update(results)
        total = int(progress.get("total", len(results) + len(errors)))
        if errors and not results:
            _set_stage(
                state,
                4,
                "failure",
                f"AI thesis failed for all {total} screened stocks. "
                f"{next(iter(errors.values()))}",
            )
        elif errors:
            _set_stage(
                state,
                4,
                "refresh_required",
                f"Generated {len(results)} of {total} screened-stock theses; "
                f"{len(errors)} failed or lacked required Stage 3 data. "
                f"{next(iter(errors.values()))}",
            )
        else:
            _set_stage(
                state,
                4,
                "success",
                f"AI theses generated for all {total} screened stocks.",
            )
    state["stage4_future"] = None
    state["stage4_progress_events"] = None


def _start_stage4_batch(
    state: dict[str, Any],
    instruments: list[dict[str, Any]],
    stage0_snapshot: dict[str, Any] | None,
    quotes: dict[str, Any],
    stage1_fetched_at: Any,
) -> None:
    if isinstance(state.get("stage4_future"), Future):
        _set_stage(state, 4, "running", "AI thesis batch is already running.")
        return
    screened_candidates = _stage4_screened_candidates(state, instruments, quotes)
    if not screened_candidates:
        _set_stage(state, 4, "refresh_required", "No Stage 2-passed stocks are available.")
        return
    macro = stage0_snapshot.get("macro") if isinstance(stage0_snapshot, dict) else None
    instruments_by_key = {
        str(instrument.get("instrument_key") or instrument.get("key") or "")
        .strip()
        .upper()
        .replace(":", "|"): instrument
        for instrument in instruments
    }
    decisions = state.get("stage2_decisions", {})
    enrichments = state.get("stage3_results", {})
    work_items: list[tuple[str, str, Any]] = []
    errors: dict[str, str] = {}
    symbols_by_key: dict[str, tuple[str, str]] = {}
    for candidate in screened_candidates:
        symbol = candidate["symbol"]
        exchange = candidate["exchange"]
        key = candidate["instrument_key"]
        symbols_by_key[key] = (symbol, exchange)
    prompt_candidates = [
        {name: value for name, value in candidate.items() if name != "instrument_key"}
        for candidate in screened_candidates
    ]
    try:
        fundamentals = get_fundamental_snapshots(
            [symbol for symbol, _exchange in symbols_by_key.values()]
        )
    except (OSError, RuntimeError, sqlite3.Error, TimeoutError, ValueError) as error:
        _set_stage(state, 4, "failure", _friendly_error(4, error))
        return
    for key, (symbol, exchange) in symbols_by_key.items():
        enrichment = enrichments.get(key) if isinstance(enrichments, dict) else None
        if not isinstance(enrichment, dict):
            errors[key] = (
                f"{symbol}: Stage 3 enrichment is unavailable; complete enrichment "
                "before generating this thesis."
            )
            continue
        instrument = instruments_by_key[key]
        decision = decisions.get(key) if isinstance(decisions, dict) else None
        quote = quotes.get(key) if isinstance(quotes, dict) else None
        try:
            governed = build_governed_ai_payload(
                instrument_key=key,
                symbol=_symbol_base(symbol),
                exchange=exchange,
                stage0_macro=macro,
                stage0_macro_fetched_at=(
                    stage0_snapshot.get("macro_updated_at")
                    if isinstance(stage0_snapshot, dict)
                    else None
                ),
                stage1_quote=quote,
                stage1_quote_fetched_at=stage1_fetched_at,
                stage2_decision=decision,
                stage3_enrichment=enrichment,
                fundamentals=fundamentals.get(symbol),
                screened_candidates=prompt_candidates,
                allow_stale_quote=True,
            )
        except (JevFirewallError, OSError, RuntimeError, sqlite3.Error, TimeoutError, ValueError) as error:
            errors[key] = f"{symbol}: {error}"
            continue
        work_items.append((key, symbol, governed))
    if not work_items:
        _set_stage(
            state,
            4,
            "failure",
            f"Couldn't prepare a thesis for any of the {len(screened_candidates)} "
            f"screened stocks. {next(iter(errors.values()), '')}",
        )
        return
    try:
        api_key = _stage4_gemini_api_key()
        if not api_key:
            raise ValueError("A Gemini API key is required.")
    except (OSError, RuntimeError, sqlite3.Error, TimeoutError, ValueError) as error:
        _set_stage(state, 4, "failure", _friendly_error(4, error))
        return
    progress_events: Queue = Queue()
    state["stage4_progress"] = {
        "completed": 0,
        "total": len(screened_candidates),
        "current": "",
    }
    state["stage4_errors"] = dict(errors)
    state["stage4_progress_events"] = progress_events
    state["stage4_future"] = _STAGE4_EXECUTOR.submit(
        _stage4_batch_worker,
        work_items,
        api_key,
        progress_events,
        len(errors),
        len(screened_candidates),
        errors,
    )
    _set_stage(
        state,
        4,
        "running",
        f"Generating AI theses for {len(work_items)} screened stocks sequentially.",
    )


def _stage4_screened_candidates(
    state: dict[str, Any],
    instruments: list[dict[str, Any]],
    quotes: dict[str, Any],
) -> list[dict[str, Any]]:
    instruments_by_key = {
        str(item.get("instrument_key") or item.get("key") or "")
        .strip()
        .upper()
        .replace(":", "|"): item
        for item in instruments
    }
    screened_candidates = []
    decisions = state.get("stage2_decisions", {})
    passed_rows = state.get("stage2_rows", [])
    if not isinstance(decisions, dict) or not isinstance(passed_rows, list):
        return screened_candidates
    for row in passed_rows:
        if not isinstance(row, dict):
            continue
        candidate_key = (
            str(row.get("Instrument_key") or row.get("instrument_key") or "")
            .strip()
            .upper()
            .replace(":", "|")
        )
        candidate_decision = decisions.get(candidate_key)
        if candidate_decision is None:
            continue
        if not candidate_decision.passed or not candidate_decision.features:
            continue
        candidate_instrument = instruments_by_key.get(candidate_key, {})
        candidate_symbol = str(
            candidate_instrument.get("symbol")
            or candidate_instrument.get("trading_symbol")
            or candidate_decision.symbol
        ).strip().upper()
        if (
            candidate_instrument.get("segment") == "BSE_EQ"
            and not candidate_symbol.endswith(".BO")
        ):
            candidate_symbol += ".BO"
        candidate_quote = quotes.get(candidate_key, {})
        candidate_features = candidate_decision.features
        screened_candidates.append(
            {
                "instrument_key": candidate_key,
                "symbol": candidate_symbol,
                "exchange": (
                    "BSE"
                    if candidate_instrument.get("segment") == "BSE_EQ"
                    else "NSE"
                ),
                "gatekeeper_tier": candidate_decision.signal_tier,
                "gatekeeper_score": candidate_decision.score,
                "last_price": (
                    candidate_quote.get("last_price")
                    if isinstance(candidate_quote, dict)
                    else candidate_features.get("last_price")
                ),
                "rsi_14": candidate_features.get("rsi_14"),
                "adx_14": candidate_features.get("adx_14"),
                "relative_volume_20d": candidate_features.get("rvol_20"),
            }
        )
    return screened_candidates


def _stage_counts(
    state: dict[str, Any],
    instruments: list[dict[str, Any]],
) -> list[int]:
    keys = [
        str(instrument.get("instrument_key") or instrument.get("key") or "")
        .strip()
        .upper()
        .replace(":", "|")
        for instrument in instruments
    ]
    stage1 = state.get("stage1_result")
    quotes = stage1.get("quotes", {}) if isinstance(stage1, dict) else {}
    stage2 = state.get("stage2_decisions", {})
    passed_count = sum(
        key in stage2
        and stage2[key].passed
        and bool(stage2[key].features)
        for key in keys
    ) if isinstance(stage2, dict) else 0
    return [
        len(instruments),
        len(quotes) if isinstance(quotes, dict) else 0,
        passed_count,
        max(
            sum(key in state["stage3_results"] for key in keys),
            int((state.get("stage3_progress") or {}).get("enriched", 0)),
        ),
        sum(key in state["stage4_results"] for key in keys),
    ]


def _stage_display_status(state: dict[str, Any], index: int) -> str:
    """Keep completed earlier stages visibly complete while a later stage runs."""
    status = state["statuses"][index]
    if index == 1 and status not in {"failure", "refresh_required"}:
        stage1 = state.get("stage1_result")
        quotes = stage1.get("quotes") if isinstance(stage1, dict) else None
        if (
            state.get("stage2_attempted")
            and isinstance(quotes, dict)
            and quotes
            and stage1.get("source") != "STALE_CACHE"
        ):
            return "success"
    return status


def _stage2_editor_disabled(state: dict[str, Any]) -> bool:
    return state["statuses"][3] == "running" or state["statuses"][4] == "running"


def _stage_totals(
    selected_count: int,
    instruments: list[dict[str, Any]],
    state: dict[str, Any],
) -> list[int]:
    decisions = state.get("stage2_decisions", {})
    passed_count = (
        sum(
            decision.passed and bool(decision.features)
            for decision in decisions.values()
        )
        if isinstance(decisions, dict)
        else 0
    )
    return [
        selected_count,
        len(instruments),
        len(instruments),
        passed_count,
        passed_count,
    ]


@st.fragment(run_every="5s")
def render_equity_pipeline(
    *,
    scope: str,
    suggested_symbols: list[str] | None = None,
) -> None:
    """Render the focused scanner controls, five pipeline actions, and table."""
    from brokers.factory import TICKER_MAP
    from features.equities.scanner import (
        _equity_labels,
        build_scan_table_payload,
    )
    from features.equities.scan_table import show_scan_table

    prefix = scope.lower().replace(" ", "_")
    stage0_session = st.session_state.get("_stage0_bootstrap")
    try:
        stage0_snapshot = read_stage0_cache()
    except sqlite3.Error as error:
        st.error(f"Could not read the Stage 0 cache: {error}")
        stage0_snapshot = None
    universe = (
        stage0_snapshot.get("universe")
        if isinstance(stage0_snapshot, dict)
        and isinstance(stage0_snapshot.get("universe"), list)
        else stage0_session.get("universe")
        if isinstance(stage0_session, dict)
        and isinstance(stage0_session.get("universe"), list)
        else []
    )
    master = pd.DataFrame(
        [
            {
                "Symbol": (
                    f"{_symbol_base(item.get('symbol', ''))}.BO"
                    if item.get("segment") == "BSE_EQ"
                    else _symbol_base(item.get("symbol", ""))
                ),
                "Ticker": (
                    f"{_symbol_base(item.get('symbol', ''))}.BO"
                    if item.get("segment") == "BSE_EQ"
                    else _symbol_base(item.get("symbol", ""))
                ),
                "Company": item.get("name", item.get("symbol", "")),
                "ISIN": item.get("isin", ""),
                "Exchange": (
                    "NSE" if item.get("segment") == "NSE_EQ" else "BSE"
                ),
            }
            for item in universe
            if isinstance(item, dict)
        ],
        columns=["Symbol", "Ticker", "Company", "ISIN", "Exchange"],
    )
    labels = _equity_labels(master)
    listed_symbols = set(labels)
    available_symbols = listed_symbols | set(TICKER_MAP)
    for candidate in suggested_symbols or []:
        base = _symbol_base(candidate)
        if base:
            available_symbols.add(base)
            labels.setdefault(base, base)
    universe_options = (
        "Select stocks to scan",
        "All listed stocks (NSE + BSE)",
        "NSE-listed stocks",
        "BSE-listed stocks",
        "Portfolio holdings",
        "Pick individual stocks",
    )
    universe_key = f"market_{prefix}_scan_universe"
    universe_initialized_key = f"{universe_key}_initialized"
    if not st.session_state.get(universe_initialized_key):
        if st.session_state.get(universe_key) == "All listed stocks (NSE + BSE)":
            st.session_state[universe_key] = "Select stocks to scan"
        st.session_state[universe_initialized_key] = True
    if st.session_state.get(universe_key) not in universe_options:
        st.session_state[universe_key] = "Select stocks to scan"
    st.selectbox(
        "Choose which stocks to scan",
        options=universe_options,
        key=universe_key,
        help="Choose the equity universe for all five pipeline stages.",
    )
    selected_universe = st.session_state[universe_key]
    if selected_universe == "Select stocks to scan":
        symbols = []
    elif selected_universe == "All listed stocks (NSE + BSE)":
        symbols = sorted(listed_symbols or available_symbols)
    elif selected_universe in {"NSE-listed stocks", "BSE-listed stocks"}:
        exchange = selected_universe.split("-", 1)[0]
        symbols = sorted(_symbols_for_exchange(master, exchange))
    elif selected_universe == "Portfolio holdings":
        symbols = []
        for key in ("equity_holdings", "portfolio"):
            holdings = st.session_state.get(key)
            if isinstance(holdings, pd.DataFrame) and "Ticker" in holdings.columns:
                symbols.extend(
                    _symbol_base(ticker)
                    for ticker in holdings["Ticker"].dropna().astype(str)
                )
        symbols = sorted(set(symbols) & available_symbols)
    else:
        watchlist_key = f"market_{prefix}_scan_symbols"
        if watchlist_key not in st.session_state:
            st.session_state[watchlist_key] = []
        st.session_state[watchlist_key] = [
            symbol
            for symbol in st.session_state[watchlist_key]
            if symbol in available_symbols
        ]
        symbols = st.multiselect(
            "Pick individual stocks",
            options=sorted(available_symbols),
            format_func=lambda ticker: labels.get(ticker, ticker),
            key=watchlist_key,
        )

    bse_catalog_needed = selected_universe in {
        "All listed stocks (NSE + BSE)",
        "BSE-listed stocks",
        "Portfolio holdings",
        "Pick individual stocks",
    }
    bse_data_missing = bse_catalog_needed and not _has_bse_listing(universe)
    instruments = _selected_instruments(symbols, universe) if universe else []
    signature = _key_signature(symbols)
    pipeline_state = st.session_state.setdefault("_equity_pipeline_state", {})
    state = pipeline_state.get(prefix)
    if not isinstance(state, dict) or state.get("signature") != signature:
        prior_state = state
        state = _new_pipeline_state(signature)
        if (
            isinstance(prior_state, dict)
            and prior_state.get("gatekeeper_settings_version") == 4
        ):
            state["gatekeeper_config"] = dict(
                prior_state.get("gatekeeper_config") or {}
            )
            state["gatekeeper_settings_version"] = 4
        if stage0_cache_is_fresh(stage0_snapshot):
            _set_stage(state, 0, "success", "Stage 0 universe is available from cache.")
        elif stage0_snapshot or universe:
            _set_stage(
                state,
                0,
                "refresh_required",
                "Stage 0 data is stale or only partially available; refresh it.",
            )
        pipeline_state[prefix] = state
    if bse_data_missing and state.get("bse_catalog_refresh_failed"):
        if st.button(
            "Retry BSE listing refresh",
            key=f"{prefix}_retry_bse_catalog",
            type="secondary",
        ):
            state["bse_catalog_refresh_failed"] = False
    if bse_data_missing:
        bse_future = state.get("bse_catalog_future")
        if isinstance(bse_future, Future) and bse_future.done():
            try:
                bse_future.result()
                refreshed_snapshot = read_stage0_cache()
            except (
                OSError,
                RuntimeError,
                sqlite3.Error,
                TimeoutError,
                ValueError,
            ) as error:
                state["bse_catalog_future"] = None
                state["bse_catalog_refresh_failed"] = True
                _set_stage(
                    state,
                    0,
                    "failure",
                    f"BSE stock-list refresh failed: {error}",
                )
            else:
                state["bse_catalog_future"] = None
                if isinstance(refreshed_snapshot, dict) and _has_bse_listing(
                    refreshed_snapshot.get("universe") or []
                ):
                    state["bse_catalog_refresh_failed"] = False
                    _set_stage(
                        state,
                        0,
                        "refresh_required",
                        "BSE stock list loaded; preparing the selected universe.",
                    )
                else:
                    state["bse_catalog_refresh_failed"] = True
                    _set_stage(
                        state,
                        0,
                        "failure",
                        "The refreshed catalog still contains no BSE listings. "
                        "Check the public BSE feed and retry.",
                    )
        elif isinstance(bse_future, Future):
            _set_stage(
                state,
                0,
                "running",
                "Loading the BSE stock list into the shared Stage 0 catalog.",
            )
        elif not state.get("bse_catalog_refresh_failed"):
            try:
                bse_future = request_stage0_refresh(force=True)
            except (OSError, RuntimeError, sqlite3.Error, TimeoutError, ValueError) as error:
                state["bse_catalog_refresh_failed"] = True
                _set_stage(
                    state,
                    0,
                    "failure",
                    f"Could not start the BSE stock-list refresh: {error}",
                )
            else:
                if isinstance(bse_future, Future):
                    state["bse_catalog_future"] = bse_future
                    state["bse_catalog_refresh_failed"] = False
                    _set_stage(
                        state,
                        0,
                        "running",
                        "Loading the BSE stock list into the shared Stage 0 catalog.",
                    )
                else:
                    _set_stage(
                        state,
                        0,
                        "refresh_required",
                        "Another worker is refreshing the shared Stage 0 catalog; "
                        "waiting for BSE listings.",
                    )
    elif _has_bse_listing(universe):
        state["bse_catalog_future"] = None
        state["bse_catalog_refresh_failed"] = False
    _consume_stage0_future(state)
    if state.get("gatekeeper_settings_version") != 4:
        migrated_config = {
            name: getattr(GatekeeperConfig(), name)
            for name in GatekeeperConfig.__dataclass_fields__
        }
        existing_config = state.get("gatekeeper_config")
        if isinstance(existing_config, dict):
            migrated_config.update(
                {
                    name: value
                    for name, value in existing_config.items()
                    if name in migrated_config
                    and not name.startswith("enable_")
                }
            )
        state["gatekeeper_config"] = migrated_config
        state["gatekeeper_settings_version"] = 4
        state["gatekeeper_widgets_initialized"] = False

    counts = _stage_counts(state, instruments)
    totals = _stage_totals(len(symbols), instruments, state)

    def apply_gatekeeper_change(field: str, widget_key: str) -> None:
        if state["statuses"][3] == "running" or widget_key not in st.session_state:
            return
        updated = dict(state.get("gatekeeper_config", {}))
        updated[field] = st.session_state[widget_key]
        validated = GatekeeperConfig(**updated)
        if updated == state.get("gatekeeper_config", {}):
            return
        state["gatekeeper_config"] = {
            name: getattr(validated, name)
            for name in GatekeeperConfig.__dataclass_fields__
        }
        state["stage2_attempted"] = False
        state["stage2_decisions"] = {}
        state["stage2_rows"] = []
        state["stage2_all_rows"] = []
        state["stage2_fundamentals"] = {}
        state["stage3_results"] = {}
        state["stage4_results"] = {}
        state["stage3_errors"] = {}
        state["stage3_progress"] = {"completed": 0, "enriched": 0, "total": 0, "current": ""}
        state["stage3_future"] = None
        state["stage3_attempted"] = False
        state["stage3_progress_events"] = None
        state["selected_symbol"] = None
        _set_stage(state, 2, "idle", "Jev rules changed; refreshing the technical screen.")
        _set_stage(state, 3, "idle")
        _set_stage(state, 4, "idle")

    def update_gatekeeper_setting(field: str, widget_key: str) -> None:
        apply_gatekeeper_change(field, widget_key)

    st.markdown(
        """
        <style>
        div[data-testid="stHorizontalBlock"]:has(
          > div[data-testid="column"] [class*="st-key-equity-pipeline-"]
        ) {
          gap:0!important;flex-wrap:nowrap!important;overflow:visible!important;
        }
        div[data-testid="stHorizontalBlock"] >
        div[data-testid="column"]:has([class*="st-key-equity-pipeline-"]) {
          min-width:0!important;max-width:none!important;overflow:visible!important;
        }
        [class*="st-key-equity-pipeline-"] button[kind="secondary"] {
          position:relative!important;isolation:isolate;
          width:100%!important;min-width:100%!important;
          min-height:2.9rem!important;
          padding:.3rem 1rem .42rem 1.05rem!important;
          overflow:hidden!important;
          background:#172235!important;color:#c5cfdd!important;
          border:1px solid #49617c!important;border-radius:0!important;
          clip-path:polygon(0 0,calc(100% - 14px) 0,100% 50%,
            calc(100% - 14px) 100%,0 100%,14px 50%)!important;
          white-space:nowrap!important;line-height:1.25!important;
          font-size:clamp(.65rem,.82vw,.78rem)!important;font-weight:500!important;
          box-shadow:inset 0 1px 0 rgba(255,255,255,.07)!important;
          transition:filter .15s ease,box-shadow .15s ease!important;
        }
        [class*="st-key-equity-pipeline-"] button[kind="secondary"] p {
          position:relative;z-index:1;
          display:flex!important;align-items:center!important;justify-content:center!important;
          gap:.34rem!important;margin:0!important;width:100%!important;
        }
        [class*="st-key-equity-pipeline-"] button[kind="secondary"] [data-testid="stMarkdownContainer"] {
          flex:0 0 auto!important;text-align:center!important;
        }
        [class*="st-key-equity-pipeline-"] button[kind="secondary"] svg {
          width:1.5rem!important;height:1.5rem!important;min-width:1.5rem!important;
          color:#d5e8ff!important;filter:drop-shadow(0 0 4px rgba(115,190,255,.65));
          stroke-width:2.6!important;
        }
        [class*="st-key-equity-pipeline-"] button[kind="secondary"] [data-testid="stMarkdownContainer"] {
          overflow:visible!important;
        }
        [class*="st-key-equity-pipeline-"] button[kind="secondary"] code {
          padding:.08rem .36rem!important;border:1px solid #69819b!important;
          border-radius:999px!important;background:#34465d!important;
          color:#e4e9f0!important;font-size:.66rem!important;
          font-weight:600!important;line-height:1.25!important;
          white-space:nowrap!important;
        }
        [class*="st-key-equity-pipeline-"] button[kind="secondary"]::after {
          content:"";position:absolute;z-index:0;left:1px;bottom:1px;
          height:2px;width:0;background:transparent;
        }
        [class*="st-key-equity-pipeline-"] button[kind="secondary"]:hover {
          filter:brightness(1.13);
        }
        [class*="st-key-equity-pipeline-2-"] button[kind="secondary"] {
          padding-right:2rem!important;
        }
        [class*="st-key-equity-pipeline-0-"] button[kind="secondary"] {
          clip-path:polygon(0 0,calc(100% - 14px) 0,100% 50%,
            calc(100% - 14px) 100%,0 100%)!important;
          border-radius:6px 0 0 6px!important;
        }
        [class*="st-key-equity-pipeline-4-"] button[kind="secondary"] {
          clip-path:polygon(0 0,100% 0,100% 100%,0 100%,14px 50%)!important;
          border-radius:0 6px 6px 0!important;
        }
        [class*="st-key-equity-pipeline-"][class*="-success"] button[kind="secondary"] {
          background:#162b32!important;border-color:#4b8190!important;color:#f0f8f6!important;
        }
        [class*="st-key-equity-pipeline-"][class*="-success"] button[kind="secondary"] svg {
          color:#73e7a3!important;filter:drop-shadow(0 0 5px rgba(50,205,115,.75));
        }
        [class*="st-key-equity-pipeline-"][class*="-success"] button[kind="secondary"]::after {
          width:76%;background:#32ad73;
        }
        [class*="st-key-equity-pipeline-"][class*="-failure"] button[kind="secondary"] {
          background:#40262e!important;border-color:#e56770!important;color:#ffd9dc!important;
        }
        [class*="st-key-equity-pipeline-"][class*="-failure"] button[kind="secondary"] svg {
          color:#ff8991!important;filter:drop-shadow(0 0 5px rgba(227,79,89,.75));
        }
        [class*="st-key-equity-pipeline-"][class*="-failure"] button[kind="secondary"]::after {
          width:76%;background:#e34f59;
        }
        [class*="st-key-equity-pipeline-"][class*="-refresh_required"] button[kind="secondary"],
        [class*="st-key-equity-pipeline-"][class*="-running"] button[kind="secondary"] {
          background:#102e52!important;border-color:#45aaff!important;color:#fff!important;
          box-shadow:0 0 8px rgba(38,151,255,.3)!important;
        }
        [class*="st-key-equity-pipeline-"][class*="-refresh_required"] button[kind="secondary"] svg,
        [class*="st-key-equity-pipeline-"][class*="-running"] button[kind="secondary"] svg {
          color:#79caff!important;filter:drop-shadow(0 0 6px rgba(38,151,255,.8));
        }
        [class*="st-key-equity-pipeline-"][class*="-refresh_required"] button[kind="secondary"]::after {
          width:76%;background:#45aaff;
        }
        [class*="st-key-equity-pipeline-"] button[kind="tertiary"] {
          position:absolute!important;top:50%!important;right:1.1rem!important;
          transform:translateY(-50%)!important;z-index:3;
          width:1.45rem!important;height:1.45rem!important;min-height:1.45rem!important;
          padding:0!important;border:1px solid rgba(123,195,255,.55)!important;
          border-radius:50%!important;clip-path:none!important;overflow:visible!important;
          background:#173653!important;color:#fff!important;
          box-shadow:0 0 4px rgba(70,170,255,.25)!important;
          cursor:pointer!important;pointer-events:auto!important;
        }
        [class*="st-key-equity-pipeline-"] button[kind="tertiary"]:hover {
          background:#20527b!important;box-shadow:0 0 8px rgba(70,170,255,.55)!important;
        }
        [class*="st-key-equity-pipeline-"] button[kind="tertiary"]:focus-visible {
          outline:2px solid #fff!important;outline-offset:2px!important;
        }
        [class*="st-key-equity-pipeline-"] button[kind="tertiary"] svg {
          width:1rem!important;height:1rem!important;min-width:1rem!important;
          color:#fff!important;stroke-width:2.7!important;
          filter:drop-shadow(0 0 3px rgba(120,200,255,.8));
        }
        [class*="st-key-equity-pipeline-"] button:disabled {
          opacity:1!important;cursor:default!important;
        }
        [class*="st-key-equity-pipeline-"] [data-testid="stPopover"] {
          position:absolute!important;top:0!important;right:1.1rem!important;z-index:30;
          pointer-events:auto!important;
        }
        [class*="st-key-equity-pipeline-"] {
          position:relative!important;z-index:1;overflow:visible!important;
        }
        [class*="st-key-equity-pipeline-"]:focus-within {
          z-index:30!important;
        }
        [class*="st-key-equity-pipeline-2-"]:focus-within,
        [class*="st-key-equity-pipeline-2-"]:has([data-testid="stPopoverBody"]) {
          z-index:40!important;
        }
        [data-testid="stPopoverBody"] {
          z-index:50!important;
        }
        .jev-rule-label {
          display:flex;align-items:center;gap:.4rem;min-height:2rem;
        }
        .jev-info-icon {
          display:inline-flex;align-items:center;justify-content:center;
          width:1rem;height:1rem;flex:0 0 1rem;border:1px solid #71829a;
          border-radius:50%;color:#c6d6e8;font-size:.68rem;font-weight:700;
          line-height:1;cursor:help;
        }
        .jev-info-icon:focus-visible {
          outline:2px solid #fff;outline-offset:2px;
        }
        [class*="st-key-equity-pipeline-"] button[kind]:focus-visible {
          outline:2px solid #fff!important;outline-offset:2px!important;
        }
        @media(max-width:700px) {
          div[data-testid="stHorizontalBlock"]:has(
            > div[data-testid="column"] [class*="st-key-equity-pipeline-"]
          ) {
            max-width:100%!important;overflow-x:auto!important;overflow-y:hidden!important;
            padding-bottom:.25rem!important;
          }
          div[data-testid="stHorizontalBlock"] >
          div[data-testid="column"]:has([class*="st-key-equity-pipeline-"]) {
            flex:0 0 auto!important;
          }
          div[data-testid="stHorizontalBlock"] >
          div[data-testid="column"]:has([class*="st-key-equity-pipeline-"]):nth-child(1),
          div[data-testid="stHorizontalBlock"] >
          div[data-testid="column"]:has([class*="st-key-equity-pipeline-"]):nth-child(2),
          div[data-testid="stHorizontalBlock"] >
          div[data-testid="column"]:has([class*="st-key-equity-pipeline-"]):nth-child(4),
          div[data-testid="stHorizontalBlock"] >
          div[data-testid="column"]:has([class*="st-key-equity-pipeline-"]):nth-child(5) {
            width:9.5rem!important;min-width:9.5rem!important;
          }
          div[data-testid="stHorizontalBlock"] >
          div[data-testid="column"]:has([class*="st-key-equity-pipeline-"]):nth-child(3) {
            width:13rem!important;min-width:13rem!important;
          }
          [class*="st-key-equity-pipeline-"] button[kind="secondary"] {
            min-height:2.75rem!important;padding:.25rem .82rem .36rem .9rem!important;
            font-size:.66rem!important;
          }
          [class*="st-key-equity-pipeline-"] button[kind="secondary"] [data-testid="stMarkdownContainer"] {
            overflow:visible!important;
          }
          [class*="st-key-equity-pipeline-"] button[kind="secondary"] code {
            font-size:.58rem!important;padding:.06rem .26rem!important;
          }
          [class*="st-key-equity-pipeline-2-"] button[kind="secondary"] {
            padding-right:1.6rem!important;
          }
          [class*="st-key-equity-pipeline-"] button[kind="tertiary"] {
            top:50%!important;right:.8rem!important;
            width:1.3rem!important;height:1.3rem!important;min-height:1.3rem!important;
            transform:translateY(-50%)!important;
          }
          [class*="st-key-equity-pipeline-"] button[kind="secondary"] svg {
            width:1.35rem!important;height:1.35rem!important;min-width:1.35rem!important;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    config_defaults = GatekeeperConfig(**state["gatekeeper_config"])
    for field in GatekeeperConfig.__dataclass_fields__:
        widget_key = (
            f"{prefix}_jev_v5_enabled_{field.removeprefix('enable_')}"
            if field.startswith("enable_")
            else f"{prefix}_jev_v2_{field}"
        )
        st.session_state[widget_key] = getattr(config_defaults, field)
    state["gatekeeper_widgets_initialized"] = True
    stage_icons = (
        ":material/query_stats:",
        ":material/ecg_heart:",
        ":material/candlestick_chart:",
        ":material/database:",
        ":material/neurology:",
    )
    clicks = []
    stage_buttons = st.columns([1, 1, 1.4, 1, 1], gap=None)
    for index, (column, stage_name) in enumerate(zip(stage_buttons, _STAGE_NAMES)):
        status = _stage_display_status(state, index)
        container_key = f"equity-pipeline-{index}-{status}"
        with column:
            with st.container(key=container_key):
                count_label = f"{counts[index]}/{totals[index]}"
                clicks.append(
                    st.button(
                        f"{stage_name}   `{count_label}`",
                        key=f"equity_pipeline_{prefix}_stage_{index}",
                        type="secondary",
                        icon=stage_icons[index],
                        help=_STAGE_DESCRIPTIONS[index],
                        disabled=(
                            index < 4
                            or status == "running"
                            or (index == 4 and counts[2] == 0)
                        ),
                        width="stretch",
                    )
                )
                if index == 2:
                    with st.popover(
                        "",
                        help="Edit enabled Jev rules and thresholds",
                        icon=":material/edit:",
                        type="tertiary",
                        width=720,
                        disabled=_stage2_editor_disabled(state),
                    ):
                        config_defaults = GatekeeperConfig(
                            **state.get("gatekeeper_config", {})
                        )
                        st.caption(
                            "Enabled rules use the shown threshold. Changes apply "
                            "automatically; the current enrichment run is not interrupted."
                        )
                        rule_controls = (
                            (
                                "min_last_price",
                                "Min price (₹)",
                                "enable_min_last_price",
                                "Reject stocks priced below this value.",
                            ),
                            (
                                "max_spread_pct",
                                "Max spread (%)",
                                "enable_max_spread_pct",
                                "Reject stocks whose bid/ask spread exceeds this percentage.",
                            ),
                            (
                                "min_daily_volume",
                                "Min daily volume",
                                "enable_min_daily_volume",
                                "Reject stocks with lower daily trading volume.",
                            ),
                            (
                                "min_adx_trend",
                                "Min ADX trend",
                                "enable_min_adx_trend",
                                "Require at least this ADX value for trend strength.",
                            ),
                            (
                                "max_choppiness_index",
                                "Max choppiness",
                                "enable_max_choppiness_index",
                                "Reject stocks with a choppiness index above this value.",
                            ),
                            (
                                "min_piotroski_score",
                                "Min Piotroski score",
                                "enable_min_piotroski_score",
                                "Require this minimum Piotroski F-score (0–9).",
                            ),
                            (
                                "min_altman_z_score",
                                "Min Altman Z-score",
                                "enable_min_altman_z_score",
                                "Require this minimum Altman Z-score.",
                            ),
                            (
                                "max_promoter_pledge_pct",
                                "Max pledge (%)",
                                "enable_max_promoter_pledge_pct",
                                "Reject stocks with promoter pledge above this percentage.",
                            ),
                        )
                        rule_columns = st.columns(2)
                        for rule_index, (
                            field_name,
                            label,
                            enabled_field,
                            description,
                        ) in enumerate(rule_controls):
                            widget_key = f"{prefix}_jev_v2_{field_name}"
                            enabled_key = (
                                f"{prefix}_jev_v5_enabled_"
                                f"{enabled_field.removeprefix('enable_')}"
                            )
                            if widget_key not in st.session_state:
                                st.session_state[widget_key] = getattr(
                                    config_defaults, field_name
                                )
                            if enabled_key not in st.session_state:
                                st.session_state[enabled_key] = getattr(
                                    config_defaults, enabled_field
                                )
                            integer_field = field_name in {
                                "min_piotroski_score",
                                "min_daily_volume",
                            }
                            with rule_columns[rule_index % len(rule_columns)]:
                                with st.container(border=True):
                                    control_columns = st.columns(
                                        [1.5, 0.65, 1.0],
                                        vertical_alignment="center",
                                        gap="small",
                                    )
                                    with control_columns[0]:
                                        st.markdown(
                                            f'<div class="jev-rule-label">'
                                            f"<strong>{escape(label)}</strong>"
                                            f'<span class="jev-info-icon" tabindex="0" '
                                            f'role="img" aria-label="Info: '
                                            f'{escape(description, quote=True)}" '
                                            f'title="{escape(description, quote=True)}">i</span>'
                                            f"</div>",
                                            unsafe_allow_html=True,
                                        )
                                    with control_columns[1]:
                                        st.toggle(
                                            "On",
                                            key=enabled_key,
                                            on_change=update_gatekeeper_setting,
                                            args=(enabled_field, enabled_key),
                                            help=description,
                                            disabled=state["statuses"][3] == "running",
                                        )
                                    with control_columns[2]:
                                        st.number_input(
                                            "Threshold",
                                            key=widget_key,
                                            on_change=update_gatekeeper_setting,
                                            args=(field_name, widget_key),
                                            min_value=0 if integer_field else 0.0,
                                            step=1 if integer_field else 0.1,
                                            label_visibility="collapsed",
                                            help=description,
                                            disabled=(
                                                not st.session_state[enabled_key]
                                                or state["statuses"][3] == "running"
                                            ),
                                        )
                        history_key = f"{prefix}_jev_v2_min_history_bars"
                        if history_key not in st.session_state:
                            st.session_state[history_key] = (
                                config_defaults.min_history_bars
                            )
                        with st.container(border=True):
                            history_columns = st.columns(
                                [1.5, 0.65, 1.0],
                                vertical_alignment="center",
                                gap="small",
                            )
                            with history_columns[0]:
                                st.markdown(
                                    '<div class="jev-rule-label">'
                                    "<strong>Required history bars</strong>"
                                    '<span class="jev-info-icon" tabindex="0" '
                                    'role="img" aria-label="Info: Minimum daily candle '
                                    'history needed for technical calculations; this is '
                                    'not a Jev rejection rule." '
                                    'title="Minimum daily candle history needed for '
                                    'technical calculations; this is not a Jev '
                                    'rejection rule.">i</span>'
                                    "</div>",
                                    unsafe_allow_html=True,
                                )
                            with history_columns[1]:
                                st.caption("Data quality")
                            with history_columns[2]:
                                st.number_input(
                                    "Required history bars",
                                    key=history_key,
                                    min_value=50,
                                    step=10,
                                    on_change=update_gatekeeper_setting,
                                    args=("min_history_bars", history_key),
                                    label_visibility="collapsed",
                                    help=(
                                        "Minimum daily candle history needed for "
                                        "technical calculations; this is not a Jev "
                                        "rejection rule."
                                    ),
                                    disabled=state["statuses"][3] == "running",
                                )
                        st.divider()
                        st.markdown("**Automatic investment classification**")
                        st.caption(
                            "These investment rules run automatically and stay "
                            "independent from Jev's technical/trading gate; they "
                            "do not add manual toggles."
                        )
                        investment_columns = st.columns(2)
                        with investment_columns[0]:
                            with st.container(border=True):
                                st.markdown(
                                    "**Short-term · 3–12 months**\n\n"
                                    "- Price above DMA 200\n"
                                    "- YoY sales / PAT / operating-profit growth: "
                                    "≥12% / ≥15% / ≥10%\n"
                                    "- PEG: >0 and ≤1.5; otherwise forward P/E ≤25 "
                                    "when PAT growth is >20%\n"
                                    "- Daily turnover ≥₹1 crore; promoter pledge ≤15%"
                                )
                        with investment_columns[1]:
                            with st.container(border=True):
                                st.markdown(
                                    "**Long-term · 3+ years**\n\n"
                                    "- 3-year sales / profit CAGR: ≥10% / ≥12%\n"
                                    "- Lenders: ROA ≥1.2%, net NPA ≤1.2%, CAR ≥15%; "
                                    "skip debt/equity, OCF/PAT, Altman and Piotroski\n"
                                    "- Other industries: ROCE (or ROE) ≥15%, "
                                    "debt/equity ≤0.6, 3-year OCF/PAT ≥0.8\n"
                                    "- Pledge ≤5%; P/B or P/E ≤120% of its 5-year median"
                                )
                        st.caption(
                            "A horizon passes when at least 80% of its available "
                            "checks pass. Missing metrics are excluded and shown "
                            "as a partial-data warning."
                        )

    stage1_result = state.get("stage1_result")
    quotes = (
        stage1_result.get("quotes")
        if isinstance(stage1_result, dict)
        and isinstance(stage1_result.get("quotes"), dict)
        else {}
    )
    try:
        stage0_snapshot = read_stage0_cache()
    except sqlite3.Error:
        stage0_snapshot = None
    _consume_stage4_future(state)
    if clicks[4]:
        stage1_fetched_at = (
            stage1_result.get("fetched_at")
            if isinstance(stage1_result, dict)
            else None
        )
        _start_stage4_batch(
            state, instruments, stage0_snapshot, quotes, stage1_fetched_at
        )
        _consume_stage4_future(state)

    market_regime = _nifty_market_regime(stage0_snapshot)
    if symbols:
        _advance_automatic_stages(
            state,
            symbols,
            instruments,
            market_regime,
        )

    for index, message in enumerate(state["messages"]):
        if message:
            if state["statuses"][index] == "failure":
                st.error(f"{_STAGE_NAMES[index]}: {message}")
            elif state["statuses"][index] == "refresh_required":
                st.info(f"{_STAGE_NAMES[index]}: {message}")
            elif state["statuses"][index] == "running":
                st.info(f"{_STAGE_NAMES[index]}: {message}")

    if symbols:
        nifty_price = market_regime["nifty_price"]
        nifty_dma_200 = market_regime["nifty_dma_200"]
        if nifty_price is not None and nifty_dma_200 is not None:
            if nifty_price <= nifty_dma_200:
                st.warning("Bear Market Regime: Stagger Entries")
        else:
            st.info(
                "Nifty 50 200-DMA is unavailable; short-term investment "
                "classifications are held back until benchmark data is available."
            )

    rows = _pipeline_details_for_rows(
        _visible_stage2_rows(state, selected_universe),
        state,
        instruments,
        stage0_snapshot.get("macro")
        if isinstance(stage0_snapshot, dict)
        and isinstance(stage0_snapshot.get("macro"), dict)
        else None,
    )
    payload, config = build_scan_table_payload(rows, labels)
    config["columns"] = [
        ["stock", "Stock", 0],
        ["investment_tag", "Investment horizon", 0],
        ["ai_confidence", "AI confidence", 1],
        ["signal", "Signal", 0],
        ["price", "Price", 1],
        ["chg", "Day change", 1],
        ["m1", "1 month", 1],
        ["rsi", "RSI 14", 1],
        ["vol", "Volume", 1],
        ["strength", "Strength", 0],
    ]
    config["defaultSort"] = {"key": "ai_confidence", "dir": -1}
    config["showFilters"] = False
    config["showColumnFilters"] = True
    config["showCart"] = False
    config["tableKey"] = f"equity-pipeline:{prefix}:{signature}"
    table_key = config["tableKey"]
    show_scan_table(
        payload,
        config,
        on_select_stock=lambda: _on_table_select(table_key, state),
    )

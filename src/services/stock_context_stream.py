"""Upstox V3 streaming adapter and session-aware microstructure accumulator."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from threading import Lock
from typing import Callable
from zoneinfo import ZoneInfo

from core.config import configured_value
from .stock_context_sources import first_number

_EXCHANGE_TZ = ZoneInfo("Asia/Kolkata")
_OPEN = time(9, 15)
_CLOSE = time(15, 30)


class UpstoxMicrostructureStream:
    """Accumulate opening bars; baseline is historical cumulative volume by HH:MM.

    Start before the opening range and keep the connection alive. Missing bars,
    stale ticks and missing baseline are surfaced instead of approximated.
    """

    def __init__(self, instrument_key: str, volume_baseline: dict[str, float], *,
                 opening_minutes: int = 15, max_age_seconds: int = 30,
                 baseline_source: dict | None = None, baseline_warnings: list[str] | None = None):
        if "|" not in instrument_key or not all(instrument_key.split("|", 1)):
            raise ValueError("A valid Upstox instrument key is required.")
        if not 1 <= opening_minutes <= 60 or max_age_seconds < 1:
            raise ValueError("Opening window and maximum tick age must be positive.")
        normalized_baseline = {key: first_number(value) for key, value in volume_baseline.items()}
        if any(value is None or value <= 0 for value in normalized_baseline.values()):
            raise ValueError("Historical cumulative-volume baselines must be positive.")
        self.instrument_key = instrument_key
        self.baseline = normalized_baseline
        self.baseline_source = deepcopy(baseline_source)
        self.baseline_warnings = list(baseline_warnings or [])
        self.opening_minutes = opening_minutes
        self.max_age_seconds = max_age_seconds
        self._lock = Lock()
        self._date = None
        self._bars: dict[datetime, tuple[float, float, int | None]] = {}
        self._latest = None

    def on_message(self, message: dict) -> None:
        feeds = message.get("feeds")
        feed = feeds.get(self.instrument_key) if isinstance(feeds, dict) else None
        full_feed = feed.get("fullFeed") if isinstance(feed, dict) else None
        full = full_feed.get("marketFF") if isinstance(full_feed, dict) else None
        mode = (feed.get("requestMode") or full_feed.get("requestMode")) if isinstance(feed, dict) and isinstance(full_feed, dict) else None
        if not isinstance(full, dict) or mode != "full_d30":
            return
        timestamp = first_number(message.get("currentTs"))
        ltpc = full.get("ltpc")
        trade_time = first_number(ltpc.get("ltt")) if isinstance(ltpc, dict) else None
        if timestamp is None or trade_time is None or timestamp <= 0 or trade_time <= 0:
            return
        try:
            observed = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).astimezone(_EXCHANGE_TZ)
            traded = datetime.fromtimestamp(trade_time / 1000, tz=timezone.utc).astimezone(_EXCHANGE_TZ)
        except (OverflowError, OSError, ValueError):
            return
        price = first_number(ltpc.get("ltp"))
        if price is None or price <= 0 or traded.date() != observed.date():
            return
        if (observed - traded).total_seconds() < 0 or (observed - traded).total_seconds() > self.max_age_seconds:
            return
        if not _OPEN <= observed.time().replace(tzinfo=None) <= _CLOSE:
            return
        market_level = full.get("marketLevel")
        levels = market_level.get("bidAskQuote", []) if isinstance(market_level, dict) else []
        best = levels[0] if isinstance(levels, list) and levels and isinstance(levels[0], dict) else {}
        bid, ask = first_number(best.get("bidP")), first_number(best.get("askP"))
        vwap, volume = first_number(full.get("atp")), first_number(full.get("vtt"))
        market_ohlc = full.get("marketOHLC")
        bars = market_ohlc.get("ohlc", []) if isinstance(market_ohlc, dict) else []
        opening_start = observed.replace(hour=9, minute=15, second=0, microsecond=0)
        with self._lock:
            if self._date is not None and observed.date() < self._date:
                return
            if self._date != observed.date():
                self._date = observed.date()
                self._bars.clear()
                self._latest = None
            if self._latest is not None and observed < self._latest["observed"]:
                return
            if isinstance(bars, list):
                for bar in bars:
                    if not isinstance(bar, dict) or bar.get("interval") not in ("I1", "1m"):
                        continue
                    stamp, high, low = first_number(bar.get("ts")), first_number(bar.get("high")), first_number(bar.get("low"))
                    if stamp is None or high is None or low is None or not 0 < low <= high:
                        continue
                    try:
                        at = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).astimezone(_EXCHANGE_TZ)
                    except (OverflowError, OSError, ValueError):
                        continue
                    if opening_start <= at and at + timedelta(minutes=1) <= observed and at.second == 0:
                        bar_volume = first_number(bar.get("vol"))
                        volume_value = int(bar_volume) if bar_volume is not None and bar_volume >= 0 and bar_volume.is_integer() else None
                        self._bars[at] = (high, low, volume_value)
            self._latest = {"observed": observed, "traded": traded, "price": price, "vwap": vwap,
                            "volume": volume, "bid": bid, "ask": ask, "raw_message": deepcopy(message)}

    def snapshot(self, *, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("Snapshot time must be timezone-aware.")
        with self._lock:
            if self._latest is None:
                raise ValueError("No current Upstox market tick has been received.")
            latest = dict(self._latest)
            bars = dict(self._bars)
        age = (now - latest["traded"]).total_seconds()
        if age < 0 or age > self.max_age_seconds:
            raise ValueError("Upstox market tick is stale or future-dated.")
        observed = latest["observed"]
        start = observed.replace(hour=9, minute=15, second=0, microsecond=0)
        expected = [start + timedelta(minutes=i) for i in range(self.opening_minutes)]
        breakout = None
        if observed >= start + timedelta(minutes=self.opening_minutes) and all(t in bars for t in expected):
            high, low = max(bars[t][0] for t in expected), min(bars[t][1] for t in expected)
            last_minute = observed.replace(second=0, microsecond=0) - timedelta(minutes=1)
            previous_minute = last_minute - timedelta(minutes=1)
            inside = last_minute in bars and previous_minute in bars and (
                bars[last_minute][0] <= bars[previous_minute][0]
                and bars[last_minute][1] >= bars[previous_minute][1]
            )
            breakout = ("ORB_BULLISH" if latest["price"] > high else
                        "ORB_BEARISH" if latest["price"] < low else
                        "INSIDE_BAR" if inside else "NONE")
        last_minute = observed.replace(second=0, microsecond=0) - timedelta(minutes=1)
        completed_count = int((last_minute - start).total_seconds() // 60) + 1
        completed = [start + timedelta(minutes=i) for i in range(max(0, completed_count))]
        baseline = first_number(self.baseline.get(last_minute.strftime("%H:%M")))
        relative_volume = None
        if completed and baseline is not None and baseline > 0 and all(
            minute in bars and bars[minute][2] is not None for minute in completed
        ):
            relative_volume = sum(bars[minute][2] for minute in completed) / baseline
        vwap, bid, ask = latest["vwap"], latest["bid"], latest["ask"]
        return {
            "current_price": latest["price"],
            "observed_at": observed.isoformat(),
            "traded_at": latest["traded"].isoformat(),
            "raw_message": deepcopy(latest["raw_message"]),
            "completed_minute_bars": [{"timestamp": at.isoformat(), "high": bar[0], "low": bar[1], "volume": bar[2]}
                                      for at, bar in sorted(bars.items())],
            "vwap": vwap if vwap is not None and vwap > 0 else None,
            "distance_from_vwap_pct": (latest["price"] / vwap - 1) * 100 if vwap is not None and vwap > 0 else None,
            "opening_range_breakout": breakout,
            "time_of_day_segment": "MORNING_RUSH" if observed.time() < time(10, 30)
            else "MIDDAY_LULL" if observed.time() < time(14, 30) else "POWER_HOUR",
            "relative_volume_intraday": relative_volume,
            "bid_ask_spread": ask - bid if bid is not None and ask is not None and 0 < bid <= ask else None,
        }

    def create_streamer(self, *, on_error: Callable, on_close: Callable):
        """Create a V3 D30 subscriber; caller must connect and disconnect it."""
        token = str(configured_value("UPSTOX_ANALYTICS_TOKEN", "")).strip()
        if not token:
            raise ValueError("UPSTOX_ANALYTICS_TOKEN is required for streaming.")
        import upstox_client
        configuration = upstox_client.Configuration()
        configuration.access_token = token
        client = upstox_client.ApiClient(configuration)
        streamer = upstox_client.MarketDataStreamerV3(client, [self.instrument_key], "full_d30")
        streamer.on("message", self.on_message)
        streamer.on("error", on_error)
        streamer.on("close", on_close)
        return streamer

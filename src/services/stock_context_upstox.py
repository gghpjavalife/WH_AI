"""Authenticated Upstox quote access for the context engine."""

from __future__ import annotations

import re
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from core.config import configured_value, settings
from .stock_context_sources import first_number


def _validate_key(instrument_key: str) -> str:
    key = instrument_key.strip()
    if "|" not in key or not all(key.split("|", 1)):
        raise ValueError("An Upstox instrument key such as NSE_EQ|ISIN is required.")
    return key


def equity_isin(instrument_key: str) -> str | None:
    key = _validate_key(instrument_key)
    exchange, identifier = key.split("|", 1)
    if exchange in {"NSE_EQ", "BSE_EQ"} and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", identifier):
        return identifier
    return None


def _get(path: str, *, params: dict | None = None, session: requests.Session | None = None,
         version: str = "v2") -> dict:
    token = str(configured_value("UPSTOX_ANALYTICS_TOKEN", "")).strip()
    if not token:
        raise ValueError("UPSTOX_ANALYTICS_TOKEN is required for Upstox data.")
    client = session or requests
    response = client.get(
        f"{settings.upstox_api_base}/{version}/{path}",
        params=params,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        timeout=(settings.http_connect_timeout_seconds, settings.http_read_timeout_seconds),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("status", "success") != "success":
        raise ValueError("Upstox returned an unsuccessful or malformed response.")
    return payload


def fetch_upstox_quote(instrument_key: str, *, session: requests.Session | None = None) -> dict[str, Any]:
    """Fetch a v2 full quote; never persist or log the bearer token."""
    key = _validate_key(instrument_key)
    payload = _get("market-quote/quotes", params={"instrument_key": key}, session=session)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("Upstox returned an invalid full-quote response.")
    matching = [quote for quote in data.values()
                if isinstance(quote, dict) and quote.get("instrument_token") == key]
    if len(matching) != 1:
        raise ValueError("Upstox did not return exactly one quote for the requested instrument.")
    return matching[0]


def fetch_upstox_shareholdings(instrument_key: str, *, session: requests.Session | None = None) -> dict:
    """Fetch all quarterly ownership categories, not promoter pledges."""
    key = _validate_key(instrument_key)
    isin = equity_isin(key)
    if isin is None:
        raise ValueError("An Indian equity instrument key containing a valid ISIN is required.")
    payload = _get(f"fundamentals/{isin}/share-holdings", session=session)
    if not isinstance(payload.get("data"), list):
        raise ValueError("Upstox returned invalid shareholding data.")
    return payload


def fetch_upstox_corporate_actions(instrument_key: str, *, session: requests.Session | None = None) -> dict:
    isin = equity_isin(instrument_key)
    if isin is None:
        raise ValueError("An Indian equity instrument key containing a valid ISIN is required.")
    payload = _get(f"fundamentals/{isin}/corporate-actions", session=session)
    if not isinstance(payload.get("data"), list):
        raise ValueError("Upstox returned invalid corporate action data.")
    return payload


def upcoming_corporate_action(payload: dict, *, today: date | None = None,
                              lookahead_days: int = 30) -> str | None:
    """Return an action in the window, prioritizing split/bonus, or None if unknown."""
    if lookahead_days < 0:
        raise ValueError("Corporate-action lookahead must be nonnegative.")
    events = payload.get("data")
    if not isinstance(events, list):
        raise ValueError("Corporate actions must contain a data list.")
    today = today or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    matches = []
    unknown = False
    for event in events:
        if not isinstance(event, dict):
            unknown = True
            continue
        name = event.get("name")
        if not isinstance(name, str):
            unknown = True
            continue
        normalized = name.strip().casefold()
        kind = {"split": "SPLIT", "stock split": "SPLIT", "bonus": "BONUS",
                "bonus issue": "BONUS", "dividend": "DIVIDEND_EX"}.get(normalized)
        if kind is None:
            continue
        raw_date = event.get("expiry_date")
        details = event.get("event_details")
        if isinstance(details, list):
            names = {"ex date", "ex-date", "ex dividend date", "ex split date", "ex bonus date"}
            for detail in details:
                if (isinstance(detail, dict) and isinstance(detail.get("name"), str)
                        and detail["name"].strip().casefold() in names):
                    raw_date = detail.get("value")
                    break
        try:
            if not isinstance(raw_date, str):
                raise ValueError("missing date")
            try:
                event_date = date.fromisoformat(raw_date.strip())
            except ValueError:
                event_date = datetime.strptime(raw_date.strip(), "%d %b %Y").date()
        except ValueError:
            unknown = True
            continue
        if today <= event_date <= today + timedelta(days=lookahead_days):
            matches.append((event_date, {"SPLIT": 0, "BONUS": 1, "DIVIDEND_EX": 2}[kind], kind))
    if matches:
        # A split/bonus in the window must not be hidden by an earlier dividend.
        protective = [item for item in matches if item[2] in {"SPLIT", "BONUS"}]
        if protective:
            return min(protective)[2]
        return None if unknown else min(matches)[2]
    return None if unknown else "NONE"


def parse_promoter_pledge(payload: dict) -> tuple[float | None, bool]:
    """Parse an explicit fractional pledge field, never promoter ownership.

    The documented shareholdings endpoint does not currently promise this
    field. Missing is unknown; malformed values use an unverified zero only.
    Even well-formed values need independent confirmation of their denominator.
    """
    if "promoter_pledged_shares_pct" not in payload:
        return None, False
    try:
        raw = payload["promoter_pledged_shares_pct"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise TypeError("pledge must be numeric")
        value = float(raw)
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("pledge must be a finite fraction")
        return value, False
    except (TypeError, ValueError, OverflowError):
        return 0.0, False


def fetch_upstox_news(instrument_key: str, *, session: requests.Session | None = None) -> tuple[list[dict], list[dict]]:
    """Retrieve every available page of the seven-day instrument news feed."""
    key = _validate_key(instrument_key)
    articles, pages = [], []
    page_number = 1
    expected_total = expected_records = None
    while True:
        payload = _get("news", params={"category": "instrument_keys", "instrument_keys": key,
                                      "page_number": page_number, "page_size": 100}, session=session)
        metadata = payload.get("metadata")
        page = metadata.get("page") if isinstance(metadata, dict) else None
        data = payload.get("data")
        if not isinstance(page, dict) or not isinstance(data, dict):
            raise ValueError("Upstox news is missing pagination metadata or instrument data.")
        total_pages, current, total_records = page.get("total_pages"), page.get("page_number"), page.get("total_records")
        if (type(total_pages) is not int or type(current) is not int or
                type(total_records) is not int or total_records < 0 or
                not 0 <= total_pages <= 100 or current != page_number):
            raise ValueError("Upstox news pagination is invalid or exceeds its documented limit.")
        if expected_total is not None and (total_pages != expected_total or total_records != expected_records):
            raise ValueError("Upstox news changed while pages were being fetched; retry.")
        expected_total, expected_records = total_pages, total_records
        entries = data.get(key, [])
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise ValueError("Upstox returned invalid instrument news entries.")
        pages.append(payload)
        for item in entries:
            raw_stamp = item.get("published_time")
            stamp = first_number(raw_stamp)
            published = None
            if raw_stamp is not None and stamp is None:
                raise ValueError("Upstox news contains an invalid publication timestamp.")
            if stamp is not None:
                try:
                    published = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).isoformat()
                except (ValueError, OverflowError, OSError) as exc:
                    raise ValueError("Upstox news contains an invalid publication timestamp.") from exc
            articles.append({
                "headline": item.get("heading"), "timestamp": published, "source": "Upstox",
                "url": item.get("article_link"), "raw_sentiment_score": None,
            })
        if page_number >= total_pages:
            break
        page_number += 1
    if len(articles) != expected_records:
        raise ValueError("Upstox news record count does not match pagination metadata.")
    return articles, pages


def fetch_upstox_volume_baseline(
    instrument_key: str, from_date: date, to_date: date, *,
    min_sessions: int = 5, session: requests.Session | None = None,
) -> tuple[dict[str, float], dict, list[str]]:
    """Build average cumulative volume by minute from complete past sessions."""
    key = _validate_key(instrument_key)
    if key.split("|", 1)[0] not in {"NSE_EQ", "BSE_EQ"}:
        raise ValueError("Intraday volume baselines require an Indian equity instrument.")
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if (from_date > to_date or (to_date - from_date).days > 31 or
            to_date >= today or min_sessions < 1):
        raise ValueError("Use a past historical window of at most 31 days and a positive session count.")
    encoded = quote(key, safe="")
    payload = _get(
        f"historical-candle/{encoded}/minutes/1/{to_date.isoformat()}/{from_date.isoformat()}",
        version="v3", session=session,
    )
    data = payload.get("data")
    candles = data.get("candles") if isinstance(data, dict) else None
    if not isinstance(candles, list):
        raise ValueError("Upstox returned invalid historical candle data.")
    zone = ZoneInfo("Asia/Kolkata")
    sessions: dict[date, dict[str, int]] = {}
    for row in candles:
        if not isinstance(row, list) or len(row) < 6:
            raise ValueError("Upstox returned a malformed historical candle.")
        try:
            stamp = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Upstox returned an invalid candle timestamp.") from exc
        volume = first_number(row[5])
        if stamp.tzinfo is None or volume is None or volume < 0 or not volume.is_integer():
            raise ValueError("Upstox returned an invalid candle timestamp or volume.")
        stamp = stamp.astimezone(zone)
        if not from_date <= stamp.date() <= to_date or stamp.second or stamp.microsecond:
            raise ValueError("Upstox returned an out-of-range or misaligned historical candle.")
        if not time(9, 15) <= stamp.time().replace(tzinfo=None) < time(15, 30):
            continue
        minute = stamp.strftime("%H:%M")
        daily = sessions.setdefault(stamp.date(), {})
        if minute in daily:
            raise ValueError("Upstox returned duplicate historical candle minutes.")
        daily[minute] = int(volume)
    start = datetime.combine(date(2000, 1, 1), time(9, 15))
    minutes = [(start + timedelta(minutes=i)).strftime("%H:%M") for i in range(375)]
    complete = [daily for daily in sessions.values() if all(minute in daily for minute in minutes)]
    if len(complete) < min_sessions:
        raise ValueError("Not enough complete historical sessions to build intraday volume norms.")
    totals = {minute: 0 for minute in minutes}
    for daily in complete:
        cumulative = 0
        for minute in minutes:
            cumulative += daily[minute]
            totals[minute] += cumulative
    warnings = []
    if len(complete) != len(sessions):
        warnings.append(f"Excluded {len(sessions) - len(complete)} incomplete historical sessions.")
    missing = sum(totals[minute] == 0 for minute in minutes)
    if missing:
        warnings.append(f"Omitted {missing} minutes with zero historical cumulative volume.")
    return {minute: totals[minute] / len(complete) for minute in minutes if totals[minute] > 0}, payload, warnings


def quote_fields(quote: dict[str, Any]) -> dict[str, float | None]:
    """Map the quote's VWAP and best bid/ask; this is not a Depth-30 feed."""
    price = first_number(quote.get("last_price"))
    vwap = first_number(quote.get("average_price"))
    depth = quote.get("depth")
    depth = depth if isinstance(depth, dict) else {}
    bids, asks = depth.get("buy"), depth.get("sell")
    bid = first_number(bids[0].get("price")) if isinstance(bids, list) and bids and isinstance(bids[0], dict) else None
    ask = first_number(asks[0].get("price")) if isinstance(asks, list) and asks and isinstance(asks[0], dict) else None
    spread = ask - bid if bid is not None and ask is not None and 0 < bid <= ask else None
    upper = first_number(quote.get("upper_circuit_limit"))
    lower = first_number(quote.get("lower_circuit_limit"))
    upper = upper if upper is not None and upper > 0 else None
    lower = lower if lower is not None and lower > 0 else None
    if upper is not None and lower is not None and lower > upper:
        upper = lower = None
    distance = ((price - lower) / lower * 100
                if price is not None and price > 0 and lower is not None else None)
    if distance is not None and not math.isfinite(distance):
        distance = None
    return {
        "current_price": price if price is not None and price > 0 else None,
        "vwap": vwap if vwap is not None and vwap > 0 else None,
        "distance_from_vwap_pct": (price / vwap - 1) * 100
        if price is not None and price > 0 and vwap is not None and vwap > 0 else None,
        "bid_ask_spread": spread,
        "upper_circuit_limit": upper,
        "lower_circuit_limit": lower,
        "distance_to_lower_circuit_pct": distance,
    }

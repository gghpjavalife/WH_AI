"""Recent public Yahoo Finance headlines for a listed company."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
import streamlit as st

from core.constants import RuntimeDefault
from features.equities.market_data import normalize_symbol

_NEWS_URL = "https://query1.finance.yahoo.com/v1/finance/search"
_NEWS_TIMEOUT = RuntimeDefault.MARKET_DATA_TIMEOUT.value
_POSITIVE_TERMS = {
    "approval",
    "beat",
    "growth",
    "gain",
    "gains",
    "improve",
    "improved",
    "launch",
    "order win",
    "profit",
    "record",
    "surge",
    "upgrade",
}
_NEGATIVE_TERMS = {
    "decline",
    "downgrade",
    "fraud",
    "investigation",
    "loss",
    "miss",
    "probe",
    "recall",
    "resign",
    "risk",
    "slump",
    "weak",
    "worse",
}


class CompanyNewsError(RuntimeError):
    """Public company headlines could not be retrieved or validated."""


def _headline_sentiment(title: str) -> int:
    lowered = title.casefold()
    positive = sum(term in lowered for term in _POSITIVE_TERMS)
    negative = sum(term in lowered for term in _NEGATIVE_TERMS)
    if positive > negative:
        return 1
    if negative > positive:
        return -1
    return 0


@st.cache_data(ttl=30 * 60, max_entries=500, show_spinner=False)
def fetch_company_news(symbol: str) -> dict[str, object]:
    """Fetch recent headlines and provide a transparent, lexicon-based context."""
    normalized = normalize_symbol(symbol)
    yahoo_symbol = (
        normalized if normalized.endswith(".BO") else f"{normalized}.NS"
    )
    try:
        response = requests.get(
            _NEWS_URL,
            params={
                "q": yahoo_symbol,
                "newsCount": 10,
                "quotesCount": 0,
            },
            headers={"User-Agent": "Mozilla/5.0 WealthHomeAI/1.0"},
            timeout=_NEWS_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise CompanyNewsError(
            f"Recent public headlines are temporarily unavailable for {normalized}."
        ) from error

    articles = payload.get("news") if isinstance(payload, dict) else None
    if not isinstance(articles, list):
        raise CompanyNewsError(
            f"Yahoo Finance returned an invalid headline response for {normalized}."
        )

    headlines: list[dict[str, object]] = []
    for article in articles[:10]:
        if not isinstance(article, dict):
            continue
        title = article.get("title")
        link = article.get("link")
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(link, str):
            continue
        parsed_link = urlsplit(link)
        if (
            parsed_link.scheme != "https"
            or parsed_link.hostname is None
            or not (
                parsed_link.hostname == "yahoo.com"
                or parsed_link.hostname.endswith(".yahoo.com")
            )
        ):
            continue
        timestamp = article.get("providerPublishTime")
        published = ""
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            try:
                published = datetime.fromtimestamp(
                    timestamp, tz=timezone.utc
                ).strftime("%d %b %Y %H:%M UTC")
            except (OverflowError, OSError, ValueError):
                published = ""
        headlines.append(
            {
                "title": title.strip(),
                "publisher": str(article.get("publisher") or "Yahoo Finance"),
                "url": link,
                "published": published,
                "sentiment": _headline_sentiment(title),
            }
        )

    return {
        "symbol": normalized,
        "source": "Yahoo Finance public headlines",
        "headlines": headlines,
    }

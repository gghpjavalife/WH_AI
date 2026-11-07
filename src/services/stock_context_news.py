"""Parse caller-fetched RSS or Atom without guessing sentiment scores."""

from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree


def parse_news_rss(content: str, *, source: str) -> list[dict]:
    """Parse every item; caller is responsible for retrieving a trusted feed."""
    if not source.strip():
        raise ValueError("A news source name is required.")
    root = ElementTree.fromstring(content)
    entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    result = []
    for entry in entries:
        def text(name: str) -> str | None:
            node = entry.find(name)
            if node is None:
                node = entry.find(f"{{http://www.w3.org/2005/Atom}}{name}")
            return "".join(node.itertext()).strip() if node is not None else None
        link = text("link")
        if not link:
            element = entry.find("{http://www.w3.org/2005/Atom}link")
            link = element.get("href") if element is not None else None
        raw_date = text("pubDate") or text("published") or text("updated")
        timestamp = None
        if raw_date:
            try:
                timestamp = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError):
                try:
                    timestamp = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("News feed contains an invalid publication timestamp.") from exc
        result.append({
            "headline": text("title"), "timestamp": timestamp.isoformat() if timestamp else None,
            "source": source, "url": link, "raw_sentiment_score": None,
        })
    return result

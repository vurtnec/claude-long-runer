"""RSS and news search collectors."""

from __future__ import annotations

import hashlib
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from .base import CollectorResult, HttpClient, NewsItem, compact_error, env


OFFICIAL_RSS_FEEDS = {
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
    "BLS": "https://www.bls.gov/feed/news_release/all.rss",
    "BEA": "https://www.bea.gov/news/rss.xml",
    "Treasury": "https://home.treasury.gov/news/press-releases/rss.xml",
    "EIA": "https://www.eia.gov/rss/todayinenergy.xml",
    "SEC": "https://www.sec.gov/news/pressreleases.rss",
}


DEFAULT_NEWS_QUERIES = [
    "Federal Reserve inflation Treasury yields",
    "S&P 500 market volatility",
    "gold dollar real yields central bank",
    "oil supply EIA inventory geopolitical",
    "Nvidia Microsoft Apple AI earnings",
]


class OfficialRssCollector:
    name = "official_rss"

    def __init__(self, client: Optional[HttpClient] = None, max_items_per_feed: int = 5):
        self.client = client or HttpClient()
        self.max_items_per_feed = max_items_per_feed

    def collect(self) -> CollectorResult:
        result = CollectorResult(name=self.name)
        for publisher, url in OFFICIAL_RSS_FEEDS.items():
            try:
                result.news_items.extend(self._fetch_feed(publisher, url))
            except Exception as exc:
                result.warnings.append(f"{publisher}: {compact_error(exc)}")
        if not result.news_items:
            result.mark_error("No official RSS items collected")
        return result

    def _fetch_feed(self, publisher: str, url: str) -> List[NewsItem]:
        text = self.client.get_text(url)
        root = ET.fromstring(text)
        items = []
        nodes = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]

        for node in nodes[: self.max_items_per_feed]:
            title = _first_text(node, ["title"])
            link = _first_link(node)
            published = _first_text(node, ["pubDate", "published", "updated"])
            summary = _first_text(node, ["description", "summary"])
            if not title:
                continue
            items.append(
                NewsItem(
                    title=title,
                    url=link or url,
                    source=publisher,
                    published_at=published,
                    category="official_rss",
                    summary=_trim(summary, 280),
                    source_rank=5,
                    matched_topics=["official_release"],
                    confidence="official",
                    extra={"feed_url": url},
                )
            )
        return items


class GdeltNewsCollector:
    name = "gdelt_news"

    def __init__(
        self,
        client: Optional[HttpClient] = None,
        queries: Optional[Iterable[str]] = None,
        max_records_per_query: int = 5,
        throttle_seconds: float = 5.2,
    ):
        self.client = client or HttpClient()
        self.queries = list(queries or DEFAULT_NEWS_QUERIES)
        self.max_records_per_query = max_records_per_query
        self.throttle_seconds = throttle_seconds

    def collect(self) -> CollectorResult:
        result = CollectorResult(name=self.name)
        seen = set()
        for index, query in enumerate(self.queries):
            if index:
                time.sleep(self.throttle_seconds)
            try:
                items = self._search(query)
                for item in items:
                    key = item.extra.get("dedupe_key") or item.url
                    if key in seen:
                        continue
                    seen.add(key)
                    result.news_items.append(item)
            except Exception as exc:
                result.warnings.append(f"{query}: {compact_error(exc)}")

        if not result.news_items:
            result.mark_error("No GDELT items collected")
        return result

    def _search(self, query: str) -> List[NewsItem]:
        url = "https://api.gdeltproject.org/api/v2/doc/doc"
        response = self.client.get(
            url,
            params={
                "query": query,
                "mode": "ArtList",
                "format": "json",
                "maxrecords": self.max_records_per_query,
                "timespan": "24h",
                "sourcelang": "English",
            },
        )
        if response.status_code == 429:
            raise RuntimeError("GDELT rate limited this query")
        payload = response.json()
        articles = payload.get("articles") or []
        items = []
        for article in articles:
            title = article.get("title", "")
            url_value = article.get("url", "")
            if not title or not url_value:
                continue
            items.append(
                NewsItem(
                    title=title,
                    url=url_value,
                    source=article.get("domain") or "GDELT",
                    published_at=article.get("seendate", ""),
                    category="news_search",
                    summary="",
                    source_rank=2,
                    matched_topics=[query],
                    confidence="aggregated",
                    extra={
                        "provider": "gdelt",
                        "query": query,
                        "source_country": article.get("sourcecountry"),
                        "language": article.get("language"),
                        "dedupe_key": _dedupe_key(title, url_value),
                    },
                )
            )
        return items


class GoogleNewsRssCollector:
    name = "google_news_rss"

    def __init__(
        self,
        client: Optional[HttpClient] = None,
        queries: Optional[Iterable[str]] = None,
        max_items_per_query: int = 5,
    ):
        self.client = client or HttpClient()
        self.queries = list(queries or DEFAULT_NEWS_QUERIES)
        self.max_items_per_query = max_items_per_query

    def collect(self) -> CollectorResult:
        result = CollectorResult(name=self.name)
        seen = set()
        for query in self.queries:
            try:
                for item in self._search(query):
                    key = item.extra.get("dedupe_key") or item.url
                    if key in seen:
                        continue
                    seen.add(key)
                    result.news_items.append(item)
            except Exception as exc:
                result.warnings.append(f"{query}: {compact_error(exc)}")
        if not result.news_items:
            result.mark_error("No Google News RSS items collected")
        return result

    def _search(self, query: str) -> List[NewsItem]:
        encoded = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        text = self.client.get_text(url)
        root = ET.fromstring(text)
        items = []
        for node in [n for n in root.iter() if _local_name(n.tag) == "item"][: self.max_items_per_query]:
            title = _first_text(node, ["title"])
            link = _first_text(node, ["link"])
            published = _first_text(node, ["pubDate"])
            if not title:
                continue
            items.append(
                NewsItem(
                    title=title,
                    url=link or url,
                    source="Google News RSS",
                    published_at=published,
                    category="news_search",
                    summary="",
                    source_rank=2,
                    matched_topics=[query],
                    confidence="fallback",
                    extra={"provider": "google_news_rss", "query": query, "dedupe_key": _dedupe_key(title, link)},
                )
            )
        return items


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _first_text(node: ET.Element, names: Iterable[str]) -> str:
    wanted = set(names)
    for child in node.iter():
        if _local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def _first_link(node: ET.Element) -> str:
    for child in node.iter():
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href:
            return href
        if child.text:
            return child.text.strip()
    return ""


def _trim(text: str, max_len: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _dedupe_key(title: str, url: str) -> str:
    raw = f"{title.strip().lower()}|{url.strip().lower()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

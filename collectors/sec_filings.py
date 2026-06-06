"""SEC filing watchlist collector."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from .base import CollectorResult, HttpClient, NewsItem, compact_error, default_user_agent


SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_doc}"


DEFAULT_SEC_WATCHLIST = {
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "NVDA": "0001045810",
    "AMZN": "0001018724",
    "GOOGL": "0001652044",
    "META": "0001326801",
    "AVGO": "0001730168",
    "TSLA": "0001318605",
}


IMPORTANT_FORMS = {"8-K", "10-K", "10-Q", "6-K", "20-F", "DEF 14A", "4"}


class SecFilingsCollector:
    name = "sec_filings"

    def __init__(
        self,
        client: Optional[HttpClient] = None,
        watchlist: Optional[Dict[str, str]] = None,
        lookback_days: int = 7,
        max_per_company: int = 5,
    ):
        self.client = client or HttpClient(user_agent=default_user_agent())
        self.watchlist = watchlist or DEFAULT_SEC_WATCHLIST
        self.lookback_days = lookback_days
        self.max_per_company = max_per_company

    def collect(self) -> CollectorResult:
        result = CollectorResult(name=self.name)
        for ticker, cik in self.watchlist.items():
            try:
                result.news_items.extend(self._fetch_company(ticker, cik))
            except Exception as exc:
                result.warnings.append(f"{ticker}: {compact_error(exc)}")

        result.meta["watchlist"] = sorted(self.watchlist.keys())
        if not result.news_items:
            result.warnings.append("No recent SEC filings matched the watchlist")
        return result

    def _fetch_company(self, ticker: str, cik: str) -> List[NewsItem]:
        padded = cik.zfill(10)
        payload = self.client.get_json(SEC_SUBMISSIONS_URL.format(cik=padded))
        recent = payload.get("filings", {}).get("recent", {})
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        accession_numbers = recent.get("accessionNumber") or []
        primary_docs = recent.get("primaryDocument") or []
        company_name = payload.get("name") or ticker
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=self.lookback_days)
        items: List[NewsItem] = []

        for index, form in enumerate(forms[:50]):
            if form not in IMPORTANT_FORMS:
                continue
            filing_date = dates[index] if index < len(dates) else ""
            try:
                if filing_date and datetime.fromisoformat(filing_date).date() < cutoff:
                    continue
            except ValueError:
                pass
            accession = accession_numbers[index] if index < len(accession_numbers) else ""
            primary_doc = primary_docs[index] if index < len(primary_docs) else ""
            url = ""
            if accession and primary_doc:
                url = SEC_ARCHIVES_URL.format(
                    cik_int=str(int(cik)),
                    accession_no_dashes=accession.replace("-", ""),
                    primary_doc=primary_doc,
                )
            items.append(
                NewsItem(
                    title=f"{ticker} filed {form}",
                    url=url or SEC_SUBMISSIONS_URL.format(cik=padded),
                    source="SEC EDGAR",
                    published_at=filing_date,
                    category="sec_filing",
                    summary=f"{company_name} filed {form} on {filing_date}.",
                    source_rank=5,
                    matched_topics=["sec", "company_filings", form],
                    asset_relevance=[ticker, "VOO", "SPY"],
                    confidence="official",
                    extra={"ticker": ticker, "cik": padded, "form": form, "accession": accession},
                )
            )
            if len(items) >= self.max_per_company:
                break
        return items

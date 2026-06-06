"""ETF, stock, and proxy market price collection."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .base import (
    CollectorResult,
    DataPoint,
    HttpClient,
    absolute_change,
    compact_error,
    env,
    pct_change,
    safe_float,
    utc_now_iso,
)


DEFAULT_SYMBOLS = [
    # US indices and index ETF proxies
    "^GSPC",
    "^DJI",
    "^IXIC",
    "SPY",
    "VOO",
    "QQQ",
    "RSP",
    "DIA",
    # HK indices and ETF proxies
    "^HSI",
    "3032.HK",
    # Mag 7 / AI leaders
    "NVDA",
    "MSFT",
    "GOOGL",
    "AMZN",
    "META",
    "AAPL",
    "TSLA",
    # Semiconductors
    "AMD",
    "AVGO",
    "MU",
    "ARM",
    # US ETFs / sectors
    "SMH",
    "SOXX",
    "BOTZ",
    "GLD",
    "TLT",
    "XLE",
    "XLF",
    "XLK",
    "XLV",
    "XLU",
    "XLY",
    "XLP",
    "SCHD",
    "VT",
    # Commodities and macro proxies
    "GC=F",
    "CL=F",
    "BZ=F",
    "SI=F",
    "^VIX",
    "DX-Y.NYB",
    "^TNX",
    "^FVX",
    "^TYX",
    # FX
    "CNH=X",
    "HKD=X",
    "JPY=X",
    # HK stocks and ETFs
    "0941.HK",
    "0883.HK",
    "1211.HK",
    "0700.HK",
    "9988.HK",
    "1810.HK",
    "3110.HK",
    "2800.HK",
    "3466.HK",
]


class MarketPriceCollector:
    name = "market_prices"

    def __init__(
        self,
        client: Optional[HttpClient] = None,
        symbols: Optional[Iterable[str]] = None,
        max_alpha_symbols: int = 5,
    ):
        self.client = client or HttpClient()
        self.symbols = [s.strip().upper() for s in (symbols or DEFAULT_SYMBOLS) if s.strip()]
        self.max_alpha_symbols = max_alpha_symbols
        self.alpha_key = env("ALPHA_VANTAGE_API_KEY")

    def collect(self) -> CollectorResult:
        result = CollectorResult(name=self.name)
        if not self.alpha_key or self.max_alpha_symbols <= 0:
            return self._collect_yahoo_parallel(result)

        used_alpha = 0

        for symbol in self.symbols:
            point = None
            alpha_error = ""

            if self.alpha_key and used_alpha < self.max_alpha_symbols:
                try:
                    point = self._from_alpha_vantage(symbol)
                    used_alpha += 1
                except Exception as exc:
                    alpha_error = compact_error(exc)

            if point is None:
                try:
                    point = self._from_yahoo_chart(symbol, alpha_error=alpha_error)
                except Exception as exc:
                    msg = f"{symbol}: {compact_error(exc)}"
                    if alpha_error:
                        msg = f"{symbol}: alpha failed ({alpha_error}); yahoo failed ({compact_error(exc)})"
                    result.warnings.append(msg)
                    continue

            result.data_points.append(point)

        if not result.data_points:
            result.mark_error("No market price data collected")

        result.meta["symbols_requested"] = self.symbols
        result.meta["alpha_symbols_attempted"] = used_alpha
        return result

    def _collect_yahoo_parallel(self, result: CollectorResult) -> CollectorResult:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(self._from_yahoo_chart_threadsafe, symbol): symbol
                for symbol in self.symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    result.data_points.append(future.result())
                except Exception as exc:
                    result.warnings.append(f"{symbol}: {compact_error(exc)}")

        result.data_points.sort(key=lambda point: point.symbol_or_series)
        if not result.data_points:
            result.mark_error("No market price data collected")
        result.meta["symbols_requested"] = self.symbols
        result.meta["alpha_symbols_attempted"] = 0
        result.meta["parallel_provider"] = "yahoo_chart"
        return result

    def _from_yahoo_chart_threadsafe(self, symbol: str) -> DataPoint:
        last_error = None
        for attempt in range(3):
            try:
                return self._from_yahoo_chart(
                    symbol,
                    client=HttpClient(timeout=8 + attempt * 4),
                )
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise last_error

    def _from_alpha_vantage(self, symbol: str) -> Optional[DataPoint]:
        url = "https://www.alphavantage.co/query"
        payload = self.client.get_json(
            url,
            params={
                "function": "TIME_SERIES_DAILY",
                "symbol": symbol,
                "apikey": self.alpha_key,
            },
        )
        if "Note" in payload:
            raise RuntimeError(payload["Note"])
        if "Information" in payload:
            raise RuntimeError(payload["Information"])
        if "Error Message" in payload:
            raise RuntimeError(payload["Error Message"])

        series = payload.get("Time Series (Daily)") or {}
        rows = []
        for date_text, row in series.items():
            close = safe_float(row.get("4. close"))
            if close is None:
                continue
            rows.append((date_text, close, safe_float(row.get("5. volume"))))
        rows.sort(key=lambda item: item[0])
        if not rows:
            raise RuntimeError("Alpha Vantage returned no daily rows")

        latest = rows[-1]
        previous = rows[-2] if len(rows) >= 2 else (None, None, None)
        return DataPoint(
            as_of=latest[0],
            source="alpha_vantage",
            source_url="https://www.alphavantage.co/documentation/",
            category="market_price",
            symbol_or_series=symbol,
            name=f"{symbol} daily close",
            value=latest[1],
            previous_value=previous[1],
            delta=absolute_change(latest[1], previous[1]),
            delta_pct=pct_change(latest[1], previous[1]),
            unit="USD",
            freshness="latest_available",
            confidence="documented_api",
            extra={"volume": latest[2], "provider": "alpha_vantage"},
        )

    def _from_yahoo_chart(
        self, symbol: str, alpha_error: str = "", client: Optional[HttpClient] = None
    ) -> DataPoint:
        client = client or self.client
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        payload = client.get_json(
            url,
            params={
                "range": "1mo",
                "interval": "1d",
                "includePrePost": "false",
                "events": "history",
            },
            headers={"User-Agent": "Mozilla/5.0"},
        )
        chart = payload.get("chart", {})
        error = chart.get("error")
        if error:
            raise RuntimeError(error)
        results = chart.get("result") or []
        if not results:
            raise RuntimeError("Yahoo chart returned no results")

        item = results[0]
        meta = item.get("meta", {})
        timestamps = item.get("timestamp") or []
        quote = (item.get("indicators", {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        rows: List[Tuple[str, float, Optional[float]]] = []
        for index, ts in enumerate(timestamps):
            close = safe_float(closes[index] if index < len(closes) else None)
            if close is None:
                continue
            date_text = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            volume = safe_float(volumes[index] if index < len(volumes) else None)
            rows.append((date_text, close, volume))
        if not rows:
            price = safe_float(meta.get("regularMarketPrice"))
            previous = safe_float(meta.get("chartPreviousClose") or meta.get("previousClose"))
            if price is None:
                raise RuntimeError("Yahoo chart returned no close prices")
            date_text = utc_now_iso()
            rows.append((date_text, price, safe_float(meta.get("regularMarketVolume"))))
            rows.insert(0, ("previous", previous, None))

        latest = rows[-1]
        previous = rows[-2] if len(rows) >= 2 else (None, None, None)
        notes = "Non-official fallback provider."
        if alpha_error:
            notes = f"Alpha Vantage unavailable; {notes}"
        return DataPoint(
            as_of=latest[0],
            source="yahoo_chart",
            source_url=url,
            category="market_price",
            symbol_or_series=symbol,
            name=meta.get("longName") or f"{symbol} daily close",
            value=latest[1],
            previous_value=previous[1],
            delta=absolute_change(latest[1], previous[1]),
            delta_pct=pct_change(latest[1], previous[1]),
            unit=meta.get("currency", "USD"),
            freshness="latest_available",
            confidence="fallback",
            notes=notes,
            extra={
                "volume": latest[2],
                "provider": "yahoo_chart",
                "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
                "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
                "exchange": meta.get("exchangeName"),
            },
        )

"""Cboe volatility index CSV collectors."""

from __future__ import annotations

import csv
from io import StringIO
from typing import Dict, Optional

from .base import (
    CollectorResult,
    DataPoint,
    HttpClient,
    absolute_change,
    compact_error,
    pct_change,
    safe_float,
)


CBOE_SERIES = {
    "VIX": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "VVIX": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv",
    "GVZ": "https://cdn.cboe.com/api/global/us_indices/daily_prices/GVZ_History.csv",
    "OVX": "https://cdn.cboe.com/api/global/us_indices/daily_prices/OVX_History.csv",
}


class CboeVolatilityCollector:
    name = "cboe_volatility"

    def __init__(self, client: Optional[HttpClient] = None):
        self.client = client or HttpClient()

    def collect(self) -> CollectorResult:
        result = CollectorResult(name=self.name)
        for symbol, url in CBOE_SERIES.items():
            try:
                point = self._fetch_series(symbol, url)
                if point:
                    result.data_points.append(point)
            except Exception as exc:
                result.warnings.append(f"{symbol}: {compact_error(exc)}")

        if not result.data_points:
            result.mark_error("No Cboe volatility data collected")
        return result

    def _fetch_series(self, symbol: str, url: str) -> Optional[DataPoint]:
        text = self.client.get_text(url)
        rows = list(csv.DictReader(StringIO(text)))
        value_column = "CLOSE"
        if rows and value_column not in rows[0] and symbol in rows[0]:
            value_column = symbol
        rows = [row for row in rows if safe_float(row.get(value_column)) is not None]
        if not rows:
            return None
        latest = rows[-1]
        previous = rows[-2] if len(rows) >= 2 else {}
        current = safe_float(latest.get(value_column))
        prev = safe_float(previous.get(value_column))
        return DataPoint(
            as_of=latest.get("DATE", ""),
            source="cboe",
            source_url=url,
            category="volatility",
            symbol_or_series=symbol,
            name=f"Cboe {symbol} volatility index close",
            value=current,
            previous_value=prev,
            delta=absolute_change(current, prev),
            delta_pct=pct_change(current, prev),
            unit="index_points",
            freshness="same_day_or_latest",
            confidence="official",
            extra={
                "open": safe_float(latest.get("OPEN")),
                "high": safe_float(latest.get("HIGH")),
                "low": safe_float(latest.get("LOW")),
            },
        )

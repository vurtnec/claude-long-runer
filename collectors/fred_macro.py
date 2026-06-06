"""FRED macro time series collector."""

from __future__ import annotations

from typing import Dict, Optional

from .base import (
    CollectorResult,
    DataPoint,
    HttpClient,
    absolute_change,
    compact_error,
    env,
    pct_change,
    safe_float,
)


FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


FRED_SERIES = {
    "DGS2": ("2Y Treasury yield", "rates", "percent"),
    "DGS10": ("10Y Treasury yield", "rates", "percent"),
    "DGS30": ("30Y Treasury yield", "rates", "percent"),
    "T10Y2Y": ("10Y minus 2Y Treasury spread", "rates", "percentage_points"),
    "DFII10": ("10Y TIPS real yield", "real_rates", "percent"),
    "T5YIE": ("5Y breakeven inflation rate", "inflation_expectations", "percent"),
    "BAMLC0A0CM": ("US corporate bond OAS", "credit", "percent"),
    "BAMLH0A0HYM2": ("US high yield OAS", "credit", "percent"),
    "DFF": ("Effective federal funds rate", "policy_rates", "percent"),
    "WALCL": ("Federal Reserve total assets", "liquidity", "millions_usd"),
    "DTWEXBGS": ("Nominal broad dollar index", "fx", "index"),
    "VIXCLS": ("VIX close via FRED", "volatility", "index_points"),
}


class FredMacroCollector:
    name = "fred_macro"

    def __init__(self, client: Optional[HttpClient] = None):
        self.client = client or HttpClient()
        self.api_key = env("FRED_API_KEY")

    def collect(self) -> CollectorResult:
        result = CollectorResult(name=self.name)
        if not self.api_key:
            result.mark_error("FRED_API_KEY is not configured")
            return result

        for series_id, (name, category, unit) in FRED_SERIES.items():
            try:
                point = self._fetch_series(series_id, name, category, unit)
                if point:
                    result.data_points.append(point)
            except Exception as exc:
                result.warnings.append(f"{series_id}: {compact_error(exc)}")

        if not result.data_points:
            result.mark_error("No FRED data collected")
        return result

    def _fetch_series(
        self, series_id: str, name: str, category: str, unit: str
    ) -> Optional[DataPoint]:
        payload = self.client.get_json(
            FRED_URL,
            params={
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 10,
            },
        )
        observations = payload.get("observations") or []
        values = []
        for row in observations:
            value = safe_float(row.get("value"))
            if value is not None:
                values.append((row.get("date", ""), value))
        if not values:
            return None
        latest = values[0]
        previous = values[1] if len(values) >= 2 else ("", None)
        return DataPoint(
            as_of=latest[0],
            source="fred",
            source_url=f"https://fred.stlouisfed.org/series/{series_id}",
            category=category,
            symbol_or_series=series_id,
            name=name,
            value=latest[1],
            previous_value=previous[1],
            delta=absolute_change(latest[1], previous[1]),
            delta_pct=pct_change(latest[1], previous[1]),
            unit=unit,
            freshness="latest_available",
            confidence="official",
        )

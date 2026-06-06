"""EIA energy data collector."""

from __future__ import annotations

from typing import Optional

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


EIA_SERIES = {
    "PET.RWTC.D": ("WTI crude oil spot price", "oil_price", "usd_per_barrel"),
    "PET.RBRTE.D": ("Brent crude oil spot price", "oil_price", "usd_per_barrel"),
    "NG.RNGWHHD.D": ("Henry Hub natural gas spot price", "gas_price", "usd_per_mmbtu"),
}


class EiaEnergyCollector:
    name = "eia_energy"

    def __init__(self, client: Optional[HttpClient] = None):
        self.client = client or HttpClient()
        self.api_key = env("EIA_API_KEY")

    def collect(self) -> CollectorResult:
        result = CollectorResult(name=self.name)
        if not self.api_key:
            result.mark_error("EIA_API_KEY is not configured")
            return result

        for series_id, (name, category, unit) in EIA_SERIES.items():
            try:
                point = self._fetch_series(series_id, name, category, unit)
                if point:
                    result.data_points.append(point)
            except Exception as exc:
                result.warnings.append(f"{series_id}: {compact_error(exc)}")

        if not result.data_points:
            result.mark_error("No EIA data collected")
        return result

    def _fetch_series(
        self, series_id: str, name: str, category: str, unit: str
    ) -> Optional[DataPoint]:
        url = f"https://api.eia.gov/v2/seriesid/{series_id}"
        payload = self.client.get_json(
            url,
            params={
                "api_key": self.api_key,
                "sort[0][column]": "period",
                "sort[0][direction]": "desc",
                "length": 5,
            },
        )
        rows = payload.get("response", {}).get("data") or []
        values = []
        for row in rows:
            value = safe_float(row.get("value"))
            if value is not None:
                values.append((row.get("period", ""), value))
        if not values:
            return None
        latest = values[0]
        previous = values[1] if len(values) >= 2 else ("", None)
        return DataPoint(
            as_of=latest[0],
            source="eia",
            source_url=url,
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

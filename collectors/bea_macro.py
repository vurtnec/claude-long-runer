"""BEA macro data collector for a small set of high-signal NIPA tables."""

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


BEA_URL = "https://apps.bea.gov/api/data/"


BEA_SERIES = [
    {
        "symbol": "BEA_REAL_GDP_QOQ",
        "name": "Real GDP percent change from preceding period",
        "category": "growth",
        "unit": "annualized_percent",
        "table": "T10101",
        "frequency": "Q",
        "line_number": "1",
    },
    {
        "symbol": "BEA_PCE_PRICE_INDEX",
        "name": "PCE price index",
        "category": "inflation",
        "unit": "index",
        "table": "T20804",
        "frequency": "M",
        "line_number": "1",
    },
    {
        "symbol": "BEA_CORE_PCE_PRICE_INDEX",
        "name": "PCE price index excluding food and energy",
        "category": "inflation",
        "unit": "index",
        "table": "T20804",
        "frequency": "M",
        "line_number": "22",
    },
]


class BeaMacroCollector:
    name = "bea_macro"

    def __init__(self, client: Optional[HttpClient] = None):
        self.client = client or HttpClient()
        self.api_key = env("BEA_API_KEY")

    def collect(self) -> CollectorResult:
        result = CollectorResult(name=self.name)
        if not self.api_key:
            result.mark_error("BEA_API_KEY is not configured")
            return result

        for config in BEA_SERIES:
            try:
                point = self._fetch_series(config)
                if point:
                    result.data_points.append(point)
            except Exception as exc:
                result.warnings.append(f"{config['symbol']}: {compact_error(exc)}")

        if not result.data_points:
            result.mark_error("No BEA data collected")
        return result

    def _fetch_series(self, config: Dict[str, str]) -> Optional[DataPoint]:
        payload = self.client.get_json(
            BEA_URL,
            params={
                "UserID": self.api_key,
                "method": "GetData",
                "datasetname": "NIPA",
                "TableName": config["table"],
                "Frequency": config["frequency"],
                "Year": "X",
                "LineNumber": config["line_number"],
                "ResultFormat": "JSON",
            },
        )
        bea_api = payload.get("BEAAPI", {})
        self._raise_for_error(bea_api)
        rows = bea_api.get("Results", {}).get("Data") or []
        values = []
        for row in rows:
            value = safe_float(row.get("DataValue"))
            period = row.get("TimePeriod", "")
            if value is not None and period:
                values.append((period, value, row))
        values.sort(key=lambda item: item[0])
        if not values:
            return None
        latest = values[-1]
        previous = values[-2] if len(values) >= 2 else ("", None, {})
        return DataPoint(
            as_of=latest[0],
            source="bea",
            source_url=BEA_URL,
            category=config["category"],
            symbol_or_series=config["symbol"],
            name=config["name"],
            value=latest[1],
            previous_value=previous[1],
            delta=absolute_change(latest[1], previous[1]),
            delta_pct=pct_change(latest[1], previous[1]),
            unit=config["unit"],
            freshness="latest_available",
            confidence="official",
            extra={
                "table": config["table"],
                "line_number": config["line_number"],
                "frequency": config["frequency"],
                "line_description": latest[2].get("LineDescription", ""),
            },
        )

    @staticmethod
    def _raise_for_error(bea_api: Dict) -> None:
        error = bea_api.get("Error") or bea_api.get("Results", {}).get("Error")
        if not error:
            return
        if isinstance(error, dict):
            code = error.get("APIErrorCode", "")
            description = error.get("APIErrorDescription", "")
            raise RuntimeError(f"BEA API error {code}: {description}")
        raise RuntimeError(f"BEA API error: {error}")

"""U.S. Treasury nominal and real yield curve collectors."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, List, Optional

from .base import (
    CollectorResult,
    DataPoint,
    HttpClient,
    absolute_change,
    compact_error,
    pct_change,
    safe_float,
)


TREASURY_XML_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"


NOMINAL_FIELDS = {
    "BC_2YEAR": ("2Y Treasury yield", "2Y"),
    "BC_10YEAR": ("10Y Treasury yield", "10Y"),
    "BC_30YEAR": ("30Y Treasury yield", "30Y"),
}


REAL_FIELDS = {
    "TC_5YEAR": ("5Y real Treasury yield", "5Y_REAL"),
    "TC_10YEAR": ("10Y real Treasury yield", "10Y_REAL"),
    "TC_30YEAR": ("30Y real Treasury yield", "30Y_REAL"),
}


class TreasuryRatesCollector:
    name = "treasury_rates"

    def __init__(self, client: Optional[HttpClient] = None):
        self.client = client or HttpClient()

    def collect(self) -> CollectorResult:
        result = CollectorResult(name=self.name)

        try:
            nominal_rows = self._fetch_rows("daily_treasury_yield_curve")
            result.data_points.extend(
                self._rows_to_points(
                    nominal_rows,
                    NOMINAL_FIELDS,
                    category="rates",
                    source_url=f"{TREASURY_XML_URL}?data=daily_treasury_yield_curve",
                )
            )
            curve_point = self._curve_spread_point(nominal_rows)
            if curve_point:
                result.data_points.append(curve_point)
        except Exception as exc:
            result.warnings.append(f"nominal rates: {compact_error(exc)}")

        try:
            real_rows = self._fetch_rows("daily_treasury_real_yield_curve")
            result.data_points.extend(
                self._rows_to_points(
                    real_rows,
                    REAL_FIELDS,
                    category="real_rates",
                    source_url=f"{TREASURY_XML_URL}?data=daily_treasury_real_yield_curve",
                )
            )
        except Exception as exc:
            result.warnings.append(f"real rates: {compact_error(exc)}")

        if not result.data_points:
            result.mark_error("No Treasury rate data collected")
        return result

    def _fetch_rows(self, data_key: str) -> List[Dict[str, str]]:
        year = datetime.now().year
        xml_text = self.client.get_text(
            TREASURY_XML_URL,
            params={"data": data_key, "field_tdr_date_value": str(year)},
        )
        root = ET.fromstring(xml_text)
        rows: List[Dict[str, str]] = []
        for entry in root.iter():
            if self._local_name(entry.tag) != "entry":
                continue
            row: Dict[str, str] = {}
            for child in entry.iter():
                tag = self._local_name(child.tag)
                text = child.text.strip() if child.text else ""
                if text:
                    row[tag] = text
            if "NEW_DATE" in row:
                rows.append(row)
        rows.sort(key=lambda row: row.get("NEW_DATE", ""))
        return rows

    def _rows_to_points(
        self,
        rows: List[Dict[str, str]],
        field_map: Dict[str, tuple[str, str]],
        category: str,
        source_url: str,
    ) -> List[DataPoint]:
        if not rows:
            return []
        latest = rows[-1]
        previous = rows[-2] if len(rows) >= 2 else {}
        points = []
        as_of = latest.get("NEW_DATE", "")

        for field, (name, symbol) in field_map.items():
            current = safe_float(latest.get(field))
            if current is None:
                continue
            prev = safe_float(previous.get(field))
            points.append(
                DataPoint(
                    as_of=as_of,
                    source="treasury",
                    source_url=source_url,
                    category=category,
                    symbol_or_series=symbol,
                    name=name,
                    value=current,
                    previous_value=prev,
                    delta=absolute_change(current, prev),
                    delta_pct=pct_change(current, prev),
                    unit="percent",
                    freshness="same_day_or_latest",
                    confidence="official",
                )
            )
        return points

    def _curve_spread_point(self, rows: List[Dict[str, str]]) -> Optional[DataPoint]:
        if not rows:
            return None
        latest = rows[-1]
        previous = rows[-2] if len(rows) >= 2 else {}
        latest_10 = safe_float(latest.get("BC_10YEAR"))
        latest_2 = safe_float(latest.get("BC_2YEAR"))
        prev_10 = safe_float(previous.get("BC_10YEAR"))
        prev_2 = safe_float(previous.get("BC_2YEAR"))
        if latest_10 is None or latest_2 is None:
            return None
        current = latest_10 - latest_2
        prev = (prev_10 - prev_2) if prev_10 is not None and prev_2 is not None else None
        return DataPoint(
            as_of=latest.get("NEW_DATE", ""),
            source="treasury",
            source_url=f"{TREASURY_XML_URL}?data=daily_treasury_yield_curve",
            category="rates",
            symbol_or_series="10Y_MINUS_2Y",
            name="10Y minus 2Y Treasury yield spread",
            value=current,
            previous_value=prev,
            delta=absolute_change(current, prev),
            delta_pct=None,
            unit="percentage_points",
            freshness="same_day_or_latest",
            confidence="official",
        )

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.split("}", 1)[-1]

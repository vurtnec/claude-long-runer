"""Market data freshness and trading-day checks."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from .base import utc_now_iso


MARKET_SPECS = {
    "us_equity": {
        "timezone": "America/New_York",
        "close_time": time(17, 0),
    },
    "hk_equity": {
        "timezone": "Asia/Hong_Kong",
        "close_time": time(17, 0),
    },
    "fx": {
        "timezone": "America/New_York",
        "close_time": time(17, 0),
    },
    "commodity_future": {
        "timezone": "America/New_York",
        "close_time": time(17, 0),
    },
}


class FreshnessChecker:
    """Check whether market-price rows are current for the expected session."""

    def check(self, data_points: Iterable[Any], generated_at: Optional[str] = None) -> Dict[str, Any]:
        now = _parse_generated_at(generated_at)
        items: List[Dict[str, Any]] = []
        counts = {"pass": 0, "warn": 0, "fail": 0, "unvalidated": 0, "skipped": 0}

        for point in data_points:
            if _point_value(point, "category") != "market_price":
                counts["skipped"] += 1
                continue

            item = self._check_market_price(point, now)
            counts[item["status"]] += 1
            items.append(item)

        checked = counts["pass"] + counts["warn"] + counts["fail"] + counts["unvalidated"]
        return {
            "generated_at": utc_now_iso(),
            "method": "market-specific weekday freshness check",
            "summary": {
                **counts,
                "checked": checked,
                "total_market_prices": len(items),
            },
            "items": items,
            "rules": [
                "pass means as_of matches the expected latest weekday session for that market.",
                "warn means the row is one weekday behind or dated ahead of the expected session.",
                "fail means the row is two or more weekdays behind the expected session.",
                "unvalidated means the as_of value could not be parsed.",
                "This lightweight check does not yet include exchange holiday calendars.",
            ],
        }

    def _check_market_price(self, point: Any, now: datetime) -> Dict[str, Any]:
        symbol = _point_value(point, "symbol_or_series")
        market = _market_for_symbol(symbol)
        expected = _expected_last_session(now, market)
        as_of_text = str(_point_value(point, "as_of") or "")
        as_of = _parse_as_of_date(as_of_text)
        reasons: List[str] = []

        if as_of is None:
            return {
                "symbol": symbol,
                "name": _point_value(point, "name"),
                "market": market,
                "status": "unvalidated",
                "as_of": as_of_text,
                "expected_last_session": expected.isoformat(),
                "business_day_lag": None,
                "reasons": ["unparseable_as_of"],
            }

        lag = _business_day_lag(as_of, expected)
        if lag < 0:
            status = "warn"
            reasons.append("as_of_after_expected_session")
        elif lag == 0:
            status = "pass"
        elif lag == 1:
            status = "warn"
            reasons.append("one_business_day_stale")
        else:
            status = "fail"
            reasons.append("two_or_more_business_days_stale")

        return {
            "symbol": symbol,
            "name": _point_value(point, "name"),
            "market": market,
            "status": status,
            "as_of": as_of.isoformat(),
            "expected_last_session": expected.isoformat(),
            "business_day_lag": lag,
            "reasons": reasons,
        }


def render_freshness_markdown(data_quality: Dict[str, Any]) -> str:
    freshness = data_quality.get("freshness") or data_quality
    summary = freshness.get("summary", {})
    lines = [
        "## Freshness",
        "",
        f"- Generated at: {freshness.get('generated_at', '')}",
        f"- Method: {freshness.get('method', '')}",
        "- Summary: pass={pass_count}, warn={warn}, fail={fail}, unvalidated={unvalidated}, checked={checked}, total_market_prices={total}".format(
            pass_count=summary.get("pass", 0),
            warn=summary.get("warn", 0),
            fail=summary.get("fail", 0),
            unvalidated=summary.get("unvalidated", 0),
            checked=summary.get("checked", 0),
            total=summary.get("total_market_prices", 0),
        ),
        "",
        "| Symbol | Status | Market | As Of | Expected | Business Lag | Reasons |",
        "|---|---|---|---|---|---:|---|",
    ]
    for item in freshness.get("items", []):
        lines.append(
            "| {symbol} | {status} | {market} | {as_of} | {expected} | {lag} | {reasons} |".format(
                symbol=item.get("symbol", ""),
                status=item.get("status", ""),
                market=item.get("market", ""),
                as_of=item.get("as_of", ""),
                expected=item.get("expected_last_session", ""),
                lag=_fmt(item.get("business_day_lag")),
                reasons=", ".join(item.get("reasons") or []),
            )
        )

    rules = freshness.get("rules") or []
    if rules:
        lines.extend(["", "### Freshness Rules", ""])
        for rule in rules:
            lines.append(f"- {rule}")

    return "\n".join(lines) + "\n"


def _market_for_symbol(symbol: str) -> str:
    symbol = str(symbol or "").upper()
    if symbol.endswith(".HK") or symbol == "^HSI":
        return "hk_equity"
    if symbol.endswith("=X"):
        return "fx"
    if symbol.endswith("=F"):
        return "commodity_future"
    return "us_equity"


def _expected_last_session(now: datetime, market: str) -> date:
    spec = MARKET_SPECS.get(market, MARKET_SPECS["us_equity"])
    local_now = now.astimezone(ZoneInfo(spec["timezone"]))
    local_date = local_now.date()
    if local_now.weekday() < 5 and local_now.time() >= spec["close_time"]:
        return local_date
    return _previous_weekday(local_date)


def _previous_weekday(local_date: date) -> date:
    current = local_date
    while True:
        current = date.fromordinal(current.toordinal() - 1)
        if current.weekday() < 5:
            return current


def _business_day_lag(as_of: date, expected: date) -> int:
    if as_of == expected:
        return 0
    sign = 1 if as_of < expected else -1
    start, end = (as_of, expected) if as_of < expected else (expected, as_of)
    days = 0
    current = start
    while current < end:
        current = date.fromordinal(current.toordinal() + 1)
        if current.weekday() < 5:
            days += 1
    return sign * days


def _parse_generated_at(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_as_of_date(value: str) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None

    iso_match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if iso_match:
        return date.fromisoformat(iso_match.group(0))

    slash_match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if slash_match:
        month, day, year = slash_match.groups()
        return date(int(year), int(month), int(day))

    return None


def _point_value(point: Any, key: str) -> Any:
    if isinstance(point, dict):
        return point.get(key)
    return getattr(point, key, None)


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)

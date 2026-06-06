"""Market price cross-source validation."""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

from .base import DataPoint, compact_error, env, pct_change, utc_now_iso
from .market_prices import MarketPriceCollector


DEFAULT_VALIDATION_SYMBOLS = [
    "VOO",
    "SPY",
    "QQQ",
    "RSP",
    "GLD",
    "NVDA",
    "MSFT",
    "AAPL",
    "AMZN",
    "GOOGL",
    "META",
    "AVGO",
    "TSLA",
    "MU",
    "AMD",
    "SMH",
    "SOXX",
    "ARM",
]

DEFAULT_THRESHOLDS = {
    "pass_close_diff_pct": 0.15,
    "fail_close_diff_pct": 0.50,
    "pass_delta_pct_diff_abs": 0.25,
    "fail_delta_pct_diff_abs": 0.75,
}


class PriceValidator:
    """Validate primary market prices against a second documented source."""

    def __init__(
        self,
        symbols: Optional[Iterable[str]] = None,
        max_symbols: int = 18,
        request_delay_seconds: float = 13.0,
        thresholds: Optional[Dict[str, float]] = None,
    ):
        self.symbols = [s.strip().upper() for s in (symbols or DEFAULT_VALIDATION_SYMBOLS)]
        self.max_symbols = max(max_symbols, 0)
        self.request_delay_seconds = max(request_delay_seconds, 0.0)
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self.alpha_key = env("ALPHA_VANTAGE_API_KEY")

    def validate(self, primary_points: Iterable[Any]) -> Dict[str, Any]:
        primary_by_symbol = {
            _point_value(point, "symbol_or_series"): point
            for point in primary_points
            if _point_value(point, "category") == "market_price"
        }

        items: List[Dict[str, Any]] = []
        counts = {"pass": 0, "warn": 0, "fail": 0, "unvalidated": 0}
        attempted = 0
        alpha_blocked = False

        for symbol in self.symbols:
            primary = primary_by_symbol.get(symbol)
            if primary is None:
                item = self._base_item(symbol, None)
                item["status"] = "unvalidated"
                item["reasons"].append("primary_price_missing")
                items.append(item)
                counts["unvalidated"] += 1
                continue

            item = self._base_item(symbol, primary)
            if not self.alpha_key:
                item["status"] = "unvalidated"
                item["reasons"].append("ALPHA_VANTAGE_API_KEY_missing")
                items.append(item)
                counts["unvalidated"] += 1
                continue

            if not _is_alpha_vantage_supported(symbol):
                item["status"] = "unvalidated"
                item["reasons"].append("symbol_not_supported_by_alpha_vantage_daily")
                items.append(item)
                counts["unvalidated"] += 1
                continue

            if alpha_blocked or attempted >= self.max_symbols:
                item["status"] = "unvalidated"
                item["reasons"].append("validation_symbol_limit_reached")
                items.append(item)
                counts["unvalidated"] += 1
                continue

            if attempted > 0 and self.request_delay_seconds:
                time.sleep(self.request_delay_seconds)

            attempted += 1
            try:
                secondary = MarketPriceCollector(
                    symbols=[symbol], max_alpha_symbols=1
                )._from_alpha_vantage(symbol)
            except Exception as exc:
                message = compact_error(exc)
                item["status"] = "unvalidated"
                item["reasons"].append(f"secondary_source_error: {message}")
                if _looks_like_alpha_limit(message):
                    alpha_blocked = True
                items.append(item)
                counts["unvalidated"] += 1
                continue

            self._attach_secondary(item, secondary)
            item["status"] = self._status_for(item)
            counts[item["status"]] += 1
            items.append(item)

        checked = counts["pass"] + counts["warn"] + counts["fail"]
        return {
            "generated_at": utc_now_iso(),
            "method": "primary market_price vs Alpha Vantage TIME_SERIES_DAILY",
            "primary_source": "market_price snapshot",
            "secondary_source": "alpha_vantage",
            "symbols_requested": self.symbols,
            "symbols_attempted": attempted,
            "thresholds": self.thresholds,
            "summary": {
                **counts,
                "checked": checked,
                "total": len(items),
            },
            "items": items,
            "rules": [
                "pass means close and daily percent move are within tolerance.",
                "warn means usable but should be described as cross-source discrepancy or date mismatch.",
                "fail means the report must not treat that price or move as confirmed without manual/source review.",
                "unvalidated means no second-source confirmation was available in this run.",
            ],
        }

    def _base_item(self, symbol: str, primary: Optional[Any]) -> Dict[str, Any]:
        item = {
            "symbol": symbol,
            "status": "unvalidated",
            "reasons": [],
            "primary": None,
            "secondary": None,
            "diff": {},
        }
        if primary is not None:
            item["primary"] = _point_summary(primary)
        return item

    def _attach_secondary(self, item: Dict[str, Any], secondary: DataPoint) -> None:
        item["secondary"] = _point_summary(secondary)
        primary = item.get("primary") or {}
        secondary_summary = item.get("secondary") or {}

        primary_close = _safe_number(primary.get("close"))
        secondary_close = _safe_number(secondary_summary.get("close"))
        primary_previous = _safe_number(primary.get("previous_close"))
        secondary_previous = _safe_number(secondary_summary.get("previous_close"))
        primary_delta_pct = _safe_number(primary.get("delta_pct"))
        secondary_delta_pct = _safe_number(secondary_summary.get("delta_pct"))

        item["diff"] = {
            "close_diff": _diff(primary_close, secondary_close),
            "close_diff_pct": _diff_pct(primary_close, secondary_close),
            "previous_close_diff": _diff(primary_previous, secondary_previous),
            "previous_close_diff_pct": _diff_pct(primary_previous, secondary_previous),
            "delta_pct_diff_abs": _abs_diff(primary_delta_pct, secondary_delta_pct),
            "as_of_match": primary.get("as_of") == secondary_summary.get("as_of"),
        }

    def _status_for(self, item: Dict[str, Any]) -> str:
        diff = item.get("diff") or {}
        reasons = item["reasons"]
        close_diff_pct = diff.get("close_diff_pct")
        delta_pct_diff_abs = diff.get("delta_pct_diff_abs")

        if close_diff_pct is None:
            reasons.append("missing_close_diff")
            return "unvalidated"

        if not diff.get("as_of_match"):
            reasons.append("as_of_date_mismatch")

        fail_close = close_diff_pct > self.thresholds["fail_close_diff_pct"]
        fail_delta = (
            delta_pct_diff_abs is not None
            and delta_pct_diff_abs > self.thresholds["fail_delta_pct_diff_abs"]
        )
        if fail_close or fail_delta:
            if fail_close:
                reasons.append("close_diff_exceeds_fail_threshold")
            if fail_delta:
                reasons.append("delta_pct_diff_exceeds_fail_threshold")
            return "fail"

        warn_close = close_diff_pct > self.thresholds["pass_close_diff_pct"]
        warn_delta = (
            delta_pct_diff_abs is not None
            and delta_pct_diff_abs > self.thresholds["pass_delta_pct_diff_abs"]
        )
        if warn_close or warn_delta or reasons:
            if warn_close:
                reasons.append("close_diff_exceeds_pass_threshold")
            if warn_delta:
                reasons.append("delta_pct_diff_exceeds_pass_threshold")
            return "warn"

        return "pass"


def render_price_validation_markdown(data_quality: Dict[str, Any]) -> str:
    validation = data_quality.get("price_validation") or data_quality
    summary = validation.get("summary", {})
    lines = [
        "# Data Quality",
        "",
        "## Price Validation",
        "",
        f"- Generated at: {validation.get('generated_at', '')}",
        f"- Method: {validation.get('method', '')}",
        "- Summary: pass={pass_count}, warn={warn}, fail={fail}, unvalidated={unvalidated}, checked={checked}, total={total}".format(
            pass_count=summary.get("pass", 0),
            warn=summary.get("warn", 0),
            fail=summary.get("fail", 0),
            unvalidated=summary.get("unvalidated", 0),
            checked=summary.get("checked", 0),
            total=summary.get("total", 0),
        ),
        "",
        "| Symbol | Status | Primary | Secondary | Close Diff % | Delta Diff | As Of Match | Reasons |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    for item in validation.get("items", []):
        primary = item.get("primary") or {}
        secondary = item.get("secondary") or {}
        diff = item.get("diff") or {}
        lines.append(
            "| {symbol} | {status} | {primary} | {secondary} | {close_diff_pct} | {delta_diff} | {as_of_match} | {reasons} |".format(
                symbol=item.get("symbol", ""),
                status=item.get("status", ""),
                primary=_fmt(primary.get("close")),
                secondary=_fmt(secondary.get("close")),
                close_diff_pct=_fmt(diff.get("close_diff_pct")),
                delta_diff=_fmt(diff.get("delta_pct_diff_abs")),
                as_of_match=diff.get("as_of_match", ""),
                reasons=", ".join(item.get("reasons") or []),
            )
        )

    rules = validation.get("rules") or []
    if rules:
        lines.extend(["", "## Rules", ""])
        for rule in rules:
            lines.append(f"- {rule}")

    return "\n".join(lines) + "\n"


def _point_summary(point: Any) -> Dict[str, Any]:
    return {
        "source": _point_value(point, "source"),
        "source_url": _point_value(point, "source_url"),
        "as_of": _point_value(point, "as_of"),
        "close": _point_value(point, "value"),
        "previous_close": _point_value(point, "previous_value"),
        "delta": _point_value(point, "delta"),
        "delta_pct": _point_value(point, "delta_pct"),
        "currency": _point_value(point, "unit"),
        "confidence": _point_value(point, "confidence"),
    }


def _point_value(point: Any, key: str) -> Any:
    if isinstance(point, dict):
        return point.get(key)
    return getattr(point, key, None)


def _is_alpha_vantage_supported(symbol: str) -> bool:
    unsupported_markers = ("^", "=", ".HK", ".SS", ".SZ")
    return not any(marker in symbol for marker in unsupported_markers)


def _looks_like_alpha_limit(message: str) -> bool:
    text = message.lower()
    return "rate limit" in text or "frequency" in text or "standard api call frequency" in text


def _safe_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _diff(left: Optional[float], right: Optional[float]) -> Optional[float]:
    if left is None or right is None:
        return None
    return left - right


def _abs_diff(left: Optional[float], right: Optional[float]) -> Optional[float]:
    raw = _diff(left, right)
    if raw is None:
        return None
    return abs(raw)


def _diff_pct(left: Optional[float], right: Optional[float]) -> Optional[float]:
    if left is None or right in (None, 0):
        return None
    return abs(pct_change(left, right) or 0.0)


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)

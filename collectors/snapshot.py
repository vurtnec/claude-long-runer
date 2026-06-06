"""Snapshot orchestration and JSON persistence."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .base import (
    CollectorResult,
    DataPoint,
    absolute_change,
    atomic_write_json,
    configure_http_tracing,
    dataclass_list,
    local_today,
    pct_change,
    source_status,
    utc_now_iso,
)
from .bea_macro import BeaMacroCollector
from .cboe_volatility import CboeVolatilityCollector
from .eia_energy import EiaEnergyCollector
from .freshness_checker import FreshnessChecker, render_freshness_markdown
from .fred_macro import FredMacroCollector
from .market_prices import DEFAULT_SYMBOLS, MarketPriceCollector
from .news import GdeltNewsCollector, GoogleNewsRssCollector, OfficialRssCollector
from .price_validator import PriceValidator, render_price_validation_markdown
from .sec_filings import SecFilingsCollector
from .treasury_rates import TreasuryRatesCollector


DEFAULT_OUTPUT_DIR = Path("data/market")


class MarketSnapshotRunner:
    def __init__(
        self,
        output_dir: Path = DEFAULT_OUTPUT_DIR,
        symbols: Optional[Iterable[str]] = None,
        include_news: bool = True,
        include_google_news: bool = False,
        include_gdelt: bool = True,
        max_alpha_symbols: int = 5,
        validate_prices: bool = True,
        max_validation_symbols: int = 18,
        validation_delay_seconds: float = 13.0,
        check_freshness: bool = True,
    ):
        self.output_dir = output_dir
        self.symbols = list(symbols or DEFAULT_SYMBOLS)
        self.include_news = include_news
        self.include_google_news = include_google_news
        self.include_gdelt = include_gdelt
        self.max_alpha_symbols = max_alpha_symbols
        self.validate_prices = validate_prices
        self.max_validation_symbols = max_validation_symbols
        self.validation_delay_seconds = validation_delay_seconds
        self.check_freshness = check_freshness
        self.trace_dir: Optional[Path] = None

    def run(self, session: str) -> Dict:
        self.trace_dir = self.output_dir / "traces" / (
            f"{local_today()}-{session}-{datetime.now().strftime('%H%M%S')}"
        )
        configure_http_tracing(self.trace_dir)
        try:
            results: List[CollectorResult] = []
            collectors = [
                MarketPriceCollector(
                    symbols=self.symbols,
                    max_alpha_symbols=self.max_alpha_symbols,
                ),
                TreasuryRatesCollector(),
                CboeVolatilityCollector(),
                FredMacroCollector(),
                BeaMacroCollector(),
                EiaEnergyCollector(),
                SecFilingsCollector(),
            ]

            if self.include_news:
                collectors.append(OfficialRssCollector())
                if self.include_gdelt:
                    collectors.append(GdeltNewsCollector())
                if self.include_google_news:
                    collectors.append(GoogleNewsRssCollector())

            for collector in collectors:
                results.append(collector.collect())

            snapshot = self._build_snapshot(session, results)
            self._write_snapshot(session, snapshot)
            return snapshot
        finally:
            configure_http_tracing(None)

    def _build_snapshot(self, session: str, results: List[CollectorResult]) -> Dict:
        data_points = []
        news_items = []
        sources = {}
        missing_sources = []

        for result in results:
            sources[result.name] = source_status(result)
            data_points.extend(result.data_points)
            news_items.extend(result.news_items)
            if not result.ok:
                missing_sources.append(result.name)

        generated_at = utc_now_iso()
        data_points.extend(_gold_cny_conversions(data_points))

        data_quality = {}
        if self.validate_prices:
            data_quality["price_validation"] = PriceValidator(
                max_symbols=self.max_validation_symbols,
                request_delay_seconds=self.validation_delay_seconds,
            ).validate(data_points)
        if self.check_freshness:
            data_quality["freshness"] = FreshnessChecker().check(
                data_points,
                generated_at=generated_at,
            )

        top_market_movers = _top_market_movers(data_points)
        payload = {
            "generated_at": generated_at,
            "session": session,
            "sources": sources,
            "missing_sources": missing_sources,
            "data_points": dataclass_list(data_points),
            "news_items": dataclass_list(news_items),
            "top_market_movers": top_market_movers,
            "data_quality": data_quality,
            "trace": {
                "dir": str(self.trace_dir) if self.trace_dir else "",
                "manifest": str(self.trace_dir / "manifest.jsonl") if self.trace_dir else "",
            },
            "summary_inputs": {
                "data_point_count": len(data_points),
                "news_item_count": len(news_items),
                "top_market_mover_count": len(top_market_movers),
                "official_news_count": sum(1 for item in news_items if item.source_rank >= 5),
                "high_confidence_data_count": sum(
                    1 for point in data_points if point.confidence == "official"
                ),
                "price_validation": (
                    data_quality.get("price_validation", {}).get("summary", {})
                ),
                "freshness": data_quality.get("freshness", {}).get("summary", {}),
            },
        }
        return payload

    def _write_snapshot(self, session: str, snapshot: Dict) -> None:
        today = local_today()
        snapshots_dir = self.output_dir / "snapshots"
        atomic_write_json(snapshots_dir / f"{today}-{session}.json", snapshot)
        atomic_write_json(self.output_dir / f"latest_{session}.json", snapshot)
        atomic_write_json(self.output_dir / "daily_market_context.json", snapshot)

        markdown = self._render_markdown(snapshot)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (snapshots_dir / f"{today}-{session}.md").write_text(markdown, encoding="utf-8")
        (self.output_dir / f"latest_{session}.md").write_text(markdown, encoding="utf-8")
        (self.output_dir / "daily_market_context.md").write_text(markdown, encoding="utf-8")

        llm_input = self._render_llm_input(snapshot)
        (snapshots_dir / f"{today}-{session}-llm-input.md").write_text(
            llm_input, encoding="utf-8"
        )
        (self.output_dir / f"latest_{session}_llm_input.md").write_text(
            llm_input, encoding="utf-8"
        )
        (self.output_dir / "daily_llm_input.md").write_text(llm_input, encoding="utf-8")

        if snapshot.get("data_quality"):
            atomic_write_json(
                snapshots_dir / f"{today}-{session}-data-quality.json",
                snapshot["data_quality"],
            )
            atomic_write_json(self.output_dir / f"latest_{session}_data_quality.json", snapshot["data_quality"])
            atomic_write_json(self.output_dir / "data_quality.json", snapshot["data_quality"])
            quality_markdown = _render_data_quality_markdown(snapshot["data_quality"])
            (snapshots_dir / f"{today}-{session}-data-quality.md").write_text(
                quality_markdown, encoding="utf-8"
            )
            (self.output_dir / f"latest_{session}_data_quality.md").write_text(
                quality_markdown, encoding="utf-8"
            )
            (self.output_dir / "data_quality.md").write_text(
                quality_markdown, encoding="utf-8"
            )

    def _render_markdown(self, snapshot: Dict) -> str:
        movers = snapshot.get("top_market_movers") or _top_market_movers(
            snapshot["data_points"]
        )
        lines = [
            f"# Market Context Snapshot - {snapshot['session']}",
            "",
            f"- Generated at: {snapshot['generated_at']}",
            f"- Data points: {len(snapshot['data_points'])}",
            f"- News items: {len(snapshot['news_items'])}",
            f"- Top market movers: {len(movers)}",
            f"- Missing sources: {', '.join(snapshot['missing_sources']) if snapshot['missing_sources'] else 'none'}",
            f"- Trace manifest: {(snapshot.get('trace') or {}).get('manifest', '')}",
            "",
            "## Source Status",
            "",
            "| Source | OK | Data | News | Warnings | Errors |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for source, status in sorted(snapshot["sources"].items()):
            lines.append(
                "| {source} | {ok} | {data} | {news} | {warnings} | {errors} |".format(
                    source=source,
                    ok="yes" if status["ok"] else "no",
                    data=status["data_points"],
                    news=status["news_items"],
                    warnings=len(status["warnings"]),
                    errors=len(status["errors"]),
                )
            )

        _extend_price_validation_lines(lines, snapshot, compact=False)
        _extend_freshness_lines(lines, snapshot, compact=False)
        _extend_rmb_fx_lines(lines, snapshot["data_points"])
        _extend_gold_cny_lines(lines, snapshot["data_points"])

        if movers:
            lines.extend(["", "## Top Market Movers", ""])
            lines.append("| Symbol | Name | Direction | Move % | Value | Delta | As of | Source |")
            lines.append("|---|---|---|---:|---:|---:|---|---|")
            for mover in movers:
                lines.append(
                    "| {symbol} | {name} | {direction} | {delta_pct} | {value} | {delta} | {as_of} | {source} |".format(
                        symbol=mover["symbol"],
                        name=_escape_table(mover["name"]),
                        direction=mover["direction"],
                        delta_pct=_fmt(mover["delta_pct"]),
                        value=_fmt(mover["value"]),
                        delta=_fmt(mover["delta"]),
                        as_of=mover["as_of"],
                        source=mover["source"],
                    )
                )

        grouped = {}
        for point in snapshot["data_points"]:
            grouped.setdefault(point["category"], []).append(point)

        lines.extend(["", "## Data Points", ""])
        for category in sorted(grouped):
            lines.extend([f"### {category}", ""])
            lines.append("| Series | Name | Value | Previous | Delta | Delta % | As of | Source |")
            lines.append("|---|---|---:|---:|---:|---:|---|---|")
            for point in grouped[category]:
                lines.append(
                    "| {symbol} | {name} | {value} | {previous} | {delta} | {delta_pct} | {as_of} | {source} |".format(
                        symbol=point["symbol_or_series"],
                        name=_escape_table(point["name"]),
                        value=_fmt(point["value"]),
                        previous=_fmt(point.get("previous_value")),
                        delta=_fmt(point.get("delta")),
                        delta_pct=_fmt(point.get("delta_pct")),
                        as_of=point["as_of"],
                        source=point["source"],
                    )
                )
            lines.append("")

        if snapshot["news_items"]:
            lines.extend(["## News Items", ""])
            for item in snapshot["news_items"][:80]:
                title = item["title"].strip()
                source = item["source"]
                published = item.get("published_at") or "unknown time"
                url = item.get("url") or ""
                summary = item.get("summary") or ""
                lines.append(f"- [{source}] {title} ({published})")
                if url:
                    lines.append(f"  - URL: {url}")
                if summary:
                    lines.append(f"  - Summary: {summary}")

        return "\n".join(lines) + "\n"

    def _render_llm_input(self, snapshot: Dict) -> str:
        points = snapshot["data_points"]
        movers = snapshot.get("top_market_movers") or _top_market_movers(points)
        core_symbols = {
            "^GSPC",
            "^DJI",
            "^IXIC",
            "^HSI",
            "VOO",
            "SPY",
            "QQQ",
            "RSP",
            "GLD",
            "SMH",
            "SOXX",
            "3110.HK",
            "2800.HK",
            "3032.HK",
            "NVDA",
            "MSFT",
            "AAPL",
            "AMZN",
            "GOOGL",
            "META",
            "AVGO",
            "TSLA",
            "AMD",
            "MU",
            "0700.HK",
            "9988.HK",
            "1810.HK",
            "0941.HK",
            "0883.HK",
            "1211.HK",
        }
        key_macro_series = {
            "2Y",
            "10Y",
            "30Y",
            "10Y_MINUS_2Y",
            "10Y_REAL",
            "T5YIE",
            "DFF",
            "DTWEXBGS",
            "BAMLC0A0CM",
            "BAMLH0A0HYM2",
            "WALCL",
            "VIX",
            "VVIX",
            "GVZ",
            "OVX",
            "PET.RWTC.D",
            "PET.RBRTE.D",
            "BEA_REAL_GDP_QOQ",
            "BEA_PCE_PRICE_INDEX",
            "BEA_CORE_PCE_PRICE_INDEX",
        }
        core_price_points = [
            point
            for point in points
            if point["category"] == "market_price"
            and point["symbol_or_series"] in core_symbols
        ]
        macro_points = [
            point for point in points if point["symbol_or_series"] in key_macro_series
        ]
        high_rank_news = []
        seen_news = set()
        for item in snapshot["news_items"]:
            if not (
                item.get("source_rank", 0) >= 5
                or item.get("category") == "sec_filing"
            ):
                continue
            if (
                item.get("category") == "sec_filing"
                and item.get("extra", {}).get("form") == "4"
            ):
                continue
            news_key = (
                item.get("source", ""),
                item.get("title", ""),
                item.get("published_at", ""),
            )
            if news_key in seen_news:
                continue
            seen_news.add(news_key)
            high_rank_news.append(item)
            if len(high_rank_news) >= 10:
                break
        if not high_rank_news:
            high_rank_news = [
                item
                for item in snapshot["news_items"]
                if item.get("source_rank", 0) >= 5 or item.get("category") == "sec_filing"
            ][:8]

        lines = [
            f"# Daily LLM Market Input - {snapshot['session']}",
            "",
            "Compact first-read file. For exact fields, missing values, or disputed facts, read `data/market/daily_market_context.md/json`.",
            "",
            f"- Generated at: {snapshot['generated_at']}",
            f"- Missing sources: {', '.join(snapshot['missing_sources']) if snapshot['missing_sources'] else 'none'}",
            f"- Full data points: {len(snapshot['data_points'])}",
            f"- Full news items: {len(snapshot['news_items'])}",
            f"- Trace manifest: {(snapshot.get('trace') or {}).get('manifest', '')}",
            "",
            "## Source Status Exceptions",
            "",
        ]
        warning_lines = []
        for source, status in sorted(snapshot["sources"].items()):
            if status["warnings"] or status["errors"]:
                warning_lines.append(
                    "- {source}: ok={ok}, data={data}, news={news}, warnings={warnings}, errors={errors}".format(
                        source=source,
                        ok="yes" if status["ok"] else "no",
                        data=status["data_points"],
                        news=status["news_items"],
                        warnings=len(status["warnings"]),
                        errors=len(status["errors"]),
                    )
                )
        lines.extend(warning_lines or ["- none"])

        if warning_lines:
            lines.append("- Read the full JSON for exact warning text.")

        _extend_price_validation_lines(lines, snapshot, compact=True)
        _extend_freshness_lines(lines, snapshot, compact=True)
        _extend_rmb_fx_lines(lines, points)
        _extend_gold_cny_lines(lines, points)

        if movers:
            lines.extend(["", "## Top Market Movers", ""])
            lines.append("| Symbol | Name | Direction | Move % | Value | Delta | As of |")
            lines.append("|---|---|---|---:|---:|---:|---|")
            for mover in movers:
                lines.append(
                    "| {symbol} | {name} | {direction} | {delta_pct} | {value} | {delta} | {as_of} |".format(
                        symbol=mover["symbol"],
                        name=_escape_table(mover["name"]),
                        direction=mover["direction"],
                        delta_pct=_fmt(mover["delta_pct"]),
                        value=_fmt(mover["value"]),
                        delta=_fmt(mover["delta"]),
                        as_of=mover["as_of"],
                    )
                )

        lines.extend(["", "## Core ETFs, Indices, and Key Companies", ""])
        lines.append("| Series | Name | Value | Previous | Delta | Delta % | As of | Source |")
        lines.append("|---|---|---:|---:|---:|---:|---|---|")
        for point in sorted(core_price_points, key=lambda p: p["symbol_or_series"]):
            lines.append(_point_row(point))

        lines.extend(["", "## Key Macro and Cross-Asset Indicators", ""])
        grouped = {}
        for point in macro_points:
            grouped.setdefault(point["category"], []).append(point)
        for category in sorted(grouped):
            lines.extend([f"### {category}", ""])
            lines.append("| Series | Name | Value | Previous | Delta | Delta % | As of | Source |")
            lines.append("|---|---|---:|---:|---:|---:|---|---|")
            for point in grouped[category]:
                lines.append(_point_row(point))
            lines.append("")

        if high_rank_news:
            lines.extend(["## Official and SEC News Candidates", ""])
            for item in high_rank_news:
                title = item["title"].strip()
                source = item["source"]
                published = item.get("published_at") or "unknown time"
                summary = _clip(item.get("summary") or "", 240)
                lines.append(f"- [{source}] {title} ({published})")
                if summary:
                    lines.append(f"  - Summary: {summary}")

        lines.extend(
            [
                "",
                "## How To Use",
                "",
                "- Use `Top Market Movers` to decide which hotspots need cause research.",
                "- When discussing USD/RMB, use `RMB FX Reference` values if present.",
                "- When discussing gold, include the `Gold RMB Conversions` values if present.",
                "- Use official macro data for rates, inflation, energy, volatility, and SEC facts.",
                "- Use WebSearch/WebFetch only for unexplained movers, future events, or non-US/HK/China AI gaps.",
                "- Do not infer a cause when high-confidence evidence is missing; mark it as pending verification.",
            ]
        )
        return "\n".join(lines) + "\n"


TROY_OUNCE_GRAMS = 31.1034768


def _gold_cny_conversions(points: List[DataPoint]) -> List[DataPoint]:
    by_symbol = {point.symbol_or_series: point for point in points}
    fx = by_symbol.get("CNH=X")
    if fx is None or fx.value in (None, ""):
        return []

    conversions: List[DataPoint] = []
    fx_rate = _number(fx.value)
    fx_previous = _number(fx.previous_value)
    if fx_rate is None:
        return []

    gold_future = by_symbol.get("GC=F")
    if gold_future is not None:
        converted = _derive_gold_future_cny(gold_future, fx, fx_rate, fx_previous)
        if converted is not None:
            conversions.append(converted)

    gld = by_symbol.get("GLD")
    if gld is not None:
        converted = _derive_gld_cny(gld, fx, fx_rate, fx_previous)
        if converted is not None:
            conversions.append(converted)

    return conversions


def _derive_gold_future_cny(
    point: DataPoint,
    fx: DataPoint,
    fx_rate: float,
    fx_previous: Optional[float],
) -> Optional[DataPoint]:
    usd_per_oz = _number(point.value)
    previous_usd_per_oz = _number(point.previous_value)
    if usd_per_oz is None:
        return None

    value = usd_per_oz * fx_rate / TROY_OUNCE_GRAMS
    previous = None
    if previous_usd_per_oz is not None:
        previous = previous_usd_per_oz * (fx_previous or fx_rate) / TROY_OUNCE_GRAMS

    return DataPoint(
        as_of=f"{point.as_of}; FX {fx.as_of}",
        source="derived_from_yahoo_chart",
        source_url=point.source_url,
        category="derived_price",
        symbol_or_series="GC_CNY_PER_GRAM",
        name="COMEX gold approximate RMB per gram",
        value=value,
        previous_value=previous,
        delta=absolute_change(value, previous),
        delta_pct=pct_change(value, previous),
        unit="CNY/g",
        freshness="derived",
        confidence="derived_from_fallback_inputs",
        notes="Derived from GC=F USD/oz and CNH=X USD/CNH; not an independent quote.",
        extra={
            "formula": "GC=F_USD_per_oz * USDCNH / 31.1034768",
            "price_symbol": point.symbol_or_series,
            "fx_symbol": fx.symbol_or_series,
            "usd_per_oz": usd_per_oz,
            "usd_cnh": fx_rate,
            "price_as_of": point.as_of,
            "fx_as_of": fx.as_of,
            "previous_fx_used": fx_previous or fx_rate,
        },
    )


def _derive_gld_cny(
    point: DataPoint,
    fx: DataPoint,
    fx_rate: float,
    fx_previous: Optional[float],
) -> Optional[DataPoint]:
    usd_per_share = _number(point.value)
    previous_usd_per_share = _number(point.previous_value)
    if usd_per_share is None:
        return None

    value = usd_per_share * fx_rate
    previous = None
    if previous_usd_per_share is not None:
        previous = previous_usd_per_share * (fx_previous or fx_rate)

    return DataPoint(
        as_of=f"{point.as_of}; FX {fx.as_of}",
        source="derived_from_yahoo_chart",
        source_url=point.source_url,
        category="derived_price",
        symbol_or_series="GLD_CNY_PER_SHARE",
        name="GLD approximate RMB per share",
        value=value,
        previous_value=previous,
        delta=absolute_change(value, previous),
        delta_pct=pct_change(value, previous),
        unit="CNY/share",
        freshness="derived",
        confidence="derived_from_fallback_inputs",
        notes="Derived from GLD USD/share and CNH=X USD/CNH; not an independent quote.",
        extra={
            "formula": "GLD_USD_per_share * USDCNH",
            "price_symbol": point.symbol_or_series,
            "fx_symbol": fx.symbol_or_series,
            "usd_per_share": usd_per_share,
            "usd_cnh": fx_rate,
            "price_as_of": point.as_of,
            "fx_as_of": fx.as_of,
            "previous_fx_used": fx_previous or fx_rate,
        },
    )


def _extend_gold_cny_lines(lines: List[str], points: List[Dict]) -> None:
    gold_points = [
        point
        for point in points
        if point.get("category") == "derived_price"
        and point.get("symbol_or_series") in {"GC_CNY_PER_GRAM", "GLD_CNY_PER_SHARE"}
    ]
    if not gold_points:
        return

    lines.extend(["", "## Gold RMB Conversions", ""])
    lines.append(
        "- Derived from USD gold/GLD quotes and USD/CNH; use as converted reference, not an independent quote."
    )
    lines.append("")
    lines.append("| Series | Name | Value | Previous | Delta | Delta % | Unit | As of |")
    lines.append("|---|---|---:|---:|---:|---:|---|---|")
    for point in sorted(gold_points, key=lambda p: p["symbol_or_series"]):
        lines.append(
            "| {symbol} | {name} | {value} | {previous} | {delta} | {delta_pct} | {unit} | {as_of} |".format(
                symbol=point["symbol_or_series"],
                name=_escape_table(point["name"]),
                value=_fmt(point["value"]),
                previous=_fmt(point.get("previous_value")),
                delta=_fmt(point.get("delta")),
                delta_pct=_fmt(point.get("delta_pct")),
                unit=point.get("unit", ""),
                as_of=point["as_of"],
            )
        )


def _extend_rmb_fx_lines(lines: List[str], points: List[Dict]) -> None:
    fx = next(
        (
            point
            for point in points
            if point.get("category") == "market_price"
            and point.get("symbol_or_series") == "CNH=X"
        ),
        None,
    )
    if not fx:
        return

    lines.extend(["", "## RMB FX Reference", ""])
    lines.append(
        "- `CNH=X` is USD/CNH: offshore RMB per 1 USD. Example: 6.78 means 1 USD ≈ 6.78 offshore RMB."
    )
    lines.append("")
    lines.append("| Series | Name | Value | Previous | Delta | Delta % | Unit | As of | Source |")
    lines.append("|---|---|---:|---:|---:|---:|---|---|---|")
    lines.append(
        "| {symbol} | {name} | {value} | {previous} | {delta} | {delta_pct} | CNH per USD | {as_of} | {source} |".format(
            symbol=fx["symbol_or_series"],
            name=_escape_table(fx["name"]),
            value=_fmt(fx["value"]),
            previous=_fmt(fx.get("previous_value")),
            delta=_fmt(fx.get("delta")),
            delta_pct=_fmt(fx.get("delta_pct")),
            as_of=fx["as_of"],
            source=fx["source"],
        )
    )


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _number(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _escape_table(value: str) -> str:
    return str(value).replace("|", "\\|")


def _point_row(point: Dict) -> str:
    return "| {symbol} | {name} | {value} | {previous} | {delta} | {delta_pct} | {as_of} | {source} |".format(
        symbol=point["symbol_or_series"],
        name=_escape_table(point["name"]),
        value=_fmt(point["value"]),
        previous=_fmt(point.get("previous_value")),
        delta=_fmt(point.get("delta")),
        delta_pct=_fmt(point.get("delta_pct")),
        as_of=point["as_of"],
        source=point["source"],
    )


def _extend_price_validation_lines(lines: List[str], snapshot: Dict, compact: bool) -> None:
    validation = (snapshot.get("data_quality") or {}).get("price_validation")
    if not validation:
        return

    summary = validation.get("summary", {})
    title = "## Price Validation Summary" if compact else "## Price Validation"
    lines.extend(
        [
            "",
            title,
            "",
            "- pass={pass_count}, warn={warn}, fail={fail}, unvalidated={unvalidated}, checked={checked}, total={total}".format(
                pass_count=summary.get("pass", 0),
                warn=summary.get("warn", 0),
                fail=summary.get("fail", 0),
                unvalidated=summary.get("unvalidated", 0),
                checked=summary.get("checked", 0),
                total=summary.get("total", 0),
            ),
            "- Rule: fail prices are not confirmed; warn prices need caveats; unvalidated prices have no second-source confirmation.",
            "",
            "| Symbol | Status | Primary | Secondary | Close Diff % | Delta Diff | Reasons |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for item in validation.get("items", []):
        if compact and item.get("status") == "pass":
            continue
        primary = item.get("primary") or {}
        secondary = item.get("secondary") or {}
        diff = item.get("diff") or {}
        lines.append(
            "| {symbol} | {status} | {primary} | {secondary} | {close_diff_pct} | {delta_diff} | {reasons} |".format(
                symbol=item.get("symbol", ""),
                status=item.get("status", ""),
                primary=_fmt(primary.get("close")),
                secondary=_fmt(secondary.get("close")),
                close_diff_pct=_fmt(diff.get("close_diff_pct")),
                delta_diff=_fmt(diff.get("delta_pct_diff_abs")),
                reasons=", ".join(item.get("reasons") or []),
            )
        )


def _extend_freshness_lines(lines: List[str], snapshot: Dict, compact: bool) -> None:
    freshness = (snapshot.get("data_quality") or {}).get("freshness")
    if not freshness:
        return

    summary = freshness.get("summary", {})
    title = "## Freshness Summary" if compact else "## Freshness"
    lines.extend(
        [
            "",
            title,
            "",
            "- pass={pass_count}, warn={warn}, fail={fail}, unvalidated={unvalidated}, checked={checked}, total_market_prices={total}".format(
                pass_count=summary.get("pass", 0),
                warn=summary.get("warn", 0),
                fail=summary.get("fail", 0),
                unvalidated=summary.get("unvalidated", 0),
                checked=summary.get("checked", 0),
                total=summary.get("total_market_prices", 0),
            ),
            "- Rule: fail means stale by two or more weekdays; warn means one weekday stale or date mismatch; holiday calendars are not yet included.",
            "",
            "| Symbol | Status | Market | As Of | Expected | Business Lag | Reasons |",
            "|---|---|---|---|---|---:|---|",
        ]
    )
    for item in freshness.get("items", []):
        if compact and item.get("status") == "pass":
            continue
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


def _render_data_quality_markdown(data_quality: Dict) -> str:
    sections = ["# Data Quality", ""]
    if data_quality.get("price_validation"):
        price_markdown = render_price_validation_markdown(data_quality).splitlines()
        sections.extend(price_markdown[2:] if price_markdown[:1] == ["# Data Quality"] else price_markdown)
        sections.append("")
    if data_quality.get("freshness"):
        sections.extend(render_freshness_markdown(data_quality).splitlines())
        sections.append("")
    return "\n".join(sections).rstrip() + "\n"


def _clip(text: str, limit: int) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _top_market_movers(points, limit: int = 18, min_abs_pct: float = 2.0) -> List[Dict]:
    movers = []
    for point in points:
        category = _point_value(point, "category")
        delta_pct = _point_value(point, "delta_pct")
        if category != "market_price" or delta_pct is None:
            continue
        if abs(delta_pct) < min_abs_pct:
            continue
        movers.append(
            {
                "symbol": _point_value(point, "symbol_or_series"),
                "name": _point_value(point, "name"),
                "direction": "up" if delta_pct > 0 else "down",
                "value": _point_value(point, "value"),
                "delta": _point_value(point, "delta"),
                "delta_pct": delta_pct,
                "as_of": _point_value(point, "as_of"),
                "source": _point_value(point, "source"),
            }
        )
    movers.sort(key=lambda item: abs(item["delta_pct"]), reverse=True)
    return movers[:limit]


def _point_value(point, key: str):
    if isinstance(point, dict):
        return point.get(key)
    return getattr(point, key, None)

"""CLI entrypoint for market information snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .market_prices import DEFAULT_SYMBOLS
from .snapshot import DEFAULT_OUTPUT_DIR, MarketSnapshotRunner


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect market data/news into local JSON snapshots.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m collectors --session premarket
  python -m collectors --session postmarket --skip-gdelt
  python -m collectors --session premarket --symbols SPY,VOO,QQQ,GLD,USO
        """,
    )
    parser.add_argument(
        "--session",
        choices=["premarket", "postmarket", "daily", "manual"],
        default="manual",
        help="Snapshot label used in output filenames.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for latest_*.json and snapshots/ files.",
    )
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated ticker list for market price collection.",
    )
    parser.add_argument(
        "--skip-news",
        action="store_true",
        help="Skip RSS/GDELT/Google News collectors.",
    )
    parser.add_argument(
        "--skip-gdelt",
        action="store_true",
        help="Skip GDELT news search. Useful for fast smoke tests.",
    )
    parser.add_argument(
        "--include-google-news",
        action="store_true",
        help="Include Google News RSS fallback searches.",
    )
    parser.add_argument(
        "--max-alpha-symbols",
        type=int,
        default=5,
        help="Maximum Alpha Vantage ticker calls before using fallback provider.",
    )
    parser.add_argument(
        "--skip-price-validation",
        action="store_true",
        help="Skip cross-source validation for core market prices.",
    )
    parser.add_argument(
        "--max-validation-symbols",
        type=int,
        default=18,
        help="Maximum core symbols to validate against Alpha Vantage.",
    )
    parser.add_argument(
        "--validation-delay-seconds",
        type=float,
        default=13.0,
        help="Delay between validation API calls to respect free API limits.",
    )
    parser.add_argument(
        "--skip-freshness-check",
        action="store_true",
        help="Skip market-price freshness and weekday session checks.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full snapshot JSON to stdout.",
    )

    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    runner = MarketSnapshotRunner(
        output_dir=Path(args.output_dir),
        symbols=symbols,
        include_news=not args.skip_news,
        include_google_news=args.include_google_news,
        include_gdelt=not args.skip_gdelt,
        max_alpha_symbols=max(args.max_alpha_symbols, 0),
        validate_prices=not args.skip_price_validation,
        max_validation_symbols=max(args.max_validation_symbols, 0),
        validation_delay_seconds=max(args.validation_delay_seconds, 0.0),
        check_freshness=not args.skip_freshness_check,
    )
    snapshot = runner.run(args.session)

    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "Collected "
            f"{len(snapshot['data_points'])} data points and "
            f"{len(snapshot['news_items'])} news items. "
            f"Missing sources: {snapshot['missing_sources'] or 'none'}"
        )
        print(f"Wrote snapshots to {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()

"""Command-line entry point."""

import argparse
from pathlib import Path
import sys
from typing import Optional, Sequence

import pandas as pd

from .client import TushareTransport
from .config import ConfigurationError, Settings


def _write_or_print(frame: pd.DataFrame, output: Optional[str]) -> None:
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(target, index=False, encoding="utf-8-sig")
        print(f"Saved {len(frame)} rows to {target}")
        return
    if frame.empty:
        print("The query succeeded but returned no rows.")
    else:
        print(frame.to_string(index=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tushare A-share data tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="Verify the token and daily-data access")

    stocks = subparsers.add_parser("stocks", help="Retrieve currently listed stocks")
    stocks.add_argument("--exchange", choices=["SSE", "SZSE", "BSE"], default="")
    stocks.add_argument("--output", help="Optional CSV output path")

    daily = subparsers.add_parser("daily", help="Retrieve unadjusted daily prices")
    daily.add_argument("--code", required=True, help="For example, 000001.SZ")
    daily.add_argument("--start", required=True, help="Start date in YYYYMMDD format")
    daily.add_argument("--end", required=True, help="End date in YYYYMMDD format")
    daily.add_argument("--output", help="Optional CSV output path")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        client = TushareTransport(settings.tushare_token)
        if args.command == "check":
            frame = client.check_connection()
            print(f"Tushare connection succeeded with {len(frame)} test rows.")
        elif args.command == "stocks":
            _write_or_print(client.stock_basic(exchange=args.exchange), args.output)
        elif args.command == "daily":
            _write_or_print(
                client.daily(args.code, args.start, args.end),
                args.output,
            )
        return 0
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # The SDK uses exceptions for all upstream failures.
        print(f"Tushare request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

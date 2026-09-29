"""Backfill two years of daily bars for every universe symbol (resumable).

One Massive call per symbol; local 5/min throttle matches the free tier.
Existing per-symbol parquet files are skipped, so the script can be rerun
after any interruption without paying for completed fetches again.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from typing import Dict

import pandas as pd

from rfr.transport import SlidingWindowLimiter, massive_get, ts_to_date

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
UNIVERSE_PATH = os.path.join(DATA_DIR, "universe.csv")
SYMBOLS_DIR = os.path.join(DATA_DIR, "symbols")
FETCH_LOG_PATH = os.path.join(DATA_DIR, "fetch_log.json")
START_DATE = (date.today() - timedelta(days=740)).isoformat()  # free tier: ~2y
END_DATE = date.today().isoformat()
COLUMNS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "transactions",
)


def symbol_path(symbol: str) -> str:
    return os.path.join(SYMBOLS_DIR, f"{symbol}.parquet")


def fetch_symbol(symbol: str, limiter: SlidingWindowLimiter) -> Dict:
    os.makedirs(SYMBOLS_DIR, exist_ok=True)
    payload = massive_get(
        f"/v2/aggs/ticker/{symbol}/range/1/day/{START_DATE}/{END_DATE}",
        {"adjusted": "true", "sort": "asc", "limit": 50_000},
        limiter,
    )
    results = payload.get("results") or []
    rows = [
        {
            "symbol": symbol,
            "date": ts_to_date(item["t"]),
            "open": item.get("o"),
            "high": item.get("h"),
            "low": item.get("l"),
            "close": item.get("c"),
            "volume": item.get("v"),
            "vwap": item.get("vw"),
            "transactions": item.get("n"),
        }
        for item in results
    ]
    frame = pd.DataFrame(rows, columns=list(COLUMNS))
    frame.to_parquet(symbol_path(symbol), index=False)
    return {
        "symbol": symbol,
        "bars": len(frame),
        "first": frame["date"].min() if len(frame) else None,
        "last": frame["date"].max() if len(frame) else None,
    }


def main() -> int:
    if not os.path.exists(UNIVERSE_PATH):
        sys.exit("run s0_universe.py first")
    os.makedirs(SYMBOLS_DIR, exist_ok=True)
    universe = pd.read_csv(UNIVERSE_PATH)["symbol"].tolist()
    pending = [s for s in universe if not os.path.exists(symbol_path(s))]
    print(f"universe: {len(universe)} symbols, pending: {len(pending)}", flush=True)
    if not pending:
        return 0

    log = []
    limiter = SlidingWindowLimiter()
    started = time.time()
    for i, symbol in enumerate(pending, start=1):
        try:
            entry = fetch_symbol(symbol, limiter)
        except RuntimeError as exc:
            entry = {"symbol": symbol, "error": str(exc)}
        log.append(entry)
        done_bars = entry.get("bars", 0)
        if i % 10 == 0 or i == len(pending):
            elapsed = time.time() - started
            eta = elapsed / i * (len(pending) - i)
            print(
                f"[{i}/{len(pending)}] {symbol}: {done_bars} bars "
                f"(elapsed {elapsed / 60:.1f}m, eta {eta / 60:.1f}m)",
                flush=True,
            )
        with open(FETCH_LOG_PATH, "w", encoding="utf-8") as handle:
            json.dump(log, handle, ensure_ascii=False, indent=1)
    errors = [e for e in log if "error" in e]
    if errors:
        print(f"finished with {len(errors)} errors; rerun to retry them", flush=True)
        return 1
    print("all symbols fetched", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

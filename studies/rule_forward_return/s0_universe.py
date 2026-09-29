"""Build the liquid universe: top 500 common stocks by median dollar volume.

Stages (idempotent via data/universe.csv):
  1. One Massive call gives the recent trading calendar (SPY bars).
  2. ~20 grouped market snapshots -> per-symbol median dollar volume.
  3. One Finnhub /stock/symbol call filters to common stock type (drops ETFs,
     leveraged products, warrants, units).
  4. Top 500 intersection -> data/universe.csv
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from typing import List

import pandas as pd

from rfr.transport import SlidingWindowLimiter, finnhub_get, massive_get, ts_to_date

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
UNIVERSE_PATH = os.path.join(DATA_DIR, "universe.csv")
SNAPSHOT_DAYS = 20
UNIVERSE_SIZE = 500
COMMON_TYPES = {"Common Stock", "Common Shares"}
REFERENCE_SYMBOL = "SPY"


def recent_trading_dates(limiter: SlidingWindowLimiter) -> List[str]:
    """Last ~20 trading dates from one reference-symbol daily call."""
    today = datetime.now().date().isoformat()
    payload = massive_get(
        f"/v2/aggs/ticker/{REFERENCE_SYMBOL}/range/1/day/"
        f"{(datetime.now() - timedelta(days=60)).date().isoformat()}/{today}",
        {"adjusted": "true", "sort": "desc", "limit": 50},
        limiter,
    )
    dates = [ts_to_date(item["t"]) for item in payload.get("results") or []]
    dates = sorted(set(dates), reverse=True)[:SNAPSHOT_DAYS]
    if len(dates) < SNAPSHOT_DAYS:
        sys.exit(f"expected {SNAPSHOT_DAYS} trading dates, got {len(dates)}")
    return sorted(dates)


def snapshot_dollar_volumes(
    dates: List[str], limiter: SlidingWindowLimiter
) -> pd.DataFrame:
    """One row per symbol: median dollar volume and bar count across dates."""
    rows: List[dict] = []
    for day in dates:
        payload = massive_get(
            f"/v2/aggs/grouped/locale/us/market/stocks/{day}",
            {"adjusted": "true", "include_otc": "false"},
            limiter,
        )
        results = payload.get("results") or []
        print(f"snapshot {day}: {len(results)} symbols", flush=True)
        for item in results:
            close = item.get("c")
            volume = item.get("v")
            if close and volume:
                rows.append(
                    {
                        "symbol": item.get("T"),
                        "date": day,
                        "dollar_volume": float(close) * float(volume),
                    }
                )
    frame = pd.DataFrame(rows)
    agg = (
        frame.groupby("symbol")
        .agg(
            median_dollar_volume=("dollar_volume", "median"),
            bar_count=("dollar_volume", "size"),
        )
        .reset_index()
    )
    return agg[agg["bar_count"] >= SNAPSHOT_DAYS * 0.8]


def common_stock_symbols() -> set:
    payload = finnhub_get("/stock/symbol", {"exchange": "US"})
    keep = set()
    for item in payload:
        if item.get("type") in COMMON_TYPES:
            keep.add(item.get("symbol"))
    print(f"finnhub common-stock symbols: {len(keep)}", flush=True)
    return keep


def main() -> int:
    if os.path.exists(UNIVERSE_PATH):
        frame = pd.read_csv(UNIVERSE_PATH)
        print(f"universe exists: {len(frame)} symbols -> {UNIVERSE_PATH}")
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)
    limiter = SlidingWindowLimiter()
    dates = recent_trading_dates(limiter)
    volumes = snapshot_dollar_volumes(dates, limiter)
    volumes = volumes.sort_values("median_dollar_volume", ascending=False)
    pool = volumes.head(UNIVERSE_SIZE * 2)
    commons = common_stock_symbols()
    picked = pool[pool["symbol"].isin(commons)].head(UNIVERSE_SIZE).copy()
    if len(picked) < UNIVERSE_SIZE:
        print(
            f"warning: only {len(picked)} common stocks in the top "
            f"{UNIVERSE_SIZE * 2} by volume; keeping them all",
            flush=True,
        )
    picked["snapshot_dates"] = f"{dates[0]}..{dates[-1]}"
    picked.to_csv(UNIVERSE_PATH, index=False)
    print(f"universe written: {len(picked)} symbols -> {UNIVERSE_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

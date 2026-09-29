"""Fetch overlays for source partitions that contain empty trading dates.

The upstream research snapshot occasionally persisted a month even when an
individual Tushare response was empty. This script detects only materially
incomplete dates and stores complete replacement days inside this study. It
does not mutate the source study.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
import tushare as ts


STUDY_DIR = Path(__file__).resolve().parent
REPO_DIR = STUDY_DIR.parents[1]
SOURCE_DIR = REPO_DIR / "studies" / "reverse_rule_induction" / "data" / "shards"
REPAIR_DIR = STUDY_DIR / "data" / "market_repair"
MIN_COVERAGE_RATIO = 0.80
MIN_CALL_INTERVAL_SECONDS = 0.18
DAILY_BASIC_FIELDS = (
    "ts_code,trade_date,turnover_rate,pe_ttm,pb,dv_ttm,total_mv"
)


def call_with_retry(function, **kwargs) -> pd.DataFrame:
    """Call one endpoint with bounded backoff and a stable rate ceiling."""
    last_error: Optional[Exception] = None
    for delay in (0, 2, 5, 10, 20, 40):
        if delay:
            time.sleep(delay)
        try:
            frame = function(**kwargs)
            time.sleep(MIN_CALL_INTERVAL_SECONDS)
            return frame
        except Exception as error:  # noqa: BLE001 - provider errors vary by endpoint.
            last_error = error
    raise RuntimeError(f"Tushare request failed after retries: {last_error}")


def atomic_parquet_write(frame: pd.DataFrame, path: Path) -> None:
    """Write one replacement trading date without partial-file risk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(temporary_path, index=False)
    temporary_path.replace(path)


def incomplete_dates(table: str) -> list[tuple[str, int]]:
    """Find source dates whose row count is materially below daily prices."""
    missing: list[tuple[str, int]] = []
    for daily_path in sorted(SOURCE_DIR.glob("daily_??????.parquet")):
        month = daily_path.stem.rsplit("_", 1)[1]
        daily_counts = pd.read_parquet(daily_path, columns=["trade_date"]).groupby(
            "trade_date"
        ).size()
        source_counts = pd.read_parquet(
            SOURCE_DIR / f"{table}_{month}.parquet",
            columns=["trade_date"],
        ).groupby("trade_date").size()
        for trade_date, expected_rows in daily_counts.items():
            actual_rows = int(source_counts.get(trade_date, 0))
            if actual_rows / expected_rows < MIN_COVERAGE_RATIO:
                missing.append((str(trade_date), int(expected_rows)))
    return missing


def fetch_repairs(pro_api, table: str) -> None:
    """Fetch every missing date once and validate response completeness."""
    dates = incomplete_dates(table)
    for position, (trade_date, expected_rows) in enumerate(dates, start=1):
        output_path = REPAIR_DIR / f"{table}_{trade_date}.parquet"
        if output_path.exists():
            repaired = pd.read_parquet(output_path)
        elif table == "adj_factor":
            repaired = call_with_retry(pro_api.adj_factor, trade_date=trade_date)
            atomic_parquet_write(repaired, output_path)
        elif table == "daily_basic":
            repaired = call_with_retry(
                pro_api.daily_basic,
                trade_date=trade_date,
                fields=DAILY_BASIC_FIELDS,
            )
            atomic_parquet_write(repaired, output_path)
        else:
            raise ValueError(f"Unsupported repair table: {table}")

        coverage = len(repaired) / expected_rows
        if coverage < MIN_COVERAGE_RATIO:
            raise RuntimeError(
                f"Repair {output_path.name} covers only {coverage:.1%} of daily rows."
            )
        if position % 25 == 0 or position == len(dates):
            print(f"repaired {table}: {position}/{len(dates)} dates", flush=True)


def main() -> None:
    """Repair adjusted factors and daily basic data without changing source files."""
    load_dotenv(REPO_DIR / ".env")
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("TUSHARE_TOKEN is missing from the repository environment.")
    pro_api = ts.pro_api(token)
    fetch_repairs(pro_api, "adj_factor")
    fetch_repairs(pro_api, "daily_basic")
    print("Market snapshot repair complete.")


if __name__ == "__main__":
    main()

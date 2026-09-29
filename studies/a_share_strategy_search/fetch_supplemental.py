"""Fetch point-in-time event data and industry-index histories for the study.

The primary stock panel is reused from the reverse-rule-induction snapshot.
This script only downloads sources that are absent from that snapshot. Every
output is written atomically so an interrupted run can safely resume.
"""

from __future__ import annotations

import calendar
import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
import tushare as ts


STUDY_DIR = Path(__file__).resolve().parent
REPO_DIR = STUDY_DIR.parents[1]
DATA_DIR = STUDY_DIR / "data"
START_YEAR = 2016
END_YEAR = 2026
END_DATE = "20260930"
MIN_CALL_INTERVAL_SECONDS = 0.18
MAX_ROWS_PER_RESPONSE = 2_000
EXPRESS_FIELDS = (
    "ts_code,ann_date,end_date,revenue,operate_profit,total_profit,n_income,"
    "total_assets,diluted_eps,diluted_roe,yoy_net_profit,bps,perf_summary"
)


def call_with_retry(function, **kwargs) -> pd.DataFrame:
    """Call one Tushare endpoint with bounded backoff and fail-fast semantics."""
    delays = (0, 2, 5, 10, 20, 40)
    last_error: Optional[Exception] = None
    for delay in delays:
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
    """Write a parquet checkpoint without exposing partially written files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(temporary_path, index=False)
    temporary_path.replace(path)


def month_windows() -> list[tuple[str, str, str]]:
    """Return bounded calendar-month windows for the event endpoint."""
    windows: list[tuple[str, str, str]] = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            start = f"{year:04d}{month:02d}01"
            last_day = calendar.monthrange(year, month)[1]
            end = min(f"{year:04d}{month:02d}{last_day:02d}", END_DATE)
            if start <= END_DATE:
                windows.append((f"{year:04d}{month:02d}", start, end))
    return windows


def fetch_express(pro_api) -> None:
    """Fetch earnings-express disclosures by announcement window."""
    output_dir = DATA_DIR / "express"
    for index, (month, start_date, end_date) in enumerate(month_windows(), start=1):
        output_path = output_dir / f"express_{month}.parquet"
        if output_path.exists():
            continue
        frame = call_with_retry(
            pro_api.express,
            start_date=start_date,
            end_date=end_date,
            fields=EXPRESS_FIELDS,
        )
        if len(frame) >= MAX_ROWS_PER_RESPONSE:
            raise RuntimeError(
                f"Express response for {month} reached {len(frame)} rows; "
                "split the checkpoint before continuing."
            )
        if frame.empty:
            frame = pd.DataFrame(columns=EXPRESS_FIELDS.split(","))
        atomic_parquet_write(frame, output_path)
        print(f"express {month}: {len(frame)} rows ({index}/{len(month_windows())})", flush=True)


def fetch_industry_indexes(pro_api) -> None:
    """Fetch published SW2021 level-one index histories in bounded chunks."""
    classification_path = DATA_DIR / "sw2021_level1.parquet"
    if classification_path.exists():
        classification = pd.read_parquet(classification_path)
    else:
        classification = call_with_retry(
            pro_api.index_classify,
            level="L1",
            src="SW2021",
        )
        required_columns = {"index_code", "industry_name", "level", "src"}
        missing = required_columns.difference(classification.columns)
        if missing:
            raise RuntimeError(f"Index classification is missing columns: {sorted(missing)}")
        atomic_parquet_write(classification, classification_path)

    output_dir = DATA_DIR / "sw_daily"
    date_chunks = (
        ("20160101", "20181231"),
        ("20190101", "20211231"),
        ("20220101", "20241231"),
        ("20250101", END_DATE),
    )
    for position, row in enumerate(classification.itertuples(index=False), start=1):
        code = str(row.index_code)
        output_path = output_dir / f"sw_daily_{code.replace('.', '_')}.parquet"
        if output_path.exists():
            continue
        frames: list[pd.DataFrame] = []
        for start_date, end_date in date_chunks:
            frame = call_with_retry(
                pro_api.sw_daily,
                ts_code=code,
                start_date=start_date,
                end_date=end_date,
            )
            if len(frame) >= MAX_ROWS_PER_RESPONSE:
                raise RuntimeError(
                    f"SW daily response for {code} {start_date}-{end_date} "
                    f"reached {len(frame)} rows; split the checkpoint before continuing."
                )
            frames.append(frame)
        history = pd.concat(frames, ignore_index=True)
        history = history.drop_duplicates(["ts_code", "trade_date"], keep="last")
        history = history.sort_values("trade_date")
        atomic_parquet_write(history, output_path)
        print(
            f"sw_daily {code}: {len(history)} rows "
            f"({position}/{len(classification)})",
            flush=True,
        )


def main() -> None:
    """Load credentials and fetch every supplemental checkpoint."""
    load_dotenv(REPO_DIR / ".env")
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("TUSHARE_TOKEN is missing from the repository environment.")
    pro_api = ts.pro_api(token)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fetch_express(pro_api)
    fetch_industry_indexes(pro_api)
    print("Supplemental data fetch complete.")


if __name__ == "__main__":
    main()

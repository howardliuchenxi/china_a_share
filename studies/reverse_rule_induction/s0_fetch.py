"""S0: fetch full-market daily snapshot from tushare into month-shard parquet files.

Checkpoint unit = (table, YYYYMM) shard file. A shard is written atomically only
when every trading day in that month succeeded for that table; on restart, any
missing shard is simply refetched. The token is read from the repo .env and is
never logged.
"""
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from dotenv import load_dotenv

REPO = "/Users/lcx/workspace/china_a_share"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SHARDS = os.path.join(DATA, "shards")

START_DATE = "20160101"
END_DATE = time.strftime("%Y%m%d")

load_dotenv(os.path.join(REPO, ".env"))
TOKEN = os.environ.get("TUSHARE_TOKEN", "")
if not TOKEN:
    sys.exit("FATAL: TUSHARE_TOKEN missing")

import tushare as ts  # noqa: E402

pro = ts.pro_api(TOKEN)

DAILY_BASIC_FIELDS = (
    "ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,"
    "pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv"
)

# Adaptive global rate limiter: start conservative, back off on rate errors.
_CALL_LOCK = threading.Lock()
_MIN_INTERVAL = 60.0 / 400.0  # 400 calls/min ceiling to start
_last_call = [0.0]


def _throttle():
    with _CALL_LOCK:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _call(fn, **kwargs) -> pd.DataFrame:
    """One rate-limited, retried tushare call. Raises after backoff exhausted."""
    global _MIN_INTERVAL
    delays = [2, 4, 8, 15, 30, 60, 90]
    last_exc: Exception | None = None
    for attempt, d in enumerate([0] + delays):
        if d:
            time.sleep(d)
        _throttle()
        try:
            return fn(**kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            msg = str(exc)
            if "每分钟" in msg or "频" in msg or "limit" in msg.lower():
                with _CALL_LOCK:
                    _MIN_INTERVAL = min(60.0 / 60.0, _MIN_INTERVAL * 1.6)
                print(f"  rate-limited -> global interval now {_MIN_INTERVAL*1000:.0f}ms")
    raise RuntimeError(f"tushare call failed after retries: {last_exc}")


def trade_dates() -> list[str]:
    cal = _call(pro.trade_cal, exchange="SSE", start_date="20151201", end_date=END_DATE)
    cal = cal[cal["is_open"] == 1]
    dates = sorted(cal["cal_date"].astype(str))
    return [d for d in dates if START_DATE <= d <= END_DATE]


def fetch_table_dates(table: str, dates: list[str]) -> None:
    os.makedirs(SHARDS, exist_ok=True)
    months = sorted({d[:6] for d in dates})
    todo = [m for m in months if not os.path.exists(os.path.join(SHARDS, f"{table}_{m}.parquet"))]
    print(f"[{table}] {len(months)} months total, {len(todo)} to fetch")
    for mi, month in enumerate(todo):
        mdates = [d for d in dates if d[:6] == month]
        frames: list[pd.DataFrame] = []
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=16) as pool:
            futs = {}
            for d in mdates:
                if table == "daily":
                    f = pool.submit(_call, pro.daily, trade_date=d)
                elif table == "adj_factor":
                    f = pool.submit(_call, pro.adj_factor, trade_date=d)
                elif table == "daily_basic":
                    f = pool.submit(
                        _call, pro.daily_basic, trade_date=d, fields=DAILY_BASIC_FIELDS)
                elif table == "stock_st":
                    f = pool.submit(_call, pro.stock_st, trade_date=d)
                futs[f] = d
            for fut in as_completed(futs):
                d = futs[fut]
                try:
                    frames.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{d}: {str(exc)[:120]}")
        if failures:
            print(f"[{table}] month {month} has {len(failures)} failures, shard NOT written")
            for f in failures:
                print("   ", f)
            continue
        shard = pd.concat(frames, ignore_index=True)
        tmp = os.path.join(SHARDS, f"{table}_{month}.parquet.tmp")
        shard.to_parquet(tmp, index=False)
        os.replace(tmp, os.path.join(SHARDS, f"{table}_{month}.parquet"))
        print(f"[{table}] {month}: {len(shard)} rows ({mi+1}/{len(todo)})")


def fetch_reference() -> None:
    os.makedirs(SHARDS, exist_ok=True)
    cal = _call(pro.trade_cal, exchange="SSE", start_date="20151201", end_date=END_DATE)
    cal.to_parquet(os.path.join(SHARDS, "trade_cal.parquet"), index=False)

    parts = []
    for status in ("L", "D", "P"):
        df = _call(pro.stock_basic, exchange="", list_status=status,
                   fields="ts_code,symbol,name,area,industry,market,list_date,list_status")
        parts.append(df)
    pd.concat(parts, ignore_index=True).to_parquet(
        os.path.join(SHARDS, "stock_basic.parquet"), index=False)

    # namechange: bulk paginated (~14k rows, fits under the 100k offset cap)
    rows = []
    offset = 0
    while True:
        df = _call(pro.namechange, ts_code="", start_date="", end_date="",
                   limit=1000, offset=offset,
                   fields="ts_code,name,start_date,end_date,ann_date,change_reason")
        if df is None or df.empty:
            break
        rows.append(df)
        if len(df) < 1000:
            break
        offset += 1000
        if offset > 200_000:
            print("namechange: safety stop at 200k rows")
            break
    if rows:
        pd.concat(rows, ignore_index=True).to_parquet(
            os.path.join(SHARDS, "namechange.parquet"), index=False)
        print(f"namechange: {sum(len(r) for r in rows)} rows")


def main() -> None:
    t0 = time.time()
    fetch_reference()
    dates = trade_dates()
    print(f"{len(dates)} trading days {dates[0]}..{dates[-1]}")
    for table in ("daily", "adj_factor", "daily_basic", "stock_st"):
        fetch_table_dates(table, dates)
    print(f"DONE in {(time.time()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()

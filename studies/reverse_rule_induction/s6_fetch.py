"""RRI-2 S6: fetch top_list / top_inst / moneyflow into month shards.

Same checkpoint pattern as s0_fetch.py: shard = (table, YYYYMM), atomic write,
resume by missing shards. Token never logged.
"""
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from dotenv import load_dotenv

STUDY = os.path.dirname(os.path.abspath(__file__))
SHARDS = os.path.join(STUDY, "data", "shards")
START_DATE = "20160101"
END_DATE = time.strftime("%Y%m%d")

load_dotenv("/Users/lcx/workspace/china_a_share/.env")
TOKEN = os.environ.get("TUSHARE_TOKEN", "")
if not TOKEN:
    sys.exit("FATAL: TUSHARE_TOKEN missing")

import tushare as ts  # noqa: E402

pro = ts.pro_api(TOKEN)

_CALL_LOCK = threading.Lock()
_MIN_INTERVAL = 60.0 / 400.0
_last_call = [0.0]


def _throttle():
    with _CALL_LOCK:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _call(fn, **kwargs) -> pd.DataFrame:
    global _MIN_INTERVAL
    delays = [2, 4, 8, 15, 30, 60, 90]
    last_exc: Exception | None = None
    for d in [0] + delays:
        if d:
            time.sleep(d)
        _throttle()
        try:
            return fn(**kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if "每分钟" in str(exc) or "频" in str(exc):
                with _CALL_LOCK:
                    _MIN_INTERVAL = min(60.0 / 60.0, _MIN_INTERVAL * 1.6)
                print(f"  rate-limited -> interval {_MIN_INTERVAL*1000:.0f}ms", flush=True)
    raise RuntimeError(f"tushare call failed after retries: {last_exc}")


def trade_dates() -> list[str]:
    cal = _call(pro.trade_cal, exchange="SSE", start_date="20151201", end_date=END_DATE)
    cal = cal[cal["is_open"] == 1]
    dates = sorted(cal["cal_date"].astype(str))
    return [d for d in dates if START_DATE <= d <= END_DATE]


def fetch_table(table: str, dates: list[str]) -> None:
    os.makedirs(SHARDS, exist_ok=True)
    months = sorted({d[:6] for d in dates})
    todo = [m for m in months
            if not os.path.exists(os.path.join(SHARDS, f"{table}_{m}.parquet"))]
    print(f"[{table}] {len(months)} months, {len(todo)} to fetch", flush=True)
    t0 = time.time()
    for mi, month in enumerate(todo):
        mdates = [d for d in dates if d[:6] == month]
        frames, failures = [], []
        with ThreadPoolExecutor(max_workers=16) as pool:
            futs = {pool.submit(_call, getattr(pro, table), trade_date=d): d
                    for d in mdates}
            for fut in as_completed(futs):
                try:
                    frames.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{futs[fut]}: {str(exc)[:120]}")
        if failures:
            print(f"[{table}] {month}: {len(failures)} failures, shard skipped", flush=True)
            for f in failures[:3]:
                print("   ", f)
            continue
        shard = pd.concat(frames, ignore_index=True)
        tmp = os.path.join(SHARDS, f"{table}_{month}.parquet.tmp")
        shard.to_parquet(tmp, index=False)
        os.replace(tmp, os.path.join(SHARDS, f"{table}_{month}.parquet"))
        if (mi + 1) % 10 == 0 or mi == len(todo) - 1:
            rate = (mi + 1) / max(time.time() - t0, 1) * 60
            print(f"[{table}] {month}: {len(shard)} rows ({mi+1}/{len(todo)}, "
                  f"{rate:.0f} min/month-pace)", flush=True)


def main() -> None:
    t0 = time.time()
    dates = trade_dates()
    print(f"{len(dates)} trading days", flush=True)
    for table in ("top_list", "top_inst", "moneyflow"):
        fetch_table(table, dates)
    print(f"DONE in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()

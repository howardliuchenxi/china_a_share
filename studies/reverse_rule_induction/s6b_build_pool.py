"""RRI-2 S6b: build factor_pool_v2.parquet = phase-1 pool + 10 new factors."""
import glob
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rri.mffactors import (  # noqa: E402
    NEW_FACTORS,
    moneyflow_factors,
    normalize_by_event_amount,
    top_list_factors,
)

STUDY = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(STUDY, "data")
SHARDS = os.path.join(DATA, "shards")


def _read(name: str) -> pd.DataFrame:
    prefix = name.split("_")
    paths = []
    for p in sorted(glob.glob(os.path.join(SHARDS, f"{name}_*.parquet"))):
        base = os.path.basename(p)[: -len(".parquet")]
        segs = base.split("_")
        if segs[:-1] == prefix and len(segs) == len(prefix) + 1:
            paths.append(p)
    if not paths:
        raise FileNotFoundError(f"no shards for {name}")
    return pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)


def main() -> None:
    t0 = time.time()
    pool = pd.read_parquet(os.path.join(DATA, "factor_pool.parquet"))
    print(f"phase-1 pool: {pool.shape}", flush=True)

    cal = pd.read_parquet(os.path.join(SHARDS, "trade_cal.parquet"))
    cal = cal[cal["is_open"] == 1].sort_values("cal_date")
    date_rank = {str(d): i for i, d in enumerate(cal["cal_date"].astype(str))}

    events_str = pool[["ts_code", "trade_date"]].copy()
    events_str["trade_date"] = events_str["trade_date"].astype(str)

    mf = _read("moneyflow")
    mf_f = moneyflow_factors(mf, date_rank, events_str)
    del mf
    print(f"moneyflow factors done ({time.time()-t0:.0f}s)", flush=True)

    tl = _read("top_list")
    ti = None
    try:
        ti = _read("top_inst")
    except FileNotFoundError:
        print("top_inst unavailable; inst_net_buy_ratio will be NaN", flush=True)
    tl_f = top_list_factors(tl, ti, date_rank, events_str)
    del tl, ti
    print(f"top-list factors done ({time.time()-t0:.0f}s)", flush=True)

    newf = pd.concat([mf_f, tl_f], axis=1)
    # pool rows are the event rows in the same order -> take columns directly
    amt = pool["amount"].to_numpy(dtype=float)
    newf = normalize_by_event_amount(newf, amt)
    # mf_main_net_5d_sum: convert 万元 -> ratio vs circ_mv (万元) at anchor
    circ = pool["circ_mv"].to_numpy(dtype=float)
    s5 = newf["mf_main_net_5d_sum"].to_numpy(dtype=float)
    ok = np.isfinite(circ) & (circ > 0) & np.isfinite(s5)
    pool["mf_main_net_5d_to_circ_mv"] = np.where(ok, s5 / circ, np.nan)
    pool["mf_main_net_z20"] = newf["mf_main_net_z20"].to_numpy()
    for col in ("mf_net_ratio", "mf_main_net_ratio", "mf_elg_net_ratio",
                "on_top_list", "tl_net_amount_ratio", "tl_amount_rate_max",
                "top_list_count_20d", "inst_net_buy_ratio"):
        pool[col] = newf[col].to_numpy()

    pool.to_parquet(os.path.join(DATA, "factor_pool_v2.parquet"), index=False)
    cov = {c: float(pd.to_numeric(pool[c], errors="coerce").notna().mean())
           for c in NEW_FACTORS if c in pool.columns} | {
        "mf_main_net_5d_to_circ_mv": float(pool["mf_main_net_5d_to_circ_mv"].notna().mean())}
    with open(os.path.join(DATA, "new_factor_coverage.json"), "w") as fh:
        json.dump(cov, fh, indent=1)
    print(f"factor_pool_v2.parquet: {pool.shape}, coverage={cov}", flush=True)
    print(f"DONE in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()

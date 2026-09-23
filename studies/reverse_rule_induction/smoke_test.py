"""Smoke test: run the full S1-S3 path on a few months of real shards (fast)."""
import glob
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rri.limits import board_of, st_flag_from_names  # noqa: E402
from rri.events import trigger_events, market_daily_returns, label_events  # noqa: E402
from rri.factors import build_factor_matrix, ALL_FACTORS  # noqa: E402
from rri.search import (  # noqa: E402
    m1_single_conditions, m2_tree_paths, m3_beam_search, bh_adjust,
)

SHARDS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "shards")


def pick_months(n: int = 4) -> list[str]:
    months = sorted({os.path.basename(p).replace("daily_", "").replace(".parquet", "")
                     for p in glob.glob(os.path.join(SHARDS, "daily_*.parquet"))})
    complete = [m for m in months
                if all(os.path.exists(os.path.join(SHARDS, f"{t}_{m}.parquet"))
                       for t in ("daily", "adj_factor", "daily_basic"))]
    return complete[-n:]


def load_subset() -> tuple[pd.DataFrame, np.ndarray]:
    daily = pd.concat([pd.read_parquet(os.path.join(SHARDS, f"daily_{m}.parquet"))
                       for m in pick_months()], ignore_index=True)
    adj = pd.concat([pd.read_parquet(os.path.join(SHARDS, f"adj_factor_{m}.parquet"))
                     for m in pick_months()], ignore_index=True)
    basic = pd.read_parquet(os.path.join(SHARDS, "stock_basic.parquet"))
    codes = set(basic[basic["ts_code"].str.endswith((".SH", ".SZ"))]
                [~basic["symbol"].astype(str).str.startswith(("900", "200"))]["ts_code"])
    daily = daily[daily["ts_code"].isin(codes)]
    adj = adj[adj["ts_code"].isin(codes)][["ts_code", "trade_date", "adj_factor"]]
    panel = daily.merge(adj, on=["ts_code", "trade_date"], how="left")
    db = pd.concat([pd.read_parquet(os.path.join(SHARDS, f"daily_basic_{m}.parquet"))
                    for m in pick_months()],
                   ignore_index=True)
    db = db[db["ts_code"].isin(codes)]
    panel = panel.merge(db[["ts_code", "trade_date", "volume_ratio", "turnover_rate",
                            "total_mv", "circ_mv", "pe_ttm", "pb", "dv_ttm"]],
                        on=["ts_code", "trade_date"], how="left")
    cal = pd.read_parquet(os.path.join(SHARDS, "trade_cal.parquet"))
    cal = cal[cal["is_open"] == 1].sort_values("cal_date")
    dates = cal["cal_date"].astype(str).to_numpy()
    date_rank = {d: i for i, d in enumerate(dates)}
    panel["trade_date"] = panel["trade_date"].astype(str)
    panel = panel[panel["trade_date"].isin(date_rank)].copy()
    panel["session_rank"] = panel["trade_date"].map(date_rank).astype(np.int64)
    panel = panel.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    panel["board"] = board_of(panel["ts_code"])
    nc = pd.read_parquet(os.path.join(SHARDS, "namechange.parquet"))
    basic_name = basic.set_index(basic["ts_code"].astype(str))["name"].astype(str)
    panel["is_st"] = st_flag_from_names(nc, basic_name, panel["ts_code"],
                                        panel["trade_date"].to_numpy(dtype=np.int64))
    for col in ("open", "high", "low", "close", "pre_close", "vol", "amount",
                "adj_factor", "volume_ratio", "turnover_rate", "total_mv", "circ_mv"):
        panel[col] = pd.to_numeric(panel[col], errors="coerce")
    return panel, dates


def main() -> None:
    t0 = time.time()
    months = pick_months()
    oos_cut = int(months[-1] + "00")  # last month acts as pseudo-OOS
    panel, dates = load_subset()
    print(f"panel: {len(panel):,} rows, {panel['ts_code'].nunique()} codes, "
          f"{len(dates)} dates ({time.time()-t0:.0f}s)")
    mkt = market_daily_returns(panel)
    events = trigger_events(panel, len(panel))
    for k, v in events.items():
        print(f"  {k}: {len(v):,} raw events")
    labeled = label_events(events["E1"].copy(), panel, mkt, window=5)
    valid = labeled.dropna(subset=["fwd_excess_5d"])
    print(f"E1 window=5 labels: {len(valid):,} valid, "
          f"positive rate {(valid['fwd_excess_5d'] >= 0.02).mean():.3f}")
    ev = valid[valid["trade_date"] < oos_cut].copy()  # last month = pseudo-OOS
    ev_all = label_events(events["E1"].copy(), panel, mkt, window=5).dropna(
        subset=["fwd_excess_5d"])
    fm = build_factor_matrix(panel, dates, ev_all)
    fcols = [c for c in ALL_FACTORS if c in fm.columns]
    merge_cols = [c for c in fcols if c not in ev_all.columns]
    ev2 = ev_all.merge(fm[["ts_code", "trade_date", *merge_cols]],
                       on=["ts_code", "trade_date"], how="inner")
    print(f"factor matrix: {fm.shape}, non-null {fm[fcols].notna().mean().mean():.3f}, "
          f"aligned events: {len(ev2)}")
    X = ev2[fcols].reset_index(drop=True)
    y = (ev2["fwd_excess_5d"].to_numpy() >= 0.02).astype(int)
    is_mask = (ev2["trade_date"] < oos_cut).to_numpy()
    Xis, yis = X[is_mask].reset_index(drop=True), y[is_mask]
    min_sup = max(20, int(len(yis) * 0.05))
    t1 = time.time()
    c1 = m1_single_conditions(Xis, yis, min_sup)
    c2 = m2_tree_paths(Xis, yis, min_sup)
    c3 = m3_beam_search(Xis, yis, min_sup)
    print(f"M1={len(c1)} M2={len(c2)} M3={len(c3)} candidates "
          f"({time.time()-t1:.1f}s)")
    allc = c1 + c2 + c3
    if allc:
        df = pd.DataFrame([{**{k2: v for k2, v in c.items() if k2 != 'conditions'},
                            'n_conds': len(c['conditions'])} for c in allc])
        df["q"] = bh_adjust(df["p_value"].to_numpy())
        print(df.sort_values("q").head(5)[["method", "n_conds", "support",
                                           "hit_rate", "baseline_rate", "lift", "q"]].to_string())
    print(f"SMOKE OK total {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()

"""RRI-2 S7: review phase-1 near-miss conditions under alternative labels,
yearly OOS trajectories, and null-distribution permutation percentiles."""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rri.data import load_panel  # noqa: E402
from rri.events import label_events, market_daily_returns  # noqa: E402
from rri.search import mask_of  # noqa: E402

STUDY = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(STUDY, "data")
IS_END = 20231231
WINDOWS = [(10, 0.05), (10, 0.15), (40, 0.05), (40, 0.15)]


def parse_conds(s):
    return [tuple(x) for x in json.loads(s)] if isinstance(s, str) else \
        [tuple(c) for c in s]


def main() -> None:
    panel, basic, dates = load_panel()
    mkt = market_daily_returns(panel)
    date_rank = {str(d): i for i, d in enumerate(dates)}

    null = pd.read_csv(os.path.join(DATA, "null_distribution.csv"))
    null_max = null["max_oos_lift"].dropna().to_numpy()

    fp = pd.read_parquet(os.path.join(DATA, "factor_pool.parquet"))
    frames = []
    for fam in ("E1", "E1_EXT", "E2"):
        df = pd.read_parquet(os.path.join(DATA, f"conditions_{fam}.parquet"))
        df["family"] = fam
        frames.append(df)
    allc = pd.concat(frames, ignore_index=True)
    near = allc[(allc["q_value"] < 0.10) & (allc["oos_lift"] >= 1.10)
                & (allc["boot_ci_lo"] > 1.0) & (allc["platform_cv"] < 0.30)]
    print(f"near-miss conditions: {len(near)}", flush=True)

    # per family: labels for all windows, merged with factor pool ONCE
    merged_by_family = {}
    for fam in ("E1", "E1_EXT", "E2"):
        ev = pd.read_parquet(os.path.join(DATA, f"events_{fam}.parquet"))
        for w, _ in WINDOWS:
            if f"fwd_excess_{w}d" not in ev.columns:
                ev = label_events(ev, panel, mkt, window=w)
        merged = ev.drop(columns=["streak_len"], errors="ignore").merge(
            fp, on=["ts_code", "trade_date"], how="inner")
        merged_by_family[fam] = merged

    rows = []
    for rec in near.itertuples(index=False):
        conds = parse_conds(rec.conditions)
        fam = rec.family
        X = merged_by_family[fam]
        entry = {"family": fam, "conditions": rec.conditions,
                 "method": rec.method,
                 "is_lift_20d_10pct": rec.lift,
                 "oos_lift_20d_10pct": rec.oos_lift,
                 "pct_vs_null": float((null_max < rec.oos_lift).mean() * 100)
                 if len(null_max) else np.nan}
        # ① label matrix
        for w, thr in WINDOWS:
            col = f"fwd_excess_{w}d"
            sub = X.dropna(subset=[col])
            y = (sub[col] >= thr).to_numpy(dtype=int)
            is_mask = (sub["trade_date"] <= IS_END).to_numpy()
            yis, yoos = y[is_mask], y[~is_mask]
            subis, suboos = sub[is_mask], sub[~is_mask]
            mi = mask_of(subis, conds)
            mo = mask_of(suboos, conds)
            bi = float(yis[~mi].mean()) if (~mi).sum() else np.nan
            bo = float(yoos[~mo].mean()) if (~mo).sum() else np.nan
            entry[f"is_lift_{w}d_{int(thr*100)}"] = (
                float(yis[mi].mean()) / bi if mi.sum() >= 30 and bi and bi > 0 else np.nan)
            entry[f"oos_lift_{w}d_{int(thr*100)}"] = (
                float(yoos[mo].mean()) / bo if mo.sum() >= 30 and bo and bo > 0 else np.nan)
        # ② yearly OOS trajectory (20d / 10% label)
        col = "fwd_excess_20d"
        sub = X.dropna(subset=[col])
        sub = sub[sub["trade_date"] > IS_END]
        y = (sub[col] >= 0.10).to_numpy(dtype=int)
        for year in (2024, 2025, 2026):
            ymask = (sub["trade_date"] // 10000 == year).to_numpy()
            if ymask.sum() < 50:
                entry[f"oos_lift_{year}"] = np.nan
                continue
            Xy, yy = sub[ymask], y[ymask]
            mo = mask_of(Xy, conds)
            bo = float(yy[~mo].mean()) if (~mo).sum() else np.nan
            entry[f"oos_lift_{year}"] = (
                float(yy[mo].mean()) / bo if mo.sum() >= 20 and bo and bo > 0 else np.nan)
        rows.append(entry)
        print(f"reviewed {fam} #{rec.name if hasattr(rec,'name') else ''}: "
              f"{rec.conditions[:50]}", flush=True)

    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(DATA, "near_miss_review.csv"), index=False)
    print(f"near_miss_review.csv written: {len(out)} rows", flush=True)


if __name__ == "__main__":
    main()

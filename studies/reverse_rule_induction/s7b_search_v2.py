"""RRI-2 S7b: re-run null distribution and three-method searches on the
expanded factor vocabulary; apply the same four-gate survival criteria."""
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rri.pipeline import (  # noqa: E402
    analyze_family,
    checkpoint,
    run_null_distribution,
)
import rri.factors as factors_mod  # noqa: E402

STUDY = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(STUDY, "data")
IS_END = 20231231


def dedup_factors(pool: pd.DataFrame, ev: pd.DataFrame,
                  candidates: list[str]) -> tuple[list[str], list[str]]:
    """Spearman |rho|>0.8 clustering on E1 IS rows; keep the higher info-gain."""
    from rri.pipeline import MAIN_THRESHOLD
    col = "fwd_excess_20d"
    ev = ev.dropna(subset=[col])
    is_mask = (ev["trade_date"] <= IS_END).to_numpy()
    Xis = ev[is_mask][candidates].astype(float)
    yis = (ev[is_mask][col] >= MAIN_THRESHOLD).to_numpy(dtype=int)
    corr = Xis.corr(method="spearman").abs()
    np.fill_diagonal(corr.to_numpy(), 0.0)
    info_gain = {c: abs(np.corrcoef(Xis[c].fillna(Xis[c].median()), yis)[0, 1])
                 for c in candidates if Xis[c].notna().sum() > 100}
    dropped: set[str] = set()
    for a in candidates:
        if a in dropped or a not in info_gain:
            continue
        for b in candidates:
            if b == a or b in dropped or b not in info_gain:
                continue
            if corr.loc[a, b] > 0.8:
                dropped.add(b if info_gain[a] >= info_gain[b] else a)
    kept = [c for c in candidates if c not in dropped]
    return kept, sorted(dropped)


def main() -> None:
    t0 = time.time()
    pool_v2 = pd.read_parquet(os.path.join(DATA, "factor_pool_v2.parquet"))
    new_cols = [c for c in pool_v2.columns
                if c not in ("ts_code", "trade_date")]

    # dedup across the full v2 vocabulary on E1 IS rows
    ev1 = pd.read_parquet(os.path.join(DATA, "events_E1.parquet"))
    ev1 = ev1.drop(columns=["streak_len"], errors="ignore").merge(
        pool_v2, on=["ts_code", "trade_date"], how="inner")
    kept, dropped = dedup_factors(pool_v2, ev1, new_cols)
    with open(os.path.join(DATA, "dropped_factors_v2.json"), "w") as fh:
        json.dump({"kept": kept, "dropped": dropped}, fh, indent=1)
    checkpoint("S6b dedup v2", {"kept": len(kept), "dropped": len(dropped)})
    pool_use = pool_v2[["ts_code", "trade_date", *kept]]

    # new null line for the expanded vocabulary (same seeds 1..20)
    ev1_raw = pd.read_parquet(os.path.join(DATA, "events_E1.parquet"))
    n_e1 = int(len(ev1_raw))
    max_date = int(pool_use["trade_date"].max())
    from rri.events import market_daily_returns  # noqa: E402
    from rri.data import load_panel  # noqa: E402
    panel, _b, _d = load_panel()
    mkt = market_daily_returns(panel)

    null = run_null_distribution(pool_use, panel, n_e1, mkt, kept, max_date)
    pd.DataFrame({"run": range(1, len(null["runs"]) + 1),
                  "max_oos_lift": null["runs"]}).to_csv(
        os.path.join(DATA, "null_distribution_v2.csv"), index=False)
    checkpoint("S4 null v2", {"null_95": round(null["null_95"], 4)
                              if null["null_95"] == null["null_95"] else None})

    results = {}
    for name in ("E1", "E1_EXT", "E2"):
        events = pd.read_parquet(os.path.join(DATA, f"events_{name}.parquet"))
        evaluated, meta = analyze_family(name, events, mkt, pool_use, kept,
                                         null["null_95"])
        results[name] = evaluated
        checkpoint(f"S7b {name} v2", meta)
        from rri.pipeline import _serialize_conditions
        _serialize_conditions(evaluated).to_parquet(
            os.path.join(DATA, f"conditions_v2_{name}.parquet"), index=False)

    # marginal contribution of the new factors among stage-1 survivors
    from rri.mffactors import NEW_FACTORS
    new_names = set(NEW_FACTORS) | {"mf_main_net_5d_to_circ_mv"}
    new_hits = {}
    for name, df in results.items():
        if not len(df):
            new_hits[name] = 0
            continue
        strong = df[df["q_value"] < 0.10]
        new_hits[name] = int(strong["conditions"].apply(
            lambda cs: any(tuple(c)[0] in new_names for c in cs)).sum())
    with open(os.path.join(DATA, "new_factor_contribution.json"), "w") as fh:
        json.dump({"strong_candidates_using_new_factors": new_hits,
                   "null_95_v2": null["null_95"]}, fh, indent=1)
    checkpoint("S7b done", {"elapsed_min": round((time.time() - t0) / 60, 1),
                            "new_factor_hits": new_hits})


if __name__ == "__main__":
    main()

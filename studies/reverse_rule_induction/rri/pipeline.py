"""End-to-end research pipeline: events -> factors -> search -> survival."""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd

import rri.factors as factors_mod
from rri.data import load_panel
from rri.events import (
    label_events,
    market_daily_returns,
    random_control_events,
    trigger_events,
)
from rri.factors import build_factor_matrix
from rri.search import (
    evaluate_candidates,
    m1_single_conditions,
    m2_tree_paths,
    m3_beam_search,
    mask_of,
    render_condition,
)
from rri.search import bh_adjust

STUDY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(STUDY_DIR, "data")
IS_END = 20231231
MAIN_WINDOW = 20
MAIN_THRESHOLD = 0.10
MIN_SUPPORT_PCT = 0.05
TOP_K_PER_NULL_RUN = 20
N_NULL_RUNS = 20
SENSITIVITY = [(10, 0.05), (10, 0.15), (40, 0.05), (40, 0.15)]


def checkpoint(stage: str, payload: dict) -> None:
    path = os.path.join(STUDY_DIR, "CHECKPOINT.md")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"- [{stamp}] {stage}: " + json.dumps(payload, ensure_ascii=False, default=str)
    prefix = ("# 研究执行检查点\n\n> 自动追加，每阶段一条；中断后从最后完成阶段续跑。\n\n"
              if not os.path.exists(path) else "")
    with open(path, "a") as fh:
        if prefix:
            fh.write(prefix)
        fh.write(line + "\n")
    print(line, flush=True)


def _prep(events: pd.DataFrame, factor_pool: pd.DataFrame,
          window: int = MAIN_WINDOW) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    col = f"fwd_excess_{window}d"
    # streak_len is provided by the factor pool; dropping the events-table copy
    # avoids an _x/_y suffix collision on merge
    ev = events.drop(columns=["streak_len"], errors="ignore").merge(
        factor_pool, on=["ts_code", "trade_date"], how="inner")
    ev = ev.dropna(subset=[col]).copy()
    y = (ev[col] >= MAIN_THRESHOLD).to_numpy(dtype=int)
    months = (ev["trade_date"] // 100).to_numpy(dtype=int)
    return ev, y, months


def run_null_distribution(factor_pool: pd.DataFrame, panel: pd.DataFrame,
                          n_events: int, mkt: pd.Series,
                          factor_list: list[str], max_date: int) -> dict:
    """N null runs: random anchors -> identical search -> max OOS lift of top-K."""
    maxima: list[float] = []
    for seed in range(1, N_NULL_RUNS + 1):
        t0 = time.time()
        ev = random_control_events(panel, n_events, seed=seed,
                                   date_lo=20160101, date_hi=max_date)
        ev = label_events(ev, panel, mkt, window=MAIN_WINDOW).dropna(
            subset=[f"fwd_excess_{MAIN_WINDOW}d"])
        ev, y, months = _prep(ev, factor_pool)
        is_mask = (ev["trade_date"] <= IS_END).to_numpy()
        Xis = ev[is_mask][factor_list].reset_index(drop=True)
        yis, mis = y[is_mask], months[is_mask]
        min_sup = max(50, int(len(yis) * MIN_SUPPORT_PCT))
        cands = (m1_single_conditions(Xis, yis, min_sup)
                 + m2_tree_paths(Xis, yis, min_sup)
                 + m3_beam_search(Xis, yis, min_sup))
        best = np.nan
        if cands:
            df = pd.DataFrame([dict(c) for c in cands])
            df["q_value"] = bh_adjust(df["p_value"].to_numpy())
            df = df[df["support"] >= min_sup].nsmallest(TOP_K_PER_NULL_RUN, "q_value")
            Xoos = ev[~is_mask][factor_list].reset_index(drop=True)
            yoos = y[~is_mask]
            base_all = float(yoos.mean())
            for conds in df["conditions"]:
                lc = [tuple(c) for c in conds]
                mask = mask_of(Xoos, lc)
                n_in = int(mask.sum())
                if n_in < 30 or n_in == len(yoos):
                    continue
                base = float(yoos[~mask].mean())
                if base <= 0:
                    continue
                lift = float(yoos[mask].mean()) / base
                if not np.isfinite(best) or lift > best:
                    best = lift
        maxima.append(best)
        checkpoint(f"S4 null {seed}/{N_NULL_RUNS}",
                   {"max_oos_lift": None if best != best else round(float(best), 4),
                    "secs": round(time.time() - t0)})
    arr = np.array([m for m in maxima if m == m])
    return {"runs": maxima,
            "null_95": float(np.percentile(arr, 95)) if len(arr) else np.nan}


def analyze_family(name: str, events: pd.DataFrame, mkt: pd.Series,
                   factor_pool: pd.DataFrame, factor_list: list[str],
                   null_95: float) -> tuple[pd.DataFrame, dict]:
    ev, y, months = _prep(events, factor_pool)
    is_mask = (ev["trade_date"] <= IS_END).to_numpy()
    oos_mask = ~is_mask
    Xis = ev[is_mask][factor_list].reset_index(drop=True)
    yis, mis = y[is_mask], months[is_mask]
    min_sup = max(50, int(len(yis) * MIN_SUPPORT_PCT))
    print(f"[{name}] IS events={len(yis)} positives={int(yis.sum())} "
          f"min_support={min_sup}", flush=True)
    t0 = time.time()
    cands = (m1_single_conditions(Xis, yis, min_sup)
             + m2_tree_paths(Xis, yis, min_sup)
             + m3_beam_search(Xis, yis, min_sup))
    print(f"[{name}] candidates={len(cands)} ({time.time()-t0:.0f}s)", flush=True)
    if not cands:
        return pd.DataFrame(), {"survivors": 0, "is_events": len(yis)}
    evaluated = evaluate_candidates(cands, Xis, yis, mis, min_sup, heavy=True)
    evaluated["family"] = name

    Xoos = ev[oos_mask][factor_list].reset_index(drop=True)
    yoos = y[oos_mask]
    oos_lifts, oos_support = [], []
    for conds in evaluated["conditions"]:
        lc = [tuple(c) for c in conds]
        mask = mask_of(Xoos, lc)
        n_in = int(mask.sum())
        base = float(yoos[~mask].mean()) if n_in < len(yoos) else np.nan
        oos_lifts.append(float(yoos[mask].mean()) / base
                         if n_in >= 30 and base and base > 0 else np.nan)
        oos_support.append(n_in)
    evaluated["oos_lift"] = oos_lifts
    evaluated["oos_support"] = oos_support
    evaluated["survives"] = (
        (evaluated["q_value"] < 0.10)
        & (evaluated["oos_lift"] >= 1.10)
        & (evaluated["platform_cv"] < 0.30)
        & (evaluated["boot_ci_lo"] > 1.0)
        & (evaluated["oos_lift"] > null_95)
    )
    meta = {"is_events": int(is_mask.sum()), "oos_events": int(oos_mask.sum()),
            "is_positive": int(yis.sum()), "oos_positive": int(yoos.sum()),
            "candidates": len(cands), "survivors": int(evaluated["survives"].sum())}
    return evaluated, meta


def _serialize_conditions(df: pd.DataFrame) -> pd.DataFrame:
    """Parquet-safe copy: conditions column -> JSON strings."""
    out = df.copy()
    out["conditions"] = out["conditions"].apply(
        lambda c: json.dumps([list(t) for t in c]))
    return out


def sensitivity_check(survivors: pd.DataFrame,
                      labeled_by_family: dict[str, pd.DataFrame],
                      mkt: pd.Series, panel: pd.DataFrame,
                      factor_pool: pd.DataFrame, factor_list: list[str]) -> pd.DataFrame:
    """Re-evaluate survivor conditions under alternative label definitions."""
    if survivors.empty:
        return pd.DataFrame()
    rows = []
    for window, thr in SENSITIVITY:
        for family in survivors["family"].unique():
            ev = labeled_by_family[family].drop(columns=["streak_len"], errors="ignore").merge(
                factor_pool, on=["ts_code", "trade_date"], how="inner")
            ev = label_events(ev, panel, mkt, window=window).dropna(
                subset=[f"fwd_excess_{window}d"])
            y = (ev[f"fwd_excess_{window}d"] >= thr).to_numpy(dtype=int)
            is_mask = (ev["trade_date"] <= IS_END).to_numpy()
            Xis = ev[is_mask][factor_list].reset_index(drop=True)
            yis = y[is_mask]
            Xoos = ev[~is_mask][factor_list].reset_index(drop=True)
            yoos = y[~is_mask]
            for rec in survivors[survivors["family"] == family].itertuples(index=False):
                conds = [tuple(c) for c in rec.conditions]
                mi, mo = mask_of(Xis, conds), mask_of(Xoos, conds)
                base_i = float(yis[~mi].mean()) if (~mi).sum() else np.nan
                base_o = float(yoos[~mo].mean()) if (~mo).sum() else np.nan
                rows.append({
                    "family": family, "conditions": render_condition(conds),
                    "window": window, "threshold": thr,
                    "is_lift": (float(yis[mi].mean()) / base_i)
                    if mi.sum() >= 30 and base_i and base_i > 0 else np.nan,
                    "oos_lift": (float(yoos[mo].mean()) / base_o)
                    if mo.sum() >= 30 and base_o and base_o > 0 else np.nan,
                })
    return pd.DataFrame(rows)


def manual_verification_sample(events: pd.DataFrame, panel: pd.DataFrame,
                               k: int = 8) -> pd.DataFrame:
    """Around-anchor OHLC rows for eyeball verification of trigger correctness."""
    sample = events.dropna(subset=[f"fwd_excess_{MAIN_WINDOW}d"]).sample(
        min(k, len(events)), random_state=11)
    rows = []
    for rec in sample.itertuples(index=False):
        g = panel[panel["ts_code"] == rec.ts_code].sort_values("trade_date")
        # events carry int64 dates; the panel stores them as strings
        pos = g.index[g["trade_date"] == str(int(rec.trade_date))]
        if len(pos) == 0:
            continue
        p = g.index.get_loc(pos[0])
        win = g.iloc[max(0, p - 8):p + 4]
        anchor_date = str(int(rec.trade_date))
        for r in win.itertuples(index=False):
            rows.append({"event": f"{rec.ts_code}@{rec.trade_date}",
                         "trade_date": r.trade_date, "open": r.open, "high": r.high,
                         "low": r.low, "close": r.close, "pre_close": r.pre_close,
                         "pct_chg": r.pct_chg, "is_anchor": r.trade_date == anchor_date})
    return pd.DataFrame(rows)


def main() -> None:
    t_start = time.time()
    panel, basic, dates = load_panel()
    checkpoint("S0 panel", {"rows": len(panel), "codes": panel["ts_code"].nunique(),
                            "dates": len(dates), "first": str(dates[0]),
                            "last": str(dates[-1])})

    mkt = market_daily_returns(panel)
    events = trigger_events(panel, len(panel))
    labeled = {name: label_events(ev.copy(), panel, mkt, window=MAIN_WINDOW)
               for name, ev in events.items()}
    for name, ev in labeled.items():
        col = f"fwd_excess_{MAIN_WINDOW}d"
        checkpoint(f"S1 {name}", {"raw_events": len(ev),
                                  "valid_labels": int(ev[col].notna().sum()),
                                  "positive_rate": round(float((ev[col] >= MAIN_THRESHOLD).mean()), 4)})

    n_e1 = len(events["E1"])
    max_date = int(panel["trade_date"].max())
    pool_parts = [random_control_events(panel, n_e1, seed=s,
                                        date_lo=20160101, date_hi=max_date)
                  for s in range(1, N_NULL_RUNS + 1)]
    e3_pool = pd.concat(pool_parts, ignore_index=True).drop_duplicates(
        ["ts_code", "trade_date"])
    labeled["E3_POOL"] = label_events(e3_pool, panel, mkt, window=MAIN_WINDOW)

    all_events = pd.concat(
        [ev[["ts_code", "trade_date", "base_date", "streak_len"]].assign(family=name)
         for name, ev in labeled.items()], ignore_index=True
    ).drop_duplicates(["ts_code", "trade_date"])
    checkpoint("S2 candidate rows", {"rows": len(all_events)})

    fp_path = os.path.join(DATA_DIR, "factor_pool.parquet")
    if os.path.exists(fp_path):
        factor_pool = pd.read_parquet(fp_path)
        checkpoint("S2 factor matrix (cached)", {"rows": len(factor_pool),
                                                 "factors": factor_pool.shape[1] - 2})
    else:
        factor_matrix = build_factor_matrix(panel, dates, all_events)
        factor_pool = factor_matrix[["ts_code", "trade_date", *factors_mod.ALL_FACTORS]]
        factor_pool = factor_pool.drop_duplicates(["ts_code", "trade_date"])
        factor_pool.to_parquet(fp_path, index=False)
        checkpoint("S2 factor matrix", {"rows": len(factor_pool),
                                        "factors": len(factors_mod.ALL_FACTORS)})

    # restore streak_len if the cached pool predates it (event tables carry it)
    if "streak_len" not in factor_pool.columns:
        sl = pd.concat(
            [labeled[n][["ts_code", "trade_date", "streak_len"]] for n in labeled],
            ignore_index=True).drop_duplicates(["ts_code", "trade_date"])
        factor_pool = factor_pool.merge(sl, on=["ts_code", "trade_date"], how="left")
        factor_pool.to_parquet(fp_path, index=False)

    # correlation dedup on E1 IS rows (Spearman |rho| > 0.8 clusters)
    ev1, y1, _ = _prep(labeled["E1"], factor_pool)
    is_mask = (ev1["trade_date"] <= IS_END).to_numpy()
    Xis = ev1[is_mask][factors_mod.ALL_FACTORS].astype(float)
    yis = y1[is_mask]
    corr = Xis.corr(method="spearman").abs()
    np.fill_diagonal(corr.to_numpy(), 0.0)
    info_gain = {c: abs(np.corrcoef(Xis[c].fillna(Xis[c].median()), yis)[0, 1])
                 for c in factors_mod.ALL_FACTORS if Xis[c].notna().sum() > 100}
    dropped: set[str] = set()
    for a in factors_mod.ALL_FACTORS:
        if a in dropped or a not in info_gain:
            continue
        for b in factors_mod.ALL_FACTORS:
            if b == a or b in dropped or b not in info_gain:
                continue
            if corr.loc[a, b] > 0.8:
                dropped.add(b if info_gain[a] >= info_gain[b] else a)
    kept = [c for c in factors_mod.ALL_FACTORS if c not in dropped]
    with open(os.path.join(DATA_DIR, "dropped_factors.json"), "w") as fh:
        json.dump({"dropped": sorted(dropped), "kept": kept}, fh, indent=1)
    checkpoint("S2 dedup", {"kept": len(kept), "dropped": len(dropped)})
    factor_pool = factor_pool[["ts_code", "trade_date", *kept]]

    null = run_null_distribution(factor_pool, panel, n_e1, mkt, kept, max_date)
    checkpoint("S4 null distribution", {"null_95": round(null["null_95"], 4) if null["null_95"] == null["null_95"] else None,
                                        "completed_runs": len([m for m in null["runs"] if m == m])})
    pd.DataFrame({"run": range(1, len(null["runs"]) + 1),
                  "max_oos_lift": null["runs"]}).to_csv(
        os.path.join(DATA_DIR, "null_distribution.csv"), index=False)

    results: dict[str, pd.DataFrame] = {}
    for name in ("E1", "E1_EXT", "E2"):
        evaluated, meta = analyze_family(name, labeled[name], mkt, factor_pool,
                                         kept, null["null_95"])
        results[name] = evaluated
        checkpoint(f"S3/S4 {name}", meta)
        _serialize_conditions(evaluated).to_parquet(
            os.path.join(DATA_DIR, f"conditions_{name}.parquet"), index=False)

    for name, ev in labeled.items():
        ev.to_parquet(os.path.join(DATA_DIR, f"events_{name}.parquet"), index=False)

    survivors = pd.concat([df[df["survives"]] for df in results.values() if len(df)],
                          ignore_index=True) if any(len(d) for d in results.values()) \
        else pd.DataFrame()
    sens = sensitivity_check(survivors, labeled, mkt, panel, factor_pool, kept)
    sens.to_csv(os.path.join(DATA_DIR, "sensitivity_survivors.csv"), index=False)

    mv = manual_verification_sample(labeled["E1"], panel)
    mv.to_csv(os.path.join(DATA_DIR, "manual_verification_sample.csv"), index=False)

    checkpoint("S4 done", {"survivors_total": len(survivors),
                           "elapsed_min": round((time.time() - t_start) / 60, 1)})


if __name__ == "__main__":
    main()

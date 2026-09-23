"""Three-condition search methods plus the evaluation statistics stack.

M1: exhaustive single-predicate enumeration over quantile cuts (+ BH-FDR).
M2: depth-3 decision tree, root-to-leaf paths as candidate conditions.
M3: subgroup-discovery beam search maximizing WRAcc (predicate masks precomputed).

Condition representation: list[(factor, op, threshold)] with op in {"<=", ">="};
AND semantics. Evaluation is staged: cheap stats + BH first, then bootstrap and
platform checks only for stage-1 survivors.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)
M3_QUANTILES = (0.20, 0.40, 0.60, 0.80)
BEAM_WIDTH = 30
MAX_DEPTH = 3
BOOTSTRAP_ROUNDS = 1000
PLATFORM_REL_PERTURB = 0.20
PLATFORM_CV_MAX = 0.30


def eval_condition(mask: np.ndarray, y: np.ndarray) -> dict | None:
    """Hit rate / lift / one-sided binomial p vs the complement baseline."""
    n_in = int(mask.sum())
    if n_in < 2 or n_in == len(y):
        return None
    hits_in = int(y[mask].sum())
    rate_in = hits_in / n_in
    n_out = len(y) - n_in
    hits_out = int(y.sum()) - hits_in
    rate_out = hits_out / n_out
    if hits_out == 0:
        pval = 0.0  # baseline has zero positives; any inside hit is decisive
    elif hits_out == n_out:
        pval = 1.0  # baseline is 100%; nothing to beat
    else:
        pval = float(stats.binomtest(hits_in, n_in, rate_out, alternative="greater").pvalue)
    return {
        "support": n_in,
        "support_pct": n_in / len(y),
        "hits": hits_in,
        "hit_rate": rate_in,
        "baseline_rate": rate_out,
        "lift": rate_in / rate_out if rate_out > 0 else np.inf,
        "p_value": pval,
    }


def bh_adjust(pvals: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg q-values (step-up, monotone, clipped to 1)."""
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    order = np.argsort(p)
    ranked = p[order]
    q = ranked * n / np.arange(1, n + 1)
    q = np.minimum.accumulate(q[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.clip(q, 0.0, 1.0)
    return out


def mask_of(X: pd.DataFrame, conds: list[tuple[str, str, float]]) -> np.ndarray:
    mask = np.ones(len(X), dtype=bool)
    for f, op, v in conds:
        col = X[f].to_numpy(dtype=float)
        with np.errstate(invalid="ignore"):
            m = (col <= v) if op == "<=" else (col >= v)
        mask &= np.isfinite(col) & m
    return mask


def dedupe(cands: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out = []
    for c in cands:
        key = (c["method"], tuple(sorted(c["conditions"])))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# ---------------------------------------------------------------- M1: enumerate
def m1_single_conditions(X: pd.DataFrame, y: np.ndarray, min_support: int) -> list[dict]:
    cands: list[dict] = []
    for col in X.columns:
        vals = X[col].to_numpy(dtype=float)
        finite = np.isfinite(vals)
        if finite.sum() < 4 * min_support:
            continue
        qs = np.unique(np.nanquantile(vals[finite], QUANTILES))
        for q in qs:
            if not np.isfinite(q):
                continue
            for op in ("<=", ">="):
                mask = np.isfinite(vals) & ((vals <= q) if op == "<=" else (vals >= q))
                if int(mask.sum()) < min_support:
                    continue
                stat = eval_condition(mask, y)
                if stat is None:
                    continue
                cands.append({"conditions": [(col, op, float(q))], "method": "M1", **stat})
    return dedupe(cands)


# ------------------------------------------------------- M2: decision tree paths
def m2_tree_paths(
    X: pd.DataFrame,
    y: np.ndarray,
    min_support: int,
    max_depth: int = 3,
    seed: int = 0,
) -> list[dict]:
    from sklearn.tree import DecisionTreeClassifier, _tree

    if len(y) < 10 * min_support or y.sum() in (0, len(y)):
        return []
    Xi = X.fillna(X.median(numeric_only=True))
    tree = DecisionTreeClassifier(
        max_depth=max_depth, min_samples_leaf=min_support, random_state=seed)
    tree.fit(Xi.to_numpy(dtype=float), y)
    t = tree.tree_
    feats = list(Xi.columns)
    cands: list[dict] = []

    def walk(node: int, conds: list[tuple[str, str, float]], rows: np.ndarray) -> None:
        if t.feature[node] == _tree.TREE_UNDEFINED:
            if min_support <= rows.sum() < len(y):
                stat = eval_condition(rows, y)
                if stat is not None:
                    cands.append({"conditions": list(conds), "method": "M2", **stat})
            return
        name = feats[t.feature[node]]
        thr = float(t.threshold[node])
        col = Xi[name].to_numpy(dtype=float)
        walk(t.children_left[node], conds + [(name, "<=", thr)], rows & (col <= thr))
        walk(t.children_right[node], conds + [(name, ">=", np.nextafter(thr, np.inf))],
             rows & (col > thr))

    walk(0, [], np.ones(len(y), dtype=bool))
    return dedupe(cands)


# ------------------------------------------------- M3: subgroup discovery beam
def _canonicalize(conds: list[tuple[str, str, float]]) -> list[tuple[str, str, float]]:
    """Drop predicates implied by another on the same factor+op:
    two "<=" keep the tighter (smaller) bound; two ">=" keep the larger."""
    by_key: dict[tuple[str, str], list[float]] = {}
    for f, op, v in conds:
        by_key.setdefault((f, op), []).append(v)
    out: list[tuple[str, str, float]] = []
    for (f, op), vs in by_key.items():
        out.append((f, op, min(vs) if op == "<=" else max(vs)))
    return sorted(out)


def m3_beam_search(
    X: pd.DataFrame,
    y: np.ndarray,
    min_support: int,
    beam_width: int = BEAM_WIDTH,
    max_depth: int = MAX_DEPTH,
) -> list[dict]:
    """Beam search over precomputed predicate masks, objective WRAcc."""
    n = len(y)
    base_rate = float(y.mean())
    preds: list[tuple[str, str, float]] = []
    cols: list[np.ndarray] = []
    for col in X.columns:
        vals = X[col].to_numpy(dtype=float)
        finite = np.isfinite(vals)
        if finite.sum() < 4 * min_support:
            continue
        qs = np.unique(np.nanquantile(vals[finite], M3_QUANTILES))
        for q in qs:
            if not np.isfinite(q):
                continue
            for op in ("<=", ">="):
                m = finite & ((vals <= q) if op == "<=" else (vals >= q))
                if m.sum() < min_support:
                    continue
                preds.append((col, op, float(q)))
                cols.append(m)
    for col in ("streak_len", "positive_days_3", "positive_days_5", "positive_days_10"):
        if col in X.columns:
            vals = X[col].to_numpy(dtype=float)
            for v in np.unique(vals[np.isfinite(vals)]):
                m = np.isfinite(vals) & (vals >= v)
                if m.sum() >= min_support:
                    preds.append((col, ">=", float(v)))
                    cols.append(m)
    if not preds:
        return []
    P = np.column_stack(cols)  # n x k boolean predicate matrix
    pred_index = {p: i for i, p in enumerate(preds)}

    def item_mask(idxs: list[int]) -> np.ndarray:
        return P[:, idxs].all(axis=1)

    def wracc(mask: np.ndarray) -> float:
        s = int(mask.sum())
        return (s / n) * (y[mask].mean() - base_rate) if s else 0.0

    beam: list[list[int]] = []
    seen: set[tuple[int, ...]] = {()}
    results: dict[tuple[int, ...], float] = {}
    for _ in range(max_depth):
        pool: list[tuple[float, list[int]]] = []
        for idxs in beam or [[]]:
            for k in range(len(preds)):
                if k in idxs:
                    continue
                cand = tuple(sorted(idxs + [k]))
                if cand in seen:
                    continue
                seen.add(cand)
                pool.append((wracc(item_mask(list(cand))), list(cand)))
        pool.sort(key=lambda t: t[0], reverse=True)
        beam = [idxs for w, idxs in pool[:beam_width] if w > 0]
        for w, idxs in pool[:beam_width]:
            results[tuple(idxs)] = w
        if not beam:
            break

    cands: list[dict] = []
    for idxs, w in results.items():
        if w <= 0:
            continue
        mask = item_mask(list(idxs))
        stat = eval_condition(mask, y)
        if stat is None:
            continue
        conds = _canonicalize([preds[i] for i in idxs])
        cands.append({"conditions": conds, "method": "M3", **stat})
    return dedupe(cands)


# ------------------------------------------------------------- statistics stack
def platform_cv(
    X: pd.DataFrame,
    y: np.ndarray,
    conds: list[tuple[str, str, float]],
    min_support: int,
) -> float:
    """CV of lift under ±20% perturbation of the largest-magnitude threshold."""
    if not conds:
        return np.inf
    f, op, v = max(conds, key=lambda c: abs(c[2]))
    if v == 0 or not np.isfinite(v):
        return np.inf
    lifts = []
    for scale in (1.0 - PLATFORM_REL_PERTURB, 1.0, 1.0 + PLATFORM_REL_PERTURB):
        perturbed = [(ff, oo, vv * scale if ff == f else vv) for ff, oo, vv in conds]
        mask = mask_of(X, perturbed)
        if mask.sum() < min_support:
            return np.inf
        stat = eval_condition(mask, y)
        if stat is None or not np.isfinite(stat["lift"]) or stat["lift"] == np.inf:
            return np.inf
        lifts.append(stat["lift"])
    lifts = np.array(lifts)
    mean = lifts.mean()
    return float(lifts.std(ddof=0) / mean) if mean > 0 else np.inf


def block_bootstrap_ci(
    X: pd.DataFrame,
    y: np.ndarray,
    months: np.ndarray,
    conds: list[tuple[str, str, float]],
    rounds: int = BOOTSTRAP_ROUNDS,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile CI for lift; calendar months are the resample blocks."""
    mask = mask_of(X, conds) if conds else np.ones(len(y), dtype=bool)
    per_month: dict[int, np.ndarray] = {
        int(m): np.flatnonzero(months == m) for m in np.unique(months)}
    uniq = np.array(sorted(per_month))
    rng = np.random.default_rng(seed)
    lifts = np.full(rounds, np.nan)
    for r in range(rounds):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([per_month[int(m)] for m in pick])
        mi = mask[idx]
        nin = int(mi.sum())
        nout = len(idx) - nin
        if nin < 2 or nout < 2:
            continue
        yi = y[idx]
        hi_in = int(yi[mi].sum())
        hi_out = int(yi.sum()) - hi_in
        if hi_out == 0 or hi_out == nout:
            continue
        lifts[r] = (hi_in / nin) / (hi_out / nout)
    lo, hi = np.nanpercentile(lifts, [2.5, 97.5])
    return float(lo), float(hi)


def evaluate_candidates(
    cands: list[dict],
    X: pd.DataFrame,
    y: np.ndarray,
    months: np.ndarray,
    min_support: int,
    *,
    heavy: bool = True,
) -> pd.DataFrame:
    """Stage 1: stats + BH on all candidates. Stage 2: bootstrap/platform on survivors."""
    rows = [dict(c) for c in cands]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["q_value"] = bh_adjust(df["p_value"].to_numpy())
    df["boot_ci_lo"] = np.nan
    df["boot_ci_hi"] = np.nan
    df["platform_cv"] = np.nan
    if not heavy:
        return df
    stage2 = df[
        (df["q_value"] < 0.10) & (df["support"] >= min_support) & (df["lift"] >= 1.05)
    ]
    for i in stage2.index:
        conds = [tuple(c) for c in df.at[i, "conditions"]]
        lo, hi = block_bootstrap_ci(X, y, months, conds, seed=7)
        df.at[i, "boot_ci_lo"] = lo
        df.at[i, "boot_ci_hi"] = hi
        df.at[i, "platform_cv"] = platform_cv(X, y, conds, min_support)
    return df


def render_condition(conds: list[tuple[str, str, float]]) -> str:
    return " 且 ".join(f"{f}{op}{v:.4g}" for f, op, v in conds) if conds else "(全体)"

"""Point-in-time factor matrix at event anchors.

Registry factor definitions mirror china_a_share/discovery/backtester.py exactly
(session-rank gated consecutive windows, ddof=0 std, rolling windows including
the current session, adjusted_close = close * adj_factor). Anchor-relative
factors generalize the bn-m conditions (MA convergence at the base day, close
vs base-day extremes, streak geometry).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

STATIC_FACTORS = [
    "amount", "circ_mv", "close", "dv_ratio", "dv_ttm", "float_share",
    "free_share", "open", "pb", "pct_chg", "pe", "pe_ttm", "ps", "ps_ttm",
    "total_mv", "total_share", "turnover_rate", "turnover_rate_f", "vol",
    "volume_ratio",
]

ANCHOR_FACTORS = [
    "streak_len",
    "days_base_to_anchor",
    "close_to_base_low_ratio",
    "close_to_base_high_ratio",
    "close_to_base_close_ratio",
    "base_day_amplitude_pct",
    "ma_convergence_base_pct",
    "return_20d_before_base_pct",
    "base_volume_to_5d_avg_ratio",
]

REGISTRY_SEQUENCE_FACTORS = [
    "amount_5d_to_20d_avg_ratio", "amount_to_5d_avg_ratio", "amount_to_20d_avg_ratio",
    "close_location_pct", "distance_from_5d_ma_pct", "distance_from_10d_ma_pct",
    "distance_from_20d_ma_pct", "distance_from_10d_peak_pct",
    "distance_from_20d_peak_pct", "distance_from_5d_peak_pct", "intraday_range_pct",
    "intraday_return_pct", "max_drawdown_5d_pct", "open_gap_pct",
    "downside_deviation_5d_pct", "downside_deviation_10d_pct",
    "downside_deviation_20d_pct", "positive_days_3", "positive_days_5",
    "positive_days_10", "return_10d_pct", "return_20d_pct", "return_5d_pct",
    "turnover_5d_avg_pct", "turnover_20d_avg_pct", "turnover_5d_to_20d_avg_ratio",
    "turnover_to_5d_avg_ratio", "turnover_to_20d_avg_ratio",
    "volume_5d_to_20d_avg_ratio", "volume_to_5d_avg_ratio",
    "volume_to_20d_avg_ratio", "volatility_10d_pct", "volatility_20d_pct",
    "volatility_5d_pct",
]

ALL_FACTORS = STATIC_FACTORS + ANCHOR_FACTORS + REGISTRY_SEQUENCE_FACTORS


def stock_features(g: pd.DataFrame, date_rank: dict[str, int]) -> pd.DataFrame:
    """All factor columns for one stock's session-sorted frame (no look-ahead)."""
    rank = g["trade_date"].map(date_rank).to_numpy(dtype=np.int64)
    n = len(g)
    out = pd.DataFrame(index=g.index.copy())

    def consec(window: int) -> np.ndarray:
        c = np.zeros(n, dtype=bool)
        if n > window:
            c[window:] = rank[window:] - rank[:-window] == window
        return c

    def consec_incl(window: int) -> np.ndarray:
        # window rows ending at current session: rank[i] - rank[i-(w-1)] == w-1
        c = np.zeros(n, dtype=bool)
        if window >= 1 and n >= window:
            c[window - 1:] = rank[window - 1:] - rank[:n - window + 1] == window - 1
        return c

    adjc = (g["close"] * g["adj_factor"]).to_numpy(dtype=float)
    out["adjusted_close"] = adjc
    for col in STATIC_FACTORS:
        if col in g.columns:
            out[col] = pd.to_numeric(g[col], errors="coerce").to_numpy()

    openp = pd.to_numeric(g.get("open"), errors="coerce").to_numpy(dtype=float)
    highp = pd.to_numeric(g.get("high"), errors="coerce").to_numpy(dtype=float)
    lowp = pd.to_numeric(g.get("low"), errors="coerce").to_numpy(dtype=float)
    closep = pd.to_numeric(g["close"], errors="coerce").to_numpy(dtype=float)
    prevc = pd.to_numeric(g.get("pre_close"), errors="coerce").to_numpy(dtype=float)

    with np.errstate(invalid="ignore", divide="ignore"):
        out["intraday_range_pct"] = np.where(
            prevc > 0, (highp - lowp) / prevc * 100.0, np.nan)
        out["open_gap_pct"] = np.where(
            (prevc > 0) & (openp > 0), (openp / prevc - 1.0) * 100.0, np.nan)
        out["intraday_return_pct"] = np.where(
            openp > 0, (closep / openp - 1.0) * 100.0, np.nan)
        rng = highp - lowp
        out["close_location_pct"] = np.where(
            rng > 0, (closep - lowp) / rng * 100.0, np.nan)

    # returns / volatility on adjusted close, consecutive-session gated
    for window in (5, 10, 20):
        c = consec(window)
        prev_adjc = np.full(n, np.nan)
        prev_adjc[window:] = adjc[:-window]
        out[f"return_{window}d_pct"] = np.where(c, (adjc / prev_adjc - 1.0) * 100.0, np.nan)

    daily_ret = np.full(n, np.nan)
    daily_ret[1:] = np.where(rank[1:] - rank[:-1] == 1,
                             adjc[1:] / adjc[:-1] - 1.0, np.nan)
    for window in (5, 10, 20):
        std = pd.Series(daily_ret).rolling(window, min_periods=window).std(ddof=0).to_numpy()
        out[f"volatility_{window}d_pct"] = np.where(consec(window), std * 100.0, np.nan)

    # positive days (up vs prior session close); N indicators need N+1
    # consecutive sessions so the earliest comparison is not across a gap
    up = np.zeros(n, dtype=int)
    up[1:] = ((rank[1:] - rank[:-1] == 1) & (adjc[1:] > adjc[:-1])).astype(int)
    for window in (3, 5, 10):
        s = pd.Series(up).rolling(window, min_periods=window).sum().to_numpy()
        out[f"positive_days_{window}"] = np.where(consec(window), s, np.nan)

    # drawdown / peak distance over the last window+1 sessions INCLUDING current
    # (product convention: "5d peak" spans 6 adjusted closes, etc.)
    for window in (5, 10, 20):
        c = consec(window)
        peak = pd.Series(adjc).rolling(window + 1, min_periods=window + 1).max().to_numpy()
        out[f"distance_from_{window}d_peak_pct"] = np.where(
            c, (adjc / peak - 1.0) * 100.0, np.nan)
        if window == 5:
            # max drawdown within the window+1 adjusted closes ending today
            w1 = window + 1
            mat = np.full((n, w1), np.nan)
            for k in range(w1):
                if k == 0:
                    mat[:, w1 - 1] = adjc
                elif n > k:
                    mat[k:, w1 - 1 - k] = adjc[:n - k]
            peaks = np.nanmax(mat, axis=1)
            valid = np.isfinite(mat).sum(axis=1) == w1
            valid &= c
            out[f"max_drawdown_{window}d_pct"] = np.where(
                valid, np.nanmin(mat / peaks[:, None] - 1.0, axis=1) * 100.0, np.nan)

    # distance from moving averages (window includes current session)
    for window in (5, 10, 20):
        ma = pd.Series(adjc).rolling(window, min_periods=window).mean().to_numpy()
        c = consec_incl(window)
        out[f"distance_from_{window}d_ma_pct"] = np.where(
            c & (ma > 0), (adjc / ma - 1.0) * 100.0, np.nan)

    # downside deviation
    down_sq = np.where(np.isfinite(daily_ret), np.minimum(daily_ret, 0.0) ** 2, np.nan)
    for window in (5, 10, 20):
        mean_sq = pd.Series(down_sq).rolling(window, min_periods=window).mean().to_numpy()
        out[f"downside_deviation_{window}d_pct"] = np.where(
            consec(window), np.sqrt(mean_sq) * 100.0, np.nan)

    # volume/amount/turnover ratios (window includes current session); the
    # gate is applied to the rolling mean itself so ratios cannot silently
    # bridge a suspension gap
    ma_by_src: dict[tuple[str, int], np.ndarray] = {}
    for src, prefix in (("vol", "volume"), ("amount", "amount"), ("turnover_rate", "turnover")):
        if src not in g.columns:
            for window in (5, 20):
                out[f"{prefix}_to_{window}d_avg_ratio"] = np.nan
                out[f"{prefix}_{window}d_avg_pct"] = np.nan
            out[f"{prefix}_5d_to_20d_avg_ratio"] = np.nan
            continue
        vals = pd.to_numeric(g[src], errors="coerce").to_numpy(dtype=float)
        for window in (5, 20):
            raw_ma = pd.Series(vals).rolling(window, min_periods=window).mean().to_numpy()
            c = consec_incl(window)
            ma = np.where(c, raw_ma, np.nan)
            ma_by_src[(src, window)] = ma
            out[f"{prefix}_to_{window}d_avg_ratio"] = np.where(
                np.isfinite(ma) & (ma > 0), vals / ma, np.nan)
            if src == "turnover_rate":
                out[f"turnover_{window}d_avg_pct"] = ma
        ratio = np.where(
            np.isfinite(ma_by_src[(src, 20)]) & (ma_by_src[(src, 20)] > 0)
            & np.isfinite(ma_by_src[(src, 5)]),
            ma_by_src[(src, 5)] / ma_by_src[(src, 20)], np.nan)
        out[f"{prefix}_5d_to_20d_avg_ratio"] = ratio
    return out


def anchor_relative_features(
    g: pd.DataFrame,
    event_rows: pd.DataFrame,
    date_rank: dict[str, int],
) -> pd.DataFrame:
    """bn-m style factors: geometry of the anchor day vs the base (first-board) day."""
    pos_by_date: dict[int, int] = {}
    dates_arr = g["trade_date"].to_numpy(dtype=np.int64)
    for i, d in enumerate(dates_arr):
        pos_by_date.setdefault(int(d), i)
    rank = g["trade_date"].map(date_rank).to_numpy(dtype=np.int64)
    adjc = (g["close"] * g["adj_factor"]).to_numpy(dtype=float)
    highp = pd.to_numeric(g.get("high"), errors="coerce").to_numpy(dtype=float)
    lowp = pd.to_numeric(g.get("low"), errors="coerce").to_numpy(dtype=float)
    closep = pd.to_numeric(g["close"], errors="coerce").to_numpy(dtype=float)
    prevc = pd.to_numeric(g.get("pre_close"), errors="coerce").to_numpy(dtype=float)
    vol = pd.to_numeric(g.get("vol"), errors="coerce").to_numpy(dtype=float)

    out = pd.DataFrame(index=pd.RangeIndex(len(event_rows)))
    vals = {k: np.full(len(event_rows), np.nan) for k in ANCHOR_FACTORS}
    for i, (code, a_date, b_date, streak) in enumerate(zip(
            event_rows["ts_code"].to_numpy(),
            event_rows["trade_date"].to_numpy(dtype=np.int64),
            event_rows["base_date"].to_numpy(dtype=np.int64),
            event_rows["streak_len"].to_numpy() if "streak_len" in event_rows
            else np.full(len(event_rows), 0))):
        a = pos_by_date.get(int(a_date))
        b = pos_by_date.get(int(b_date))
        if a is None or b is None or b > a:
            continue
        vals["streak_len"][i] = streak
        vals["days_base_to_anchor"][i] = rank[a] - rank[b]
        with np.errstate(invalid="ignore", divide="ignore"):
            if lowp[b] and lowp[b] > 0:
                vals["close_to_base_low_ratio"][i] = closep[a] / lowp[b]
            if highp[b] and highp[b] > 0:
                vals["close_to_base_high_ratio"][i] = closep[a] / highp[b]
            if closep[b] and closep[b] > 0:
                vals["close_to_base_close_ratio"][i] = closep[a] / closep[b]
            if prevc[b] and prevc[b] > 0:
                vals["base_day_amplitude_pct"][i] = (highp[b] - lowp[b]) / prevc[b] * 100.0
        # MA convergence at base day (bn-m condition #3 generalization)
        if b >= 59:
            mas = []
            ok = True
            for w in (5, 20, 60):
                if rank[b] - rank[b - w + 1] != w - 1:
                    ok = False
                    break
                mas.append(adjc[b - w + 1:b + 1].mean())
            if ok and min(mas) > 0:
                vals["ma_convergence_base_pct"][i] = (max(mas) - min(mas)) / min(mas) * 100.0
        # 20-session return BEFORE the base day
        if b >= 20 and rank[b] - rank[b - 20] == 20:
            vals["return_20d_before_base_pct"][i] = (adjc[b] / adjc[b - 20] - 1.0) * 100.0
        # base-day volume vs its prior 5-session average (excludes base day)
        if b >= 5 and rank[b] - rank[b - 5] == 5:
            avg5 = vol[b - 5:b].mean()
            if avg5 and avg5 > 0:
                vals["base_volume_to_5d_avg_ratio"][i] = vol[b] / avg5
    for k in ANCHOR_FACTORS:
        out[k] = vals[k]
    return out


def build_factor_matrix(
    panel: pd.DataFrame,
    dates: np.ndarray,
    events: pd.DataFrame,
) -> pd.DataFrame:
    """Factor values for every event row (all factors computed point-in-time).

    Matches events to panel rows explicitly by (ts_code, trade_date); the
    caller's event index is irrelevant.
    """
    date_rank = {str(d): i for i, d in enumerate(dates)}
    ev = events.reset_index(drop=True)
    parts: list[pd.DataFrame] = []
    for code, g in panel.groupby("ts_code", sort=False):
        sub = ev[ev["ts_code"] == code]
        if sub.empty:
            continue
        gr = g.reset_index(drop=True)
        feats = stock_features(gr, date_rank)
        pos_by_date: dict[int, int] = {}
        for i, d in enumerate(gr["trade_date"].to_numpy(dtype=np.int64)):
            pos_by_date.setdefault(int(d), i)
        keep_pos: list[int] = []
        for d in sub["trade_date"].to_numpy(dtype=np.int64):
            p = pos_by_date.get(int(d))
            if p is not None:
                keep_pos.append(p)
        if not keep_pos:
            continue
        sel = feats.iloc[keep_pos].reset_index(drop=True)
        anchor = anchor_relative_features(gr, sub.reset_index(drop=True), date_rank)
        merged = pd.concat([sel, anchor], axis=1)
        merged["ts_code"] = code
        merged["trade_date"] = sub["trade_date"].to_numpy()[:len(merged)]
        parts.append(merged)
    if not parts:
        return pd.DataFrame(columns=["ts_code", "trade_date", *ALL_FACTORS])
    return pd.concat(parts, ignore_index=True).drop(columns=["adjusted_close"],
                                                    errors="ignore")

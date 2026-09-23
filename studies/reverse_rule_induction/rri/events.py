"""Event triggers (E1/E2/E3), streak segmentation, and forward labels."""
from __future__ import annotations

import numpy as np
import pandas as pd

from rri.limits import limit_pct_bp, limit_up_cents, yuan_to_cents

FIRST_SESSIONS_EXCLUDED = 5   # skip new-listing noise / uncapped IPO window
DEDUP_CALENDAR_DAYS = 30      # keep first event per stock within any 30-day span
MIN_STREAK_FOR_EVENT = 2


def session_consecutive(rank: np.ndarray, window: int) -> np.ndarray:
    """rank[i] - rank[i-window] == window (no suspension gap inside the lookback)."""
    if len(rank) <= window:
        return np.zeros(len(rank), dtype=bool)
    out = np.zeros(len(rank), dtype=bool)
    out[window:] = (rank[window:] - rank[:-window]) == window
    return out


def per_stock_limit_flags(panel_stock: pd.DataFrame) -> np.ndarray:
    """Exchange-exact is-limit-up per row for one stock's sorted frame."""
    pct_bp = limit_pct_bp(
        panel_stock["board"].to_numpy(),
        panel_stock["trade_date"].to_numpy(dtype=np.int64),
        panel_stock["is_st"].to_numpy(),
    )
    pre_cents = yuan_to_cents(panel_stock["pre_close"].to_numpy())
    close_cents = yuan_to_cents(panel_stock["close"].to_numpy())
    valid = np.isfinite(panel_stock["pre_close"].to_numpy()) & (pre_cents > 0)
    limit = limit_up_cents(pre_cents, pct_bp)
    return valid & (close_cents == limit)


def streak_runs(is_limit: np.ndarray, rank: np.ndarray) -> list[tuple[int, int]]:
    """Maximal runs of limit-up days on consecutive sessions.

    Returns list of (start_idx, end_idx) inclusive into the stock frame.
    """
    runs: list[tuple[int, int]] = []
    i, n = 0, len(is_limit)
    while i < n:
        if is_limit[i]:
            j = i
            while j + 1 < n and is_limit[j + 1] and rank[j + 1] - rank[j] == 1:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    return runs


def dedup_first(events: pd.DataFrame, day_gap: int = DEDUP_CALENDAR_DAYS) -> pd.DataFrame:
    """Keep the first event per stock within any rolling `day_gap`-day span."""
    if events.empty:
        return events
    kept: list[pd.DataFrame] = []
    for _, g in events.sort_values(["ts_code", "trade_date"]).groupby("ts_code", sort=False):
        last_kept = -10**9
        rows = []
        for rec in g.itertuples(index=False):
            if int(rec.trade_date) - last_kept > day_gap:
                rows.append(rec._asdict() if hasattr(rec, "_asdict") else rec)
                last_kept = int(rec.trade_date)
        kept.append(pd.DataFrame(rows))
    out = pd.concat(kept, ignore_index=True) if kept else events.iloc[0:0]
    return out


def trigger_events(panel: pd.DataFrame, market_row_count: int) -> dict[str, pd.DataFrame]:
    """E1 (2-board), E1_EXT (>=3-board), E2 (big gain + volume).

    All events carry: ts_code, trade_date (anchor = streak end), base_date
    (streak start / trigger day), streak_len. First-5-sessions excluded.
    Dedup keeps the first anchor per stock per 30 calendar days.
    """
    events: dict[str, list[pd.DataFrame]] = {"E1": [], "E1_EXT": [], "E2": []}
    for code, g in panel.groupby("ts_code", sort=False):
        g = g.sort_values("trade_date")
        n = len(g)
        if n < FIRST_SESSIONS_EXCLUDED + MIN_STREAK_FOR_EVENT:
            continue
        rank = g["session_rank"].to_numpy(dtype=np.int64)
        is_limit = per_stock_limit_flags(g)
        # eligible rows: not among the stock's first sessions
        eligible = np.ones(n, dtype=bool)
        eligible[:FIRST_SESSIONS_EXCLUDED] = False

        runs = streak_runs(is_limit, rank)
        e1_rows, ext_rows = [], []
        for s, e in runs:
            length = e - s + 1
            if length < MIN_STREAK_FOR_EVENT or not eligible[e]:
                continue
            rec = {"ts_code": code, "trade_date": int(g["trade_date"].iloc[e]),
                   "base_date": int(g["trade_date"].iloc[s]), "streak_len": length}
            (ext_rows if length >= 3 else e1_rows).append(rec)
        events["E1"].extend(e1_rows or [])
        events["E1_EXT"].extend(ext_rows or [])

        # E2: pct_chg >= 9.8 AND volume_ratio >= 2 (needs daily_basic)
        pct = pd.to_numeric(g["pct_chg"], errors="coerce").to_numpy()
        if "volume_ratio" in g.columns:
            vr = pd.to_numeric(g["volume_ratio"], errors="coerce").to_numpy()
        else:
            vr = np.full(n, np.nan)
        hit = eligible & (pct >= 9.8) & np.isfinite(vr) & (vr >= 2.0)
        for i in np.flatnonzero(hit):
            events["E2"].append({"ts_code": code, "trade_date": int(g["trade_date"].iloc[i]),
                                 "base_date": int(g["trade_date"].iloc[i]), "streak_len": 1})

    out = {}
    for key, rows in events.items():
        df = pd.DataFrame(rows, columns=["ts_code", "trade_date", "base_date", "streak_len"])
        out[key] = dedup_first(df)
    return out


def random_control_events(
    panel: pd.DataFrame,
    n_events: int,
    seed: int,
    date_lo: int,
    date_hi: int,
) -> pd.DataFrame:
    """E3: month-stratified random (stock, session) anchors, same exclusions as E1.

    Draws n_events anchors stratified by calendar month over [date_lo, date_hi],
    excluding each stock's first sessions and the tail before the outcome window.
    """
    rng = np.random.default_rng(seed)
    sessions = panel[["ts_code", "trade_date", "session_rank"]].copy()
    sessions["trade_date"] = sessions["trade_date"].astype(np.int64)
    sessions["month"] = sessions["trade_date"] // 100
    first_rank = sessions.groupby("ts_code")["session_rank"].transform("min")
    candidates = sessions[
        (sessions["session_rank"] - first_rank >= FIRST_SESSIONS_EXCLUDED)
        & (sessions["trade_date"] <= date_hi)
        & (sessions["trade_date"] >= date_lo)
    ]
    by_month = {m: g for m, g in candidates.groupby("month")}
    months = sorted(by_month)
    per = n_events // len(months)
    rows: list[pd.DataFrame] = []
    drawn = 0
    for m in months:
        if drawn >= n_events:
            break
        g = by_month[m]
        take = min(per + (1 if drawn < n_events - per * len(months) else 0),
                   n_events - drawn)
        if len(g) == 0 or take <= 0:
            continue
        idx = rng.integers(0, len(g), size=take)
        rows.append(g.iloc[idx])
        drawn += len(rows[-1])
    out = pd.concat(rows, ignore_index=True)[["ts_code", "trade_date"]]
    out["base_date"] = out["trade_date"]
    out["streak_len"] = 0
    return out.drop_duplicates(["ts_code", "trade_date"]).reset_index(drop=True)


def market_daily_returns(panel: pd.DataFrame) -> pd.Series:
    """Equal-weight market daily return indexed by session_rank."""
    r = panel["close"] / panel["pre_close"] - 1.0
    r = r.replace([np.inf, -np.inf], np.nan)
    return r.groupby(panel["session_rank"]).mean()


def label_events(
    events: pd.DataFrame,
    panel: pd.DataFrame,
    mkt_ret: pd.Series,
    *,
    window: int = 20,
) -> pd.DataFrame:
    """Forward excess return over the stock's next `window` own sessions.

    excess = (1 + stock cumulative) / (1 + market cumulative over the same
    calendar span) - 1, using adjusted close. NaN label when the stock has
    fewer than `window` sessions after the anchor (insufficient outcome).
    """
    log_mkt = np.log1p(mkt_ret.sort_index())
    mkt_ranks = log_mkt.index.to_numpy(dtype=np.int64)
    mkt_cum = np.concatenate([[0.0], np.cumsum(log_mkt.to_numpy(dtype=float))])

    def mkt_cum_at(rank: int) -> float:
        """Cumulative market log return up to and including `rank`; NaN if unknown."""
        pos = int(np.searchsorted(mkt_ranks, rank))
        if pos < len(mkt_ranks) and mkt_ranks[pos] == rank:
            return float(mkt_cum[pos + 1])
        return np.nan

    stock_adj: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for code, g in panel.groupby("ts_code", sort=False):
        stock_adj[code] = (
            g["trade_date"].to_numpy(dtype=np.int64),
            g["session_rank"].to_numpy(dtype=np.int64),
            (g["close"] * g["adj_factor"]).to_numpy(dtype=float),
        )

    labels = np.full(len(events), np.nan)
    fdates = np.full(len(events), -1, dtype=np.int64)
    ev_codes = events["ts_code"].to_numpy()
    ev_dates = events["trade_date"].to_numpy(dtype=np.int64)
    for i, (code, d) in enumerate(zip(ev_codes, ev_dates)):
        entry = stock_adj.get(code)
        if entry is None:
            continue
        dates, ranks, adjc = entry
        start = np.searchsorted(dates, d)
        if start >= len(dates) or dates[start] != d:
            continue
        end = start + window
        if end >= len(dates):
            continue  # insufficient forward sessions
        start_rank = ranks[start]
        end_rank = ranks[end]
        # market log return spans the calendar sessions strictly after the
        # anchor session through the outcome session: cum[end] - cum[start]
        mkt_log = mkt_cum_at(int(end_rank)) - mkt_cum_at(int(start_rank))
        if not np.isfinite(mkt_log):
            continue  # outcome session beyond available market data
        stock_log = np.log(adjc[end] / adjc[start])
        labels[i] = np.expm1(stock_log - mkt_log)
        fdates[i] = dates[end]
    out = events.copy()
    out[f"fwd_excess_{window}d"] = labels
    out["outcome_date"] = fdates
    return out

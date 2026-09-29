"""Forward N-day return computation: entry next open, exit T+N close."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import numpy as np
import pandas as pd

HOLD_GRID = (1, 3, 5, 10, 20)
ROUND_TRIP_COST = 0.0010  # 10 bp round trip, for the net column only


@dataclass
class EventResult:
    """All events for one rule x holding period, plus skip bookkeeping."""

    rule: str
    n: int
    events: pd.DataFrame  # columns: date, symbol, ret, base_ret, excess
    skipped_missing: int = 0
    skipped_truncated: int = 0


def _pivot(panel: pd.DataFrame, column: str, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    wide = panel.pivot(index="date", columns="symbol", values=column)
    return wide.reindex(calendar)


def forward_return_matrix(
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    signal_pos: int,
    n: int,
) -> pd.Series:
    """Per-symbol return buying next open and selling at the T+N close."""
    exit_pos = signal_pos + n
    if exit_pos >= len(opens.index):
        return pd.Series(dtype=float)
    entry = opens.iloc[signal_pos + 1] if signal_pos + 1 < len(opens.index) else None
    exit_close = closes.iloc[exit_pos]
    ret = exit_close / entry - 1.0
    return ret


def collect_events(
    factors: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    spec,
    evaluate_fn,
    hold_grid=HOLD_GRID,
) -> Dict[int, EventResult]:
    """Evaluate one rule across all dates and holding periods."""
    opens = _pivot(factors, "open", calendar)
    closes = _pivot(factors, "close", calendar)

    date_to_pos = {ts: i for i, ts in enumerate(calendar)}
    by_date = {ts: frame for ts, frame in factors.groupby("date")}

    results: Dict[int, EventResult] = {}
    for n in hold_grid:
        rows: List[dict] = []
        skipped_missing = 0
        skipped_truncated = 0
        for ts, day in by_date.items():
            pos = date_to_pos[ts]
            if pos + n >= len(calendar):
                # Signals exist here but the window runs past the panel edge.
                mask = evaluate_fn(spec, day)
                skipped_truncated += int(mask.sum())
                continue
            mask = evaluate_fn(spec, day)
            if not mask.any():
                continue
            ret = forward_return_matrix(opens, closes, pos, n)
            pool_syms = day.loc[day["in_pool"], "symbol"]
            pool_ret = ret.reindex(pool_syms).dropna()
            baseline = float(pool_ret.mean()) if len(pool_ret) else np.nan
            event_syms = day.loc[mask, "symbol"]
            event_ret = ret.reindex(event_syms)
            valid = event_ret.dropna()
            skipped_missing += int(event_ret.isna().sum())
            for sym, value in valid.items():
                rows.append(
                    {
                        "date": ts,
                        "symbol": sym,
                        "ret": float(value),
                        "base_ret": baseline,
                        "excess": float(value) - baseline,
                    }
                )
        events = pd.DataFrame(
            rows, columns=["date", "symbol", "ret", "base_ret", "excess"]
        )
        results[n] = EventResult(
            rule=spec.name,
            n=n,
            events=events,
            skipped_missing=skipped_missing,
            skipped_truncated=skipped_truncated,
        )
    return results


def null_runs(
    factors: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    spec,
    evaluate_fn,
    n: int,
    runs: int = 20,
    seed: int = 20260928,
) -> float:
    """95th percentile of matched-size random-pick OOS mean excess."""
    opens = _pivot(factors, "open", calendar)
    closes = _pivot(factors, "close", calendar)
    date_to_pos = {ts: i for i, ts in enumerate(calendar)}
    by_date = {ts: frame for ts, frame in factors.groupby("date")}
    rng = np.random.RandomState(seed)

    # IS/OOS boundary mirrors the headline split: last 30% of signal dates.
    dates = sorted(by_date)
    cut = int(len(dates) * 0.7)
    oos_dates = set(dates[cut:])

    means: List[float] = []
    for _ in range(runs):
        excesses: List[float] = []
        for ts in sorted(oos_dates):
            day = by_date[ts]
            pos = date_to_pos[ts]
            if pos + n >= len(calendar):
                continue
            mask = evaluate_fn(spec, day)
            size = int(mask.sum())
            if size == 0:
                continue
            ret = forward_return_matrix(opens, closes, pos, n)
            pool_syms = day.loc[day["in_pool"], "symbol"]
            pool_ret = ret.reindex(pool_syms).dropna()
            if not len(pool_ret) or size > len(pool_ret):
                continue
            baseline = float(pool_ret.mean())
            draws = rng.choice(pool_ret.to_numpy(), size=size, replace=False)
            excesses.extend((draws - baseline).tolist())
        means.append(float(np.mean(excesses)) if excesses else 0.0)
    return float(np.percentile(means, 95)) if means else float("nan")

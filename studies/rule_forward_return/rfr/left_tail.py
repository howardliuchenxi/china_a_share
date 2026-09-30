"""Left-tail controls for the N=5 gap-down portfolio."""
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

from rfr.portfolio import (
    monthly_portfolio_returns,
    run_overlapping_portfolio,
    summarize_portfolio,
)
from rfr.returns import collect_events, null_runs
from rfr.rules import RuleSpec
from rfr.stats import summarize_with_split, verdict

DEEP_GAP_LIMIT = 0.08
LEFT_TAIL_SCENARIOS = (
    ("baseline", "基线", None, None),
    ("exclude_gap8", "剔除绝对缺口>8%", DEEP_GAP_LIMIT, None),
    ("cap_1pct", "聚合单票上限1%", None, 0.01),
    ("cap_2pct", "聚合单票上限2%", None, 0.02),
    ("cap_3pct", "聚合单票上限3%", None, 0.03),
)


def _event_distribution(events: pd.DataFrame) -> Dict[str, float]:
    if events.empty:
        return {
            "p5_return": float("nan"),
            "p95_return": float("nan"),
            "worst_return": float("nan"),
        }
    return {
        "p5_return": float(events["ret"].quantile(0.05)),
        "p95_return": float(events["ret"].quantile(0.95)),
        "worst_return": float(events["ret"].min()),
    }


def annual_event_stability(events: pd.DataFrame, scenario: str) -> pd.DataFrame:
    """Return the requested 2024Q4/2025/2026 event-level stability table."""
    rows = []
    for year, label in ((2024, "2024Q4"), (2025, "2025"), (2026, "2026")):
        frame = events[events["date"].dt.year == year]
        rows.append(
            {
                "scenario": scenario,
                "period": label,
                "event_count": int(len(frame)),
                "mean_return": float(frame["ret"].mean()),
                "mean_excess": float(frame["excess"].mean()),
                "win_rate": float((frame["ret"] > 0.0).mean()),
                "p5_return": float(frame["ret"].quantile(0.05)),
                "worst_return": float(frame["ret"].min()),
            }
        )
    return pd.DataFrame(rows)


def evaluate_event_filters(
    factors: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    gapdown_spec,
    evaluate_fn,
    null_run_count: int = 20,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Evaluate baseline and the 8% deep-gap exclusion with both event gates."""
    filtered_factors = factors.copy()
    filtered_column = "bool_gapdown2_ex_gap8"
    filtered_factors[filtered_column] = (
        filtered_factors[gapdown_spec.column]
        & filtered_factors["open_gap"].abs().le(DEEP_GAP_LIMIT)
    )
    filtered_spec = RuleSpec(
        "gapdown2_ex_gap8",
        "开盘低开≤-2%且绝对缺口≤8%",
        "bool",
        filtered_column,
    )

    rows = []
    annual_frames = []
    event_frames = []
    baseline_count = None
    for scenario, work, spec in (
        ("baseline", factors, gapdown_spec),
        ("exclude_gap8", filtered_factors, filtered_spec),
    ):
        events = collect_events(
            work, calendar, spec, evaluate_fn, hold_grid=(5,)
        )[5].events
        stats = summarize_with_split(events)
        null95 = null_runs(
            work,
            calendar,
            spec,
            evaluate_fn,
            n=5,
            runs=null_run_count,
        )
        if baseline_count is None:
            baseline_count = len(events)
        stats.update(_event_distribution(events))
        stats.update(
            {
                "scenario": scenario,
                "removed_events": int(baseline_count - len(events)),
                "null95": null95,
                "verdict": verdict(stats, null95),
            }
        )
        rows.append(stats)
        annual_frames.append(annual_event_stability(events, scenario))
        frame = events.copy()
        frame["scenario"] = scenario
        event_frames.append(frame)
    return (
        pd.DataFrame(rows),
        pd.concat(annual_frames, ignore_index=True),
        pd.concat(event_frames, ignore_index=True),
    )


def yearly_portfolio_stability(monthly: pd.DataFrame) -> pd.DataFrame:
    """Compound monthly scenario returns into calendar-year comparisons."""
    work = monthly.copy()
    work["year"] = work["month"].str[:4].astype(int)
    rows = []
    for (scenario, year), frame in work.groupby(["scenario", "year"], sort=False):
        net_return = float((1.0 + frame["net_return"]).prod() - 1.0)
        benchmark_return = float(
            (1.0 + frame["benchmark_return"]).prod() - 1.0
        )
        rows.append(
            {
                "scenario": scenario,
                "period": "2024Q4" if year == 2024 else str(year),
                "months": int(len(frame)),
                "net_return": net_return,
                "benchmark_return": benchmark_return,
                "excess_return": net_return - benchmark_return,
                "positive_excess_months": int(
                    (frame["excess_return"] > 0.0).sum()
                ),
            }
        )
    return pd.DataFrame(rows)


def evaluate_portfolio_controls(
    factors: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    gapdown_spec,
    evaluate_fn,
) -> Tuple[
    pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame
]:
    """Run every disclosed gap-down left-tail control without selection."""
    daily_frames = []
    monthly_frames = []
    position_frames = []
    summary_rows = []
    for scenario, label, max_gap, max_weight in LEFT_TAIL_SCENARIOS:
        daily, positions = run_overlapping_portfolio(
            factors,
            calendar,
            gapdown_spec,
            evaluate_fn,
            max_abs_open_gap=max_gap,
            max_position_weight=max_weight,
        )
        monthly = monthly_portfolio_returns(daily)
        summary = summarize_portfolio(daily, monthly)
        summary.update(
            {
                "scenario": scenario,
                "label": label,
                "positions": int(len(positions)),
                "filtered_signals": int(daily["filtered_signals"].sum()),
                "capped_out_signals": int(daily["capped_out_signals"].sum()),
                "skipped_missing_entry": int(
                    daily["skipped_missing_entry"].sum()
                ),
                "skipped_missing_exit": int(daily["skipped_missing_exit"].sum()),
                "mean_cap_idle_weight": float(daily["cap_idle_weight"].mean()),
                "max_entry_weight": float(positions["entry_weight"].max()),
                "max_aggregate_entry_weight": float(
                    positions["aggregate_weight_after_entry"].max()
                ),
            }
        )
        daily["scenario"] = scenario
        monthly["scenario"] = scenario
        positions["scenario"] = scenario
        daily_frames.append(daily)
        monthly_frames.append(monthly)
        position_frames.append(positions)
        summary_rows.append(summary)
    monthly_all = pd.concat(monthly_frames, ignore_index=True)
    return (
        pd.concat(daily_frames, ignore_index=True),
        monthly_all,
        pd.concat(position_frames, ignore_index=True),
        pd.DataFrame(summary_rows),
        yearly_portfolio_stability(monthly_all),
    )

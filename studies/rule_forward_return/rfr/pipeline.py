"""One-shot pipeline: panel -> factors -> events -> stats -> REPORT.md."""
from __future__ import annotations

import os
from typing import Dict, List

import numpy as np
import pandas as pd

from rfr import panel as panel_mod
from rfr import rules as rules_mod
from rfr.left_tail import evaluate_event_filters, evaluate_portfolio_controls
from rfr.portfolio import evaluate_portfolios
from rfr.returns import HOLD_GRID, collect_events, null_runs
from rfr.stats import summarize_with_split, verdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(HERE, "data")
REPORT_PATH = os.path.join(HERE, "REPORT.md")
EVENTS_PATH = os.path.join(DATA_DIR, "events.parquet")
NULL_RUNS = 20


def build_factors() -> pd.DataFrame:
    """Load the panel and attach pool flags plus all rule factors."""
    raw = panel_mod.load_panel()
    pooled = panel_mod.attach_pool_mask(raw)
    return rules_mod.attach_factors(pooled)


def evaluate(factors: pd.DataFrame, null_run_count: int = NULL_RUNS) -> pd.DataFrame:
    """Run every rule x holding period and return one long stats table."""
    calendar = panel_mod.trading_calendar(factors)
    rows: List[dict] = []
    for spec in rules_mod.RULE_REGISTRY:
        results = collect_events(factors, calendar, spec, rules_mod.evaluate_rule)
        for n, result in results.items():
            stats = summarize_with_split(result.events)
            null95 = null_runs(
                factors, calendar, spec, rules_mod.evaluate_rule, n=n, runs=null_run_count
            )
            stats.update(
                {
                    "rule": spec.name,
                    "label": spec.label,
                    "n_hold": n,
                    "null95": null95,
                    "skipped_missing": result.skipped_missing,
                    "skipped_truncated": result.skipped_truncated,
                    "verdict": verdict(stats, null95),
                }
            )
            rows.append(stats)
        print(f"rule {spec.name}: done", flush=True)
    return pd.DataFrame(rows)


def save_events(factors: pd.DataFrame, path: str = EVENTS_PATH) -> None:
    """Persist the raw event table so numbers stay auditable after the run."""
    calendar = panel_mod.trading_calendar(factors)
    frames = []
    for spec in rules_mod.RULE_REGISTRY:
        for n, result in collect_events(
            factors, calendar, spec, rules_mod.evaluate_rule
        ).items():
            frame = result.events.copy()
            if frame.empty:
                continue
            frame["rule"] = spec.name
            frame["n_hold"] = n
            frames.append(frame)
    if frames:
        pd.concat(frames, ignore_index=True).to_parquet(path, index=False)


def run() -> int:
    factors = build_factors()
    universe = factors[["symbol"]]["symbol"].nunique()
    dates = panel_mod.trading_calendar(factors)
    stats_table = evaluate(factors)
    stats_table.to_parquet(os.path.join(DATA_DIR, "stats.parquet"), index=False)
    save_events(factors)
    portfolio_daily, portfolio_monthly, portfolio_positions, portfolio_summary = (
        evaluate_portfolios(
            factors,
            dates,
            rules_mod.RULE_REGISTRY,
            rules_mod.evaluate_rule,
        )
    )
    portfolio_daily.to_parquet(
        os.path.join(DATA_DIR, "portfolio_daily.parquet"), index=False
    )
    portfolio_monthly.to_parquet(
        os.path.join(DATA_DIR, "portfolio_monthly.parquet"), index=False
    )
    portfolio_positions.to_parquet(
        os.path.join(DATA_DIR, "portfolio_positions.parquet"), index=False
    )
    portfolio_summary.to_parquet(
        os.path.join(DATA_DIR, "portfolio_summary.parquet"), index=False
    )
    spec_by_name = {spec.name: spec for spec in rules_mod.RULE_REGISTRY}
    left_tail_event_summary, left_tail_annual, left_tail_events = (
        evaluate_event_filters(
            factors,
            dates,
            spec_by_name["gapdown2"],
            rules_mod.evaluate_rule,
        )
    )
    (
        left_tail_daily,
        left_tail_monthly,
        left_tail_positions,
        left_tail_portfolio_summary,
        left_tail_yearly,
    ) = evaluate_portfolio_controls(
        factors,
        dates,
        spec_by_name["gapdown2"],
        rules_mod.evaluate_rule,
    )
    for name, frame in (
        ("left_tail_event_summary", left_tail_event_summary),
        ("left_tail_annual", left_tail_annual),
        ("left_tail_events", left_tail_events),
        ("left_tail_daily", left_tail_daily),
        ("left_tail_monthly", left_tail_monthly),
        ("left_tail_positions", left_tail_positions),
        ("left_tail_portfolio_summary", left_tail_portfolio_summary),
        ("left_tail_yearly", left_tail_yearly),
    ):
        frame.to_parquet(os.path.join(DATA_DIR, f"{name}.parquet"), index=False)
    from rfr.report import render_report

    render_report(
        stats_table,
        universe=universe,
        dates=dates,
        portfolio_summary=portfolio_summary,
        portfolio_monthly=portfolio_monthly,
        left_tail_event_summary=left_tail_event_summary,
        left_tail_annual=left_tail_annual,
        left_tail_portfolio_summary=left_tail_portfolio_summary,
        left_tail_monthly=left_tail_monthly,
        left_tail_yearly=left_tail_yearly,
    )
    print(f"pipeline done -> {REPORT_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

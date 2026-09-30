"""Prepare validated workbook inputs from the rule study parquet outputs."""
from __future__ import annotations

import json
import os
from typing import Dict

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")


def _records(frame: pd.DataFrame):
    clean = frame.replace({np.nan: None})
    return clean.to_dict(orient="records")


def _scenario_weight_maps(
    positions: pd.DataFrame,
) -> Dict[str, Dict[tuple, float]]:
    maps = {}
    for scenario in ("cap_1pct", "cap_2pct", "cap_3pct"):
        frame = positions[positions["scenario"] == scenario]
        maps[scenario] = {
            (row.signal_date, row.symbol): row.aggregate_weight_after_entry
            for row in frame.itertuples()
        }
    return maps


def prepare() -> tuple:
    portfolio_summary = pd.read_parquet(
        os.path.join(DATA_DIR, "portfolio_summary.parquet")
    )
    portfolio_monthly = pd.read_parquet(
        os.path.join(DATA_DIR, "portfolio_monthly.parquet")
    )
    event_summary = pd.read_parquet(
        os.path.join(DATA_DIR, "left_tail_event_summary.parquet")
    )
    annual_events = pd.read_parquet(
        os.path.join(DATA_DIR, "left_tail_annual.parquet")
    )
    control_summary = pd.read_parquet(
        os.path.join(DATA_DIR, "left_tail_portfolio_summary.parquet")
    )
    control_monthly = pd.read_parquet(
        os.path.join(DATA_DIR, "left_tail_monthly.parquet")
    )
    control_yearly = pd.read_parquet(
        os.path.join(DATA_DIR, "left_tail_yearly.parquet")
    )
    positions = pd.read_parquet(
        os.path.join(DATA_DIR, "portfolio_positions.parquet")
    )
    control_positions = pd.read_parquet(
        os.path.join(DATA_DIR, "left_tail_positions.parquet")
    )

    weight_maps = _scenario_weight_maps(control_positions)
    gap_mask = positions["rule"] == "gapdown2"
    positions["security_name"] = ""
    positions["deep_gap_excluded"] = np.where(
        gap_mask, positions["open_gap"].abs() > 0.08, False
    )
    keys = list(zip(positions.loc[gap_mask, "signal_date"], positions.loc[gap_mask, "symbol"]))
    for scenario, weight_map in weight_maps.items():
        column = f"{scenario}_aggregate_weight"
        positions[column] = np.nan
        positions.loc[gap_mask, column] = [weight_map.get(key, 0.0) for key in keys]

    detail_columns = [
        "rule",
        "signal_date",
        "entry_date",
        "exit_date",
        "symbol",
        "security_name",
        "signal_close",
        "rule_value",
        "rule_percentile",
        "open_gap",
        "ret_5d",
        "ret_20d",
        "vol_ratio",
        "adv20",
        "pool_size",
        "cohort_size",
        "entry_price",
        "exit_price",
        "gross_return",
        "net_return",
        "benchmark_return",
        "excess_return",
        "entry_weight",
        "deep_gap_excluded",
        "cap_1pct_aggregate_weight",
        "cap_2pct_aggregate_weight",
        "cap_3pct_aggregate_weight",
    ]
    detail = positions[detail_columns].sort_values(
        ["rule", "signal_date", "symbol"]
    )
    if len(detail) != 69_458:
        raise AssertionError(f"unexpected workbook detail rows: {len(detail)}")

    p1_counts = detail.groupby("rule").size().to_dict()
    for row in portfolio_summary.itertuples():
        if p1_counts[row.rule] <= 0:
            raise AssertionError(f"missing detail rows for {row.rule}")

    summary = {
        "as_of_date": "2026-09-28",
        "generated_date": "2026-09-30",
        "source": "Massive adjusted daily bars",
        "source_extraction_date": "2026-09-29",
        "universe": 500,
        "trading_days": 500,
        "detail_rows": len(detail),
        "detail_counts": {key: int(value) for key, value in p1_counts.items()},
        "security_name_available": False,
        "portfolio_summary": _records(portfolio_summary),
        "portfolio_monthly": _records(portfolio_monthly),
        "event_summary": _records(event_summary),
        "annual_events": _records(annual_events),
        "control_summary": _records(control_summary),
        "control_monthly": _records(control_monthly),
        "control_yearly": _records(control_yearly),
    }

    summary_path = os.path.join(DATA_DIR, "workbook_summary.json")
    detail_path = os.path.join(DATA_DIR, "workbook_detail.csv")
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    detail.to_csv(detail_path, index=False, date_format="%Y-%m-%d")
    return summary_path, detail_path


if __name__ == "__main__":
    result = prepare()
    print("\n".join(result))

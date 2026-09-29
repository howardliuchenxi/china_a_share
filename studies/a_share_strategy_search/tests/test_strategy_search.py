"""Focused regression tests for the strategy-search execution contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd


MODULE_PATH = Path(__file__).resolve().parents[1] / "run_search.py"
SPEC = importlib.util.spec_from_file_location("a_share_strategy_search", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
SEARCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEARCH)


def test_split_boundaries_are_frozen() -> None:
    """Keep validation and blind dates from leaking into earlier samples."""
    dates = pd.Series(
        pd.to_datetime(["2021-12-31", "2022-01-01", "2023-12-31", "2024-01-01"])
    )

    result = SEARCH.split_name(dates)

    assert result.tolist() == ["train", "validation", "validation", "blind"]


def test_condition_mask_preserves_inclusive_rule_boundaries() -> None:
    """Select threshold values exactly when the documented rule is inclusive."""
    frame = pd.DataFrame(
        {
            "value": [0.29, 0.30, 0.45, 0.46],
            "quality": [0.80, 0.80, 0.90, 0.90],
        }
    )

    mask = SEARCH.condition_mask(
        frame,
        [("value", "between", (0.30, 0.45)), ("quality", "ge", 0.80)],
    )

    assert mask.tolist() == [False, True, True, False]


def test_overlapping_positions_are_removed_per_security() -> None:
    """Reject a same-security signal until the earlier position has exited."""
    events = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ", "000001.SZ", "000002.SZ"],
            "entry_date": pd.to_datetime(
                ["2024-01-02", "2024-01-05", "2024-01-12", "2024-01-05"]
            ),
            "exit_date": pd.to_datetime(
                ["2024-01-10", "2024-01-15", "2024-01-20", "2024-01-08"]
            ),
        }
    )

    result = SEARCH.remove_overlapping_positions(events)

    assert result.index.tolist() == [0, 2, 3]


def test_clustered_summary_applies_round_trip_cost() -> None:
    """Calculate net and benchmark-relative results from typed event returns."""
    events = pd.DataFrame(
        {
            "split": ["blind", "blind", "blind", "train"],
            "signal_date": pd.to_datetime(
                ["2024-01-02", "2024-01-02", "2024-02-01", "2020-01-02"]
            ),
            "gross_return": [0.03, 0.01, -0.01, 0.50],
            "benchmark_return": [0.01, 0.00, -0.02, 0.10],
        }
    )

    result = SEARCH.clustered_summary(events, "blind")

    assert result["events"] == 3
    assert result["signal_dates"] == 2
    assert np.isclose(result["avg_gross_return"], 0.01)
    assert np.isclose(result["avg_net_return"], 0.008)
    assert np.isclose(result["avg_net_excess_return"], 0.008 - (-0.01 / 3))

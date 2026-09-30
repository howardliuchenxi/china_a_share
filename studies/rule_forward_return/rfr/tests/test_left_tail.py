"""Left-tail reporting table tests."""
from __future__ import annotations

import numpy as np
import pandas as pd

from rfr.left_tail import annual_event_stability, yearly_portfolio_stability


def test_annual_event_stability_uses_requested_periods():
    events = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-10-01", "2025-06-01", "2026-01-05"]),
            "ret": [0.10, -0.10, 0.05],
            "excess": [0.08, -0.12, 0.03],
        }
    )
    result = annual_event_stability(events, "baseline")
    assert result["period"].tolist() == ["2024Q4", "2025", "2026"]
    assert result["event_count"].tolist() == [1, 1, 1]
    assert np.allclose(result["mean_excess"], [0.08, -0.12, 0.03])


def test_yearly_portfolio_stability_compounds_months():
    monthly = pd.DataFrame(
        {
            "scenario": ["baseline"] * 3,
            "month": ["2024-10", "2024-11", "2025-01"],
            "net_return": [0.10, -0.05, 0.02],
            "benchmark_return": [0.05, -0.02, 0.01],
            "excess_return": [0.05, -0.03, 0.01],
        }
    )
    result = yearly_portfolio_stability(monthly)
    q4 = result[result["period"] == "2024Q4"].iloc[0]
    assert abs(q4["net_return"] - (1.10 * 0.95 - 1.0)) < 1e-12
    assert abs(q4["benchmark_return"] - (1.05 * 0.98 - 1.0)) < 1e-12
    assert q4["positive_excess_months"] == 1

"""Statistics: IS/OOS split, monthly bootstrap, verdicts, null behaviour."""
from __future__ import annotations

import numpy as np
import pandas as pd

from rfr.stats import (
    month_block_bootstrap_ci,
    split_is_oos,
    summarize_with_split,
    verdict,
)


def _events(seed: int = 1, months: int = 6) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2025-01-01", periods=months * 21)
    rows = []
    for ts in dates:
        for _ in range(4):
            ret = rng.normal(0.001, 0.02)
            rows.append(
                {
                    "date": ts,
                    "symbol": f"S{rng.randint(0, 50)}",
                    "ret": ret,
                    "base_ret": 0.0,
                    "excess": ret,
                }
            )
    return pd.DataFrame(rows)


def test_split_is_oos_partitions_by_signal_date():
    events = _events()
    parts = split_is_oos(events)
    assert len(parts["is"]) + len(parts["oos"]) == len(events)
    assert parts["is"]["date"].max() < parts["oos"]["date"].min()
    # ~70/30 by distinct dates.
    ratio = len(parts["is"]) / len(events)
    assert 0.6 < ratio < 0.8


def test_bootstrap_ci_reproducible_and_wider_than_point():
    events = _events(seed=3)
    months = events["date"].dt.strftime("%Y-%m")
    ci_a = month_block_bootstrap_ci(events["excess"], months)
    ci_b = month_block_bootstrap_ci(events["excess"], months)
    assert ci_a == ci_b  # fixed seed
    assert ci_a is not None and np.isfinite(ci_a)
    # A strongly positive mean keeps the CI low end below the mean itself.
    assert ci_a < events["excess"].mean()


def test_bootstrap_ci_needs_multiple_months():
    events = _events(seed=4, months=1)
    months = events["date"].dt.strftime("%Y-%m")
    assert month_block_bootstrap_ci(events["excess"], months) is None


def test_summarize_fields_and_net_excess():
    events = _events(seed=5)
    stats = summarize_with_split(events)
    assert stats["n"] == len(events)
    assert stats["net_excess"] < stats["mean_excess"]  # round-trip cost applied
    assert np.isfinite(stats["is_excess"]) and np.isfinite(stats["oos_excess"])


def test_verdict_levels():
    strong = {"n": 100, "ci_low": 0.001, "oos_excess": 0.01}
    assert verdict(strong, null95=0.005) == "过随机关"
    assert verdict(strong, null95=0.02) == "方向稳健·未过随机关"
    weak = {"n": 100, "ci_low": -0.001, "oos_excess": 0.01}
    assert verdict(weak, null95=0.005) == "未通过"
    empty = {"n": 0, "ci_low": float("nan"), "oos_excess": float("nan")}
    assert verdict(empty, float("nan")) == "无事件"

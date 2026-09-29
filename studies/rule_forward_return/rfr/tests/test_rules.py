"""Rule evaluation checks on small hand-built cross-sections."""
from __future__ import annotations

import numpy as np
import pandas as pd

from rfr import panel as panel_mod
from rfr import rules as rules_mod
from rfr.rules import RuleSpec, evaluate_rule
from rfr.tests.synthetic import business_dates, make_bars


def _cross_section_panel(symbols=("S1", "S2", "S3", "S4", "S5")):
    """Five symbols with distinct drifts so cross-sectional ranks are fixed."""
    dates = business_dates("2025-01-01", 40)
    drifts = {"S1": 0.0, "S2": 0.001, "S3": 0.002, "S4": 0.003, "S5": 0.004}
    frames = []
    for sym in symbols:
        closes = 100.0 * np.cumprod(1.0 + drifts[sym] * np.ones(len(dates)))
        frames.append(make_bars(sym, dates, closes))
    return pd.concat(frames, ignore_index=True)


def _day_frame(factors, date):
    return factors[factors["date"] == date]


def test_quantile_top_decile_picks_strongest_momentum():
    factors = rules_mod.attach_factors(panel_mod.attach_pool_mask(_cross_section_panel()))
    spec = RuleSpec("m", "m", "quantile", "ret_20d", "top")
    # Pool has 5 members (< CROSS_POOL_MIN): no signals should be emitted.
    day = _day_frame(factors, factors["date"].max())
    assert not evaluate_rule(spec, day).any()

    rules_mod.CROSS_POOL_MIN = 4  # shrink threshold for the small fixture
    try:
        mask = evaluate_rule(spec, day)
        assert mask.sum() == 1
        winner = day[day["symbol"] == "S5"].index[0]
        assert bool(mask[winner])
    finally:
        rules_mod.CROSS_POOL_MIN = 50


def test_volume_spike_uses_prior_window_only():
    dates = business_dates("2025-01-01", 30)
    volumes = np.full(30, 1e7)
    volumes[-1] = 3e7  # 3x the prior 20-bar mean -> ratio exactly 3
    frame = make_bars("AAA", dates, np.full(30, 10.0), volumes=volumes)
    factors = rules_mod.attach_factors(panel_mod.attach_pool_mask(frame))
    last = factors.iloc[-1]
    assert abs(last["vol_ratio"] - 3.0) < 1e-9
    assert bool(last["bool_volspike2"])
    # A normal day inside the window does not fire.
    assert not bool(factors.iloc[-5]["bool_volspike2"])


def test_gap_rules_require_prior_close():
    dates = business_dates("2025-01-01", 10)
    closes = np.full(10, 10.0)
    opens = np.full(10, 10.0)
    opens[5] = 10.3  # +3% gap
    opens[7] = 9.7  # -3% gap
    frame = make_bars("AAA", dates, closes, opens)
    factors = rules_mod.attach_factors(panel_mod.attach_pool_mask(frame))
    assert bool(factors.iloc[5]["bool_gapup2"]) and not bool(factors.iloc[5]["bool_gapdown2"])
    assert bool(factors.iloc[7]["bool_gapdown2"]) and not bool(factors.iloc[7]["bool_gapup2"])
    assert factors.iloc[0]["open_gap"] != factors.iloc[0]["open_gap"]  # NaN, no prior close


def test_rsi_extremes_and_golden_cross():
    dates = business_dates("2025-01-01", 260)
    closes = np.full(260, 50.0)
    # Long flat stretch, then a sustained rally lifts RSI to 100 and drives
    # exactly one 50/200 golden cross after bar 200.
    closes[200:] = 50.0 + np.arange(60) * 0.8
    frame = make_bars("AAA", dates, closes)
    factors = rules_mod.attach_factors(panel_mod.attach_pool_mask(frame))
    assert bool(factors.iloc[-1]["bool_rsi14_high"])
    assert int(factors["bool_golden_cross"].sum()) == 1
    assert bool(factors[factors["bool_golden_cross"]].iloc[0]["bool_rsi14_high"])
    assert not factors.iloc[:200]["bool_golden_cross"].any()
    # Flat prices are undefined momentum: RSI must sit at 50, not overbought.
    assert abs(factors.iloc[100]["rsi14"] - 50.0) < 1e-9
    assert not bool(factors.iloc[100]["bool_rsi14_high"])
    # Before any 252-day window exists the 52w rule must stay silent.
    assert factors.iloc[100]["dist_high252"] != factors.iloc[100]["dist_high252"]

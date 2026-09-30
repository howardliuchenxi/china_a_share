"""Frozen improvement-candidate and ensemble construction tests."""
from __future__ import annotations

import numpy as np
import pandas as pd

from rfr.improvements import (
    COMPOSITE_SPECS,
    ENSEMBLE_CAP,
    _capped_ensemble,
    _inverse_vol_weights,
    _static_ensemble,
    attach_improvement_factors,
)
from rfr.portfolio import PORTFOLIO_RULES
from rfr.rules import RuleSpec
from rfr.tests.synthetic import business_dates, make_bars


def test_improvement_factors_match_frozen_cross_sectional_intersections():
    dates = pd.to_datetime(["2025-01-02", "2025-04-02"])
    rows = []
    for date, shift in zip(dates, (0.0, -200.0)):
        for value in range(100):
            rows.append(
                {
                    "date": date,
                    "symbol": f"S{value:03d}",
                    "in_pool": True,
                    "ret_20d": float(value),
                    "ret_5d": float(-value),
                    "ret_60d": float(value + shift),
                    "bool_gapdown2": value >= 90,
                    "bool_volspike2": value % 2 == 0,
                }
            )
    result = attach_improvement_factors(pd.DataFrame(rows))

    by_date = result.groupby("date")
    assert by_date["bool_mom20_rev5"].sum().tolist() == [10, 10]
    assert by_date["bool_mom20_gapdown2"].sum().tolist() == [10, 10]
    assert by_date["bool_gapdown2_volspike2"].sum().tolist() == [5, 5]
    assert by_date["risk_on"].first().tolist() == [True, False]
    assert len(COMPOSITE_SPECS) == 3


def test_static_ensemble_is_the_sum_of_four_independent_capital_books():
    dates = business_dates("2025-01-01", 3)
    frames = []
    for index, rule in enumerate(PORTFOLIO_RULES):
        frames.append(
            pd.DataFrame(
                {
                    "rule": rule,
                    "date": dates,
                    "strategy_nav": [1.0, 1.0 + 0.04 * (index + 1), 1.1],
                    "benchmark_nav": [1.0, 1.02, 1.03],
                    "entries": [0, 1, 0],
                }
            )
        )
    daily, _, _, weights = _static_ensemble(
        pd.concat(frames, ignore_index=True), "ensemble_equal", "equal"
    )

    assert abs(daily.iloc[1]["strategy_nav"] - 1.10) < 1e-12
    assert abs(daily.iloc[2]["strategy_nav"] - 1.10) < 1e-12
    assert weights["target_weight"].eq(0.25).all()


def test_inverse_vol_weights_use_equal_fallback_then_reward_lower_risk():
    thin = pd.DataFrame(
        np.ones((5, 4)), columns=PORTFOLIO_RULES
    )
    assert np.allclose(_inverse_vol_weights(thin), 0.25)

    x = np.arange(20, dtype=float)
    history = pd.DataFrame(
        {
            PORTFOLIO_RULES[0]: x,
            PORTFOLIO_RULES[1]: x * 2.0,
            PORTFOLIO_RULES[2]: x * 3.0,
            PORTFOLIO_RULES[3]: x * 4.0,
        }
    )
    weights = _inverse_vol_weights(history)
    assert weights.is_monotonic_decreasing
    assert abs(weights.sum() - 1.0) < 1e-12


def test_shared_cap_limits_overlapping_rule_entries_to_two_percent():
    dates = business_dates("2025-01-01", 8)
    frames = []
    for value in range(60):
        symbol = "AAA" if value == 0 else f"S{value:03d}"
        frame = make_bars(symbol, dates, closes=np.full(len(dates), 10.0))
        frame["in_pool"] = True
        for rule in PORTFOLIO_RULES:
            frame[f"bool_{rule}"] = (symbol == "AAA") & (
                frame["date"] == dates[0]
            )
        for column in ("open_gap", "ret_5d", "ret_20d", "vol_ratio", "adv20"):
            frame[column] = 0.0
        frames.append(frame)
    factors = pd.concat(frames, ignore_index=True)
    specs = [
        RuleSpec(rule, rule, "bool", f"bool_{rule}")
        for rule in PORTFOLIO_RULES
    ]
    daily, _, _, positions = _capped_ensemble(
        factors,
        dates,
        specs,
        lambda spec, day: day[spec.column],
    )

    assert len(positions) == 4
    assert positions["symbol"].eq("AAA").all()
    assert positions["aggregate_weight_after_entry"].max() <= ENSEMBLE_CAP + 1e-12
    assert abs(positions["entry_weight"].sum() - ENSEMBLE_CAP) < 1e-12
    assert daily["cap_idle_cash"].max() > 0.0


def test_shared_cap_does_not_add_to_a_position_that_drifted_above_cap():
    dates = business_dates("2025-01-01", 9)
    frames = []
    for value in range(60):
        symbol = "AAA" if value == 0 else f"S{value:03d}"
        if symbol == "AAA":
            closes = np.array([10.0, 10.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0])
            opens = np.array([10.0, 10.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0])
        else:
            closes = np.full(len(dates), 10.0)
            opens = closes.copy()
        frame = make_bars(symbol, dates, closes=closes, opens=opens)
        frame["in_pool"] = True
        for rule in PORTFOLIO_RULES:
            frame[f"bool_{rule}"] = (symbol == "AAA") & frame["date"].isin(
                dates[:2]
            )
        for column in ("open_gap", "ret_5d", "ret_20d", "vol_ratio", "adv20"):
            frame[column] = 0.0
        frames.append(frame)
    factors = pd.concat(frames, ignore_index=True)
    specs = [
        RuleSpec(rule, rule, "bool", f"bool_{rule}")
        for rule in PORTFOLIO_RULES
    ]

    _, _, _, positions = _capped_ensemble(
        factors,
        dates,
        specs,
        lambda spec, day: day[spec.column],
    )

    assert len(positions) == 4
    assert positions["signal_date"].eq(dates[0]).all()

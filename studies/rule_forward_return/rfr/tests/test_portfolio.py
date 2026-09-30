"""Executable overlapping-portfolio tests with hand-computed paths."""
from __future__ import annotations

import numpy as np
import pandas as pd

from rfr.portfolio import (
    monthly_portfolio_returns,
    run_overlapping_portfolio,
    summarize_portfolio,
)
from rfr.rules import RuleSpec
from rfr.tests.synthetic import business_dates, make_bars


def test_portfolio_enters_next_open_and_exits_fifth_close():
    dates = business_dates("2025-01-01", 8)
    frame = make_bars(
        "AAA",
        dates,
        closes=[10.0, 11.0, 12.0, 13.0, 14.0, 14.0, 15.0, 15.0],
        opens=[10.0] * 8,
    )
    frame["in_pool"] = True
    frame["bool_forced"] = frame["date"] == dates[0]
    for column in ("open_gap", "ret_5d", "ret_20d", "vol_ratio", "adv20"):
        frame[column] = 0.0
    spec = RuleSpec("forced", "forced", "bool", "bool_forced")
    daily, positions = run_overlapping_portfolio(
        frame,
        dates,
        spec,
        lambda rule, day: day[rule.column],
    )

    assert len(positions) == 1
    position = positions.iloc[0]
    assert position["entry_date"] == dates[1]
    assert position["exit_date"] == dates[5]
    assert abs(position["gross_return"] - 0.40) < 1e-12

    expected_entry_close_nav = 0.8 + 0.2 * (1.0 - 0.0005) * 1.1
    expected_exit_nav = 0.8 + 0.2 * (1.0 - 0.0005) * 1.4 * (1.0 - 0.0005)
    by_date = daily.set_index("date")
    assert abs(by_date.loc[dates[1], "strategy_nav"] - expected_entry_close_nav) < 1e-12
    assert abs(by_date.loc[dates[5], "strategy_nav"] - expected_exit_nav) < 1e-12


def test_portfolio_uses_five_independent_rotating_sleeves():
    dates = business_dates("2025-01-01", 12)
    frames = []
    for symbol in ("AAA", "BBB"):
        frame = make_bars(symbol, dates, closes=np.full(len(dates), 10.0))
        frame["in_pool"] = True
        frame["bool_forced"] = frame["date"] < dates[7]
        for column in ("open_gap", "ret_5d", "ret_20d", "vol_ratio", "adv20"):
            frame[column] = 0.0
        frames.append(frame)
    factors = pd.concat(frames, ignore_index=True)
    spec = RuleSpec("forced", "forced", "bool", "bool_forced")
    daily, positions = run_overlapping_portfolio(
        factors,
        dates,
        spec,
        lambda rule, day: day[rule.column],
    )

    assert daily["active_positions"].max() == 10
    assert positions.groupby("signal_date")["symbol"].count().eq(2).all()
    assert positions["entry_weight"].max() < 0.11
    assert daily["strategy_nav"].iloc[-1] < 1.0
    assert abs(daily["benchmark_nav"].iloc[-1] - 1.0) < 1e-12


def test_terminal_missing_signal_is_skipped_and_baseline_matches_events():
    dates = business_dates("2025-01-01", 8)
    aaa = make_bars(
        "AAA", dates, closes=[10.0, 10.0, 10.0, 10.0, 10.0, 11.0, 11.0, 11.0]
    )
    bbb = make_bars("BBB", dates, closes=np.full(len(dates), 10.0))
    bbb.loc[bbb["date"] == dates[5], "close"] = np.nan
    factors = pd.concat([aaa, bbb], ignore_index=True)
    factors["in_pool"] = True
    factors["bool_forced"] = (
        factors["date"] == dates[0]
    )
    for column in ("open_gap", "ret_5d", "ret_20d", "vol_ratio", "adv20"):
        factors[column] = 0.0
    spec = RuleSpec("forced", "forced", "bool", "bool_forced")
    daily, positions = run_overlapping_portfolio(
        factors,
        dates,
        spec,
        lambda rule, day: day[rule.column],
    )

    assert len(positions) == 1
    assert positions.iloc[0]["symbol"] == "AAA"
    assert abs(positions.iloc[0]["benchmark_return"] - 0.10) < 1e-12
    assert daily["skipped_missing_exit"].sum() == 1


def test_missing_entry_is_reported_separately_from_position_cap():
    dates = business_dates("2025-01-01", 8)
    frame = make_bars("AAA", dates, closes=np.full(len(dates), 10.0))
    frame.loc[frame["date"] == dates[1], "open"] = np.nan
    frame["in_pool"] = True
    frame["bool_forced"] = frame["date"] == dates[0]
    for column in ("open_gap", "ret_5d", "ret_20d", "vol_ratio", "adv20"):
        frame[column] = 0.0
    spec = RuleSpec("forced", "forced", "bool", "bool_forced")
    daily, positions = run_overlapping_portfolio(
        frame,
        dates,
        spec,
        lambda rule, day: day[rule.column],
        max_position_weight=0.10,
    )

    assert positions.empty
    assert daily["skipped_missing_entry"].sum() == 1
    assert daily["capped_out_signals"].sum() == 0


def test_deep_gap_filter_removes_only_signals_beyond_threshold():
    dates = business_dates("2025-01-01", 8)
    frames = []
    for symbol, gap in (("AAA", -0.05), ("BBB", -0.10)):
        frame = make_bars(symbol, dates, closes=np.full(len(dates), 10.0))
        frame["in_pool"] = True
        frame["bool_forced"] = frame["date"] == dates[0]
        frame["open_gap"] = gap
        for column in ("ret_5d", "ret_20d", "vol_ratio", "adv20"):
            frame[column] = 0.0
        frames.append(frame)
    factors = pd.concat(frames, ignore_index=True)
    spec = RuleSpec("forced", "forced", "bool", "bool_forced")
    daily, positions = run_overlapping_portfolio(
        factors,
        dates,
        spec,
        lambda rule, day: day[rule.column],
        max_abs_open_gap=0.08,
    )

    assert positions["symbol"].tolist() == ["AAA"]
    assert daily["filtered_signals"].sum() == 1


def test_position_cap_uses_aggregate_existing_exposure():
    dates = business_dates("2025-01-01", 12)
    frame = make_bars("AAA", dates, closes=np.full(len(dates), 10.0))
    frame["in_pool"] = True
    frame["bool_forced"] = frame["date"] < dates[7]
    for column in ("open_gap", "ret_5d", "ret_20d", "vol_ratio", "adv20"):
        frame[column] = 0.0
    spec = RuleSpec("forced", "forced", "bool", "bool_forced")
    daily, positions = run_overlapping_portfolio(
        frame,
        dates,
        spec,
        lambda rule, day: day[rule.column],
        max_position_weight=0.10,
    )

    assert len(positions) == 2
    assert positions["signal_date"].tolist() == [dates[0], dates[5]]
    assert positions["aggregate_weight_after_entry"].max() <= 0.10 + 1e-12
    assert daily["capped_out_signals"].sum() > 0
    assert daily["cap_idle_cash"].max() > 0.0


def test_monthly_summary_requires_both_stability_gates():
    dates = pd.date_range("2024-10-31", periods=24, freq="ME")
    excess = np.full(24, 0.01)
    benchmark = np.zeros(24)
    net = excess.copy()
    strategy_nav = np.cumprod(1.0 + net)
    benchmark_nav = np.cumprod(1.0 + benchmark)
    daily = pd.DataFrame(
        {
            "rule": "stable",
            "date": dates,
            "strategy_nav": strategy_nav,
            "benchmark_nav": benchmark_nav,
            "entries": 1,
            "net_return": net,
            "benchmark_return": benchmark,
        }
    )
    monthly = monthly_portfolio_returns(daily)
    summary = summarize_portfolio(daily, monthly)
    assert summary["months"] == 24
    assert summary["positive_excess_months"] == 24
    assert summary["ci_low"] > 0.0
    assert summary["verdict"] == "通过"

    monthly.loc[:8, "excess_return"] = -0.01
    unstable = summarize_portfolio(daily, monthly)
    assert unstable["positive_excess_months"] == 15
    assert unstable["verdict"] == "未通过"

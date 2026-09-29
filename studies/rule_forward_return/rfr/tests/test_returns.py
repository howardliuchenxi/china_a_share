"""Hand-computed forward return checks and window-edge behaviour."""
from __future__ import annotations

import numpy as np
import pandas as pd

from rfr import panel as panel_mod
from rfr import rules as rules_mod
from rfr.returns import collect_events, forward_return_matrix
from rfr.rules import RuleSpec
from rfr.tests.synthetic import business_dates, make_bars


def _three_symbol_panel():
    dates = business_dates("2025-01-01", 30)
    closes = np.full(30, 10.0)
    opens = np.full(30, 10.0)
    a = make_bars("AAA", dates, closes.copy(), opens.copy())
    a.loc[a.index[-1], "close"] = 12.0
    b = make_bars("BBB", dates, closes.copy(), opens.copy())
    b.loc[b.index[-1], "close"] = 9.0
    c = make_bars("CCC", dates, closes.copy(), opens.copy())
    return pd.concat([a, b, c], ignore_index=True)


def test_forward_return_hand_computed():
    dates = business_dates("2025-01-01", 6)
    frame = make_bars("AAA", dates, closes=[10, 10, 11, 12, 12, 12], opens=[10] * 6)
    index = pd.DatetimeIndex(frame["date"].unique())
    opens = frame.pivot(index="date", columns="symbol", values="open").reindex(index)
    closes = frame.pivot(index="date", columns="symbol", values="close").reindex(index)
    # Signal at position 1: entry open at pos 2 = 10, exit close at pos 1+N.
    ret_n1 = forward_return_matrix(opens, closes, 1, 1)["AAA"]
    assert abs(ret_n1 - (11.0 / 10.0 - 1.0)) < 1e-12
    ret_n4 = forward_return_matrix(opens, closes, 1, 4)["AAA"]
    assert abs(ret_n4 - (12.0 / 10.0 - 1.0)) < 1e-12


def test_collect_events_gap_rule_pool_baseline_and_excess():
    raw = _three_symbol_panel()
    calendar = pd.DatetimeIndex(sorted(raw["date"].unique()))
    # Inject a +10% opening gap on AAA three bars before the end.
    raw.loc[(raw["symbol"] == "AAA") & (raw["date"] == calendar[-3]), "open"] = 11.0
    factors = rules_mod.attach_factors(panel_mod.attach_pool_mask(raw))
    spec = RuleSpec("gap", "gap", "bool", "bool_gapup2")
    events = collect_events(
        factors, calendar, spec, rules_mod.evaluate_rule, hold_grid=(1,)
    )[1].events
    assert len(events) == 1
    row = events.iloc[0]
    assert row["symbol"] == "AAA"
    assert row["date"] == calendar[-3]
    # Entry = next open (10), exit = close of T+1 (10): ret and baseline are 0.
    assert abs(row["ret"]) < 1e-12
    assert abs(row["base_ret"]) < 1e-12


def test_collect_events_uses_pool_as_baseline():
    raw = _three_symbol_panel()
    calendar = pd.DatetimeIndex(sorted(raw["date"].unique()))
    factors = rules_mod.attach_factors(panel_mod.attach_pool_mask(raw))
    spec = RuleSpec("forced", "forced", "bool", "bool_forced")
    # Force every pool member to signal on the second-to-last date: entries at
    # the final open (10) and exits at the final closes (12/9/10).
    factors["bool_forced"] = factors["date"] == calendar[-2]
    events = collect_events(
        factors, calendar, spec, rules_mod.evaluate_rule, hold_grid=(1,)
    )[1].events
    assert len(events) == 3
    by_symbol = events.set_index("symbol")
    assert abs(by_symbol.loc["AAA", "ret"] - 0.20) < 1e-12
    assert abs(by_symbol.loc["BBB", "ret"] - (-0.10)) < 1e-12
    expected_baseline = (0.20 - 0.10 + 0.0) / 3.0
    assert abs(events["base_ret"].iloc[0] - expected_baseline) < 1e-12
    assert abs(by_symbol.loc["AAA", "excess"] - (0.20 - expected_baseline)) < 1e-12


def test_window_edge_counts_truncated_events():
    raw = _three_symbol_panel()
    factors = rules_mod.attach_factors(panel_mod.attach_pool_mask(raw))
    calendar = pd.DatetimeIndex(sorted(factors["date"].unique()))
    spec = RuleSpec("forced", "forced", "bool", "bool_forced")
    factors["bool_forced"] = factors["date"] >= calendar[-2]
    results = collect_events(
        factors, calendar, spec, rules_mod.evaluate_rule, hold_grid=(1, 5)
    )
    # Signals on the final date cannot produce an N=1 event: truncated.
    assert results[1].skipped_truncated == 3
    assert len(results[1].events) == 3  # second-to-last date still resolves
    assert results[5].skipped_truncated == 6
    assert len(results[5].events) == 0

"""No-lookahead invariant: signals at date T must not change when the panel
is truncated after T. This is the general guard against rolling/shift bugs
and cross-sectional rank leakage from future bars."""
from __future__ import annotations

import numpy as np
import pandas as pd

from rfr import panel as panel_mod
from rfr import rules as rules_mod
from rfr.rules import RULE_REGISTRY, evaluate_rule
from rfr.tests.synthetic import business_dates, make_bars


def _ragged_panel():
    """Random-walk panel with staggered history lengths and volume noise."""
    rng = np.random.RandomState(7)
    dates = business_dates("2025-01-01", 300)
    frames = []
    for i, sym in enumerate([f"S{k}" for k in range(60)]):
        length = 300 - (i % 5) * 10  # staggered ends, some histories shorter
        closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.02, length))
        opens = closes * (1.0 + rng.normal(0.0, 0.005, length))
        volumes = 5e6 * np.exp(rng.normal(0.0, 0.6, length))
        frames.append(make_bars(sym, dates[:length], closes, opens, volumes))
    return pd.concat(frames, ignore_index=True)


def test_signals_invariant_to_future_truncation():
    full = rules_mod.attach_factors(panel_mod.attach_pool_mask(_ragged_panel()))
    calendar = panel_mod.trading_calendar(full)
    cut = calendar[len(calendar) // 2]

    truncated_bars = full[full["date"] <= cut][
        ["symbol", "date", "open", "high", "low", "close", "volume", "vwap", "transactions"]
    ]
    truncated = rules_mod.attach_factors(panel_mod.attach_pool_mask(truncated_bars))

    check_dates = [d for d in calendar[: len(calendar) // 2]][-30:]
    for spec in RULE_REGISTRY:
        for ts in check_dates:
            day_full = full[full["date"] == ts]
            day_trunc = truncated[truncated["date"] == ts]
            mask_full = evaluate_rule(spec, day_full)
            mask_trunc = evaluate_rule(spec, day_trunc)
            symbols_full = set(day_full[mask_full]["symbol"])
            symbols_trunc = set(day_trunc[mask_trunc]["symbol"])
            assert symbols_full == symbols_trunc, (
                f"{spec.name} leaked future data at {ts}: "
                f"{symbols_full ^ symbols_trunc}"
            )


def test_pool_flags_invariant_to_future_truncation():
    full = panel_mod.attach_pool_mask(_ragged_panel())
    calendar = panel_mod.trading_calendar(full)
    cut = calendar[len(calendar) // 2]
    truncated = panel_mod.attach_pool_mask(full[full["date"] <= cut][
        ["symbol", "date", "open", "high", "low", "close", "volume", "vwap", "transactions"]
    ])
    for ts in list(calendar[: len(calendar) // 2])[-10:]:
        a = set(full[(full["date"] == ts) & full["in_pool"]]["symbol"])
        b = set(truncated[(truncated["date"] == ts) & truncated["in_pool"]]["symbol"])
        assert a == b, f"pool membership leaked future data at {ts}: {a ^ b}"

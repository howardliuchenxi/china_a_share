"""Self-certification tests: exchange-exact limits, no look-ahead, method recovery.

Run with the study venv:  ./.venv/bin/python -m pytest tests/ -q
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rri.events import (  # noqa: E402
    dedup_first,
    label_events,
    market_daily_returns,
    streak_runs,
    trigger_events,
)
from rri.factors import ALL_FACTORS, build_factor_matrix  # noqa: E402
from rri.limits import (  # noqa: E402
    board_of,
    limit_pct_bp,
    limit_up_cents,
    st_flag_from_names,
    yuan_to_cents,
)
from rri.search import (  # noqa: E402
    bh_adjust,
    block_bootstrap_ci,
    eval_condition,
    m1_single_conditions,
    m2_tree_paths,
    m3_beam_search,
)


# --------------------------------------------------------------- limit pricing
def test_limit_up_rounding_half_cent():
    # 6.05 * 1.1 = 6.655 -> exchange rounds half-up to 6.66
    pre = yuan_to_cents(np.array([6.05, 10.00, 9.87, 3.33]))
    bp = np.full(4, 1000)
    assert list(limit_up_cents(pre, bp)) == [666, 1100, 1086, 366]
    # 5% ST: 10.00 -> 10.50 exact
    assert limit_up_cents(np.array([1000]), np.array([500]))[0] == 1050
    # 20%: 6.05 -> 7.26
    assert limit_up_cents(np.array([605]), np.array([2000]))[0] == 726


def test_board_and_st_rules():
    codes = pd.Series(["688001.SH", "300001.SZ", "301001.SZ", "600000.SH", "000001.SZ"])
    boards = board_of(codes)
    assert list(boards) == ["star", "chinext", "chinext", "main", "main"]
    dates = np.array([20200101, 20200101, 20200824, 20240101, 20240101])
    is_st = np.array([False, False, False, False, True])
    bp = limit_pct_bp(boards, dates, is_st)
    # chinext 10% before 2020-08-24, 20% after; star always 20; ST main 5
    assert list(bp) == [2000, 1000, 2000, 1000, 500]


def test_st_flag_time_varying():
    nc = pd.DataFrame({
        "ts_code": ["000001.SZ", "000001.SZ"],
        "name": ["平安银行", "ST平安"],
        "start_date": ["20150101", "20200101"],
        "end_date": ["20191231", None],
    })
    basic_name = pd.Series({"000001.SZ": "ST平安"})
    codes = pd.Series(["000001.SZ", "000001.SZ", "000001.SZ"])
    dates = np.array([20190601, 20200601, 20240601])
    flags = st_flag_from_names(nc, basic_name, codes, dates)
    assert list(flags) == [False, True, True]


# ------------------------------------------------------------------- events
def _make_panel(prices: dict[str, list[float]], boards=None, st=None) -> pd.DataFrame:
    """prices: code -> list of closes; one row per business day from 2024-01-02."""
    rows = []
    dates = pd.bdate_range("2024-01-02", periods=max(len(v) for v in prices.values()))
    dates = dates.strftime("%Y%m%d").astype(int).tolist()
    for code, closes in prices.items():
        pre = closes[0] / 1.0
        for i, c in enumerate(closes):
            rows.append({
                "ts_code": code, "trade_date": dates[i], "session_rank": i,
                "open": c * 0.99, "high": c * 1.01, "low": c * 0.98, "close": c,
                "pre_close": pre, "pct_chg": (c / pre - 1) * 100,
                "vol": 1e6, "amount": 1e9, "adj_factor": 1.0,
                "board": (boards or {}).get(code, "main"),
                "is_st": (st or {}).get(code, False),
                "volume_ratio": 1.0, "turnover_rate": 1.0,
            })
            pre = c
    return pd.DataFrame(rows)


def test_streak_runs_and_trigger():
    # 3-board streak at sessions 6..8 (goes to E1_EXT), then an independent
    # exact 2-board streak at sessions 15..16 (goes to E1)
    closes = [10.0] * 6 + [11.0, 12.1, 13.31] + [13.5, 13.6, 13.7, 13.8, 13.9,
              14.0, 15.4, 16.94, 17.0, 17.1, 17.2, 17.3, 17.4, 17.5, 17.6,
              17.7, 17.8, 17.9, 18.0, 18.1]
    panel = _make_panel({"AAA.SZ": closes})
    is_limit = []
    for _, g in panel.groupby("ts_code"):
        from rri.events import per_stock_limit_flags
        is_limit = per_stock_limit_flags(g)
    runs = streak_runs(is_limit, panel["session_rank"].to_numpy())
    assert (6, 8) in runs and (15, 16) in runs
    events = trigger_events(panel, len(panel))
    # E1 anchors at the 2nd board of the 2-streak (session 16); the 3-board
    # run is E1_EXT anchored at session 8
    assert len(events["E1"]) == 1
    assert events["E1"]["trade_date"].iloc[0] == panel["trade_date"].iloc[16]
    assert len(events["E1_EXT"]) == 1
    assert events["E1_EXT"]["trade_date"].iloc[0] == panel["trade_date"].iloc[8]


def test_dedup_first_30_days():
    ev = pd.DataFrame({
        "ts_code": ["A", "A", "A", "B"],
        "trade_date": [20240102, 20240120, 20240220, 20240102],
        "base_date": [20240101, 20240119, 20240219, 20240101],
        "streak_len": [2, 2, 2, 2],
    })
    out = dedup_first(ev)
    assert list(out["trade_date"]) == [20240102, 20240220, 20240102]


def test_label_uses_stock_sessions_and_market_excess():
    # stock B doubles; market flat -> excess positive; stock C flat -> excess ~0
    closes_b = [10.0] * 15 + [11.0] * 15
    closes_c = [10.0] * 30
    panel = _make_panel({"B.SZ": closes_b, "C.SZ": closes_c})
    mkt = market_daily_returns(panel)
    events = pd.DataFrame({
        "ts_code": ["B.SZ", "C.SZ"],
        "trade_date": [panel["trade_date"].iloc[4]] * 2,
        "base_date": [panel["trade_date"].iloc[4]] * 2,
        "streak_len": [1, 1],
    })
    labeled = label_events(events, panel, mkt, window=20)
    b_label = labeled.loc[0, "fwd_excess_20d"]
    c_label = labeled.loc[1, "fwd_excess_20d"]
    # market baseline rises because B rises (2-stock panel); B beats market by
    # ~+4.8%, flat C lags it by the mirrored ~-4.8%
    assert b_label > 0.03
    assert c_label < -0.03
    assert b_label > c_label


# ------------------------------------------------------------------ no lookahead
def test_factors_have_no_lookahead():
    rng = np.random.default_rng(3)
    n = 60
    closes = list(10 * np.exp(np.cumsum(rng.normal(0, 0.02, n))))
    base = _make_panel({"X.SZ": closes})
    pert = _make_panel({"X.SZ": closes})  # identical prefix; spike after day 40
    pert.loc[pert.index[41:], "close"] *= 5.0
    pert.loc[pert.index[41:], "high"] *= 5.0
    date_rank = {d: i for i, d in enumerate(base["trade_date"].unique())}
    ev = pd.DataFrame({"ts_code": ["X.SZ"], "trade_date": [base["trade_date"].iloc[39]],
                       "base_date": [base["trade_date"].iloc[39]], "streak_len": [1]})
    f_base = build_factor_matrix(base, list(date_rank), ev.set_index(pd.Index([1000])))
    ev2 = ev.copy()
    f_pert = build_factor_matrix(pert, list(date_rank), ev2.set_index(pd.Index([1000])))
    cols = [c for c in f_base.columns if c in ALL_FACTORS]
    for c in cols:
        a, b = f_base[c].iloc[0], f_pert[c].iloc[0]
        if pd.isna(a) and pd.isna(b):
            continue
        assert np.isclose(a, b), f"look-ahead leak in {c}: {a} vs {b}"


# ------------------------------------------------- method recovery on synthetic
def _synthetic_events(n=4000, seed=5, planted_lift=2.0, planted_share=0.3):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame({
        "alpha": rng.normal(size=n),
        "beta": rng.normal(size=n),
        "gamma": rng.normal(size=n),
        "noise1": rng.normal(size=n),
        "noise2": rng.normal(size=n),
    })
    planted = X["alpha"] <= -0.524  # 30% lowest alpha
    p = np.where(planted, 0.30 * planted_lift, 0.30)
    y = (rng.random(n) < p).astype(int)
    months = np.repeat(np.arange(1, 13), n // 12)
    return X, y, months, ("alpha", "<=", -0.524)


def test_m1_m2_m3_recover_planted_rule():
    X, y, months, planted = _synthetic_events()
    min_sup = max(50, int(len(y) * 0.05))
    for name, cands in (
        ("M1", m1_single_conditions(X, y, min_sup)),
        ("M2", m2_tree_paths(X, y, min_sup)),
        ("M3", m3_beam_search(X, y, min_sup)),
    ):
        found = [c for c in cands
                 if any(f == planted[0] and op == planted[1] for f, op, _ in c["conditions"])]
        assert found, f"{name} failed to recover the planted factor"
        best = max(found, key=lambda c: c["lift"])
        assert best["lift"] > 1.3, f"{name} lift too low: {best['lift']}"


def test_eval_condition_p_value_edges():
    # baseline (complement) 100% positives -> nothing to beat -> p = 1
    y = np.array([0, 0, 0, 1, 1, 1, 1, 1, 1, 1])
    mask = np.array([True] * 5 + [False] * 5)
    stat = eval_condition(mask, y)
    assert stat["p_value"] == 1.0
    # baseline zero positives -> any inside hit is decisive -> p = 0
    y2 = np.array([1, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    stat2 = eval_condition(mask, y2)
    assert stat2["p_value"] == 0.0
    stat3 = eval_condition(mask, np.array([1, 0, 1, 0, 1, 1, 0, 1, 0, 1]))
    assert 0.0 < stat3["p_value"] < 1.0


def test_bh_adjust_monotone():
    q = bh_adjust(np.array([0.001, 0.002, 0.5, 0.9]))
    assert q[0] <= q[1] <= 0.01
    assert q[2] <= q[3]
    assert (q >= 0).all() and (q <= 1).all()


def test_bootstrap_ci_reasonable():
    rng = np.random.default_rng(9)
    n = 2000
    X = pd.DataFrame({"f": rng.normal(size=n)})
    y = (rng.random(n) < 0.3 + 0.2 * (X["f"] > 0)).astype(int)
    months = np.repeat(np.arange(24), n // 24)
    lo, hi = block_bootstrap_ci(X, y, months, [("f", ">=", 0.0)], rounds=200, seed=1)
    assert lo < hi
    assert lo > 1.0  # planted effect should be clearly positive

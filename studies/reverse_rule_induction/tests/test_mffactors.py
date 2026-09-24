"""RRI-2 tests: new moneyflow / top-list factors have no look-ahead."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rri.mffactors import (  # noqa: E402
    NEW_FACTORS,
    moneyflow_factors,
    normalize_by_event_amount,
    top_list_factors,
)

DATES = [str(20240102 + i) for i in range(10)]  # 10 consecutive "sessions"
DATE_RANK = {d: i for i, d in enumerate(DATES)}


def _mf_frame(main_net_path):
    rows = []
    for code, path in main_net_path.items():
        for i, d in enumerate(DATES):
            buy_lg = 100.0 + path[i]
            rows.append({
                "ts_code": code, "trade_date": d,
                "buy_lg_amount": buy_lg, "sell_lg_amount": 50.0,
                "buy_elg_amount": 20.0 + path[i] / 2, "sell_elg_amount": 10.0,
                "net_mf_amount": 30.0 + path[i],
            })
    return pd.DataFrame(rows)


def _same(a, b):
    if pd.isna(a) and pd.isna(b):
        return True
    return np.isclose(a, b)


def test_moneyflow_factors_ignore_future():
    ev = pd.DataFrame({"ts_code": ["A.SZ"], "trade_date": [DATES[4]]})
    base_path = {code: [1.0] * 10 for code in ("A.SZ", "B.SZ")}
    base = moneyflow_factors(_mf_frame(base_path), DATE_RANK, ev)

    spiked_path = {code: ([1.0] * 10) for code in ("A.SZ", "B.SZ")}
    spiked_path["A.SZ"][5:] = [10_000.0] * 5  # massive change AFTER the anchor
    spiked = moneyflow_factors(_mf_frame(spiked_path), DATE_RANK, ev)

    for col in ("mf_net_ratio", "mf_main_net_ratio", "mf_elg_net_ratio",
                "mf_main_net_z20"):
        assert _same(base[col].iloc[0], spiked[col].iloc[0]), col
    # 5-session sum includes the anchor day itself but nothing after
    assert _same(base["mf_main_net_5d_sum"].iloc[0],
                 spiked["mf_main_net_5d_sum"].iloc[0])


def test_moneyflow_z20_uses_prior_twenty():
    # 21 sessions; anchor on the last day; prior 20 constant, today spiked
    dates = [str(20240102 + i) for i in range(21)]
    rank = {d: i for i, d in enumerate(dates)}
    rows = []
    for i, d in enumerate(dates):
        rows.append({"ts_code": "A.SZ", "trade_date": d,
                     "buy_lg_amount": 100.0, "sell_lg_amount": 50.0,
                     "buy_elg_amount": 1000.0 if i == 20 else 20.0,
                     "sell_elg_amount": 10.0,
                     "net_mf_amount": 60.0})
    mf = pd.DataFrame(rows)
    ev = pd.DataFrame({"ts_code": ["A.SZ"], "trade_date": [dates[20]]})
    out = moneyflow_factors(mf, rank, ev)
    # prior 20 main_net = (100-50)+(20-10) = 60 each, std=0 -> z NaN
    assert np.isnan(out["mf_main_net_z20"].iloc[0])
    # make prior vary
    mf.loc[mf.index[:20], "buy_lg_amount"] = 100.0 + np.arange(20) * 5
    out2 = moneyflow_factors(mf, rank, ev)
    z = out2["mf_main_net_z20"].iloc[0]
    assert np.isfinite(z) and z > 1.0  # today 1090-ish vs prior mean ~97


def test_top_list_flags_and_counts():
    tl = pd.DataFrame({
        "ts_code": ["A.SZ", "A.SZ", "B.SZ"],
        "trade_date": [DATES[2], DATES[6], DATES[3]],
        "net_amount": [1_000_000.0, -500_000.0, 300_000.0],
        "amount_rate": [12.5, 8.0, 3.0],
    })
    ti = pd.DataFrame({
        "ts_code": ["A.SZ"], "trade_date": [DATES[6]],
        "exalter": ["机构专用"], "net_buy": [800_000.0],
    })
    ev = pd.DataFrame({"ts_code": ["A.SZ", "B.SZ"],
                       "trade_date": [DATES[6], DATES[4]]})
    out = top_list_factors(tl, ti, DATE_RANK, ev)
    assert out["on_top_list"].iloc[0] == 1.0
    assert out["on_top_list"].iloc[1] == 0.0
    # A.SZ on list at sessions 2 and 6; anchor session 6 -> count 2
    assert out["top_list_count_20d"].iloc[0] == 2.0
    # B.SZ on list at session 3; anchor session 4 -> count 1
    assert out["top_list_count_20d"].iloc[1] == 1.0
    assert np.isclose(out["tl_net_amount_ratio"].iloc[0], -500_000.0)  # day-6 row only
    # B.SZ is NOT on the list on its anchor day -> raw ratio NaN (history
    # appearances are captured by top_list_count_20d instead)
    assert np.isnan(out["tl_net_amount_ratio"].iloc[1])
    assert np.isclose(out["inst_net_buy_ratio"].iloc[0], 800_000.0)
    assert np.isnan(out["inst_net_buy_ratio"].iloc[1])


def test_normalize_by_event_amount():
    f = pd.DataFrame({
        "mf_net_ratio": [100.0],      # 万元
        "mf_main_net_ratio": [50.0],
        "mf_elg_net_ratio": [20.0],
        "tl_net_amount_ratio": [1_000_000.0],  # 元
        "inst_net_buy_ratio": [500_000.0],
        "on_top_list": [1.0], "tl_amount_rate_max": [10.0],
        "top_list_count_20d": [1.0], "mf_main_net_5d_sum": [1.0],
        "mf_main_net_z20": [1.0],
    })
    amount = [2000.0]  # 千元 -> 2,000,000 元
    out = normalize_by_event_amount(f, amount)
    assert np.isclose(out["mf_net_ratio"].iloc[0], 100 * 1e4 / 2e6)   # 0.5
    assert np.isclose(out["tl_net_amount_ratio"].iloc[0], 1e6 / 2e6)  # 0.5
    assert np.isclose(out["inst_net_buy_ratio"].iloc[0], 0.25)
    assert np.isclose(out["on_top_list"].iloc[0], 1.0)  # untouched


def test_new_factors_registered():
    assert len(NEW_FACTORS) == 10

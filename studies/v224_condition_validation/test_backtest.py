"""Regression tests for the v2.24 state-transition invariants."""

from __future__ import annotations

import pandas as pd

from studies.v224_condition_validation.backtest import simulate_prepared_panel


def _rows(specifications: list[dict[str, object]]) -> pd.DataFrame:
    defaults: dict[str, object] = {
        "ts_code": "000001.SZ",
        "name": "Example",
        "industry_code": "881001.TI",
        "industry_name": "Example industry",
        "l1_pass": True,
        "l2_pass": True,
        "l3_day1": False,
        "l3_follow": False,
        "r60_pass": True,
        "l45_trigger": False,
        "l67_trigger": False,
        "qf": 12.0,
        "qf_open": 11.9,
        "ma5": 11.5,
        "ma20": 11.0,
        "ma60": 10.5,
        "prev_ma5": 11.4,
        "prev_ma20": 11.0,
        "prev_ma60": 10.5,
        "amount": 180.0,
        "prev5_amount_mean": 100.0,
        "turnover_rate": 3.0,
        "market_cap_billion": 80.0,
        "pe": 30.0,
    }
    records = []
    for position, changes in enumerate(specifications):
        record = dict(defaults)
        record.update(changes)
        record["trade_date"] = f"202501{position + 1:02d}"
        record["session_rank"] = position
        records.append(record)
    return pd.DataFrame(records)


def _path_to_l6(*, fail_l2_in_l4: bool = False, l45_r60: bool = True) -> pd.DataFrame:
    rows = [
        {"l3_day1": True},
        *[{"l3_follow": True} for _ in range(8)],
        {
            "l3_follow": True,
            "l2_pass": not fail_l2_in_l4,
            "l45_trigger": True,
            "r60_pass": l45_r60,
        },
        {"l2_pass": False},
    ]
    return _rows(rows)


def test_v224_does_not_retroactively_eject_l3_l6_on_l2_failure() -> None:
    panel = _path_to_l6()
    panel.loc[len(panel)] = panel.iloc[-1]
    panel.loc[len(panel) - 1, "trade_date"] = "20250112"
    panel.loc[len(panel) - 1, "session_rank"] = 11
    panel.loc[len(panel) - 1, "l67_trigger"] = True
    canonical, _ = simulate_prepared_panel(
        panel,
        retroactive_l1_l2_gate=False,
        version_label="v2.24",
    )
    retroactive, diagnostics = simulate_prepared_panel(
        panel,
        retroactive_l1_l2_gate=True,
        version_label="retroactive_counterfactual",
    )
    assert len(canonical) == 1
    assert retroactive.empty
    assert diagnostics["retroactive_gate_ejections_l3_l6"] == 1


def test_new_l6_position_cannot_trigger_l7_until_the_next_session() -> None:
    panel = _path_to_l6()
    panel.loc[10, "l67_trigger"] = True
    entry_day_only, _ = simulate_prepared_panel(
        panel,
        retroactive_l1_l2_gate=False,
        version_label="v2.24",
    )
    assert entry_day_only.empty

    next_day = panel.iloc[-1].copy()
    next_day["trade_date"] = "20250112"
    next_day["session_rank"] = 11
    next_day["l67_trigger"] = True
    panel = pd.concat([panel, next_day.to_frame().T], ignore_index=True)
    next_day_event, _ = simulate_prepared_panel(
        panel,
        retroactive_l1_l2_gate=False,
        version_label="v2.24",
    )
    assert len(next_day_event) == 1
    assert next_day_event.iloc[0]["dwell6"] == 2


def test_r60_failure_wins_over_same_day_l4_to_l5_trigger() -> None:
    panel = _path_to_l6(l45_r60=False)
    later = panel.iloc[-1].copy()
    later["trade_date"] = "20250112"
    later["session_rank"] = 11
    later["l67_trigger"] = True
    panel = pd.concat([panel, later.to_frame().T], ignore_index=True)
    events, diagnostics = simulate_prepared_panel(
        panel,
        retroactive_l1_l2_gate=False,
        version_label="v2.24",
    )
    assert events.empty
    assert diagnostics["r60_ejections_l4_l6"] == 1

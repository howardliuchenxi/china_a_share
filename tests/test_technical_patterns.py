import pandas as pd
import pytest

from china_a_share.technical_patterns import TechnicalPatternStudy


def _panel() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=85).strftime("%Y%m%d")
    rows = []
    for code, industry, multiplier in (
        ("000001.SZ", "Banking", 1.0),
        ("600000.SH", "Software", 1.1),
    ):
        closes = [20.0 - index * 0.1 for index in range(61)]
        closes.extend([14.0 + index * 0.2 for index in range(24)])
        for index, (trade_date, close) in enumerate(zip(dates, closes)):
            rows.append(
                {
                    "ts_code": code,
                    "name": f"Stock {code}",
                    "industry": industry,
                    "trade_date": trade_date,
                    "open": (close - 0.4) * multiplier,
                    "high": (close + (5.0 if index == 0 else 0.3)) * multiplier,
                    "close": close * multiplier,
                    "turnover_rate": 2.0,
                    "adj_factor": 1.0,
                }
            )
    return pd.DataFrame(rows)


def test_pattern_study_finds_general_l_bottom_and_forward_curve():
    result = TechnicalPatternStudy.run(_panel())

    events = result.events[result.events["pattern"] == "l_bottom"]
    assert not events.empty
    assert events["prior_60d_high"].gt(1.2 * events["close"]).all()
    assert events["ma5"].gt(events["ma10"]).all()
    assert events["forward_10d_return"].notna().any()
    assert set(events["industry"]) == {"Banking", "Software"}


def test_pattern_study_requires_complete_prior_60_session_peak_window():
    panel = _panel().groupby("ts_code", group_keys=False).head(60)

    result = TechnicalPatternStudy.run(panel)

    assert not (result.events["pattern"] == "l_bottom").any()


def test_pattern_study_ma20_cross_uses_last_60_market_dates_and_two_day_return():
    dates = pd.bdate_range("2026-01-05", periods=85).strftime("%Y%m%d").tolist()
    signal_date = dates[-4]
    rows = []
    for index, trade_date in enumerate(dates):
        is_signal = trade_date == signal_date
        rows.append(
            {
                "ts_code": "000001.SZ",
                "name": "Stock 000001.SZ",
                "industry": "Banking",
                "trade_date": trade_date,
                "open": 9.95 if is_signal else 10.0,
                "high": 10.1,
                "close": 10.05 if is_signal else 10.0,
                "turnover_rate": 1.61 if is_signal else 2.0,
                "adj_factor": 1.0,
            }
        )
    panel = pd.DataFrame(rows)

    result = TechnicalPatternStudy.run(panel)
    event = result.events[
        (result.events["pattern"] == "ma20_intraday_cross")
        & (result.events["ts_code"] == "000001.SZ")
        & (result.events["trade_date"] == signal_date)
    ]

    assert len(event) == 1
    assert event.iloc[0]["turnover_rate"] == pytest.approx(1.61)
    assert pd.notna(event.iloc[0]["forward_2d_return"])
    summary = result.industry_summary
    assert {
        "event_count",
        "mean_cumulative_return",
        "median_cumulative_return",
        "positive_return_rate",
    } <= set(summary.columns)


def test_pattern_study_rejects_duplicate_security_dates():
    panel = _panel()
    duplicated = pd.concat([panel, panel.iloc[[0]]], ignore_index=True)

    with pytest.raises(ValueError, match="duplicate security dates"):
        TechnicalPatternStudy.run(duplicated)


def test_pattern_study_does_not_bridge_a_suspended_security_session():
    panel = _panel()
    missing_date = sorted(panel["trade_date"].unique())[62]
    panel = panel[
        ~(
            (panel["ts_code"] == "000001.SZ")
            & (panel["trade_date"] == missing_date)
        )
    ]

    result = TechnicalPatternStudy.run(panel)
    later_dates = sorted(panel["trade_date"].unique())[63:68]
    affected = result.events[
        (result.events["ts_code"] == "000001.SZ")
        & (result.events["trade_date"].isin(later_dates))
    ]

    assert affected.empty

"""Validate generic relative time-range normalization."""

from datetime import date

from china_a_share.time_range import (
    resolve_explicit_time_range,
    resolve_future_horizon,
    resolve_relative_time_range,
)


def test_year_and_half_year_resolve_to_distinct_windows():
    end_date = date(2026, 7, 27)

    one_year = resolve_relative_time_range("A股过去1年连续涨停三天", end_date)
    half_year = resolve_relative_time_range("A股过去半年年连续涨停三天", end_date)

    assert one_year == (date(2025, 7, 27), end_date)
    assert half_year == (date(2026, 1, 27), end_date)
    assert one_year != half_year


def test_relative_window_parameters_are_not_fixed():
    end_date = date(2026, 7, 27)

    assert resolve_relative_time_range("最近3个月", end_date) == (
        date(2026, 4, 27),
        end_date,
    )
    assert resolve_relative_time_range("近两周", end_date) == (
        date(2026, 7, 13),
        end_date,
    )


def test_complex_event_study_keeps_event_window_and_outcome_horizon_separate():
    prompt = "A股20260101～20260601连续涨停三天的情况下，接下来一个月的上涨情况数据分析"

    assert resolve_explicit_time_range(prompt) == (
        date(2026, 1, 1),
        date(2026, 6, 1),
    )
    assert resolve_future_horizon(prompt) == (1, "month")


def test_date_formats_chinese_numbers_and_trading_sessions_are_structured():
    assert resolve_explicit_time_range("2026-01-01至2026-06-01") == (
        date(2026, 1, 1),
        date(2026, 6, 1),
    )
    assert resolve_explicit_time_range("2026年1月1日至2026年6月1日") == (
        date(2026, 1, 1),
        date(2026, 6, 1),
    )
    assert resolve_future_horizon("未来十二个月") == (12, "month")
    assert resolve_future_horizon("接下来二十个交易日") == (
        20,
        "trading_session",
    )


def test_trading_day_lookback_is_not_misrepresented_as_calendar_days():
    assert resolve_relative_time_range(
        "过去30个交易日",
        date(2026, 7, 27),
    ) is None

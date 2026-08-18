"""Validate generic relative time-range normalization."""

from datetime import date

import pytest

from china_a_share.time_range import (
    resolve_consecutive_session_count,
    resolve_explicit_time_range,
    resolve_future_horizon,
    resolve_relative_time_range,
)


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("连续两天涨停后第三天上涨的概率", 2),
        ("统计三连板事件未来一个月收益", 3),
        ("连续涨停四个交易日后未来两周", 4),
        ("两个交易日连板后下一天还涨的频率", 2),
        ("一周，明确按五个连续交易日计算", 5),
        ("查询最近一个月涨停股票", None),
    ],
)
def test_consecutive_session_counts_are_structured(prompt, expected):
    assert resolve_consecutive_session_count(prompt) == expected


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("连续两天涨停后第三天上涨的概率", (1, "trading_session")),
        ("过去一个月二连板第三日的平均收益", (1, "trading_session")),
        (
            "A股20260101～20260601连续涨停2天的情况下，第三天上涨、下跌的概率",
            (1, "trading_session"),
        ),
        ("两个交易日连板后下一天还涨的频率", (1, "trading_session")),
        ("三连板次日收益分布", (1, "trading_session")),
    ],
)
def test_sequence_outcome_phrases_resolve_to_a_trading_session_offset(
    prompt,
    expected,
):
    assert resolve_future_horizon(prompt) == expected


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


@pytest.mark.parametrize(
    "prompt",
    ["本月大宗交易", "This month block trades", "Month-to-date turnover"],
)
def test_relative_ranges_support_current_month_to_date(prompt):
    assert resolve_relative_time_range(prompt, date(2026, 8, 17)) == (
        date(2026, 8, 1),
        date(2026, 8, 17),
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


def test_explicit_calendar_month_expands_to_its_full_date_range():
    assert resolve_explicit_time_range("查看2025年2月的完整数据") == (
        date(2025, 2, 1),
        date(2025, 2, 28),
    )


def test_english_calendar_month_expands_to_its_full_date_range():
    assert resolve_explicit_time_range("Top September 2026 unlocks") == (
        date(2026, 9, 1),
        date(2026, 9, 30),
    )


@pytest.mark.parametrize("prompt", ["Q4 2026 unlocks", "2026 Q4 unlocks"])
def test_explicit_quarter_expands_to_its_full_date_range(prompt):
    assert resolve_explicit_time_range(prompt) == (
        date(2026, 10, 1),
        date(2026, 12, 31),
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "2026\u5e74\u7b2c\u4e8c\u5b63\u5ea6\u5ba3\u5e03\u56de\u8d2d\u7684\u516c\u53f8",
        "2026\u5e742\u5b63\u5ea6\u5ba3\u5e03\u56de\u8d2d\u7684\u516c\u53f8",
    ],
)
def test_chinese_explicit_quarter_expands_to_its_full_date_range(prompt):
    assert resolve_explicit_time_range(prompt) == (
        date(2026, 4, 1),
        date(2026, 6, 30),
    )


def test_quarter_without_year_uses_reference_year():
    assert resolve_explicit_time_range("Q4 有多少家公司解禁", date(2026, 8, 17)) == (
        date(2026, 10, 1),
        date(2026, 12, 31),
    )


def test_partial_month_uses_most_recent_started_calendar_month():
    assert resolve_explicit_time_range(
        "大A在6月上涨最多的股票前十",
        date(2026, 8, 10),
    ) == (date(2026, 6, 1), date(2026, 6, 30))
    assert resolve_explicit_time_range(
        "查看11月份的数据",
        date(2026, 2, 10),
    ) == (date(2025, 11, 1), date(2025, 11, 30))


def test_partial_current_month_stops_at_reference_date():
    assert resolve_explicit_time_range(
        "查看8月的数据",
        date(2026, 8, 10),
    ) == (date(2026, 8, 1), date(2026, 8, 10))


def test_trading_day_lookback_is_not_misrepresented_as_calendar_days():
    assert resolve_relative_time_range(
        "过去30个交易日",
        date(2026, 7, 27),
    ) is None

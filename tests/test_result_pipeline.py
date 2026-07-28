import pytest

from china_a_share.core.contracts import QueryResult, ResultPipeline
from china_a_share.result_pipeline import ResultPipelineExecutor


def source_result():
    return QueryResult(
        query_id="retail-proxy",
        provider="tushare",
        operation="top10_floatholders",
        status="success",
        columns=["ts_code", "end_date", "cr10_float_registered"],
        rows=[
            {"ts_code": "000001.SZ", "end_date": "20250331", "cr10_float_registered": 45.0},
            {"ts_code": "000001.SZ", "end_date": "20250630", "cr10_float_registered": 35.0},
            {"ts_code": "600000.SH", "end_date": "20250630", "cr10_float_registered": 55.0},
            {"ts_code": "300001.SZ", "end_date": "20250630", "cr10_float_registered": None},
        ],
        row_count=4,
    )


def test_result_pipeline_selects_latest_derives_sorts_and_limits():
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "retail-proxy",
            "output_query_id": "top-retail-proxy",
            "steps": [
                {"operation": "latest_by_group", "group_by": ["ts_code"], "order_by": "end_date"},
                {
                    "operation": "derive",
                    "field": "cr10_float_registered",
                    "output_field": "non_top10_float_ratio",
                    "arithmetic_operator": "constant_minus",
                    "value": 100,
                },
                {"operation": "drop_missing", "fields": ["non_top10_float_ratio"]},
                {"operation": "sort", "field": "non_top10_float_ratio", "direction": "desc"},
                {"operation": "limit", "count": 2},
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source_result())

    assert [row["ts_code"] for row in result.rows] == ["000001.SZ", "600000.SH"]
    assert [row["non_top10_float_ratio"] for row in result.rows] == [65.0, 45.0]
    assert result.summary["source_row_count"] == 4


def test_result_pipeline_filters_by_quantile():
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "retail-proxy",
            "output_query_id": "top-half",
            "steps": [
                {
                    "operation": "derive",
                    "field": "cr10_float_registered",
                    "output_field": "non_top10_float_ratio",
                    "arithmetic_operator": "constant_minus",
                    "value": 100,
                },
                {"operation": "drop_missing", "fields": ["non_top10_float_ratio"]},
                {
                    "operation": "quantile_filter",
                    "field": "non_top10_float_ratio",
                    "quantile": 0.5,
                    "comparison": "ge",
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source_result())

    assert {row["ts_code"] for row in result.rows} == {"000001.SZ"}


def test_result_pipeline_fails_fast_when_a_field_is_missing():
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "retail-proxy",
            "output_query_id": "invalid",
            "steps": [{"operation": "sort", "field": "missing"}],
        }
    )

    with pytest.raises(ValueError, match="sort fields are missing: missing"):
        ResultPipelineExecutor().execute(pipeline, source_result())


def test_result_pipeline_contract_rejects_nonnumeric_derive_scalar():
    with pytest.raises(ValueError, match="derive requires a numeric scalar"):
        ResultPipeline.model_validate(
            {
                "source_query_id": "source",
                "output_query_id": "derived",
                "steps": [
                    {
                        "operation": "derive",
                        "field": "ratio",
                        "output_field": "adjusted_ratio",
                        "arithmetic_operator": "multiply",
                        "value": "",
                    }
                ],
            }
        )


def test_result_pipeline_composes_windowed_event_probability():
    source = QueryResult(
        query_id="daily-prices",
        provider="tushare",
        operation="daily",
        status="success",
        columns=["ts_code", "trade_date", "close", "pct_chg"],
        rows=[
            {
                "ts_code": "000001.SZ",
                "trade_date": f"2026010{index}",
                "close": close,
                "pct_chg": pct_chg,
            }
            for index, (close, pct_chg) in enumerate(
                [
                    (10.0, 0.0),
                    (10.0, 0.0),
                    (10.0, 0.0),
                    (8.0, -20.0),
                    (9.0, 12.5),
                ],
                start=1,
            )
        ],
        row_count=5,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "daily-prices",
            "output_query_id": "event-probability",
            "steps": [
                {
                    "operation": "rolling_mean",
                    "field": "close",
                    "output_field": "moving_average",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "window": 3,
                },
                {
                    "operation": "shift",
                    "field": "close",
                    "output_field": "previous_close",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "periods": 1,
                },
                {
                    "operation": "shift",
                    "field": "moving_average",
                    "output_field": "previous_moving_average",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "periods": 1,
                },
                {
                    "operation": "shift",
                    "field": "pct_chg",
                    "output_field": "next_pct_chg",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "periods": -1,
                },
                {
                    "operation": "compare_fields",
                    "field": "close",
                    "right_field": "moving_average",
                    "output_field": "below_average",
                    "comparison": "lt",
                },
                {
                    "operation": "compare_fields",
                    "field": "previous_close",
                    "right_field": "previous_moving_average",
                    "output_field": "previously_above",
                    "comparison": "ge",
                },
                {
                    "operation": "compare_scalar",
                    "field": "next_pct_chg",
                    "output_field": "next_day_up",
                    "comparison": "gt",
                    "value": 0,
                },
                {
                    "operation": "filter",
                    "field": "below_average",
                    "comparison": "eq",
                    "value": 1,
                },
                {
                    "operation": "filter",
                    "field": "previously_above",
                    "comparison": "eq",
                    "value": 1,
                },
                {
                    "operation": "filter",
                    "field": "trade_date",
                    "comparison": "ge",
                    "value": "20260104",
                },
                {"operation": "drop_missing", "fields": ["next_pct_chg"]},
                {
                    "operation": "summarize",
                    "aggregations": [
                        {
                            "output_field": "event_count",
                            "field": "next_day_up",
                            "function": "count",
                        },
                        {
                            "output_field": "rise_probability",
                            "field": "next_day_up",
                            "function": "mean",
                        },
                    ],
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.rows == [{"event_count": 1, "rise_probability": 1.0}]


def test_result_pipeline_rejects_invalid_rolling_minimum():
    with pytest.raises(ValueError, match="min_periods cannot exceed"):
        ResultPipeline.model_validate(
            {
                "source_query_id": "source",
                "output_query_id": "invalid",
                "steps": [
                    {
                        "operation": "rolling_mean",
                        "field": "close",
                        "output_field": "ma",
                        "group_by": ["ts_code"],
                        "order_by": "trade_date",
                        "window": 3,
                        "min_periods": 4,
                    }
                ],
            }
        )


def test_shift_can_require_the_next_global_order_value():
    source = QueryResult(
        query_id="daily",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "A", "trade_date": "20260101", "pct_chg": 1.0},
            {"ts_code": "A", "trade_date": "20260103", "pct_chg": 3.0},
            {"ts_code": "B", "trade_date": "20260101", "pct_chg": 1.0},
            {"ts_code": "B", "trade_date": "20260102", "pct_chg": 2.0},
            {"ts_code": "B", "trade_date": "20260103", "pct_chg": 3.0},
        ],
        row_count=5,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "daily",
            "output_query_id": "next-session",
            "steps": [
                {
                    "operation": "shift",
                    "field": "pct_chg",
                    "output_field": "next_pct_chg",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "periods": -1,
                    "require_consecutive": True,
                }
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    first_a = next(
        row
        for row in result.rows
        if row["ts_code"] == "A" and row["trade_date"] == "20260101"
    )
    first_b = next(
        row
        for row in result.rows
        if row["ts_code"] == "B" and row["trade_date"] == "20260101"
    )
    assert first_a["next_pct_chg"] is None
    assert first_b["next_pct_chg"] == 2.0


def test_pipeline_matches_another_source_and_summarizes_a_parameterized_streak():
    daily = QueryResult(
        query_id="daily",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {
                "ts_code": "A",
                "trade_date": f"2026010{day}",
                "pct_chg": 2.0 if day == 4 else 10.0,
            }
            for day in range(1, 5)
        ],
        row_count=4,
    )
    limit_ups = QueryResult(
        query_id="limit-ups",
        provider="tushare",
        operation="limit_list_d",
        status="success",
        rows=[
            {"ts_code": "A", "trade_date": f"2026010{day}"}
            for day in range(1, 4)
        ],
        row_count=3,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "daily",
            "output_query_id": "event-study",
            "steps": [
                {
                    "operation": "match_source",
                    "right_source_query_id": "limit-ups",
                    "join_on": ["trade_date", "ts_code"],
                    "output_field": "is_limit_up",
                },
                {
                    "operation": "rolling_sum",
                    "field": "is_limit_up",
                    "output_field": "streak_count",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "window": 3,
                },
                {
                    "operation": "shift",
                    "field": "pct_chg",
                    "output_field": "next_pct_chg",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "periods": -1,
                    "require_consecutive": True,
                },
                {
                    "operation": "compare_scalar",
                    "field": "streak_count",
                    "output_field": "is_streak",
                    "comparison": "eq",
                    "value": 3,
                },
                {
                    "operation": "compare_scalar",
                    "field": "next_pct_chg",
                    "output_field": "next_up",
                    "comparison": "gt",
                    "value": 0,
                },
                {
                    "operation": "derive",
                    "field": "next_up",
                    "output_field": "next_up_pct",
                    "arithmetic_operator": "multiply",
                    "value": 100,
                },
                {
                    "operation": "filter",
                    "field": "is_streak",
                    "comparison": "eq",
                    "value": 1,
                },
                {"operation": "drop_missing", "fields": ["next_pct_chg"]},
                {
                    "operation": "summarize",
                    "aggregations": [
                        {
                            "output_field": "event_count",
                            "label": "Event count",
                            "field": "next_up",
                            "function": "count",
                        },
                        {
                            "output_field": "probability_pct",
                            "label": "Probability (%)",
                            "field": "next_up_pct",
                            "function": "mean",
                        },
                    ],
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(
        pipeline,
        daily,
        {"daily": daily, "limit-ups": limit_ups},
    )

    assert result.rows == [{"event_count": 1, "probability_pct": 100.0}]
    assert result.summary == {
        "Event count": 1,
        "Probability (%)": 100.0,
    }


def test_pipeline_matches_the_next_available_observation_after_calendar_offset():
    source = QueryResult(
        query_id="daily",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "A", "trade_date": "20260105", "close": 10.0},
            {"ts_code": "A", "trade_date": "20260205", "close": 12.0},
            {"ts_code": "B", "trade_date": "20260105", "close": 20.0},
            {"ts_code": "B", "trade_date": "20260206", "close": 18.0},
        ],
        row_count=4,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "daily",
            "output_query_id": "forward-values",
            "steps": [
                {
                    "operation": "match_at_offset",
                    "field": "close",
                    "output_field": "one_month_close",
                    "matched_date_output_field": "one_month_trade_date",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "offset_value": 1,
                    "offset_unit": "month",
                }
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.rows[0]["one_month_close"] == 12.0
    assert result.rows[2]["one_month_close"] is None
    assert result.rows[0]["one_month_trade_date"] == "20260205"
    assert result.rows[2]["one_month_trade_date"] is None


def test_pipeline_matches_a_global_trading_session_offset():
    source = QueryResult(
        query_id="daily",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": code, "trade_date": trade_date, "close": close}
            for code, values in {
                "A": [("20260105", 10.0), ("20260106", 11.0)],
                "B": [("20260105", 20.0)],
            }.items()
            for trade_date, close in values
        ],
        row_count=3,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "daily",
            "output_query_id": "next-session",
            "steps": [
                {
                    "operation": "match_at_offset",
                    "field": "close",
                    "output_field": "next_close",
                    "matched_date_output_field": "next_trade_date",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "offset_value": 1,
                    "offset_unit": "trading_session",
                }
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.rows[0]["next_close"] == 11.0
    assert result.rows[0]["next_trade_date"] == "20260106"
    assert result.rows[2]["next_close"] is None

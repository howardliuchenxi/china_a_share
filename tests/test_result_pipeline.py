import logging

import pytest

from china_a_share.core.contracts import QueryResult, ResultPipeline
from china_a_share.result_pipeline import ResultPipelineExecutor, ResultValidationError
from china_a_share.observability import ANALYSIS_REQUEST_ID


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


def test_pipeline_logs_request_scoped_step_row_counts(caplog):
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "retail-proxy",
            "output_query_id": "limited",
            "steps": [
                {"operation": "drop_missing", "fields": ["cr10_float_registered"]},
                {"operation": "limit", "count": 2},
            ],
        }
    )
    context_token = ANALYSIS_REQUEST_ID.set("trace-123")
    try:
        with caplog.at_level(logging.INFO):
            ResultPipelineExecutor().execute(pipeline, source_result())
    finally:
        ANALYSIS_REQUEST_ID.reset(context_token)

    events = [
        getattr(record, "structured_fields", {})
        for record in caplog.records
        if getattr(record, "structured_fields", {}).get("event")
        == "result_pipeline_step_completed"
    ]
    assert [event["request_id"] for event in events] == ["trace-123", "trace-123"]
    assert events[0]["input_row_count"] == 4
    assert events[0]["output_row_count"] == 3
    assert events[0]["eliminated_row_count"] == 1


def test_pipeline_preserves_complete_source_retrieval_evidence():
    source = source_result().model_copy(
        update={
            "completeness": "complete",
            "completeness_evidence": ["query_shape=security"],
            "retrieval_partition_count": 3,
        }
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": source.query_id,
            "output_query_id": "limited",
            "steps": [{"operation": "limit", "count": 2}],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.completeness == "complete"
    assert result.completeness_evidence == ["query_shape=security"]
    assert result.retrieval_partition_count == 3


def market_direction_source():
    """Return one source whose directional categories form a complete partition."""
    return QueryResult(
        query_id="market-direction",
        provider="test",
        operation="daily",
        status="success",
        columns=["change"],
        rows=[
            {"change": 2.0},
            {"change": -1.0},
            {"change": 0.0},
            {"change": None},
        ],
        row_count=4,
    )


def market_direction_pipeline():
    """Return a complete conditional-count pipeline for reconciliation tests."""
    return ResultPipeline.model_validate(
        {
            "source_query_id": "market-direction",
            "output_query_id": "market-direction-summary",
            "steps": [
                {
                    "operation": "compare_scalar",
                    "field": "change",
                    "output_field": "is_positive",
                    "comparison": "gt",
                    "value": 0,
                },
                {
                    "operation": "compare_scalar",
                    "field": "change",
                    "output_field": "is_negative",
                    "comparison": "lt",
                    "value": 0,
                },
                {
                    "operation": "compare_scalar",
                    "field": "change",
                    "output_field": "is_flat",
                    "comparison": "eq",
                    "value": 0,
                },
                {
                    "operation": "summarize",
                    "aggregations": [
                        {
                            "output_field": "positive_count",
                            "field": "is_positive",
                            "function": "sum",
                        },
                        {
                            "output_field": "negative_count",
                            "field": "is_negative",
                            "function": "sum",
                        },
                        {
                            "output_field": "flat_count",
                            "field": "is_flat",
                            "function": "sum",
                        },
                    ],
                },
            ],
        }
    )


def test_pipeline_reconciles_conditional_summary_independently():
    result = ResultPipelineExecutor().execute(
        market_direction_pipeline(), market_direction_source()
    )

    assert result.rows == [
        {"positive_count": 1, "negative_count": 1, "flat_count": 1}
    ]


def test_pipeline_rejects_summary_that_disagrees_with_independent_recalculation():
    class CorruptSummaryExecutor(ResultPipelineExecutor):
        def _execute_step(self, frame, step, sources):
            result = super()._execute_step(frame, step, sources)
            if step.operation == "summarize":
                result.loc[0, "positive_count"] += 1
            return result

    with pytest.raises(ResultValidationError, match="Result reconciliation failed"):
        CorruptSummaryExecutor().execute(
            market_direction_pipeline(), market_direction_source()
        )


def test_pipeline_rejects_condition_that_disagrees_with_source_values():
    class CorruptComparisonExecutor(ResultPipelineExecutor):
        def _execute_step(self, frame, step, sources):
            result = super()._execute_step(frame, step, sources)
            if (
                step.operation == "compare_scalar"
                and step.output_field == "is_positive"
            ):
                result[step.output_field] = True
            return result

    with pytest.raises(
        ResultValidationError,
        match="conditional field 'is_positive'",
    ):
        CorruptComparisonExecutor().execute(
            market_direction_pipeline(), market_direction_source()
        )


def test_pipeline_applies_projection_rename_distinct_and_predicate_operators():
    source = QueryResult(
        query_id="source",
        provider="test",
        operation="source",
        status="success",
        rows=[
            {"code": "A", "score": 10.0, "sector": "Tech", "note": None},
            {"code": "A", "score": 10.0, "sector": "Tech", "note": None},
            {"code": "B", "score": 20.0, "sector": "Bank", "note": "ok"},
            {"code": "C", "score": 30.0, "sector": "Tech", "note": "ok"},
        ],
        row_count=4,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "source",
            "output_query_id": "filtered",
            "steps": [
                {"operation": "distinct", "fields": ["code"]},
                {
                    "operation": "filter_set",
                    "field": "sector",
                    "values": ["Tech"],
                },
                {
                    "operation": "filter_range",
                    "field": "score",
                    "lower_value": 5,
                    "upper_value": 25,
                },
                {"operation": "filter_null", "field": "note"},
                {"operation": "rename_fields", "fields": {"code": "ts_code"}},
                {
                    "operation": "select_fields",
                    "fields": ["ts_code", "score"],
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.rows == [{"ts_code": "A", "score": 10.0}]


@pytest.mark.parametrize(
    ("operation", "expected_codes"),
    [
        ("semi_join", ["B", "C"]),
        ("anti_join", ["A"]),
    ],
)
def test_pipeline_applies_key_set_joins(operation, expected_codes):
    source = QueryResult(
        query_id="left",
        provider="test",
        operation="source",
        status="success",
        rows=[{"code": code, "value": index} for index, code in enumerate("ABC")],
        row_count=3,
    )
    right = QueryResult(
        query_id="right",
        provider="test",
        operation="source",
        status="success",
        rows=[{"code": "B"}, {"code": "C"}, {"code": "C"}],
        row_count=3,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "left",
            "output_query_id": "joined",
            "steps": [
                {
                    "operation": operation,
                    "right_source_query_id": "right",
                    "join_on": ["code"],
                }
            ],
        }
    )

    result = ResultPipelineExecutor().execute(
        pipeline,
        source,
        {"right": right},
    )

    assert [row["code"] for row in result.rows] == expected_codes


def test_pipeline_applies_inner_join_and_union_all():
    left = QueryResult(
        query_id="left",
        provider="test",
        operation="source",
        status="success",
        rows=[{"code": "A", "value": 1}, {"code": "B", "value": 2}],
        row_count=2,
    )
    enrichment = QueryResult(
        query_id="enrichment",
        provider="test",
        operation="source",
        status="success",
        rows=[{"code": "B", "label": "selected"}],
        row_count=1,
    )
    appended = QueryResult(
        query_id="appended",
        provider="test",
        operation="source",
        status="success",
        rows=[{"code": "C", "value": 3, "tag": "new"}],
        row_count=1,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "left",
            "output_query_id": "combined",
            "steps": [
                {
                    "operation": "inner_join",
                    "right_source_query_id": "enrichment",
                    "join_on": ["code"],
                    "fields": {"label": "tag"},
                    "cardinality": "many_to_one",
                },
                {
                    "operation": "union_all",
                    "right_source_query_id": "appended",
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(
        pipeline,
        left,
        {"enrichment": enrichment, "appended": appended},
    )

    assert result.rows == [
        {"code": "B", "value": 2, "tag": "selected"},
        {"code": "C", "value": 3, "tag": "new"},
    ]


def test_pipeline_applies_extended_aggregations_and_having():
    source = QueryResult(
        query_id="source",
        provider="test",
        operation="source",
        status="success",
        rows=[
            {"sector": "A", "code": "X", "value": 1.0},
            {"sector": "A", "code": "X", "value": 3.0},
            {"sector": "A", "code": "Y", "value": 5.0},
            {"sector": "B", "code": "Z", "value": 2.0},
        ],
        row_count=4,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "source",
            "output_query_id": "aggregated",
            "steps": [
                {
                    "operation": "aggregate",
                    "group_by": ["sector"],
                    "aggregations": [
                        {
                            "output_field": "security_count",
                            "field": "code",
                            "function": "count_distinct",
                        },
                        {
                            "output_field": "median_value",
                            "field": "value",
                            "function": "median",
                        },
                        {
                            "output_field": "upper_quartile",
                            "field": "value",
                            "function": "quantile",
                            "quantile": 0.75,
                        },
                        {
                            "output_field": "first_value",
                            "field": "value",
                            "function": "first",
                        },
                        {
                            "output_field": "last_value",
                            "field": "value",
                            "function": "last",
                        },
                    ],
                },
                {
                    "operation": "having",
                    "field": "security_count",
                    "comparison": "ge",
                    "value": 2,
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.rows == [
        {
            "sector": "A",
            "security_count": 2,
            "median_value": 3.0,
            "upper_quartile": 4.0,
            "first_value": 1.0,
            "last_value": 5.0,
        }
    ]


def test_pipeline_applies_group_ranking_and_top_k():
    source = QueryResult(
        query_id="source",
        provider="test",
        operation="source",
        status="success",
        rows=[
            {"sector": "A", "code": "X", "value": 10.0},
            {"sector": "A", "code": "Y", "value": 10.0},
            {"sector": "A", "code": "Z", "value": 5.0},
            {"sector": "B", "code": "Q", "value": 7.0},
            {"sector": "B", "code": "R", "value": 3.0},
        ],
        row_count=5,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "source",
            "output_query_id": "ranked",
            "steps": [
                {
                    "operation": "rank",
                    "field": "value",
                    "output_field": "rank_value",
                    "group_by": ["sector"],
                    "direction": "desc",
                    "rank_method": "min",
                },
                {
                    "operation": "dense_rank",
                    "field": "value",
                    "output_field": "dense_rank_value",
                    "group_by": ["sector"],
                    "direction": "desc",
                },
                {
                    "operation": "top_k_by_group",
                    "field": "value",
                    "group_by": ["sector"],
                    "direction": "desc",
                    "count": 2,
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert [(row["sector"], row["code"]) for row in result.rows] == [
        ("A", "X"),
        ("A", "Y"),
        ("B", "Q"),
        ("B", "R"),
    ]
    assert [row["rank_value"] for row in result.rows[:2]] == [1.0, 1.0]
    assert [row["dense_rank_value"] for row in result.rows[:2]] == [1.0, 1.0]


def test_pipeline_applies_differences_growth_and_rolling_statistics():
    source = QueryResult(
        query_id="source",
        provider="test",
        operation="source",
        status="success",
        rows=[
            {"code": "A", "date": "20260101", "value": 10.0},
            {"code": "A", "date": "20260102", "value": 12.0},
            {"code": "A", "date": "20260103", "value": 18.0},
        ],
        row_count=3,
    )
    steps = [
        {
            "operation": operation,
            "field": "value",
            "output_field": output_field,
            "group_by": ["code"],
            "order_by": "date",
            **arguments,
        }
        for operation, output_field, arguments in [
            ("diff", "value_diff", {"periods": 1}),
            ("pct_change", "value_growth", {"periods": 1}),
            ("rolling_min", "rolling_minimum", {"window": 2}),
            ("rolling_max", "rolling_maximum", {"window": 2}),
            ("rolling_std", "rolling_deviation", {"window": 2}),
        ]
    ]
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "source",
            "output_query_id": "windowed",
            "steps": steps,
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.rows[-1]["value_diff"] == 6.0
    assert result.rows[-1]["value_growth"] == 0.5
    assert result.rows[-1]["rolling_minimum"] == 12.0
    assert result.rows[-1]["rolling_maximum"] == 18.0
    assert result.rows[-1]["rolling_deviation"] == pytest.approx(4.242640687)


def test_pipeline_applies_ordered_cumulative_and_row_number_operations():
    source = QueryResult(
        query_id="source",
        provider="test",
        operation="source",
        status="success",
        rows=[
            {"group": "B", "date": 2, "value": 4.0},
            {"group": "A", "date": 2, "value": 3.0},
            {"group": "A", "date": 1, "value": 1.0},
        ],
        row_count=3,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "source",
            "output_query_id": "ordered",
            "steps": [
                {
                    "operation": "row_number",
                    "output_field": "sequence",
                    "group_by": ["group"],
                    "order_by": "date",
                },
                {
                    "operation": "cumulative_sum",
                    "field": "value",
                    "output_field": "running_total",
                    "group_by": ["group"],
                    "order_by": "date",
                },
                {
                    "operation": "expanding_mean",
                    "field": "value",
                    "output_field": "running_mean",
                    "group_by": ["group"],
                    "order_by": "date",
                    "min_periods": 1,
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert [(row["group"], row["date"]) for row in result.rows] == [
        ("A", 1),
        ("A", 2),
        ("B", 2),
    ]
    assert [row["sequence"] for row in result.rows] == [1, 2, 1]
    assert [row["running_total"] for row in result.rows] == [1.0, 4.0, 4.0]
    assert [row["running_mean"] for row in result.rows] == [1.0, 2.0, 4.0]


def test_pipeline_applies_null_bounding_and_conditional_value_operations():
    source = QueryResult(
        query_id="source",
        provider="test",
        operation="source",
        status="success",
        rows=[
            {"primary": None, "fallback": 12.0, "score": -2.0},
            {"primary": 8.0, "fallback": 20.0, "score": 15.0},
        ],
        row_count=2,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "source",
            "output_query_id": "derived",
            "steps": [
                {
                    "operation": "coalesce",
                    "fields": ["primary", "fallback"],
                    "output_field": "selected",
                },
                {
                    "operation": "fill_constant",
                    "field": "primary",
                    "output_field": "filled",
                    "value": 0,
                },
                {
                    "operation": "clip",
                    "field": "score",
                    "output_field": "bounded",
                    "lower_value": 0,
                    "upper_value": 10,
                },
                {
                    "operation": "conditional_value",
                    "field": "score",
                    "output_field": "bucket",
                    "comparison": "ge",
                    "value": 10,
                    "true_value": "high",
                    "false_value": "normal",
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.rows == [
        {
            "primary": None,
            "fallback": 12.0,
            "score": -2.0,
            "selected": 12.0,
            "filled": 0.0,
            "bounded": 0.0,
            "bucket": "normal",
        },
        {
            "primary": 8.0,
            "fallback": 20.0,
            "score": 15.0,
            "selected": 8.0,
            "filled": 8.0,
            "bounded": 10.0,
            "bucket": "high",
        },
    ]


def test_pipeline_applies_key_sets_and_bounded_asof_join():
    left = QueryResult(
        query_id="left",
        provider="test",
        operation="source",
        status="success",
        rows=[
            {"code": "A", "time": 3, "value": 1},
            {"code": "B", "time": 5, "value": 2},
            {"code": "C", "time": 5, "value": 3},
        ],
        row_count=3,
    )
    membership = QueryResult(
        query_id="membership",
        provider="test",
        operation="source",
        status="success",
        rows=[{"code": "A"}, {"code": "B"}, {"code": "B"}],
        row_count=3,
    )
    history = QueryResult(
        query_id="history",
        provider="test",
        operation="source",
        status="success",
        rows=[
            {"code": "A", "effective_time": 1, "label": "old"},
            {"code": "A", "effective_time": 2, "label": "current"},
            {"code": "B", "effective_time": 1, "label": "expired"},
        ],
        row_count=3,
    )
    intersect = ResultPipeline.model_validate(
        {
            "source_query_id": "left",
            "output_query_id": "intersection",
            "steps": [
                {
                    "operation": "intersect_keys",
                    "right_source_query_id": "membership",
                    "join_on": ["code"],
                }
            ],
        }
    )
    difference = ResultPipeline.model_validate(
        {
            "source_query_id": "left",
            "output_query_id": "difference",
            "steps": [
                {
                    "operation": "except_keys",
                    "right_source_query_id": "membership",
                    "join_on": ["code"],
                }
            ],
        }
    )
    asof = ResultPipeline.model_validate(
        {
            "source_query_id": "left",
            "output_query_id": "matched",
            "steps": [
                {
                    "operation": "asof_join",
                    "right_source_query_id": "history",
                    "group_by": ["code"],
                    "order_by": "time",
                    "right_order_by": "effective_time",
                    "fields": {"label": "asof_label"},
                    "asof_direction": "backward",
                    "tolerance": 2,
                }
            ],
        }
    )
    sources = {"membership": membership, "history": history}

    intersect_result = ResultPipelineExecutor().execute(intersect, left, sources)
    difference_result = ResultPipelineExecutor().execute(difference, left, sources)
    asof_result = ResultPipelineExecutor().execute(asof, left, sources)

    assert intersect_result.rows == [{"code": "A"}, {"code": "B"}]
    assert difference_result.rows == [{"code": "C"}]
    assert "effective_time" not in asof_result.columns
    assert [row["asof_label"] for row in asof_result.rows] == [
        "current",
        None,
        None,
    ]


def test_pipeline_contract_rejects_unbounded_asof_and_invalid_clip():
    with pytest.raises(ValueError, match="Missing required arguments for asof_join"):
        ResultPipeline.model_validate(
            {
                "source_query_id": "source",
                "output_query_id": "invalid",
                "steps": [
                    {
                        "operation": "asof_join",
                        "right_source_query_id": "history",
                        "group_by": ["code"],
                        "order_by": "time",
                        "right_order_by": "effective_time",
                        "fields": {"value": "historical_value"},
                    }
                ],
            }
        )

    with pytest.raises(ValueError, match="clip lower_value cannot exceed"):
        ResultPipeline.model_validate(
            {
                "source_query_id": "source",
                "output_query_id": "invalid",
                "steps": [
                    {
                        "operation": "clip",
                        "field": "value",
                        "output_field": "bounded",
                        "lower_value": 2,
                        "upper_value": 1,
                    }
                ],
            }
        )


def test_pipeline_applies_group_transform_and_normalization():
    source = QueryResult(
        query_id="source",
        provider="test",
        operation="source",
        status="success",
        rows=[
            {"sector": "Tech", "value": 10.0},
            {"sector": "Tech", "value": 20.0},
            {"sector": "Bank", "value": 5.0},
        ],
        row_count=3,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "source",
            "output_query_id": "normalized",
            "steps": [
                {
                    "operation": "group_transform",
                    "field": "value",
                    "output_field": "sector_mean",
                    "group_by": ["sector"],
                    "transform_function": "mean",
                },
                {
                    "operation": "normalize",
                    "field": "value",
                    "output_field": "sector_percentile",
                    "group_by": ["sector"],
                    "normalization": "percentile",
                },
                {
                    "operation": "normalize",
                    "field": "value",
                    "output_field": "global_minmax",
                    "normalization": "minmax",
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert [row["sector_mean"] for row in result.rows] == [15.0, 15.0, 5.0]
    assert [row["sector_percentile"] for row in result.rows] == [0.5, 1.0, 1.0]
    assert [row["global_minmax"] for row in result.rows] == pytest.approx(
        [1 / 3, 1.0, 0.0]
    )


def test_pipeline_applies_weighted_mean_with_strict_weight_contract():
    source = QueryResult(
        query_id="source",
        provider="test",
        operation="source",
        status="success",
        rows=[
            {"sector": "A", "return": 0.1, "market_cap": 1.0},
            {"sector": "A", "return": 0.2, "market_cap": 3.0},
            {"sector": "B", "return": -0.1, "market_cap": 2.0},
        ],
        row_count=3,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "source",
            "output_query_id": "weighted",
            "steps": [
                {
                    "operation": "weighted_mean",
                    "field": "return",
                    "weight_field": "market_cap",
                    "output_field": "weighted_return",
                    "group_by": ["sector"],
                }
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.rows == [
        {"sector": "A", "weighted_return": pytest.approx(0.175)},
        {"sector": "B", "weighted_return": pytest.approx(-0.1)},
    ]

    invalid = source.model_copy(
        update={
            "rows": [{"sector": "A", "return": 0.1, "market_cap": -1.0}],
            "row_count": 1,
        }
    )
    with pytest.raises(ValueError, match="weights must be non-negative"):
        ResultPipelineExecutor().execute(pipeline, invalid)

    missing_weights = source.model_copy(
        update={
            "rows": [{"sector": "A", "return": 0.1, "market_cap": None}],
            "row_count": 1,
        }
    )
    with pytest.raises(ValueError, match="positive total weight per group"):
        ResultPipelineExecutor().execute(pipeline, missing_weights)


def test_pipeline_applies_advanced_rolling_statistics():
    source = QueryResult(
        query_id="source",
        provider="test",
        operation="source",
        status="success",
        rows=[
            {"code": "A", "date": 1, "left": 1.0, "right": 2.0},
            {"code": "A", "date": 2, "left": 2.0, "right": 4.0},
            {"code": "A", "date": 3, "left": 3.0, "right": 6.0},
        ],
        row_count=3,
    )
    common = {
        "field": "left",
        "group_by": ["code"],
        "order_by": "date",
        "window": 3,
    }
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "source",
            "output_query_id": "rolling",
            "steps": [
                {
                    "operation": "rolling_quantile",
                    "output_field": "rolling_median",
                    "quantile": 0.5,
                    **common,
                },
                {
                    "operation": "rolling_correlation",
                    "right_field": "right",
                    "output_field": "rolling_corr",
                    **common,
                },
                {
                    "operation": "rolling_covariance",
                    "right_field": "right",
                    "output_field": "rolling_cov",
                    **common,
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.rows[-1]["rolling_median"] == 2.0
    assert result.rows[-1]["rolling_corr"] == pytest.approx(1.0)
    assert result.rows[-1]["rolling_cov"] == pytest.approx(2.0)


def test_pipeline_resamples_grouped_calendar_periods():
    source = QueryResult(
        query_id="source",
        provider="test",
        operation="source",
        status="success",
        rows=[
            {"code": "A", "date": "20260102", "return": 0.1},
            {"code": "A", "date": "20260120", "return": 0.2},
            {"code": "A", "date": "20260202", "return": -0.1},
        ],
        row_count=3,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "source",
            "output_query_id": "monthly",
            "steps": [
                {
                    "operation": "resample",
                    "group_by": ["code"],
                    "order_by": "date",
                    "frequency": "month",
                    "aggregations": [
                        {
                            "output_field": "mean_return",
                            "field": "return",
                            "function": "mean",
                        }
                    ],
                }
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.rows == [
        {"code": "A", "date": "20260131", "mean_return": pytest.approx(0.15)},
        {"code": "A", "date": "20260228", "mean_return": pytest.approx(-0.1)},
    ]

def test_pipeline_contract_rejects_invalid_range_and_quantile_arguments():
    with pytest.raises(ValueError, match="lower_value cannot exceed"):
        ResultPipeline.model_validate(
            {
                "source_query_id": "source",
                "output_query_id": "invalid",
                "steps": [
                    {
                        "operation": "filter_range",
                        "field": "value",
                        "lower_value": 2,
                        "upper_value": 1,
                    }
                ],
            }
        )

    with pytest.raises(ValueError, match="requires exactly one quantile"):
        ResultPipeline.model_validate(
            {
                "source_query_id": "source",
                "output_query_id": "invalid",
                "steps": [
                    {
                        "operation": "summarize",
                        "aggregations": [
                            {
                                "output_field": "invalid_quantile",
                                "field": "value",
                                "function": "quantile",
                            }
                        ],
                    }
                ],
            }
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


def test_pipeline_does_not_require_an_earlier_sort_after_group_selection():
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "retail-proxy",
            "output_query_id": "latest-by-security",
            "steps": [
                {"operation": "sort", "field": "end_date", "direction": "asc"},
                {
                    "operation": "latest_by_group",
                    "group_by": ["ts_code"],
                    "order_by": "end_date",
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source_result())

    assert result.row_count == 3


def test_time_series_sort_and_limit_allows_repeated_security_identifiers():
    source = QueryResult(
        query_id="prices",
        provider="tushare",
        operation="daily",
        status="success",
        columns=["ts_code", "trade_date", "close"],
        rows=[
            {"ts_code": "000001.SZ", "trade_date": "20260806", "close": 10.0},
            {"ts_code": "000001.SZ", "trade_date": "20260807", "close": 10.2},
        ],
        row_count=2,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "prices",
            "output_query_id": "recent-prices",
            "steps": [
                {"operation": "sort", "field": "trade_date", "direction": "asc"},
                {"operation": "limit", "count": 20},
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.row_count == 2


@pytest.mark.parametrize(
    ("comparison", "expected_codes"),
    [
        ("contains", ["000001.SZ"]),
        ("not_contains", ["600000.SH"]),
    ],
)
def test_pipeline_filters_string_substrings(comparison, expected_codes):
    source = QueryResult(
        query_id="universe",
        provider="tushare",
        operation="stock_basic",
        status="success",
        columns=["ts_code", "industry"],
        rows=[
            {"ts_code": "000001.SZ", "industry": "汽车零部件"},
            {"ts_code": "600000.SH", "industry": "银行"},
        ],
        row_count=2,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "universe",
            "output_query_id": "filtered",
            "steps": [
                {
                    "operation": "filter",
                    "field": "industry",
                    "comparison": comparison,
                    "value": "汽车",
                }
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert [row["ts_code"] for row in result.rows] == expected_codes


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


def test_result_pipeline_derives_a_return_from_two_fields():
    source = QueryResult(
        query_id="daily",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"close": 10.0, "one_month_close": 12.0},
            {"close": 20.0, "one_month_close": 18.0},
        ],
        row_count=2,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "daily",
            "output_query_id": "returns",
            "steps": [
                {
                    "operation": "derive",
                    "field": "one_month_close",
                    "right_field": "close",
                    "output_field": "price_ratio",
                    "arithmetic_operator": "divide",
                },
                {
                    "operation": "derive",
                    "field": "price_ratio",
                    "output_field": "return_pct",
                    "arithmetic_operator": "subtract",
                    "value": 1,
                },
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert [row["price_ratio"] for row in result.rows] == pytest.approx(
        [1.2, 0.9]
    )
    assert [row["return_pct"] for row in result.rows] == pytest.approx(
        [0.2, -0.1]
    )


def test_result_pipeline_rejects_same_field_division():
    with pytest.raises(
        ValueError,
        match="derive requires different left and right fields",
    ):
        ResultPipeline.model_validate(
            {
                "source_query_id": "daily",
                "output_query_id": "invalid-return",
                "steps": [
                    {
                        "operation": "derive",
                        "field": "close",
                        "right_field": "close",
                        "output_field": "return_ratio",
                        "arithmetic_operator": "divide",
                    }
                ],
            }
        )


def test_result_pipeline_rejects_matching_a_date_as_the_outcome_value():
    with pytest.raises(
        ValueError,
        match="match_at_offset value field must differ from order_by",
    ):
        ResultPipeline.model_validate(
            {
                "source_query_id": "daily",
                "output_query_id": "invalid-outcome",
                "steps": [
                    {
                        "operation": "match_at_offset",
                        "field": "trade_date",
                        "output_field": "future_trade_date",
                        "matched_date_output_field": "matched_trade_date",
                        "group_by": ["ts_code"],
                        "order_by": "trade_date",
                        "offset_value": 1,
                        "offset_unit": "month",
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
    event_metadata = result.summary_metadata["Event count"]
    assert event_metadata.output_field == "event_count"
    assert event_metadata.function == "count"
    assert event_metadata.formula == "count((shift(pct_chg, -1) > 0))"
    assert event_metadata.source_fields == ["pct_chg"]
    assert event_metadata.initial_sample_count == 4
    assert event_metadata.valid_sample_count == 1
    assert [step.operation for step in event_metadata.calculation_steps] == [
        step.operation for step in pipeline.steps[:-1]
    ]

    probability_metadata = result.summary_metadata["Probability (%)"]
    assert probability_metadata.output_field == "probability_pct"
    assert probability_metadata.function == "mean"
    assert probability_metadata.value_format == "percentage_points"
    assert probability_metadata.formula == (
        "mean(((shift(pct_chg, -1) > 0) * 100))"
    )
    assert probability_metadata.source_fields == ["pct_chg"]
    assert probability_metadata.valid_sample_count == 1
    assert result.column_metadata["probability_pct"].formula == (
        "mean(((shift(pct_chg, -1) > 0) * 100))"
    )
    assert result.column_metadata["probability_pct"].source_fields == [
        "pct_chg"
    ]


@pytest.mark.parametrize("streak_length", [3, 4, 5])
def test_pipeline_supports_any_consecutive_trading_session_window(
    streak_length,
):
    source = QueryResult(
        query_id="daily",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {
                "ts_code": "A",
                "trade_date": f"202601{session:02d}",
                "is_event": True,
            }
            for session in range(1, streak_length + 1)
        ],
        row_count=streak_length,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "daily",
            "output_query_id": "streaks",
            "steps": [
                {
                    "operation": "rolling_sum",
                    "field": "is_event",
                    "output_field": "streak_count",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "window": streak_length,
                    "min_periods": streak_length,
                    "require_consecutive": True,
                }
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    assert result.rows[-1]["streak_count"] == streak_length
    assert all(row["streak_count"] is None for row in result.rows[:-1])


def test_rolling_window_rejects_a_security_gap_in_the_market_calendar():
    source = QueryResult(
        query_id="daily",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "A", "trade_date": "20260101", "is_event": True},
            {"ts_code": "A", "trade_date": "20260103", "is_event": True},
            {"ts_code": "A", "trade_date": "20260104", "is_event": True},
            {"ts_code": "B", "trade_date": "20260101", "is_event": False},
            {"ts_code": "B", "trade_date": "20260102", "is_event": False},
            {"ts_code": "B", "trade_date": "20260103", "is_event": False},
            {"ts_code": "B", "trade_date": "20260104", "is_event": False},
        ],
        row_count=7,
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "daily",
            "output_query_id": "streaks",
            "steps": [
                {
                    "operation": "rolling_sum",
                    "field": "is_event",
                    "output_field": "streak_count",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "window": 3,
                    "require_consecutive": True,
                }
            ],
        }
    )

    result = ResultPipelineExecutor().execute(pipeline, source)

    last_a = next(
        row
        for row in result.rows
        if row["ts_code"] == "A" and row["trade_date"] == "20260104"
    )
    assert last_a["streak_count"] is None


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


def test_pipeline_matches_offsets_for_market_wide_data_without_scalar_writes():
    security_count = 500
    trading_dates = [f"202601{day:02d}" for day in range(1, 21)]
    rows = [
        {
            "ts_code": f"{security:06d}.SZ",
            "trade_date": trade_date,
            "close": float(day),
        }
        for security in range(security_count)
        for day, trade_date in enumerate(trading_dates, start=1)
    ]
    source = QueryResult(
        query_id="daily",
        provider="tushare",
        operation="daily",
        status="success",
        rows=rows,
        row_count=len(rows),
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

    assert result.row_count == security_count * len(trading_dates)
    assert result.rows[0]["next_close"] == 2.0
    assert result.rows[0]["next_trade_date"] == "20260102"
    assert result.rows[-1]["next_close"] is None
    assert result.rows[-1]["next_trade_date"] is None


def test_result_pipeline_exists_in_source():
    source = QueryResult(
        query_id="daily",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[{"ts_code": "000001.SZ", "close": 12.0}],
        row_count=1,
    )
    right = QueryResult(
        query_id="limit_up",
        provider="tushare",
        operation="limit_list_d",
        status="success",
        rows=[{"ts_code": "000001.SZ", "limit_up": True}],
        row_count=1,
    )
    pipeline = ResultPipeline.model_validate({
        "source_query_id": "daily",
        "output_query_id": "out",
        "steps": [
            {
                "operation": "exists_in_source",
                "right_source_query_id": "limit_up",
                "join_on": ["ts_code"],
                "output_field": "is_limit_up",
            }
        ]
    })
    result = ResultPipelineExecutor().execute(pipeline, source, {"limit_up": right})
    assert result.rows[0]["is_limit_up"] is True


def test_result_pipeline_join_fields_one_to_one():
    source = QueryResult(
        query_id="start",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "000001.SZ", "close": 10.0},
            {"ts_code": "000002.SZ", "close": 20.0},
        ],
        row_count=2,
    )
    right = QueryResult(
        query_id="end",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "000001.SZ", "close": 11.0},
            {"ts_code": "000002.SZ", "close": 18.0},
        ],
        row_count=2,
    )
    pipeline = ResultPipeline.model_validate({
        "source_query_id": "start",
        "output_query_id": "out",
        "steps": [
            {
                "operation": "join_fields",
                "right_source_query_id": "end",
                "join_on": ["ts_code"],
                "fields": {"close": "end_close"},
                "cardinality": "one_to_one",
            }
        ]
    })
    result = ResultPipelineExecutor().execute(pipeline, source, {"end": right})
    assert result.rows[0]["close"] == 10.0
    assert result.rows[0]["end_close"] == 11.0
    assert result.rows[1]["close"] == 20.0
    assert result.rows[1]["end_close"] == 18.0


def test_result_pipeline_join_fields_violates_cardinality():
    import pytest
    source = QueryResult(
        query_id="start",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "000001.SZ", "close": 10.0},
            {"ts_code": "000001.SZ", "close": 12.0},  # Duplicate left keys
        ],
        row_count=2,
    )
    right = QueryResult(
        query_id="end",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "000001.SZ", "close": 11.0},
        ],
        row_count=1,
    )
    pipeline = ResultPipeline.model_validate({
        "source_query_id": "start",
        "output_query_id": "out",
        "steps": [
            {
                "operation": "join_fields",
                "right_source_query_id": "end",
                "join_on": ["ts_code"],
                "fields": {"close": "end_close"},
                "cardinality": "one_to_one",
            }
        ]
    })
    with pytest.raises(ValueError, match="one_to_one cardinality violated"):
        ResultPipelineExecutor().execute(pipeline, source, {"end": right})


def test_result_pipeline_invariants_limit():
    import pytest
    source = QueryResult(
        query_id="daily",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "000001.SZ", "close": 10.0},
            {"ts_code": "000002.SZ", "close": 20.0},
        ],
        row_count=2,
    )
    pipeline = ResultPipeline.model_validate({
        "source_query_id": "daily",
        "output_query_id": "out",
        "steps": [
            {
                "operation": "limit",
                "count": 1,
            }
        ]
    })
    # Since we did not drop or slice in execute before invariants run (or we test that limit invariant enforces maximum boundaries):
    # Actually, execute applies steps sequentially. The "limit" step in execute applies .head(1).
    # If the rows returned exceed limit due to some bug, it raises. Let's verify normal head(1) runs:
    res = ResultPipelineExecutor().execute(pipeline, source)
    assert res.row_count == 1


def test_result_pipeline_invariants_monotonicity():
    import pytest
    source = QueryResult(
        query_id="daily",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "000001.SZ", "close": 12.0},
            {"ts_code": "000002.SZ", "close": 10.0},
        ],
        row_count=2,
    )
    # Correct sort ascending should be 10.0 first, then 12.0. Let's create an invalid non-monotonic list by sorting descending but asserting asc (or vice-versa):
    # Actually, the sort step itself sorts correctly, so sorting normally always passes invariants.
    # To test invariant verification, let's pass steps where we sort but somehow monotonicity is violated (e.g. invalid non-sorted or nan fields).
    source_nan = QueryResult(
        query_id="daily",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "000001.SZ", "close": 12.0},
            {"ts_code": "000002.SZ", "close": None},  # NaN in sorted field!
        ],
        row_count=2,
    )
    pipeline = ResultPipeline.model_validate({
        "source_query_id": "daily",
        "output_query_id": "out",
        "steps": [
            {
                "operation": "sort",
                "field": "close",
                "direction": "asc",
            }
        ]
    })
    with pytest.raises(ValueError, match="contains invalid or missing"):
        ResultPipelineExecutor().execute(pipeline, source_nan)


def test_result_pipeline_invariants_zero_return_check():
    import pytest
    source = QueryResult(
        query_id="start",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "000001.SZ", "close": 10.0},
            {"ts_code": "000002.SZ", "close": 20.0},
        ],
        row_count=2,
    )
    right = QueryResult(
        query_id="end",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "000001.SZ", "close": 10.0}, # identical closes!
            {"ts_code": "000002.SZ", "close": 20.0},
        ],
        row_count=2,
    )
    pipeline = ResultPipeline.model_validate({
        "source_query_id": "start",
        "output_query_id": "out",
        "steps": [
            {
                "operation": "join_fields",
                "right_source_query_id": "end",
                "join_on": ["ts_code"],
                "fields": {"close": "end_close"},
                "cardinality": "one_to_one",
            },
            {
                "operation": "derive",
                "field": "end_close",
                "output_field": "period_return_pct",
                "arithmetic_operator": "divide",
                "right_field": "close",
            },
            {
                "operation": "derive",
                "field": "period_return_pct",
                "output_field": "period_return_pct",
                "arithmetic_operator": "subtract",
                "value": 1.0,
            },
            {
                "operation": "sort",
                "field": "period_return_pct",
                "direction": "asc",
            }
        ]
    })
    # If starting close and ending close are identical for all rows, period_return_pct is 0.0, raising zero-return check!
    # Let's add start_close and end_close columns renaming to mimic our start/end closes mapping:
    pipeline_renamed = ResultPipeline.model_validate({
        "source_query_id": "start",
        "output_query_id": "out",
        "steps": [
            {
                "operation": "derive",
                "field": "close",
                "output_field": "start_close",
                "arithmetic_operator": "multiply",
                "value": 1.0,
            },
            {
                "operation": "join_fields",
                "right_source_query_id": "end",
                "join_on": ["ts_code"],
                "fields": {"close": "end_close"},
                "cardinality": "one_to_one",
            },
            {
                "operation": "derive",
                "field": "end_close",
                "output_field": "period_return_pct",
                "arithmetic_operator": "divide",
                "right_field": "start_close",
            },
            {
                "operation": "derive",
                "field": "period_return_pct",
                "output_field": "period_return_pct",
                "arithmetic_operator": "subtract",
                "value": 1.0,
            },
            {
                "operation": "sort",
                "field": "period_return_pct",
                "direction": "asc",
            }
        ]
    })
    with pytest.raises(ValueError, match="all calculated return values are exactly 0.0"):
        ResultPipelineExecutor().execute(pipeline_renamed, source, {"end": right})

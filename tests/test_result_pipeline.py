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

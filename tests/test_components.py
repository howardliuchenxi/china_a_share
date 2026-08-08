import json
import re
from datetime import date, datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

from china_a_share.client import TushareApiError
from china_a_share.application.workflow import (
    ASharePlanValidator,
    AnalysisService,
    DataQueryExecutor,
    PlanValidationError,
)
from china_a_share.core.contracts import (
    AnswerContract,
    AnalysisIntent,
    AnalysisRequest,
    DataFilter,
    DataOperation,
    DataQuery,
    ExecutionNode,
    ExecutionPlan,
    QueryPlan,
    QueryResult,
    RequirementCoverage,
    ResultPipeline,
    ResultPipelineStep,
)
from china_a_share.core.errors import PlannerError
from china_a_share.planners.deepseek import DeepSeekQueryPlanner
from china_a_share.planners.vertex_claude import VertexClaudeQueryPlanner
from china_a_share.result_pipeline import ResultPipelineExecutor
from china_a_share.registry import TushareOperationCatalog
from china_a_share.capabilities import build_capability_manifest
from china_a_share.registry import (
    READ_ONLY_API_NAMES,
    STOCK_API_NAMES,
    TUSHARE_API_CATEGORIES,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None, exception=None):
        self.response = response
        self.exception = exception
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exception:
            raise self.exception
        return self.response


class SequenceFakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


class FakeMarketDataProvider:
    def __init__(self, frame=None, error=None, stock_frame=None):
        self.frame = frame
        self.error = error
        self.stock_frame = stock_frame
        self.calls = []

    @property
    def name(self):
        return "tushare"

    def search_operations(self, prompt):
        return [DataOperation(name="daily", description="Daily prices.")]

    def supports(self, operation):
        return operation in {
            "daily",
            "daily_basic",
            "monthly",
            "ths_member",
            "top10_floatholders",
            "weekly",
        } or (
            operation == "stock_basic" and self.stock_frame is not None
        )

    def query(
        self,
        operation,
        params,
        fields,
        *,
        api_route,
        request_id,
        query_id,
    ):
        self.calls.append(operation)
        if self.error:
            raise self.error
        if operation == "stock_basic":
            return self.stock_frame
        return self.frame


def make_daily_plan():
    return QueryPlan(
        interpretation="Count daily market direction.",
        requirements=[
            {
                "requirement": "Count advancing and declining securities.",
                "status": "covered",
                "implementation": "Aggregate the daily change field locally.",
                "evidence": "The daily operation documents the change field.",
            }
        ],
        queries=[
            DataQuery(
                query_id="market_direction",
                operation="daily",
                params={"trade_date": "20260717"},
                fields=["ts_code", "change"],
                purpose="Retrieve full-market daily changes.",
                aggregations=[
                    {"label": "Advanced", "field": "change", "operator": "gt", "value": 0},
                    {"label": "Declined", "field": "change", "operator": "lt", "value": 0},
                ],
            )
        ],
    )


def test_catalog_and_validator_accept_stock_operation_plan():
    catalog = TushareOperationCatalog()
    provider = FakeMarketDataProvider()

    assert any(item.name == "daily" for item in catalog.search("Count stocks."))
    assert (
        ASharePlanValidator(provider).validate(make_daily_plan()).queries[0].operation
        == "daily"
    )


def test_runtime_manifest_covers_every_connected_tushare_operation():
    class FullyConnectedProvider:
        name = "tushare"
        operation_names = READ_ONLY_API_NAMES

        @staticmethod
        def supports(operation):
            return operation in READ_ONLY_API_NAMES

    manifest = build_capability_manifest(
        FullyConnectedProvider(),
        {"limit_up_streak": lambda: None},
    )

    assert manifest["provider_operation_count"] == len(READ_ONLY_API_NAMES)
    assert manifest["tushare_catalog_fully_connected"] is True
    assert manifest["fingerprint"].startswith("sha256:")
    assert manifest["capabilities"][0]["id"] == "limit_up_streak"
    assert manifest["tushare_category_coverage"]["stock"] == {
        "documented": 108,
        "connected": 108,
    }
    assert manifest["tushare_category_coverage"]["index"] == {
        "documented": 20,
        "connected": 20,
    }
    assert (
        manifest["capabilities"][0]["parameters"]["streak_length"]["minimum"]
        == 1
    )


def test_read_only_catalog_matches_the_official_category_snapshot():
    expected_counts = {
        "stock": 108,
        "etf": 13,
        "index": 20,
        "fund": 9,
        "futures": 13,
        "spot": 2,
        "option": 3,
        "bond": 17,
        "forex": 2,
        "hong_kong": 9,
        "united_states": 9,
        "macro": 19,
        "text": 9,
        "portfolio_read": 2,
    }

    assert {
        category: len(operations)
        for category, operations in TUSHARE_API_CATEGORIES.items()
    } == expected_counts
    assert len(READ_ONLY_API_NAMES) == 235
    assert len(set(READ_ONLY_API_NAMES)) == len(READ_ONLY_API_NAMES)
    assert "p_save" not in READ_ONLY_API_NAMES
    assert "p_delete" not in READ_ONLY_API_NAMES


def test_registered_limit_up_capability_recovers_variable_streak_plan():
    plan = QueryPlan(
        interpretation="The provider has no consecutive-limit-up field.",
        feasibility="unsupported",
        requirements=[
            {
                "requirement": "Find four-session limit-up streaks.",
                "status": "unsupported",
                "implementation": "No direct field was found.",
                "evidence": "The provider exposes daily limit-up rows.",
            }
        ],
        limitations=["No direct consecutive-limit-up field exists."],
    )

    AnalysisService._compile_limit_up_streak_pipeline(
        plan,
        "20250101至20251231连续涨停四个交易日",
        4,
    )

    assert plan.feasibility == "supported"
    assert [query.operation for query in plan.queries] == ["daily", "limit_list_d"]
    rolling_step = next(
        step for step in plan.result_pipeline.steps if step.operation == "rolling_sum"
    )
    assert rolling_step.window == 4
    assert rolling_step.min_periods == 4
    assert rolling_step.require_consecutive is True


def test_validator_rejects_operation_outside_provider_catalog():
    plan = make_daily_plan()
    plan.queries[0].operation = "us_daily"

    try:
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)
    except PlanValidationError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("Expected a plan validation error.")


def test_validator_rejects_market_wide_monthly_range_without_native_key():
    plan = make_daily_plan()
    plan.queries[0].operation = "monthly"
    plan.queries[0].params = {
        "start_date": "20260601",
        "end_date": "20260630",
    }

    with pytest.raises(
        PlanValidationError,
        match="monthly requires ts_code or trade_date",
    ):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_validator_rejects_invalid_or_reversed_date_ranges():
    plan = make_daily_plan()
    plan.queries[0].params = {
        "start_date": "20260230",
        "end_date": "20260101",
    }

    with pytest.raises(PlanValidationError, match="valid calendar date"):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)

    plan.queries[0].params = {
        "start_date": "20260630",
        "end_date": "20260601",
    }
    with pytest.raises(PlanValidationError, match="must not be later"):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_validator_does_not_infer_execution_rules_from_requirement_prose():
    plan = make_daily_plan()
    plan.queries[0].aggregations = []
    plan.requirements[0].implementation = (
        "Join locally and calculate the average result."
    )

    result = ASharePlanValidator(FakeMarketDataProvider()).validate(plan)

    assert result.feasibility == "supported"
    assert result.queries
    assert result.requirements[0].status == "covered"


def test_future_horizon_rejects_a_next_day_substitution():
    plan = make_daily_plan()
    plan.queries.append(
        DataQuery(
            query_id="limit-ups",
            operation="limit_list_d",
            params={
                "start_date": "20260101",
                "end_date": "20260601",
                "limit_type": "U",
            },
            fields=["ts_code", "trade_date"],
            purpose="Identify native limit-up events.",
        )
    )
    plan.queries[0].params = {
        "start_date": "20260101",
        "end_date": "20260701",
    }
    plan.queries[0].fields = ["ts_code", "trade_date", "close"]
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": plan.queries[0].query_id,
            "output_query_id": "wrong-horizon",
            "steps": [
                {
                    "operation": "shift",
                    "field": "close",
                    "output_field": "next_close",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "periods": -1,
                }
            ],
        }
    )

    with pytest.raises(
        PlanValidationError,
        match="preserve the requested future outcome horizon",
    ):
        AnalysisService._validate_planned_time_semantics(
            plan,
            "A股20260101～20260601涨停事件接下来一个月的上涨情况数据分析",
        )


def test_future_horizon_accepts_the_exact_calendar_offset_and_data_coverage():
    plan = make_daily_plan()
    plan.queries.append(
        DataQuery(
            query_id="limit-ups",
            operation="limit_list_d",
            params={
                "start_date": "20260101",
                "end_date": "20260601",
                "limit_type": "U",
            },
            fields=["ts_code", "trade_date"],
            purpose="Identify native limit-up events.",
        )
    )
    plan.queries[0].params = {
        "start_date": "20260101",
        "end_date": "20260701",
    }
    plan.queries[0].fields = ["ts_code", "trade_date", "close"]
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": plan.queries[0].query_id,
            "output_query_id": "one-month-horizon",
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

    result = AnalysisService._validate_planned_time_semantics(
        plan,
        "A股20260101～20260601涨停事件接下来一个月的上涨情况数据分析",
    )

    assert result.result_pipeline.steps[0].offset_unit == "month"


def test_future_horizon_expands_an_existing_source_date_range():
    plan = make_daily_plan()
    plan.queries.append(
        DataQuery(
            query_id="limit-ups",
            operation="limit_list_d",
            params={
                "start_date": "20260101",
                "end_date": "20260601",
                "limit_type": "U",
            },
            fields=["ts_code", "trade_date"],
            purpose="Identify native limit-up events.",
        )
    )
    plan.queries[0].params = {
        "start_date": "20260115",
        "end_date": "20260615",
    }
    plan.queries[0].fields = ["ts_code", "trade_date", "close"]
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": plan.queries[0].query_id,
            "output_query_id": "one-month-horizon",
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

    result = AnalysisService._validate_planned_time_semantics(
        plan,
        "A股20260101～20260601涨停事件接下来一个月的上涨情况数据分析",
    )

    assert result.queries[0].params["start_date"] == "20260101"
    assert result.queries[0].params["end_date"] == "20260701"


def test_limit_up_streak_requires_the_exact_complete_session_window():
    plan = make_daily_plan()
    plan.queries.append(
        DataQuery(
            query_id="limit-ups",
            operation="limit_list_d",
            params={
                "start_date": "20260101",
                "end_date": "20260601",
                "limit_type": "U",
            },
            fields=["ts_code", "trade_date"],
            purpose="Identify native limit-up events.",
        )
    )
    plan.queries[0].params = {
        "start_date": "20260101",
        "end_date": "20260601",
    }
    plan.queries[0].fields = ["ts_code", "trade_date", "close"]
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": plan.queries[0].query_id,
            "output_query_id": "streaks",
            "steps": [
                {
                    "operation": "match_source",
                    "right_source_query_id": "limit-ups",
                    "join_on": ["ts_code", "trade_date"],
                    "output_field": "is_limit_up",
                },
                {
                    "operation": "rolling_sum",
                    "field": "is_limit_up",
                    "output_field": "streak_count",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "window": 2,
                    "min_periods": 2,
                    "require_consecutive": True,
                },
            ],
        }
    )

    with pytest.raises(
        PlanValidationError,
        match=r"requested consecutive session count \(3\)",
    ):
        AnalysisService._validate_planned_time_semantics(
            plan,
            "统计三连板事件数量",
        )

    plan.result_pipeline.steps[1].window = 3
    plan.result_pipeline.steps[1].min_periods = 3

    validated = AnalysisService._validate_planned_time_semantics(
        plan,
        "统计三连板事件数量",
    )
    assert validated.result_pipeline.steps[1].window == 3


def test_limit_up_analysis_requires_the_native_limit_list():
    plan = make_daily_plan()

    with pytest.raises(
        PlanValidationError,
        match="must use the native limit_list_d operation",
    ):
        AnalysisService._validate_planned_time_semantics(
            plan,
            "Analyze stocks with three consecutive limit-up days.",
        )


def test_future_performance_request_cannot_be_marked_supported():
    plan = make_daily_plan()

    with pytest.raises(
        PlanValidationError,
        match="Future price or return rankings are not supported",
    ):
        AnalysisService._validate_planned_time_semantics(
            plan,
            "给出下周收益率最高的公司",
        )


def test_current_day_prompt_receives_a_trusted_completed_trading_date():
    provider = FakeMarketDataProvider(frame=pd.DataFrame())
    service = AnalysisService(
        Mock(),
        provider,
        ASharePlanValidator(provider),
        DataQueryExecutor(provider),
    )

    enriched = service._append_resolved_time_range(
        "request-current-day",
        "今天A股上涨、下跌和平盘各有多少只？",
    )

    assert "<trusted_analysis_window>" in enriched
    start = re.search(r"event_start_date=(\d{8})", enriched)
    end = re.search(r"event_end_date=(\d{8})", enriched)
    assert start is not None
    assert end is not None
    assert start.group(1) == end.group(1)


def test_dividend_rejects_non_native_provider_parameters():
    validator = ASharePlanValidator(FakeMarketDataProvider())

    with pytest.raises(
        PlanValidationError,
        match="dividend uses unsupported provider parameters: year",
    ):
        validator._validate_params("dividend", {"year": 2025})


def test_planner_parses_deepseek_json_plan():
    plan = make_daily_plan()
    session = FakeSession(
        FakeResponse({"choices": [{"message": {"content": plan.model_dump_json()}}]})
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="Count stocks."),
        [DataOperation(name="daily", description="Daily prices.")],
    )

    assert result.queries[0].params["trade_date"] == "20260717"
    sent = session.calls[0][1]
    assert sent["headers"]["Authorization"] == "Bearer test-key"
    assert sent["json"]["thinking"] == {"type": "disabled"}
    system_prompt = sent["json"]["messages"][0]["content"]
    assert "Mark feasibility as supported only when every requirement maps" in system_prompt
    assert "return no queries" in system_prompt
    assert "Do not substitute a similar metric" in system_prompt
    assert "Preserve every numeric value" in system_prompt
    assert "rolling_sum" in system_prompt
    assert "match_source" in system_prompt
    assert "\u4e2d\u56fd\u5e73\u5b89 is 601318.SH" in system_prompt
    assert "full-market request as a fan-out template" in system_prompt
    assert "return separate query results unless" in system_prompt


def test_planner_retries_one_contract_invalid_response():
    plan = make_daily_plan()
    session = SequenceFakeSession(
        [
            FakeResponse(
                {"choices": [{"message": {"content": '{"market":"A_SHARE"'}}]}
            ),
            FakeResponse(
                {"choices": [{"message": {"content": plan.model_dump_json()}}]}
            ),
        ]
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="Count stocks."),
        [DataOperation(name="daily", description="Daily prices.")],
    )

    assert result.queries[0].operation == "daily"
    assert len(session.calls) == 2
    retry_messages = session.calls[1][1]["json"]["messages"]
    assert "previous query plan was rejected" in retry_messages[-1]["content"]
    assert "violates the contract" in retry_messages[-1]["content"]
    assert "invalid JSON at line 1, column 20" in retry_messages[-1]["content"]


def test_planner_normalizes_null_pipeline_collections_to_defaults():
    plan = make_daily_plan().model_dump(mode="json")
    plan["result_pipeline"] = {
        "source_query_id": "market_direction",
        "output_query_id": "sorted",
        "steps": [
            {
                "operation": "sort",
                "field": "change",
                "join_on": None,
                "fields": None,
                "group_by": None,
                "aggregations": None,
            }
        ],
    }

    result = DeepSeekQueryPlanner("test-key").normalize_and_validate_plan(
        json.dumps(plan)
    )

    step = result.result_pipeline.steps[0]
    assert step.join_on == []
    assert step.fields == []
    assert step.group_by == []
    assert step.aggregations == []


def test_planner_replaces_first_last_period_return_aggregation():
    plan = make_daily_plan().model_dump(mode="json")
    plan["queries"][0]["fields"] = ["ts_code", "trade_date", "close"]
    plan["result_pipeline"] = {
        "source_query_id": plan["queries"][0]["query_id"],
        "output_query_id": "period-return",
        "steps": [
            {
                "operation": "aggregate",
                "group_by": ["ts_code"],
                "aggregations": [
                    {"output_field": "first_close", "field": "close", "function": "first"},
                    {"output_field": "last_close", "field": "close", "function": "last"},
                ],
            },
            {"operation": "sort", "field": "period_return_pct", "direction": "desc"},
            {"operation": "limit", "count": 10},
        ],
    }

    result = DeepSeekQueryPlanner("test-key").normalize_and_validate_plan(
        json.dumps(plan)
    )

    assert result.queries[0].transform == "period_return_by_ts_code"
    assert [step.operation for step in result.result_pipeline.steps] == [
        "sort",
        "limit",
    ]


def test_planner_inserts_shift_for_missing_previous_margin_field():
    plan = make_daily_plan().model_dump(mode="json")
    plan["queries"][0]["operation"] = "margin_detail"
    plan["queries"][0]["fields"] = ["ts_code", "trade_date", "rzye"]
    plan["result_pipeline"] = {
        "source_query_id": plan["queries"][0]["query_id"],
        "output_query_id": "margin-change",
        "steps": [
            {
                "operation": "derive",
                "field": "rzye",
                "right_field": "rzye_prev",
                "output_field": "rzye_change",
                "arithmetic_operator": "subtract",
            }
        ],
    }

    result = DeepSeekQueryPlanner("test-key").normalize_and_validate_plan(
        json.dumps(plan)
    )

    assert [step.operation for step in result.result_pipeline.steps] == [
        "shift",
        "derive",
    ]
    assert result.result_pipeline.steps[0].output_field == "rzye_prev"


def test_planner_retries_with_field_level_contract_feedback():
    invalid_plan = make_daily_plan().model_dump(mode="json")
    invalid_plan["result_pipeline"] = {
        "source_query_id": invalid_plan["queries"][0]["query_id"],
        "output_query_id": "invalid-shift",
        "steps": [
            {
                "operation": "derive",
                "field": "close",
                "output_field": "previous_close",
                "arithmetic_operator": "shift",
                "periods": 1,
            }
        ],
    }
    valid_plan = make_daily_plan()
    session = SequenceFakeSession(
        [
            FakeResponse(
                {
                    "choices": [
                        {"message": {"content": json.dumps(invalid_plan)}}
                    ]
                }
            ),
            FakeResponse(
                {"choices": [{"message": {"content": valid_plan.model_dump_json()}}]}
            ),
        ]
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="Count stocks."),
        [DataOperation(name="daily", description="Daily prices.")],
    )

    assert result.queries[0].operation == "daily"
    retry_feedback = session.calls[1][1]["json"]["messages"][-1]["content"]
    assert "result_pipeline.steps.0.arithmetic_operator" in retry_feedback
    assert "Input should be 'add', 'subtract', 'multiply', 'divide' or" in retry_feedback


def test_planner_normalizes_derive_comparison_to_compare_scalar():
    plan = make_daily_plan().model_dump(mode="json")
    plan["result_pipeline"] = {
        "source_query_id": plan["queries"][0]["query_id"],
        "output_query_id": "comparison",
        "steps": [
            {
                "operation": "derive",
                "field": "pct_chg",
                "output_field": "is_positive",
                "arithmetic_operator": "gt",
                "value": 0,
            }
        ],
    }
    session = FakeSession(
        FakeResponse(
            {"choices": [{"message": {"content": json.dumps(plan)}}]}
        )
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="Find positive returns."),
        [DataOperation(name="daily", description="Daily prices.")],
    )

    step = result.result_pipeline.steps[0]
    assert step.operation == "compare_scalar"
    assert step.comparison == "gt"


def test_planner_normalizes_pipeline_syntax_across_operations():
    plan = make_daily_plan().model_dump(mode="json")
    plan["result_pipeline"] = {
        "source_query_id": plan["queries"][0]["query_id"],
        "output_query_id": "normalized",
        "steps": [
            {
                "operation": "sort",
                "order_by": "trade_date",
                "direction": "asc",
                "purpose": "Order observations before analysis.",
            },
            {
                "operation": "compare_scalar",
                "field": "pct_chg",
                "operator": "gt",
                "value": 0,
                "description": "Mark positive sessions.",
            },
        ],
    }
    planner = DeepSeekQueryPlanner("test-key")

    result = planner.normalize_and_validate_plan(json.dumps(plan))

    sort_step, comparison_step = result.result_pipeline.steps
    assert sort_step.field == "trade_date"
    assert comparison_step.comparison == "gt"
    assert comparison_step.output_field == "condition_1"


def test_planner_normalizes_event_summary_syntax():
    plan = make_daily_plan().model_dump(mode="json")
    plan["result_pipeline"] = {
        "source_query_id": plan["queries"][0]["query_id"],
        "output_query_id": "event-summary",
        "steps": [
            {
                "operation": "sort",
                "order_by": "trade_date",
                "direction": "asc",
            },
            {
                "operation": "derive",
                "field": "price_ratio",
                "output_field": "return_pct",
                "arithmetic_operator": "constant_minus",
                "value": 1,
            },
            {
                "operation": "summarize",
                "aggregations": [
                    {
                        "output_field": "up_count",
                        "field": "return_pct",
                        "function": "count",
                        "condition": {"operator": "gt", "value": 0},
                    },
                    {
                        "output_field": "up_ratio",
                        "field": "return_pct",
                        "function": "mean",
                        "condition": {"operator": "gt", "value": 0},
                    },
                ],
            },
        ],
    }
    planner = DeepSeekQueryPlanner("test-key")

    result = planner.normalize_and_validate_plan(json.dumps(plan))

    assert [step.operation for step in result.result_pipeline.steps] == [
        "sort",
        "derive",
        "compare_scalar",
        "summarize",
    ]
    sort_step, return_step, condition_step, summary_step = (
        result.result_pipeline.steps
    )
    assert sort_step.field == "trade_date"
    assert return_step.arithmetic_operator == "subtract"
    assert condition_step.field == "return_pct"
    assert summary_step.aggregations[0].field == condition_step.output_field
    assert summary_step.aggregations[0].function == "sum"
    assert summary_step.aggregations[1].field == condition_step.output_field
    assert summary_step.aggregations[1].function == "mean"


def test_planner_preserves_a_valid_event_study_aggregation():
    plan = make_daily_plan().model_dump(mode="json")
    plan["queries"][0]["params"] = {
        "start_date": "20260105",
        "end_date": "20260601",
    }
    plan["queries"][0]["fields"] = ["ts_code", "trade_date", "close"]
    plan["queries"].append(
        {
            "query_id": "limit-ups",
            "operation": "limit_list_d",
            "params": {
                "start_date": "20260101",
                "end_date": "20260601",
                "limit_type": "U",
            },
            "fields": ["ts_code", "trade_date"],
            "purpose": "Identify native limit-up events.",
            "filters": [],
            "aggregations": [],
        }
    )
    plan["result_pipeline"] = {
        "source_query_id": "market_direction",
        "output_query_id": "event-study",
        "steps": [
            {
                "operation": "match_source",
                "right_source_query_id": "limit-ups",
                "join_on": ["ts_code", "trade_date"],
                "output_field": "is_limit_up",
            },
            {
                "operation": "rolling_sum",
                "field": "limit_up_flag",
                "output_field": "streak_count",
                "group_by": ["ts_code"],
                "order_by": "trade_date",
                "window": 3,
                "min_periods": 1,
            },
            {
                "operation": "match_at_offset",
                "field": "close",
                "output_field": "future_close",
                "matched_date_output_field": "future_date",
                "group_by": ["ts_code"],
                "order_by": "trade_date",
                "offset_value": 1,
                "offset_unit": "month",
            },
            {
                "operation": "filter",
                "field": "streak_count",
                "comparison": "eq",
                "value": 3,
            },
            {"operation": "drop_missing", "fields": ["future_close"]},
            {
                "operation": "derive",
                "field": "future_close",
                "right_field": "close",
                "output_field": "outcome_ratio",
                "arithmetic_operator": "divide",
            },
            {
                "operation": "derive",
                "field": "outcome_ratio",
                "output_field": "outcome_return",
                "arithmetic_operator": "subtract",
                "value": 1,
            },
            {
                "operation": "derive",
                "field": "outcome_return",
                "output_field": "outcome_return_pct",
                "arithmetic_operator": "multiply",
                "value": 100,
            },
            {
                "operation": "summarize",
                "aggregations": [
                    {
                        "output_field": "average_return_pct",
                        "field": "outcome_return_pct",
                        "function": "mean",
                    }
                ],
            },
        ],
    }
    planner = DeepSeekQueryPlanner("test-key")

    result = planner.normalize_and_validate_plan(json.dumps(plan))

    steps = result.result_pipeline.steps
    assert [step.operation for step in steps] == [
        "match_source",
        "rolling_sum",
        "match_at_offset",
        "filter",
        "drop_missing",
        "derive",
        "derive",
        "derive",
        "summarize",
    ]
    assert steps[1].min_periods == 3
    assert steps[1].require_consecutive is True
    assert steps[1].field == "is_limit_up"
    assert steps[3].field == "streak_count"
    assert steps[5].field == "future_close"
    assert steps[5].right_field == "close"
    assert steps[6].arithmetic_operator == "subtract"
    assert len(steps[-1].aggregations) == 1
    assert steps[-1].aggregations[0].output_field == "average_return_pct"
    assert steps[-1].aggregations[0].function == "mean"
    assert result.result_pipeline.source_query_id == "market_direction"
    source_query = next(
        query for query in result.queries if query.query_id == "market_direction"
    )
    assert source_query.params["start_date"] == "20260101"
    assert source_query.params["end_date"] == "20260701"


def test_planner_retries_with_semantic_validation_feedback():
    invalid_plan = make_daily_plan()
    invalid_plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": invalid_plan.queries[0].query_id,
            "output_query_id": "invalid-ranking",
            "steps": [{"operation": "sort", "field": "missing_field"}],
        }
    )
    valid_plan = make_daily_plan()
    invalid_plan.answer_contract = AnswerContract.model_validate(
        {
            "result_query_id": "invalid-ranking",
            "result_kind": "table",
            "outputs": [
                {
                    "field": "ts_code",
                    "description": "A-share security code.",
                }
            ],
        }
    )
    valid_plan.answer_contract = AnswerContract.model_validate(
        {
            "result_query_id": "market_direction",
            "result_kind": "table",
            "outputs": [
                {
                    "field": "ts_code",
                    "description": "A-share security code.",
                }
            ],
        }
    )
    session = SequenceFakeSession(
        [
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": invalid_plan.model_dump_json()
                            }
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "choices": [
                        {"message": {"content": valid_plan.model_dump_json()}}
                    ]
                }
            ),
        ]
    )
    validator = ASharePlanValidator(FakeMarketDataProvider())

    result = DeepSeekQueryPlanner(
        "test-key",
        session=session,
    ).plan_validated(
        AnalysisRequest(prompt="Count stocks."),
        [DataOperation(name="daily", description="Daily prices.")],
        validator.validate,
    )

    assert result == valid_plan
    assert len(session.calls) == 2
    retry_messages = session.calls[1][1]["json"]["messages"]
    assert "sort references unavailable fields: missing_field" in (
        retry_messages[-1]["content"]
    )


def test_planner_retries_when_answer_contract_is_omitted():
    missing_contract = make_daily_plan()
    valid_plan = make_daily_plan()
    valid_plan.answer_contract = AnswerContract.model_validate(
        {
            "result_query_id": "market_direction",
            "result_kind": "table",
            "outputs": [
                {
                    "field": "ts_code",
                    "description": "A-share security code.",
                }
            ],
        }
    )
    session = SequenceFakeSession(
        [
            FakeResponse(
                {
                    "choices": [
                        {"message": {"content": missing_contract.model_dump_json()}}
                    ]
                }
            ),
            FakeResponse(
                {
                    "choices": [
                        {"message": {"content": valid_plan.model_dump_json()}}
                    ]
                }
            ),
        ]
    )

    result = DeepSeekQueryPlanner(
        "test-key",
        session=session,
    ).plan_validated(
        AnalysisRequest(prompt="Return the requested fields."),
        [DataOperation(name="daily", description="Daily prices.")],
        ASharePlanValidator(FakeMarketDataProvider()).validate,
    )

    assert result == valid_plan
    retry_messages = session.calls[1][1]["json"]["messages"]
    assert "must include answer_contract" in retry_messages[-1]["content"]


def test_vertex_planner_fallback_preserves_semantic_validation():
    invalid_plan = make_daily_plan()
    valid_plan = make_daily_plan()
    planner = VertexClaudeQueryPlanner("test-key")
    planner._plan_with_claude = Mock(return_value=invalid_plan)
    planner._fallback = Mock()
    planner._fallback.plan_validated.return_value = valid_plan
    validator = Mock(side_effect=ValueError("invalid field lineage"))
    request = AnalysisRequest(prompt="Count stocks.")
    operations = [DataOperation(name="daily", description="Daily prices.")]

    result = planner.plan_validated(request, operations, validator)

    assert result is valid_plan
    planner._fallback.plan_validated.assert_called_once_with(
        request,
        operations,
        validator,
    )


def test_planner_accepts_limit_up_query_with_native_limit_type():
    plan = QueryPlan(
        interpretation="List yesterday's limit-up stocks.",
        requirements=[
            {
                "requirement": "Count and list limit-up stocks.",
                "status": "covered",
                "implementation": "Use limit_list_d and its returned row count.",
                "evidence": "limit_list_d supports the native limit_type parameter.",
            }
        ],
        queries=[
            DataQuery(
                query_id="limit-ups",
                operation="limit_list_d",
                params={"trade_date": "20260723", "limit_type": "U"},
                fields=["ts_code", "name"],
                purpose="Retrieve yesterday's limit-up stocks.",
            )
        ],
    )
    session = FakeSession(
        FakeResponse({"choices": [{"message": {"content": plan.model_dump_json()}}]})
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="昨天涨停多少只股票，分别是哪些？"),
        [
            DataOperation(
                name="limit_list_d",
                description="Daily limit list with native limit_type=U for limit-up.",
            )
        ],
    )

    assert result.queries[0].params == {
        "trade_date": "20260723",
        "limit_type": "U",
    }
    assert result.queries[0].fields == ["ts_code", "name"]


def test_planner_repairs_model_generated_limit_up_filter_and_code_count():
    raw_plan = {
        "market": "A_SHARE",
        "interpretation": "Count and list yesterday's limit-up stocks.",
        "feasibility": "supported",
        "requirements": [
            {
                "requirement": "Count and list limit-up stocks.",
                "status": "covered",
                "implementation": "Use limit_list_d.",
                "evidence": "The operation provides daily limit-list rows.",
            }
        ],
        "limitations": [],
        "queries": [
            {
                "query_id": "limit-ups",
                "operation": "limit_list_d",
                "params": {"trade_date": "20260723"},
                "fields": ["ts_code", "name", "limit_type"],
                "purpose": "Retrieve yesterday's limit-list rows.",
                "filters": [
                    {
                        "field": "limit_type",
                        "operator": "eq",
                        "value": "U",
                    }
                ],
                "aggregations": [
                    {
                        "label": "Limit-up count",
                        "field": "ts_code",
                        "operator": "gt",
                        "value": 0,
                    }
                ],
            }
        ],
    }
    session = FakeSession(
        FakeResponse(
            {
                "choices": [
                    {"message": {"content": json.dumps(raw_plan)}}
                ]
            }
        )
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="昨天涨停多少只股票，分别是哪些？"),
        [DataOperation(name="limit_list_d", description="Daily limit list.")],
    )

    query = result.queries[0]
    assert query.params["limit_type"] == "U"
    assert query.fields == ["ts_code", "name"]
    assert query.filters == []
    assert query.aggregations == []


def test_workflow_moves_snapshot_query_to_safely_published_trading_date():
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    plan = make_daily_plan()
    plan.queries[0].operation = "daily_basic"
    plan.queries[0].params["trade_date"] = now.strftime("%Y%m%d")

    AnalysisService._normalize_latest_plan_dates(
        plan,
        date(2026, 7, 24),
    )

    assert plan.queries[0].params["trade_date"] == "20260723"


def test_workflow_clamps_future_period_return_to_completed_trading_date():
    plan = make_daily_plan()
    plan.queries[0].params = {
        "start_date": "20260101",
        "end_date": "20261231",
    }
    plan.queries[0].transform = "period_return_by_ts_code"

    AnalysisService._normalize_latest_plan_dates(plan, date(2026, 8, 7))

    assert plan.queries[0].params["end_date"] == "20260807"


def test_workflow_marks_explicit_dividend_total_boundary_unsupported():
    plan = make_daily_plan()

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "2025\u5e74\u73b0\u91d1\u5206\u7ea2\u603b\u989d\u6700\u9ad8\u7684A\u80a1\uff0c\u4e0d\u63a5\u53d7\u6bcf\u80a1\u5206\u7ea2\u66ff\u4ee3",
    )

    assert result.feasibility == "unsupported"
    assert result.queries == []
    assert result.result_pipeline is None


def test_workflow_compiles_limit_up_streak_pipeline_from_request_semantics():
    plan = make_daily_plan()
    plan.queries[0].fields = ["ts_code", "trade_date", "close"]
    plan.queries.append(
        DataQuery(
            query_id="limit-ups",
            operation="limit_list_d",
            params={},
            fields=["ts_code", "trade_date"],
            purpose="Retrieve limit-up membership.",
        )
    )

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "\u4e24\u4e2a\u4ea4\u6613\u65e5\u8fde\u677f\u540e\u4e0b\u4e00\u5929\u8fd8\u6da8\u7684\u9891\u7387",
    )

    steps = result.result_pipeline.steps
    rolling_step = next(step for step in steps if step.operation == "rolling_sum")
    offset_step = next(step for step in steps if step.operation == "match_at_offset")
    assert rolling_step.window == 2
    assert rolling_step.min_periods == 2
    assert rolling_step.require_consecutive is True
    assert (offset_step.offset_value, offset_step.offset_unit) == (
        1,
        "trading_session",
    )


def test_workflow_compiles_event_intent_without_model_authored_queries_or_fields():
    plan = QueryPlan(
        interpretation="Measure third-session direction after two limit-up sessions.",
        intent=AnalysisIntent.model_validate(
            {
                "analysis_type": "event_outcome_probability",
                "event_window": {"start": "20260101", "end": "20260601"},
                "event_type": "limit_up",
                "consecutive_sessions": 2,
                "observation_offset": 1,
                "observation_unit": "trading_session",
                "outcomes": ["up", "down"],
                "aggregation": "probability",
            }
        ),
    )

    result = AnalysisService._compile_intent(plan)

    assert [query.operation for query in result.queries] == [
        "daily",
        "limit_list_d",
    ]
    assert result.queries[0].fields == ["ts_code", "trade_date", "close"]
    assert result.queries[1].params == {
        "start_date": "20260101",
        "end_date": "20260601",
        "limit_type": "U",
    }
    offset = next(
        step
        for step in result.result_pipeline.steps
        if step.operation == "match_at_offset"
    )
    assert offset.field == "close"
    assert offset.order_by == "trade_date"
    assert offset.offset_value == 1
    assert result.answer_contract.result_query_id == "event_outcome_probability"
    assert [
        output.field for output in result.answer_contract.outputs
    ] == ["positive_event_ratio", "negative_event_ratio"]


def test_deepseek_raw_normalization_preserves_event_intent_null_legacy_fields():
    raw_plan = {
        "interpretation": "Measure post-event direction probabilities.",
        "feasibility": "supported",
        "queries": [{"operation": "daily"}],
        "result_pipeline": {
            "steps": [
                {
                    "operation": "match_at_offset",
                    "field": "trade_date",
                    "order_by": "trade_date",
                }
            ]
        },
        "execution_plan": {"nodes": []},
        "answer_contract": {"result_query_id": "model-owned"},
        "clarification_options": None,
        "intent": {
            "analysis_type": "event_outcome_probability",
            "metric": None,
            "ranking": None,
            "event_window": {"start": "20260101", "end": "20260601"},
            "event_type": "limit_up",
            "consecutive_sessions": 2,
            "observation_offset": 1,
            "observation_unit": "trading_session",
            "outcomes": ["up", "down"],
            "aggregation": "probability",
        },
    }

    DeepSeekQueryPlanner._normalize_raw_query_defaults(raw_plan)

    assert raw_plan["queries"] == []
    assert raw_plan["result_pipeline"] is None
    assert raw_plan["execution_plan"] is None
    assert raw_plan["answer_contract"] is None
    assert raw_plan["clarification_options"] == []
    assert raw_plan["intent"]["analysis_type"] == "event_outcome_probability"


def test_compiled_event_intent_executes_direction_probabilities():
    intent = AnalysisIntent.model_validate(
        {
            "analysis_type": "event_outcome_probability",
            "event_window": {"start": "20260101", "end": "20260103"},
            "event_type": "limit_up",
            "consecutive_sessions": 2,
            "observation_offset": 1,
            "observation_unit": "trading_session",
            "outcomes": ["up", "down"],
            "aggregation": "probability",
        }
    )
    plan = AnalysisService._compile_intent(
        QueryPlan(interpretation="Compile an event study.", intent=intent)
    )
    prices = QueryResult(
        query_id="event_prices",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": code, "trade_date": trade_date, "close": close}
            for code, values in {
                "A": [("20260101", 10.0), ("20260102", 11.0), ("20260103", 12.0)],
                "B": [("20260101", 10.0), ("20260102", 11.0), ("20260103", 9.0)],
            }.items()
            for trade_date, close in values
        ],
        row_count=6,
    )
    events = QueryResult(
        query_id="event_membership",
        provider="tushare",
        operation="limit_list_d",
        status="success",
        rows=[
            {"ts_code": code, "trade_date": trade_date}
            for code in ("A", "B")
            for trade_date in ("20260101", "20260102")
        ],
        row_count=4,
    )

    result = ResultPipelineExecutor().execute(
        plan.result_pipeline,
        prices,
        {"event_membership": events},
    )

    assert result.rows == [
        {
            "event_count": 2,
            "positive_event_ratio": 0.5,
            "negative_event_ratio": 0.5,
        }
    ]


def test_workflow_compiles_separated_ordinal_outcome_with_both_probabilities():
    plan = make_daily_plan()
    plan.queries[0].fields = ["ts_code", "trade_date", "close"]
    plan.queries.append(
        DataQuery(
            query_id="limit-ups",
            operation="limit_list_d",
            params={},
            fields=["ts_code", "trade_date"],
            purpose="Retrieve limit-up membership.",
        )
    )

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "A股20260101～20260601连续涨停2天的情况下，第三天上涨、下跌的概率",
    )

    assert len(result.result_pipeline.steps) > 3
    offset = next(
        step
        for step in result.result_pipeline.steps
        if step.operation == "match_at_offset"
    )
    assert (offset.offset_value, offset.offset_unit) == (
        1,
        "trading_session",
    )
    summary = result.result_pipeline.steps[-1]
    assert summary.operation == "summarize"
    assert {item.output_field for item in summary.aggregations}.issuperset(
        {"positive_event_ratio", "negative_event_ratio"}
    )


def test_workflow_preserves_planner_selected_limit_up_aggregation():
    plan = make_daily_plan()
    plan.queries[0].fields = ["ts_code", "trade_date", "close"]
    plan.queries.append(
        DataQuery(
            query_id="limit-ups",
            operation="limit_list_d",
            params={},
            fields=["ts_code", "trade_date"],
            purpose="Retrieve limit-up membership.",
        )
    )
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": plan.queries[0].query_id,
            "output_query_id": "average-third-day-return",
            "steps": [
                {
                    "operation": "match_source",
                    "right_source_query_id": "limit-ups",
                    "join_on": ["ts_code", "trade_date"],
                    "output_field": "is_limit_up",
                },
                {
                    "operation": "rolling_sum",
                    "field": "is_limit_up",
                    "output_field": "streak_count",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "window": 2,
                },
                {
                    "operation": "match_at_offset",
                    "field": "close",
                    "output_field": "future_close",
                    "matched_date_output_field": "future_trade_date",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "offset_value": 1,
                    "offset_unit": "trading_session",
                },
                {
                    "operation": "filter",
                    "field": "streak_count",
                    "comparison": "eq",
                    "value": 2,
                },
                {"operation": "drop_missing", "fields": ["future_close"]},
                {
                    "operation": "derive",
                    "field": "future_close",
                    "right_field": "close",
                    "output_field": "return_ratio",
                    "arithmetic_operator": "divide",
                },
                {
                    "operation": "derive",
                    "field": "return_ratio",
                    "output_field": "return_value",
                    "arithmetic_operator": "subtract",
                    "value": 1,
                },
                {
                    "operation": "summarize",
                    "aggregations": [
                        {
                            "output_field": "average_return",
                            "field": "return_value",
                            "function": "mean",
                        }
                    ],
                },
            ],
        }
    )
    plan.answer_contract = AnswerContract.model_validate(
        {
            "result_query_id": "average-third-day-return",
            "result_kind": "summary",
            "outputs": [
                {
                    "field": "average_return",
                    "description": "Mean return after the streak, expressed as a ratio.",
                }
            ],
        }
    )

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "过去一个月连续涨停2天条件成立后的表现均值",
    )

    assert result.result_pipeline.output_query_id == "average-third-day-return"
    summary = result.result_pipeline.steps[-1]
    assert summary.operation == "summarize"
    assert [item.output_field for item in summary.aggregations] == [
        "average_return"
    ]
    rolling = result.result_pipeline.steps[1]
    assert rolling.min_periods == 2
    assert rolling.require_consecutive is True


def test_validator_rejects_a_missing_answer_contract_output():
    plan = make_daily_plan()
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "market_direction",
            "output_query_id": "market-summary",
            "steps": [
                {
                    "operation": "summarize",
                    "aggregations": [
                        {
                            "output_field": "row_count",
                            "field": "change",
                            "function": "count",
                        }
                    ],
                }
            ],
        }
    )
    plan.answer_contract = AnswerContract.model_validate(
        {
            "result_query_id": "market-summary",
            "result_kind": "summary",
            "outputs": [
                {
                    "field": "average_change",
                    "description": "Mean daily price change.",
                }
            ],
        }
    )

    with pytest.raises(
        PlanValidationError,
        match="missing fields: average_change",
    ):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_workflow_compiles_market_period_return_at_security_grain():
    plan = make_daily_plan()
    plan.queries[0].params = {
        "start_date": "20260601",
        "end_date": "20260630",
    }

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "\u5927A\u57286\u6708\u4e0a\u6da8\u6700\u591a\u7684\u80a1\u7968\u524d\u5341",
    )

    assert result.queries[0].transform == "period_return_by_ts_code"
    assert result.result_pipeline.source_query_id == plan.queries[0].query_id
    assert result.result_pipeline.steps[0].field == "period_return_pct"


def test_workflow_compiles_intent_and_aligns_answer_contract_result():
    plan = make_daily_plan()
    plan.queries = []
    plan.result_pipeline = None
    plan.intent = AnalysisIntent.model_validate(
        {
            "metric": {
                "window": {"start": "20260601", "end": "20260630"},
            },
            "ranking": {"direction": "desc", "limit": 10},
        }
    )
    plan.answer_contract = AnswerContract.model_validate(
        {
            "result_query_id": "model_proposed_period_ranking",
            "result_kind": "table",
            "outputs": [
                {
                    "field": "period_return_pct",
                    "description": "Security return over the requested period.",
                }
            ],
        }
    )

    provider = FakeMarketDataProvider()
    service = AnalysisService(
        Mock(),
        provider,
        ASharePlanValidator(provider),
        DataQueryExecutor(provider),
    )

    result = service._compile_intent(plan)

    assert result.result_pipeline.output_query_id == "period_return_output"
    assert result.answer_contract.result_query_id == "period_return_output"
    ASharePlanValidator(FakeMarketDataProvider()).validate(result)


def test_workflow_compiles_limit_up_count_ranking_with_period_returns():
    class LimitUpProvider(FakeMarketDataProvider):
        def supports(self, operation):
            return operation in {"daily", "limit_list_d"}

    plan = make_daily_plan()
    plan.queries = []
    plan.result_pipeline = None

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "A\u80a12025\u5e74\u6da8\u505c\u6700\u591a\u7684\u516c\u53f8\u662ftop3\uff0c\u5e76\u628a\u5168\u5e74\u6da8\u5e45\u5217\u4e00\u4e0b",
    )

    assert [query.operation for query in result.queries] == [
        "limit_list_d",
        "daily",
    ]
    assert result.queries[0].params == {
        "start_date": "20250101",
        "end_date": "20251231",
        "limit_type": "U",
    }
    assert result.queries[1].transform == "period_return_by_ts_code"
    assert [step.operation for step in result.result_pipeline.steps] == [
        "aggregate",
        "sort",
        "limit",
        "join_fields",
    ]
    assert result.result_pipeline.steps[2].count == 3
    assert result.answer_contract.result_query_id == (
        result.result_pipeline.output_query_id
    )
    assert {output.field for output in result.answer_contract.outputs} == {
        "ts_code",
        "name",
        "limit_up_count",
        "period_return_pct",
    }
    ASharePlanValidator(LimitUpProvider()).validate(result)

    event_rows = [
        {"ts_code": "000001.SZ", "name": "Alpha", "trade_date": "20250102"},
        {"ts_code": "000001.SZ", "name": "Alpha", "trade_date": "20250103"},
        {"ts_code": "000002.SZ", "name": "Beta", "trade_date": "20250102"},
        {"ts_code": "000002.SZ", "name": "Beta", "trade_date": "20250103"},
        {"ts_code": "000002.SZ", "name": "Beta", "trade_date": "20250106"},
        {"ts_code": "600001.SH", "name": "Gamma", "trade_date": "20250102"},
        {"ts_code": "600002.SH", "name": "Delta", "trade_date": "20250102"},
        {"ts_code": "600002.SH", "name": "Delta", "trade_date": "20250103"},
        {"ts_code": "600002.SH", "name": "Delta", "trade_date": "20250106"},
        {"ts_code": "600002.SH", "name": "Delta", "trade_date": "20250107"},
    ]
    event_result = QueryResult(
        query_id="period_limit_up_events",
        provider="tushare",
        operation="limit_list_d",
        status="success",
        rows=event_rows,
        row_count=len(event_rows),
    )
    return_result = QueryResult(
        query_id="period_security_returns",
        provider="tushare",
        operation="daily",
        status="success",
        rows=[
            {"ts_code": "000001.SZ", "name": "Alpha", "period_return_pct": 12.5},
            {"ts_code": "000002.SZ", "name": "Beta", "period_return_pct": -3.0},
            {"ts_code": "600001.SH", "name": "Gamma", "period_return_pct": 8.0},
            {"ts_code": "600002.SH", "name": "Delta", "period_return_pct": 25.0},
        ],
        row_count=4,
    )

    pipeline_result = ResultPipelineExecutor().execute(
        result.result_pipeline,
        event_result,
        {return_result.query_id: return_result},
    )

    assert [row["ts_code"] for row in pipeline_result.rows] == [
        "600002.SH",
        "000002.SZ",
        "000001.SZ",
    ]
    assert pipeline_result.rows[0] == {
        "ts_code": "600002.SH",
        "limit_up_count": 4,
        "name": "Delta",
        "period_return_pct": 25.0,
    }


def test_workflow_compiles_valuation_selection_before_period_return_join():
    plan = make_daily_plan()
    plan.queries[0].operation = "daily_basic"
    plan.queries[0].fields = ["ts_code", "pe"]
    plan.queries.append(
        DataQuery(
            query_id="period-prices",
            operation="daily",
            params={"start_date": "20260701", "end_date": "20260731"},
            fields=["ts_code", "trade_date", "close"],
            purpose="Retrieve period prices.",
        )
    )

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "\u9ad8PE\u7684\u524d20\u53ea\u80a1\u7968\u6700\u8fd1\u4e00\u4e2a\u6708\u6da8\u4e86\u591a\u5c11",
    )

    assert result.queries[1].transform == "period_return_by_ts_code"
    join_step = result.result_pipeline.steps[-1]
    assert join_step.operation == "join_fields"
    assert join_step.join_on == ["ts_code"]
    assert join_step.cardinality == "many_to_one"
    assert result.answer_contract.result_query_id == "valuation_period_return"
    assert {output.field for output in result.answer_contract.outputs} == {
        "ts_code",
        "pe",
        "period_return_pct",
    }


def test_workflow_uses_daily_volume_and_joins_same_day_turnover():
    plan = make_daily_plan()
    plan.queries[0].operation = "daily_basic"
    plan.queries[0].fields = ["ts_code", "trade_date", "turnover_rate"]
    plan.queries.append(
        DataQuery(
            query_id="daily-volume",
            operation="daily",
            params={"trade_date": "20260806"},
            fields=["ts_code", "trade_date", "vol"],
            purpose="Retrieve daily volume.",
        )
    )

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "\u5217\u51fa\u4eca\u65e5\u6210\u4ea4\u91cf\u548c\u6362\u624b\u6700\u6d3b\u8dc3\u7684\u80a1\u7968",
    )

    assert result.result_pipeline.source_query_id == "daily-volume"
    sort_step, limit_step, join_step = result.result_pipeline.steps
    assert sort_step.field == "vol"
    assert limit_step.operation == "limit"
    assert join_step.join_on == ["ts_code", "trade_date"]
    assert join_step.cardinality == "one_to_one"
    assert result.answer_contract.result_query_id == "volume_turnover_ranking"
    assert {output.field for output in result.answer_contract.outputs} == {
        "ts_code",
        "trade_date",
        "vol",
        "turnover_rate",
    }


def test_composed_result_supports_filter_join_filter_and_second_join():
    plan = make_daily_plan()
    valuation_query = DataQuery(
        query_id="valuation-enrichment",
        operation="daily_basic",
        params={"trade_date": "20260806"},
        fields=["ts_code", "pe"],
        purpose="Retrieve valuation enrichment.",
    )
    turnover_query = DataQuery(
        query_id="turnover-enrichment",
        operation="daily_basic",
        params={"trade_date": "20260806"},
        fields=["ts_code", "turnover_rate"],
        purpose="Retrieve turnover enrichment.",
    )
    plan.queries.extend((valuation_query, turnover_query))

    AnalysisService._compile_composed_result(
        plan,
        source_query=plan.queries[0],
        output_query_id="filtered-multi-stage-result",
        steps=[
            {
                "operation": "filter",
                "field": "change",
                "comparison": "gt",
                "value": 0,
            },
            {
                "operation": "join_fields",
                "right_source_query_id": valuation_query.query_id,
                "join_on": ["ts_code"],
                "fields": {"pe": "pe"},
                "cardinality": "many_to_one",
            },
            {
                "operation": "filter",
                "field": "pe",
                "comparison": "lt",
                "value": 20,
            },
            {
                "operation": "join_fields",
                "right_source_query_id": turnover_query.query_id,
                "join_on": ["ts_code"],
                "fields": {"turnover_rate": "turnover_rate"},
                "cardinality": "many_to_one",
            },
            {
                "operation": "filter",
                "field": "turnover_rate",
                "comparison": "gt",
                "value": 1,
            },
            {"operation": "sort", "field": "change", "direction": "desc"},
            {"operation": "limit", "count": 10},
        ],
        output_descriptions={
            "ts_code": "A-share security code.",
            "change": "Price change used for ranking.",
            "pe": "Price-to-earnings ratio used for filtering.",
            "turnover_rate": "Turnover rate used for filtering.",
        },
    )

    validated = ASharePlanValidator(FakeMarketDataProvider()).validate(plan)

    assert [step.operation for step in validated.result_pipeline.steps] == [
        "filter",
        "join_fields",
        "filter",
        "join_fields",
        "filter",
        "sort",
        "limit",
    ]
    assert validated.answer_contract.result_query_id == "filtered-multi-stage-result"


def test_validator_allows_join_before_selection_when_enrichment_is_ranked():
    plan = make_daily_plan()
    plan.queries.append(
        DataQuery(
            query_id="ranking-metric",
            operation="daily_basic",
            params={"trade_date": "20260806"},
            fields=["ts_code", "turnover_rate"],
            purpose="Retrieve the metric that defines the ranking.",
        )
    )
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": plan.queries[0].query_id,
            "output_query_id": "joined-metric-ranking",
            "steps": [
                {
                    "operation": "join_fields",
                    "right_source_query_id": "ranking-metric",
                    "join_on": ["ts_code"],
                    "fields": {"turnover_rate": "turnover_rate"},
                    "cardinality": "many_to_one",
                },
                {
                    "operation": "sort",
                    "field": "turnover_rate",
                    "direction": "desc",
                },
                {"operation": "limit", "count": 10},
            ],
        }
    )

    validated = ASharePlanValidator(FakeMarketDataProvider()).validate(plan)

    assert validated.result_pipeline.output_query_id == "joined-metric-ranking"


def test_composed_result_compiler_supports_multiple_metrics():
    plan = make_daily_plan()
    valuation_query = DataQuery(
        query_id="valuation-metric",
        operation="daily_basic",
        params={"trade_date": "20260806"},
        fields=["ts_code", "pe"],
        purpose="Retrieve valuation enrichment.",
    )
    turnover_query = DataQuery(
        query_id="turnover-metric",
        operation="daily_basic",
        params={"trade_date": "20260806"},
        fields=["ts_code", "turnover_rate"],
        purpose="Retrieve turnover enrichment.",
    )
    plan.queries.extend((valuation_query, turnover_query))

    AnalysisService._compile_composed_result(
        plan,
        source_query=plan.queries[0],
        output_query_id="multi-metric-ranking",
        steps=[
            {"operation": "sort", "field": "change", "direction": "desc"},
            {"operation": "limit", "count": 5},
            {
                "operation": "join_fields",
                "right_source_query_id": valuation_query.query_id,
                "join_on": ["ts_code"],
                "fields": {"pe": "pe"},
                "cardinality": "many_to_one",
            },
            {
                "operation": "join_fields",
                "right_source_query_id": turnover_query.query_id,
                "join_on": ["ts_code"],
                "fields": {"turnover_rate": "turnover_rate"},
                "cardinality": "many_to_one",
            },
        ],
        output_descriptions={
            "ts_code": "A-share security code.",
            "change": "Price change used for ranking.",
            "pe": "Price-to-earnings ratio.",
            "turnover_rate": "Turnover rate in percent.",
        },
    )

    validated = ASharePlanValidator(FakeMarketDataProvider()).validate(plan)

    assert [step.operation for step in validated.result_pipeline.steps] == [
        "sort",
        "limit",
        "join_fields",
        "join_fields",
    ]
    assert validated.answer_contract.result_query_id == "multi-metric-ranking"
    assert {output.field for output in validated.answer_contract.outputs} == {
        "ts_code",
        "change",
        "pe",
        "turnover_rate",
    }


def test_execution_dag_drives_queries_from_intermediate_candidates():
    class DagProvider:
        name = "tushare"

        def __init__(self):
            self.calls = []

        def supports(self, operation):
            return operation in {"daily", "daily_basic", "top10_floatholders"}

        def search_operations(self, prompt):
            return []

        def query(
            self,
            operation,
            params,
            fields,
            *,
            api_route,
            request_id,
            query_id,
        ):
            self.calls.append((operation, dict(params)))
            if operation == "daily":
                return pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "change": 2.0},
                        {"ts_code": "000002.SZ", "change": 1.0},
                        {"ts_code": "600001.SH", "change": -1.0},
                    ]
                )
            if operation == "daily_basic":
                pe = {"000001.SZ": 12.0, "000002.SZ": 28.0}[params["ts_code"]]
                return pd.DataFrame([{"pe": pe}])
            return pd.DataFrame([{"hold_ratio": 10.0}, {"hold_ratio": 20.0}])

    plan = QueryPlan(
        interpretation="Filter candidates, query valuation, filter again, and summarize holders.",
        requirements=[
            {
                "requirement": "Apply sequential candidate-driven analysis.",
                "status": "covered",
                "implementation": "Execute a validated dependency graph.",
                "evidence": "Every query and compute node declares its dependencies.",
            }
        ],
        answer_contract=AnswerContract.model_validate(
            {
                "result_query_id": "holder-summary",
                "result_kind": "table",
                "outputs": [
                    {
                        "field": "ts_code",
                        "description": "A-share security code.",
                    },
                    {
                        "field": "average_hold_ratio",
                        "description": "Mean disclosed holder ratio in percent.",
                    },
                ],
            }
        ),
        execution_plan=ExecutionPlan(
            result_node_id="holder-summary",
            nodes=[
                ExecutionNode(
                    node_id="holder-summary",
                    kind="compute",
                    input_result_ids=["holder-details"],
                    step=ResultPipelineStep.model_validate(
                        {
                            "operation": "aggregate",
                            "group_by": ["ts_code"],
                            "aggregations": [
                                {
                                    "output_field": "average_hold_ratio",
                                    "field": "hold_ratio",
                                    "function": "mean",
                                }
                            ],
                        }
                    ),
                ),
                ExecutionNode(
                    node_id="market",
                    kind="query",
                    query=DataQuery(
                        query_id="market",
                        operation="daily",
                        params={"trade_date": "20260717"},
                        fields=["ts_code", "change"],
                        purpose="Retrieve the initial market candidates.",
                    ),
                ),
                ExecutionNode(
                    node_id="positive-candidates",
                    kind="compute",
                    input_result_ids=["market"],
                    step=ResultPipelineStep(
                        operation="filter",
                        field="change",
                        comparison="gt",
                        value=0,
                    ),
                ),
                ExecutionNode(
                    node_id="valuations",
                    kind="query",
                    input_result_ids=["positive-candidates"],
                    query=DataQuery(
                        query_id="valuations",
                        operation="daily_basic",
                        fields=["pe"],
                        purpose="Retrieve valuation for each surviving candidate.",
                    ),
                    fanout_input_field="ts_code",
                    fanout_param="ts_code",
                ),
                ExecutionNode(
                    node_id="low-pe-candidates",
                    kind="compute",
                    input_result_ids=["valuations"],
                    step=ResultPipelineStep(
                        operation="filter",
                        field="pe",
                        comparison="lt",
                        value=20,
                    ),
                ),
                ExecutionNode(
                    node_id="holder-details",
                    kind="query",
                    input_result_ids=["low-pe-candidates"],
                    query=DataQuery(
                        query_id="holder-details",
                        operation="top10_floatholders",
                        fields=["hold_ratio"],
                        purpose="Retrieve holder rows for the filtered candidates.",
                    ),
                    fanout_input_field="ts_code",
                    fanout_param="ts_code",
                ),
            ],
        ),
    )
    provider = DagProvider()

    class StaticDagPlanner:
        name = "static-dag"

        def plan(self, request, candidate_operations):
            return plan

    service = AnalysisService(
        planner=StaticDagPlanner(),
        provider=provider,
        validator=ASharePlanValidator(provider),
        executor=DataQueryExecutor(provider),
    )

    response = service.analyze(
        "dag-request",
        AnalysisRequest(prompt="Run the multi-stage analysis."),
        api_route="/api/analysis",
        progress_callback=lambda completed, total: None,
    )

    assert response.status == "success", response.model_dump()
    assert response.results[0].query_id == "holder-summary"
    assert response.results[0].rows == [
        {"ts_code": "000001.SZ", "average_hold_ratio": 15.0}
    ]
    assert [call for call in provider.calls if call[0] == "daily_basic"] == [
        ("daily_basic", {"ts_code": "000001.SZ"}),
        ("daily_basic", {"ts_code": "000002.SZ"}),
    ]
    assert [call for call in provider.calls if call[0] == "top10_floatholders"] == [
        ("top10_floatholders", {"ts_code": "000001.SZ"})
    ]


def test_execution_dag_rejects_cycles_before_provider_calls():
    plan = QueryPlan(
        interpretation="Reject cyclic execution dependencies.",
        requirements=[
            {
                "requirement": "Validate graph topology.",
                "status": "covered",
                "implementation": "Run deterministic dependency validation.",
                "evidence": "Execution nodes declare every upstream result.",
            }
        ],
        execution_plan=ExecutionPlan(
            result_node_id="node-a",
            nodes=[
                ExecutionNode(
                    node_id="node-a",
                    kind="compute",
                    input_result_ids=["node-b"],
                    step=ResultPipelineStep(
                        operation="filter",
                        field="value",
                        comparison="gt",
                        value=0,
                    ),
                ),
                ExecutionNode(
                    node_id="node-b",
                    kind="compute",
                    input_result_ids=["node-a"],
                    step=ResultPipelineStep(
                        operation="filter",
                        field="value",
                        comparison="lt",
                        value=10,
                    ),
                ),
            ],
        ),
    )

    with pytest.raises(PlanValidationError, match="dependency cycle"):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_execution_dag_rejects_unknown_dependencies():
    plan = QueryPlan(
        interpretation="Reject an unknown execution dependency.",
        requirements=[
            {
                "requirement": "Validate graph dependencies.",
                "status": "covered",
                "implementation": "Resolve every declared upstream result.",
                "evidence": "Unknown node identifiers are not executable.",
            }
        ],
        execution_plan=ExecutionPlan(
            result_node_id="filtered",
            nodes=[
                ExecutionNode(
                    node_id="filtered",
                    kind="compute",
                    input_result_ids=["missing-source"],
                    step=ResultPipelineStep(
                        operation="filter",
                        field="value",
                        comparison="gt",
                        value=0,
                    ),
                )
            ],
        ),
    )

    with pytest.raises(PlanValidationError, match="unknown inputs: missing-source"):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_workflow_resolves_security_code_adjacent_to_chinese_text():
    plan = make_daily_plan()
    plan.queries[0].operation = "income"

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "\u67e5\u770b600519.SH\u8fd1\u4e09\u5e74\u4e3b\u8425\u4e1a\u52a1\u6bdb\u5229\u7387\u53d8\u5316",
    )

    assert result.queries[0].operation == "fina_mainbz"
    assert result.queries[0].params == {"ts_code": "600519.SH"}


def test_workflow_rejects_market_wide_forecast_period_query():
    plan = make_daily_plan()
    plan.queries[0].operation = "forecast"
    plan.queries[0].params = {"period": "20260630"}

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "\u7edf\u8ba12026\u5e74\u4e0a\u534a\u5e74\u9884\u4e8f\u516c\u53f8\u6570\u91cf",
    )

    assert result.feasibility == "unsupported"
    assert result.queries == []


def test_deepseek_drops_non_numeric_query_aggregation_thresholds():
    raw_plan = {
        "queries": [
            {
                "aggregations": [
                    {
                        "label": "row_count",
                        "field": "ts_code",
                        "operator": "eq",
                        "value": "not_null",
                    },
                    {
                        "label": "invalid_count_operator",
                        "field": "ts_code",
                        "operator": "count",
                        "value": 1,
                    },
                ]
            }
        ]
    }

    DeepSeekQueryPlanner._normalize_raw_query_defaults(raw_plan)

    assert raw_plan["queries"][0]["aggregations"] == []


def test_workflow_builds_daily_basic_query_when_model_omits_it():
    plan = make_daily_plan()
    plan.interpretation = "Screen valuations on 2026-08-07."

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "Find A-shares with PE below 15 and PB below 2 on the latest trading day.",
    )

    assert result.queries[0].operation == "daily_basic"
    assert result.queries[0].params == {"trade_date": "20260807"}


def test_workflow_removes_non_native_suspension_fields_and_pipeline():
    plan = make_daily_plan()
    plan.queries[0].operation = "suspend_d"
    plan.queries[0].params = {
        "trade_date": "20260508",
        "resume_date": "20260508",
    }
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": plan.queries[0].query_id,
            "output_query_id": "resumed",
            "steps": [{"operation": "drop_missing", "fields": ["resume_date"]}],
        }
    )

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "Which stocks resumed trading on 2026-05-08?",
    )

    assert "resume_date" not in result.queries[0].params
    assert "resume_date" not in result.queries[0].fields
    assert result.result_pipeline is None


def test_latest_completed_date_uses_the_provider_trade_calendar():
    class CalendarProvider(FakeMarketDataProvider):
        def supports(self, operation):
            return operation == "trade_cal"

    provider = CalendarProvider(
        frame=pd.DataFrame(
            [
                {"cal_date": "20260930", "is_open": 1},
                {"cal_date": "20261009", "is_open": 1},
            ]
        )
    )
    service = AnalysisService(
        planner=None,
        provider=provider,
        validator=ASharePlanValidator(provider),
        executor=DataQueryExecutor(provider),
    )

    result = service._latest_completed_trading_date(
        "request-1",
        datetime(2026, 10, 10, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result == date(2026, 10, 9)
    assert provider.calls[0] == "trade_cal"


@pytest.mark.parametrize(
    ("operation", "params", "evidence"),
    [
        (
            "top_list",
            {"start_date": "20260601", "end_date": "20260630"},
            "The operation only supports one trading date.",
        ),
        (
            "limit_list_d",
            {"trade_date": "待填充"},
            "The trading date is unresolved.",
        ),
    ],
)
def test_planner_downgrades_known_unexecutable_or_proxy_plan(
    operation,
    params,
    evidence,
):
    plan = make_daily_plan()
    plan.queries[0].operation = operation
    plan.queries[0].params = params
    plan.requirements[0].evidence = evidence
    session = FakeSession(
        FakeResponse({"choices": [{"message": {"content": plan.model_dump_json()}}]})
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="Run the requested screen."),
        [DataOperation(name=operation, description="Provider operation.")],
    )

    assert result.feasibility == "unsupported"
    assert result.queries == []
    assert result.requirements[0].status == "unsupported"
    assert result.limitations


def test_planner_accepts_fixed_non_top10_float_retail_proxy():
    plan = make_daily_plan()
    plan.queries[0].operation = "top10_floatholders"
    plan.queries[0].params = {
        "ts_code": "300308.SZ",
        "period": "20260331",
    }
    plan.queries[0].fields = [
        "ts_code",
        "ann_date",
        "end_date",
        "holder_name",
        "hold_float_ratio",
    ]
    plan.queries[0].transform = "cr10_float_trend"
    plan.requirements[0].implementation = (
        "Use non_top10_float_ratio as the approved retail-ratio proxy."
    )
    plan.requirements[0].evidence = (
        "The proxy equals 100% minus the top-ten unrestricted float-holder ratios."
    )
    session = FakeSession(
        FakeResponse({"choices": [{"message": {"content": plan.model_dump_json()}}]})
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="分析这只股票的散户比例。"),
        [
            DataOperation(
                name="top10_floatholders",
                description="Float-holder snapshots.",
            )
        ],
    )

    assert result.feasibility == "supported"
    assert result.queries[0].transform == "cr10_float_trend"
    assert len(result.limitations) == 1
    assert "non_top10_float_ratio" in result.limitations[0]


def test_planner_preserves_universe_query_for_generic_retail_ranking_pipeline():
    plan = QueryPlan(
        interpretation="Rank the full A-share market by the retail proxy.",
        requirements=[
            {
                "requirement": "Return the ten highest retail proxy values.",
                "status": "covered",
                "implementation": "Use local sorting and take the top ten.",
                "evidence": "A validated result pipeline performs the ranking.",
            }
        ],
        queries=[
            DataQuery(
                query_id="universe",
                operation="stock_basic",
                fields=["ts_code"],
                purpose="Retrieve the A-share universe.",
            ),
            DataQuery(
                query_id="retail-proxy",
                operation="top10_floatholders",
                fields=[
                    "ts_code",
                    "ann_date",
                    "end_date",
                    "holder_name",
                    "hold_float_ratio",
                ],
                purpose="Calculate the retail holding proxy for each security.",
                transform="cr10_float_trend",
            ),
        ],
        result_pipeline={
            "source_query_id": "retail-proxy",
            "output_query_id": "top-retail-proxy",
            "steps": [
                {
                    "operation": "latest_by_group",
                    "group_by": ["ts_code"],
                    "order_by": "end_date",
                },
                {
                    "operation": "drop_missing",
                    "fields": ["non_top10_float_ratio"],
                },
                {
                    "operation": "sort",
                    "field": "non_top10_float_ratio",
                    "direction": "desc",
                },
                {"operation": "limit", "count": 10},
            ],
        },
    )
    session = FakeSession(
        FakeResponse({"choices": [{"message": {"content": plan.model_dump_json()}}]})
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="查找散户比例最高的10只A股股票。"),
        [
            DataOperation(name="stock_basic", description="A-share universe."),
            DataOperation(
                name="top10_floatholders",
                description="Float-holder snapshots.",
            ),
        ],
    )

    assert [query.operation for query in result.queries] == [
        "stock_basic",
        "top10_floatholders",
    ]
    assert result.result_pipeline.output_query_id == "top-retail-proxy"
    assert len(session.calls) == 1
    validated = ASharePlanValidator(
        FakeMarketDataProvider(stock_frame=pd.DataFrame())
    ).validate(result)
    assert validated.feasibility == "supported"


def test_validator_rejects_result_pipeline_field_before_provider_execution():
    plan = make_daily_plan()
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "market_direction",
            "output_query_id": "invalid",
            "steps": [{"operation": "sort", "field": "missing"}],
        }
    )

    with pytest.raises(
        PlanValidationError,
        match="sort references unavailable fields: missing",
    ):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_validator_accepts_join_fields_mapping_lineage():
    plan = make_daily_plan()
    plan.queries.append(
        DataQuery(
            query_id="ending-prices",
            operation="daily",
            params={"trade_date": "20260718"},
            fields=["ts_code", "close"],
            purpose="Fetch ending prices.",
        )
    )
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "market_direction",
            "output_query_id": "joined-prices",
            "steps": [
                {
                    "operation": "join_fields",
                    "right_source_query_id": "ending-prices",
                    "join_on": ["ts_code"],
                    "fields": {"close": "ending_close"},
                    "cardinality": "one_to_one",
                }
            ],
        }
    )

    validated = ASharePlanValidator(FakeMarketDataProvider()).validate(plan)

    assert validated.result_pipeline.steps[0].fields == {"close": "ending_close"}


def test_validator_rejects_existing_membership_output_field():
    plan = make_daily_plan()
    plan.queries.append(
        DataQuery(
            query_id="matching-prices",
            operation="daily",
            params={"trade_date": "20260718"},
            fields=["ts_code"],
            purpose="Identify matching securities.",
        )
    )
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "market_direction",
            "output_query_id": "matched-prices",
            "steps": [
                {
                    "operation": "match_source",
                    "right_source_query_id": "matching-prices",
                    "join_on": ["ts_code"],
                    "output_field": "change",
                }
            ],
        }
    )

    with pytest.raises(
        PlanValidationError,
        match="match_source output field already exists: change",
    ):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_contract_rejects_duplicate_match_at_offset_output_fields():
    with pytest.raises(
        ValueError,
        match="value and matched-date output fields must differ",
    ):
        ResultPipelineStep.model_validate(
            {
                "operation": "match_at_offset",
                "field": "close",
                "output_field": "future_value",
                "matched_date_output_field": "future_value",
                "group_by": ["ts_code"],
                "order_by": "trade_date",
                "offset_value": 1,
                "offset_unit": "month",
            }
        )


def test_validator_uses_transformed_result_fields_for_pipeline_lineage():
    plan = make_daily_plan()
    plan.queries[0].transform = "period_return_by_ts_code"
    plan.queries[0].fields = ["trade_date", "ts_code", "close"]
    plan.queries[0].aggregations = []
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "market_direction",
            "output_query_id": "ranked-counts",
            "steps": [{"operation": "sort", "field": "period_return_pct"}],
        }
    )

    validated = ASharePlanValidator(FakeMarketDataProvider()).validate(plan)
    assert validated.result_pipeline.steps[0].field == "period_return_pct"

    plan.result_pipeline.steps[0].field = "close"
    with pytest.raises(
        PlanValidationError,
        match="sort references unavailable fields: close",
    ):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_validator_rejects_security_fanout_template_without_universe():
    plan = QueryPlan(
        interpretation="Rank the full market by the retail proxy.",
        requirements=[
            {
                "requirement": "Rank all A-shares by the retail proxy.",
                "status": "covered",
                "implementation": "Fan out top10_floatholders and sort locally.",
                "evidence": "The holder operation supports one security per call.",
            }
        ],
        queries=[
            DataQuery(
                query_id="retail-proxy",
                operation="top10_floatholders",
                params={"end_date": "20260727"},
                fields=[
                    "ts_code",
                    "ann_date",
                    "end_date",
                    "holder_name",
                    "hold_float_ratio",
                ],
                purpose="Calculate the retail proxy.",
                transform="cr10_float_trend",
            )
        ],
        result_pipeline={
            "source_query_id": "retail-proxy",
            "output_query_id": "ranked-retail-proxy",
            "steps": [
                {
                    "operation": "sort",
                    "field": "non_top10_float_ratio",
                    "direction": "desc",
                },
                {"operation": "limit", "count": 10},
            ],
        },
    )

    with pytest.raises(
        PlanValidationError,
        match="Security fan-out templates require.*universe query",
    ):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_analysis_returns_structured_error_when_result_pipeline_fails():
    plan = QueryPlan(
        interpretation="Run one invalid arithmetic pipeline.",
        requirements=[
            {
                "requirement": "Calculate a derived value.",
                "status": "covered",
                "implementation": "Use a deterministic result pipeline.",
                "evidence": "The pipeline contract defines scalar division.",
            }
        ],
        queries=[
            DataQuery(
                query_id="source",
                operation="daily",
                params={"trade_date": "20260717"},
                fields=["value"],
                purpose="Retrieve one numeric value.",
            )
        ],
        result_pipeline={
            "source_query_id": "source",
            "output_query_id": "derived",
            "steps": [
                {
                    "operation": "sort",
                    "field": "value",
                }
            ],
        },
    )

    class StaticPlanner:
        name = "static"

        def plan(self, request, candidate_operations):
            return plan

    provider = FakeMarketDataProvider(frame=pd.DataFrame([{"value": 1.0}]))

    class FailingPipelineExecutor:
        def execute(self, pipeline, source, sources=None):
            raise ValueError("pipeline execution failed")

    service = AnalysisService(
        planner=StaticPlanner(),
        provider=provider,
        validator=ASharePlanValidator(provider),
        executor=DataQueryExecutor(provider),
        result_pipeline_executor=FailingPipelineExecutor(),
    )

    response = service.analyze(
        "request-1",
        AnalysisRequest(prompt="Calculate the ratio."),
        api_route="/api/analysis",
    )

    assert response.status == "error"
    assert response.results[0].query_id == "derived"
    assert response.results[0].status == "error"
    assert response.results[0].error.message == "pipeline execution failed"


def test_analysis_rejects_synchronous_security_fanout_before_provider_calls():
    plan = QueryPlan(
        interpretation="Rank the full market by the retail proxy.",
        requirements=[
            {
                "requirement": "Rank listed A-shares by the retail proxy.",
                "status": "covered",
                "implementation": (
                    "Fan out top10_floatholders over the stock_basic universe."
                ),
                "evidence": (
                    "The worker supports durable per-security fan-out."
                ),
            }
        ],
        queries=[
            DataQuery(
                query_id="universe",
                operation="stock_basic",
                params={"list_status": "L"},
                fields=["ts_code"],
                purpose="Retrieve listed A-shares.",
            ),
            DataQuery(
                query_id="retail-proxy",
                operation="top10_floatholders",
                fields=[
                    "ts_code",
                    "ann_date",
                    "end_date",
                    "holder_name",
                    "hold_float_ratio",
                ],
                purpose="Calculate the retail proxy per security.",
                transform="cr10_float_trend",
            ),
        ],
        result_pipeline={
            "source_query_id": "retail-proxy",
            "output_query_id": "ranked-retail-proxy",
            "steps": [
                {
                    "operation": "drop_missing",
                    "fields": ["non_top10_float_ratio"],
                },
                {
                    "operation": "sort",
                    "field": "non_top10_float_ratio",
                    "direction": "desc",
                },
                {"operation": "limit", "count": 10},
            ],
        },
    )

    class StaticPlanner:
        name = "static"

        def plan(self, request, candidate_operations):
            return plan

    provider = FakeMarketDataProvider(
        stock_frame=pd.DataFrame(columns=["ts_code"])
    )
    service = AnalysisService(
        planner=StaticPlanner(),
        provider=provider,
        validator=ASharePlanValidator(provider),
        executor=DataQueryExecutor(provider),
    )

    response = service.analyze(
        "request-fanout",
        AnalysisRequest(prompt="找到散户比例top10的股票"),
        api_route="/api/analysis",
    )

    assert response.status == "error"
    assert response.error.source == "system"
    assert "requires a background task" in response.error.message
    assert response.decision_trace[-2].status == "skipped"
    assert provider.calls == []


def test_executor_applies_exact_string_filter():
    frame = pd.DataFrame(
        [
            {"ts_code": "000001.SZ", "limit_type": "U"},
            {"ts_code": "600000.SH", "limit_type": "D"},
        ]
    )
    query = DataQuery(
        query_id="limit-ups",
        operation="daily",
        fields=["ts_code", "limit_type"],
        purpose="Keep only limit-up rows.",
        filters=[
            {
                "field": "limit_type",
                "operator": "eq",
                "value": "U",
            }
        ],
    )

    result = DataQueryExecutor(FakeMarketDataProvider(frame=frame)).execute(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "success"
    assert result.row_count == 1
    assert result.rows == [{"ts_code": "000001.SZ", "limit_type": "U"}]


def test_executor_applies_string_membership_filter():
    frame = pd.DataFrame(
        [
            {"ts_code": "000001.SZ", "industry": "医疗保健"},
            {"ts_code": "000002.SZ", "industry": "化学制药"},
            {"ts_code": "600000.SH", "industry": "银行"},
        ]
    )
    query = DataQuery(
        query_id="healthcare-universe",
        operation="daily",
        fields=["ts_code", "industry"],
        purpose="Build a categorical security universe.",
        filters=[
            {
                "field": "industry",
                "operator": "in",
                "value": ["医疗保健", "化学制药"],
            }
        ],
    )

    result = DataQueryExecutor(FakeMarketDataProvider(frame=frame)).execute(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "success"
    assert result.row_count == 2
    assert [row["ts_code"] for row in result.rows] == [
        "000001.SZ",
        "000002.SZ",
    ]


def test_executor_calculates_period_returns():
    return_frame = pd.DataFrame(
        [
            {"ts_code": "000001.SZ", "trade_date": "20260701", "close": 10},
            {"ts_code": "000001.SZ", "trade_date": "20260702", "close": 12},
        ]
    )
    return_query = DataQuery(
        query_id="period-return",
        operation="daily",
        fields=["ts_code", "trade_date", "close"],
        purpose="Calculate one period return.",
        transform="period_return_by_ts_code",
    )
    return_result = DataQueryExecutor(
        FakeMarketDataProvider(frame=return_frame)
    ).execute(
        return_query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert return_result.rows[0]["period_return_pct"] == 20.0


def test_full_market_period_return_reads_only_boundary_snapshots():
    class BoundaryProvider(FakeMarketDataProvider):
        def supports(self, operation):
            return operation in {"daily", "stock_basic"}

        def query(
            self,
            operation,
            params,
            fields,
            *,
            api_route,
            request_id,
            query_id,
        ):
            self.calls.append((operation, dict(params)))
            if operation == "stock_basic":
                return pd.DataFrame(
                    [
                        {
                            "ts_code": "000001.SZ",
                            "name": "First",
                            "industry": "Bank",
                        },
                        {
                            "ts_code": "000002.SZ",
                            "name": "Second",
                            "industry": "Property",
                        },
                    ]
                )
            closes = {
                "20260601": [10.0, 20.0],
                "20260630": [15.0, 18.0, 8.0],
            }
            values = closes.get(params["trade_date"], [])
            return pd.DataFrame(
                [
                    {
                        "ts_code": code,
                        "trade_date": params["trade_date"],
                        "close": close,
                    }
                    for code, close in zip(
                        ("000001.SZ", "000002.SZ", "000003.SZ"),
                        values,
                    )
                ]
            )

    provider = BoundaryProvider()
    executor = DataQueryExecutor(provider)
    service = AnalysisService(
        planner=None,
        provider=provider,
        validator=ASharePlanValidator(provider),
        executor=executor,
    )
    query = DataQuery(
        query_id="june-return",
        operation="daily",
        params={"start_date": "20260601", "end_date": "20260630"},
        fields=["ts_code", "trade_date", "close"],
        purpose="Calculate full-market June returns.",
        transform="period_return_by_ts_code",
    )

    result = service._execute_full_market_range_by_date(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "success"
    assert result.rows[0] == {
        "ts_code": "000001.SZ",
        "start_date": "20260601",
        "end_date": "20260630",
        "start_close": 10.0,
        "end_close": 15.0,
        "period_return_pct": 50.0,
        "name": "First",
        "industry": "Bank",
    }
    assert {row["ts_code"] for row in result.rows} == {
        "000001.SZ",
        "000002.SZ",
    }
    daily_calls = [call for call in provider.calls if call[0] == "daily"]
    assert daily_calls == [
        ("daily", {"trade_date": "20260601"}),
        ("daily", {"trade_date": "20260630"}),
    ]


def test_planner_normalizes_fields_misplaced_in_params():
    plan = make_daily_plan()
    plan.queries[0].params["fields"] = ["ts_code", "change"]
    plan.queries[0].fields = []
    session = FakeSession(
        FakeResponse({"choices": [{"message": {"content": plan.model_dump_json()}}]})
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="Count stocks."),
        [DataOperation(name="daily", description="Daily prices.")],
    )

    assert result.queries[0].fields == ["ts_code", "change"]
    assert "fields" not in result.queries[0].params


def test_planner_splits_multi_security_float_holder_query():
    query = DataQuery(
        query_id="holders",
        operation="top10_floatholders",
        params={
            "ts_code": "300059.SZ，601012.SH,002594.SZ",
            "end_date": "20260722",
        },
        fields=["ts_code", "holder_name", "hold_float_ratio"],
        purpose="Retrieve the latest disclosed float-holder snapshots.",
        transform="cr10_float_trend",
    )
    plan = QueryPlan(
        interpretation="Compare three securities.",
        requirements=[
            {
                "requirement": "Compare float-holder concentration.",
                "status": "covered",
                "implementation": "Use top10_floatholders.",
                "evidence": "The operation provides disclosed float holders.",
            }
        ],
        queries=[query],
    )
    session = FakeSession(
        FakeResponse({"choices": [{"message": {"content": plan.model_dump_json()}}]})
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="Compare three securities."),
        [DataOperation(name="top10_floatholders", description="Float holders.")],
    )

    assert [item.query_id for item in result.queries] == [
        "holders-1",
        "holders-2",
        "holders-3",
    ]
    assert [item.params["ts_code"] for item in result.queries] == [
        "300059.SZ",
        "601012.SH",
        "002594.SZ",
    ]
    assert all(item.params["end_date"] == "20260722" for item in result.queries)
    assert all(item.transform == "cr10_float_trend" for item in result.queries)


def test_split_float_holder_queries_still_respect_plan_query_limit():
    codes = ",".join(f"{index:06d}.SZ" for index in range(9))
    query = DataQuery(
        query_id="holders",
        operation="top10_floatholders",
        params={"ts_code": codes, "end_date": "20260722"},
        purpose="Retrieve float-holder snapshots.",
    )
    plan = QueryPlan(
        interpretation="Retrieve nine securities.",
        requirements=[
            {
                "requirement": "Retrieve nine securities.",
                "status": "covered",
                "implementation": "Use top10_floatholders.",
                "evidence": "The operation provides disclosed float holders.",
            }
        ],
        queries=[query],
    )
    session = FakeSession(
        FakeResponse({"choices": [{"message": {"content": plan.model_dump_json()}}]})
    )
    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="Retrieve nine securities."),
        [DataOperation(name="top10_floatholders", description="Float holders.")],
    )

    with pytest.raises(PlanValidationError, match="at most 8 calls"):
        ASharePlanValidator(FakeMarketDataProvider()).validate(result)


def test_planner_converts_network_failure_to_displayable_error():
    session = FakeSession(exception=requests.ConnectionError("network unavailable"))

    try:
        DeepSeekQueryPlanner("test-key", session=session).plan(
            AnalysisRequest(prompt="Count stocks."),
            [DataOperation(name="daily", description="Daily prices.")],
        )
    except PlannerError as exc:
        assert "network unavailable" in str(exc)
    else:
        raise AssertionError("Expected a DeepSeek API error.")


def test_executor_computes_controlled_counts():
    frame = pd.DataFrame(
        [{"ts_code": "000001.SZ", "change": 1.2}, {"ts_code": "600000.SH", "change": -0.4}]
    )

    stock_frame = pd.DataFrame(
        [
            {"ts_code": "000001.SZ", "name": "Ping An Bank"},
            {"ts_code": "600000.SH", "name": "SPD Bank"},
        ]
    )
    result = DataQueryExecutor(
        FakeMarketDataProvider(frame=frame, stock_frame=stock_frame)
    ).execute(
        make_daily_plan().queries[0],
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "success"
    assert result.summary == {"Advanced": 1, "Declined": 1}
    assert result.row_count == 2
    assert result.columns == ["ts_code", "name", "change"]
    assert result.rows[0]["name"] == "Ping An Bank"


def test_executor_builds_cr10_float_trend_from_complete_snapshots():
    frame = pd.DataFrame(
        [
            {
                "ts_code": "300308.SZ",
                "ann_date": "20260417",
                "end_date": "20260331",
                "holder_name": f"Holder {index}",
                "hold_float_ratio": 3.5,
            }
            for index in range(10)
        ]
    )
    frame.loc[0, "holder_name"] = "Hong Kong Securities Clearing Company"
    query = DataQuery(
        query_id="cr10-1",
        operation="top10_floatholders",
        params={"ts_code": "300308.SZ", "period": "20260331"},
        fields=list(frame.columns),
        purpose="Build the registered-account float concentration trend.",
        transform="cr10_float_trend",
    )

    result = DataQueryExecutor(FakeMarketDataProvider(frame=frame)).execute(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "success"
    assert result.rows[0]["cr10_float_registered"] == 35.0
    assert result.rows[0]["non_top10_float_ratio"] == 65.0
    assert result.rows[0]["holder_count"] == 10


def test_executor_rejects_incomplete_cr10_float_snapshot():
    frame = pd.DataFrame(
        [
            {
                "ts_code": "300308.SZ",
                "ann_date": "20260417",
                "end_date": "20260331",
                "holder_name": f"Holder {index}",
                "hold_float_ratio": 3.5,
            }
            for index in range(9)
        ]
    )
    query = DataQuery(
        query_id="cr10-1",
        operation="top10_floatholders",
        params={"ts_code": "300308.SZ", "period": "20260331"},
        fields=list(frame.columns),
        purpose="Build the registered-account float concentration trend.",
        transform="cr10_float_trend",
    )

    result = DataQueryExecutor(FakeMarketDataProvider(frame=frame)).execute(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "error"
    assert result.error is not None
    assert "requires 10 unique holders" in result.error.message


def test_validator_rejects_non_quarter_end_float_holder_period():
    plan = QueryPlan(
        interpretation="Retrieve one float-holder snapshot.",
        requirements=[
            {
                "requirement": "Retrieve the requested snapshot.",
                "status": "covered",
                "implementation": "Use top10_floatholders.",
                "evidence": "The operation supports reporting periods.",
            }
        ],
        queries=[
            DataQuery(
                query_id="cr10-invalid-period",
                operation="top10_floatholders",
                params={"ts_code": "002594.SZ", "period": "20260701"},
                purpose="Retrieve the requested snapshot.",
            )
        ],
    )

    with pytest.raises(PlanValidationError, match="quarter-end date"):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_executor_returns_partial_cr10_when_one_ratio_is_missing():
    frame = pd.DataFrame(
        [
            {
                "ts_code": "002594.SZ",
                "ann_date": "20260429",
                "end_date": "20260331",
                "holder_name": f"Holder {index}",
                "hold_float_ratio": None if index == 0 else 4.0,
            }
            for index in range(10)
        ]
    )
    query = DataQuery(
        query_id="cr10-partial",
        operation="top10_floatholders",
        params={"ts_code": "002594.SZ", "period": "20260331"},
        fields=list(frame.columns),
        purpose="Build an honest partial concentration result.",
        transform="cr10_float_trend",
    )

    result = DataQueryExecutor(FakeMarketDataProvider(frame=frame)).execute(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "success"
    assert result.rows[0]["calculation_status"] == "partial_missing_ratio"
    assert result.rows[0]["cr10_float_registered"] is None
    assert result.rows[0]["non_top10_float_ratio"] is None
    assert result.rows[0]["known_top_holder_float_ratio"] == 36.0
    assert result.rows[0]["uncovered_float_ratio_upper_bound"] == 64.0
    assert result.rows[0]["ratio_holder_count"] == 9
    assert result.rows[0]["missing_ratio_holders"] == ["Holder 0"]


def test_executor_selects_latest_snapshot_for_as_of_query():
    frame = pd.DataFrame(
        [
            {
                "ts_code": "002594.SZ",
                "ann_date": ann_date,
                "end_date": end_date,
                "holder_name": f"Holder {index}",
                "hold_float_ratio": ratio,
            }
            for ann_date, end_date, ratio in (
                ("20251030", "20250930", 3.0),
                ("20260429", "20260331", 4.0),
            )
            for index in range(10)
        ]
    )
    query = DataQuery(
        query_id="cr10-as-of",
        operation="top10_floatholders",
        params={"ts_code": "002594.SZ", "end_date": "20260701"},
        fields=list(frame.columns),
        purpose="Build the latest disclosed concentration result as of one date.",
        transform="cr10_float_trend",
    )

    result = DataQueryExecutor(FakeMarketDataProvider(frame=frame)).execute(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "success"
    assert result.row_count == 1
    assert result.rows[0]["end_date"] == "20260331"
    assert result.rows[0]["cr10_float_registered"] == 40.0


def test_executor_selects_latest_snapshot_when_planner_omits_date():
    frame = pd.DataFrame(
        [
            {
                "ts_code": "300059.SZ",
                "ann_date": ann_date,
                "end_date": end_date,
                "holder_name": f"Holder {index}",
                "hold_float_ratio": ratio,
            }
            for ann_date, end_date, ratio in (
                ("20120420", "20111231", 2.0),
                ("20260425", "20260331", 3.0),
            )
            for index in range(10)
        ]
    )
    query = DataQuery(
        query_id="cr10-latest",
        operation="top10_floatholders",
        params={"ts_code": "300059.SZ"},
        fields=list(frame.columns),
        purpose="Build the latest available concentration result.",
        transform="cr10_float_trend",
    )

    result = DataQueryExecutor(FakeMarketDataProvider(frame=frame)).execute(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "success"
    assert result.row_count == 1
    assert result.rows[0]["end_date"] == "20260331"
    assert result.rows[0]["cr10_float_registered"] == 30.0


def test_executor_filters_numeric_rows_and_excludes_missing_values():
    frame = pd.DataFrame(
        [
            {"ts_code": "000001.SZ", "trade_date": "20260717", "pe": 4.9069},
            {"ts_code": "000002.SZ", "trade_date": "20260717", "pe": None},
            {"ts_code": "000006.SZ", "trade_date": "20260717", "pe": 1691.5923},
            {"ts_code": "000009.SZ", "trade_date": "20260717", "pe": 10},
            {"ts_code": "000010.SZ", "trade_date": "20260717", "pe": "invalid"},
        ]
    )
    query = DataQuery(
        query_id="low_pe",
        operation="daily_basic",
        params={"trade_date": "20260717"},
        fields=["ts_code", "trade_date", "pe"],
        purpose="Find stocks with PE no greater than 10.",
        filters=[{"field": "pe", "operator": "le", "value": 10}],
    )

    result = DataQueryExecutor(FakeMarketDataProvider(frame=frame)).execute(
        query,
        api_route="/api/analysis",
        request_id="request-low-pe",
    )

    assert result.status == "success"
    assert result.row_count == 2
    assert [row["ts_code"] for row in result.rows] == ["000001.SZ", "000009.SZ"]
    assert all(row["pe"] <= 10 for row in result.rows)


def test_executor_preserves_requested_columns_for_an_empty_result():
    result = DataQueryExecutor(
        FakeMarketDataProvider(frame=pd.DataFrame())
    ).execute(
        make_daily_plan().queries[0],
        api_route="/api/analysis",
        request_id="request-empty",
    )

    assert result.status == "success"
    assert result.rows == []
    assert result.columns == ["ts_code", "change"]


def test_validator_rejects_filter_field_missing_from_requested_fields():
    plan = make_daily_plan()
    plan.queries[0].filters = [DataFilter(field="pe", operator="le", value=10)]

    with pytest.raises(PlanValidationError, match="Filter field is not requested"):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_executor_preserves_existing_names_without_catalog_query():
    frame = pd.DataFrame(
        [{"ts_code": "000001.SZ", "name": "Existing Name", "change": 1.2}]
    )
    provider = FakeMarketDataProvider(frame=frame)

    result = DataQueryExecutor(provider).execute(
        make_daily_plan().queries[0],
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.rows[0]["name"] == "Existing Name"
    assert provider.calls == ["daily"]


def test_executor_keeps_unmatched_stock_name_empty():
    frame = pd.DataFrame([{"ts_code": "000001.SZ", "change": 1.2}])
    stock_frame = pd.DataFrame([{"ts_code": "600000.SH", "name": "SPD Bank"}])

    result = DataQueryExecutor(
        FakeMarketDataProvider(frame=frame, stock_frame=stock_frame)
    ).execute(
        make_daily_plan().queries[0],
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.rows[0]["name"] is None


def test_executor_preserves_tushare_permission_error():
    error = TushareApiError(
        "No permission", code=2002, http_status=200, raw_response={"code": 2002, "msg": "No permission"}
    )

    result = DataQueryExecutor(FakeMarketDataProvider(error=error)).execute(
        make_daily_plan().queries[0],
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.source == "tushare"
    assert result.error.raw_response == {"code": 2002, "msg": "No permission"}

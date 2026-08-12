import json
import logging
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
    AnalysisStatusReason,
    DataFilter,
    DataOperation,
    DataQuery,
    ExecutionNode,
    ExecutionPlan,
    QueryPlan,
    QueryConstraint,
    QueryResult,
    QueryStatus,
    RequirementCoverage,
    ResultPipeline,
    ResultPipelineStep,
)
from china_a_share.core.errors import PlannerError
from china_a_share.planners.deepseek import DeepSeekQueryPlanner
from china_a_share.planners.vertex_claude import VertexClaudeQueryPlanner
from china_a_share.result_pipeline import ResultPipelineExecutor, ResultValidationError
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


def test_validator_accepts_universe_constraint_enforced_before_ranking():
    plan = QueryPlan(
        interpretation="Rank valuation inside one classified security universe.",
        intent=AnalysisIntent.model_validate(
            {
                "analysis_type": "rank_metric",
                "universe": {
                    "filters": [
                        {"field": "area", "operator": "eq", "value": "Shanghai"}
                    ]
                },
                "metric": {"type": "pe", "as_of": "20260717"},
                "ranking": {"direction": "desc", "limit": 10},
            }
        ),
        requirements=[
            {
                "requirement": "Restrict securities by area before ranking.",
                "status": "covered",
                "implementation": "Filter stock_basic and enforce membership.",
                "evidence": "stock_basic exposes the area classification.",
            }
        ],
        constraints=[
            {
                "constraint_id": "target_area",
                "scope": "universe",
                "field": "area",
                "operator": "eq",
                "value": "Shanghai",
                "query_id": "classified-universe",
                "enforcement_step_index": 2,
            }
        ],
        queries=[
            DataQuery(
                query_id="valuation",
                operation="daily_basic",
                params={"trade_date": "20260717"},
                fields=["ts_code", "pe"],
                purpose="Retrieve market valuation rows.",
            ),
            DataQuery(
                query_id="classified-universe",
                operation="stock_basic",
                fields=["ts_code", "area"],
                purpose="Build the requested classified universe.",
                filters=[
                    {"field": "area", "operator": "eq", "value": "Shanghai"}
                ],
            ),
        ],
        result_pipeline={
            "source_query_id": "valuation",
            "output_query_id": "ranked-valuation",
            "steps": [
                {"operation": "drop_missing", "fields": ["pe"]},
                {
                    "operation": "match_source",
                    "right_source_query_id": "classified-universe",
                    "join_on": ["ts_code"],
                    "output_field": "in_requested_universe",
                },
                {
                    "operation": "filter",
                    "field": "in_requested_universe",
                    "comparison": "eq",
                    "value": 1,
                },
                {"operation": "sort", "field": "pe", "direction": "desc"},
                {"operation": "limit", "count": 10},
            ],
        },
    )

    validated = ASharePlanValidator(
        FakeMarketDataProvider(stock_frame=pd.DataFrame())
    ).validate(plan)

    assert validated.constraints[0].field == "area"


def test_validator_accepts_universe_constraint_enforced_by_semi_join():
    plan = make_daily_plan()
    plan.queries[0].fields = ["ts_code", "trade_date", "close"]
    plan.queries[0].aggregations = []
    plan.queries.append(
        DataQuery(
            query_id="classified-universe",
            operation="stock_basic",
            fields=["ts_code", "industry"],
            purpose="Build the requested classified universe.",
            filters=[
                {"field": "industry", "operator": "contains", "value": "汽车"}
            ],
        )
    )
    plan.constraints = [
        QueryConstraint(
            constraint_id="target_industry",
            scope="universe",
            field="industry",
            operator="contains",
            value="汽车",
            query_id="classified-universe",
            enforcement_step_index=0,
        )
    ]
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": plan.queries[0].query_id,
            "output_query_id": "industry-prices",
            "steps": [
                {
                    "operation": "semi_join",
                    "right_source_query_id": "classified-universe",
                    "join_on": ["ts_code"],
                }
            ],
        }
    )
    plan.answer_contract = AnswerContract.model_validate(
        {
            "result_query_id": "industry-prices",
            "result_kind": "table",
            "outputs": [{"field": "ts_code", "description": "Security code."}],
        }
    )

    validated = ASharePlanValidator(
        FakeMarketDataProvider(stock_frame=pd.DataFrame())
    ).validate(plan)

    assert validated.constraints[0].enforcement_step_index == 0


def test_validator_rejects_universe_constraint_enforced_after_ranking():
    plan = QueryPlan(
        interpretation="Reject late universe enforcement.",
        intent=AnalysisIntent.model_validate(
            {
                "analysis_type": "rank_metric",
                "universe": {
                    "filters": [
                        {
                            "field": "industry",
                            "operator": "eq",
                            "value": "Automotive",
                        }
                    ]
                },
                "metric": {"type": "pe", "as_of": "20260717"},
                "ranking": {"direction": "desc", "limit": 10},
            }
        ),
        requirements=[
            {
                "requirement": "Restrict securities by industry before ranking.",
                "status": "covered",
                "implementation": "Filter stock_basic and enforce membership.",
                "evidence": "stock_basic exposes the industry classification.",
            }
        ],
        constraints=[
            {
                "constraint_id": "target_industry",
                "scope": "universe",
                "field": "industry",
                "operator": "eq",
                "value": "Automotive",
                "query_id": "classified-universe",
                "enforcement_step_index": 3,
            }
        ],
        queries=[
            DataQuery(
                query_id="valuation",
                operation="daily_basic",
                params={"trade_date": "20260717"},
                fields=["ts_code", "pe"],
                purpose="Retrieve market valuation rows.",
            ),
            DataQuery(
                query_id="classified-universe",
                operation="stock_basic",
                fields=["ts_code", "industry"],
                purpose="Build the requested classified universe.",
                filters=[
                    {
                        "field": "industry",
                        "operator": "eq",
                        "value": "Automotive",
                    }
                ],
            ),
        ],
        result_pipeline={
            "source_query_id": "valuation",
            "output_query_id": "invalid-ranking",
            "steps": [
                {"operation": "drop_missing", "fields": ["pe"]},
                {"operation": "sort", "field": "pe", "direction": "desc"},
                {
                    "operation": "match_source",
                    "right_source_query_id": "classified-universe",
                    "join_on": ["ts_code"],
                    "output_field": "in_requested_universe",
                },
                {
                    "operation": "filter",
                    "field": "in_requested_universe",
                    "comparison": "eq",
                    "value": 1,
                },
                {"operation": "limit", "count": 10},
            ],
        },
    )

    with pytest.raises(
        PlanValidationError,
        match="must be enforced before sorting, limiting, or aggregation",
    ):
        ASharePlanValidator(
            FakeMarketDataProvider(stock_frame=pd.DataFrame())
        ).validate(plan)


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


def test_year_to_date_prompt_uses_the_completed_trading_year():
    provider = FakeMarketDataProvider(frame=pd.DataFrame())
    service = AnalysisService(
        Mock(),
        provider,
        ASharePlanValidator(provider),
        DataQueryExecutor(provider),
    )
    service._latest_completed_trading_date = Mock(return_value=date(2026, 8, 7))

    enriched = service._append_resolved_time_range(
        "request-year-to-date",
        "市净率最低的50家公司今年以来的收益率",
    )

    assert "event_start_date=20260101" in enriched
    assert "event_end_date=20260807" in enriched


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
    assert sent["json"]["temperature"] == 0
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
    assert "Security classification constraints" in system_prompt
    assert "filter that universe first" in system_prompt
    assert "before sorting or limiting" in system_prompt
    assert "Critical contract invariants" in system_prompt
    assert "requirements[].status is exactly covered or unsupported" in system_prompt


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


def test_planner_revises_invalid_requirement_status_with_structured_feedback():
    invalid_plan = make_daily_plan().model_dump(mode="json")
    invalid_plan["requirements"][0]["status"] = "supported"
    valid_plan = make_daily_plan()
    session = SequenceFakeSession(
        [
            FakeResponse(
                {"choices": [{"message": {"content": json.dumps(invalid_plan)}}]}
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

    assert result.requirements[0].status == "covered"
    feedback_message = session.calls[1][1]["json"]["messages"][-1]["content"]
    assert '"decision": "REVISE"' in feedback_message
    assert '"phase": "contract_validation"' in feedback_message
    assert '"location": "requirements.0.status"' in feedback_message
    assert '\\"status\\": \\"supported\\"' in feedback_message


def test_planner_replans_semantic_rejection_with_complete_candidate():
    first_plan = make_daily_plan()
    revised_plan = make_daily_plan()
    answer_contract = AnswerContract.model_validate(
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
    first_plan.answer_contract = answer_contract
    revised_plan.answer_contract = answer_contract
    revised_plan.queries[0].purpose = "Use the documented provider capability."
    session = SequenceFakeSession(
        [
            FakeResponse(
                {"choices": [{"message": {"content": first_plan.model_dump_json()}}]}
            ),
            FakeResponse(
                {"choices": [{"message": {"content": revised_plan.model_dump_json()}}]}
            ),
        ]
    )
    validation_attempts = 0

    def reject_once(plan):
        nonlocal validation_attempts
        validation_attempts += 1
        if validation_attempts == 1:
            raise PlanValidationError("Operation is outside the provider catalog.")
        return plan

    result = DeepSeekQueryPlanner("test-key", session=session).plan_validated(
        AnalysisRequest(prompt="Count stocks."),
        [DataOperation(name="daily", description="Daily prices.")],
        reject_once,
    )

    assert result.queries[0].purpose == "Use the documented provider capability."
    feedback_message = session.calls[1][1]["json"]["messages"][-1]["content"]
    assert '"decision": "REPLAN"' in feedback_message
    assert '"phase": "capability_validation"' in feedback_message
    assert '"rejected_plan"' in feedback_message
    assert "Operation is outside the provider catalog." in feedback_message


def test_planner_can_converge_across_contract_and_capability_rejections():
    invalid_json = '{"market":"A_SHARE"'
    invalid_status_plan = make_daily_plan().model_dump(mode="json")
    invalid_status_plan["requirements"][0]["status"] = "supported"
    semantic_plan = make_daily_plan()
    semantic_plan.answer_contract = AnswerContract.model_validate(
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
    valid_plan = semantic_plan.model_copy(deep=True)
    valid_plan.queries[0].purpose = "Use the corrected executable dependency."
    session = SequenceFakeSession(
        [
            FakeResponse({"choices": [{"message": {"content": invalid_json}}]}),
            FakeResponse(
                {
                    "choices": [
                        {"message": {"content": json.dumps(invalid_status_plan)}}
                    ]
                }
            ),
            FakeResponse(
                {
                    "choices": [
                        {"message": {"content": semantic_plan.model_dump_json()}}
                    ]
                }
            ),
            FakeResponse(
                {"choices": [{"message": {"content": valid_plan.model_dump_json()}}]}
            ),
        ]
    )
    validation_attempts = 0

    def reject_first_valid_candidate(plan):
        nonlocal validation_attempts
        validation_attempts += 1
        if validation_attempts == 1:
            raise PlanValidationError("Constraint references an unknown query.")
        return plan

    result = DeepSeekQueryPlanner("test-key", session=session).plan_validated(
        AnalysisRequest(prompt="Run one multi-stage analysis."),
        [DataOperation(name="daily", description="Daily prices.")],
        reject_first_valid_candidate,
    )

    assert result.queries[0].purpose == "Use the corrected executable dependency."
    assert len(session.calls) == 4
    assert '"decision": "REPLAN"' in (
        session.calls[3][1]["json"]["messages"][-1]["content"]
    )


def test_planner_selects_contract_fidelity_over_extra_outputs():
    candidates = []
    for index, (output_fields, limitations) in enumerate(
        (
            (["ts_code"], []),
            (["ts_code", "change"], ["The answer includes an unrequested field."]),
            (["change"], ["The requested identifier is unavailable."]),
        ),
        start=1,
    ):
        candidate = make_daily_plan()
        candidate.requirements.append(
            candidate.requirements[0].model_copy(
                update={"requirement": "Return every requested output field."}
            )
        )
        candidate.answer_contract = AnswerContract.model_validate(
            {
                "result_query_id": "market_direction",
                "result_kind": "table",
                "outputs": [
                    {"field": field, "description": f"Requested field {field}."}
                    for field in output_fields
                ],
            }
        )
        candidate.limitations = limitations
        candidate.queries[0].purpose = f"Candidate {index}."
        candidates.append(candidate)
    session = SequenceFakeSession(
        [
            FakeResponse(
                {"choices": [{"message": {"content": plan.model_dump_json()}}]}
            )
            for plan in candidates
        ]
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan_validated(
        AnalysisRequest(prompt="Return two related market measures."),
        [DataOperation(name="daily", description="Daily prices.")],
        lambda plan: plan,
    )

    assert [output.field for output in result.answer_contract.outputs] == ["ts_code"]
    assert len(session.calls) == 3
    assert "candidate 2" in session.calls[1][1]["json"]["messages"][-1]["content"]
    assert "candidate 3" in session.calls[2][1]["json"]["messages"][-1]["content"]


def test_execution_status_fails_when_required_answer_fails():
    plan = make_daily_plan()
    plan.answer_contract = AnswerContract.model_validate(
        {
            "result_query_id": "market_direction",
            "result_kind": "table",
            "outputs": [{"field": "ts_code", "description": "Security identifier."}],
        }
    )
    plan.queries.append(
        DataQuery(
            query_id="advisory_context",
            operation="daily_basic",
            params={"trade_date": "20260717"},
            fields=["ts_code"],
            purpose="Retrieve optional context.",
        )
    )

    status, required_result_id, status_reason = AnalysisService._classify_execution_status(
        plan,
        [
            QueryResult(
                query_id="market_direction",
                provider="test",
                operation="daily",
                status=QueryStatus.ERROR,
            ),
            QueryResult(
                query_id="advisory_context",
                provider="test",
                operation="daily_basic",
                status=QueryStatus.SUCCESS,
            ),
        ],
    )

    assert status == "error"
    assert required_result_id == "market_direction"
    assert status_reason == AnalysisStatusReason.REQUIRED_RESULT_FAILED


def test_execution_status_degrades_when_only_advisory_result_fails():
    plan = make_daily_plan()
    plan.answer_contract = AnswerContract.model_validate(
        {
            "result_query_id": "market_direction",
            "result_kind": "table",
            "outputs": [{"field": "ts_code", "description": "Security identifier."}],
        }
    )
    plan.queries.append(
        DataQuery(
            query_id="advisory_context",
            operation="daily_basic",
            params={"trade_date": "20260717"},
            fields=["ts_code"],
            purpose="Retrieve optional context.",
        )
    )

    status, required_result_id, status_reason = AnalysisService._classify_execution_status(
        plan,
        [
            QueryResult(
                query_id="market_direction",
                provider="test",
                operation="daily",
                status=QueryStatus.SUCCESS,
                completeness="complete",
            ),
            QueryResult(
                query_id="advisory_context",
                provider="test",
                operation="daily_basic",
                status=QueryStatus.ERROR,
            ),
        ],
    )

    assert status == "partial_success"
    assert required_result_id == "market_direction"
    assert status_reason == AnalysisStatusReason.ADVISORY_RESULT_FAILED


def test_execution_status_rejects_partial_required_answer():
    plan = make_daily_plan()

    status, required_result_id, status_reason = AnalysisService._classify_execution_status(
        plan,
        [
            QueryResult(
                query_id="market_direction",
                provider="test",
                operation="daily",
                status=QueryStatus.SUCCESS,
                completeness="partial",
            )
        ],
    )

    assert status == "error"
    assert required_result_id == "market_direction"
    assert status_reason == AnalysisStatusReason.REQUIRED_RESULT_INCOMPLETE


def test_execution_status_requires_complete_evidence_when_contract_demands_it():
    plan = make_daily_plan()
    plan.answer_contract = AnswerContract.model_validate(
        {
            "result_query_id": "market_direction",
            "result_kind": "table",
            "outputs": [{"field": "ts_code", "description": "Security identifier."}],
            "required_completeness": "complete",
        }
    )

    status, required_result_id, status_reason = (
        AnalysisService._classify_execution_status(
            plan,
            [
                QueryResult(
                    query_id="market_direction",
                    provider="test",
                    operation="daily",
                    status=QueryStatus.SUCCESS,
                    completeness="unknown",
                )
            ],
        )
    )

    assert status == "error"
    assert required_result_id == "market_direction"
    assert status_reason == AnalysisStatusReason.REQUIRED_RESULT_INCOMPLETE


def test_answer_contract_rejects_overlapping_dependency_roles():
    with pytest.raises(ValueError, match="both required and advisory"):
        AnswerContract.model_validate(
            {
                "result_query_id": "market_direction",
                "result_kind": "table",
                "outputs": [
                    {"field": "ts_code", "description": "Security identifier."}
                ],
                "required_result_ids": ["market_direction"],
                "advisory_result_ids": ["market_direction"],
            }
        )


def test_validator_rejects_unknown_answer_dependency():
    plan = make_daily_plan()
    plan.answer_contract = AnswerContract.model_validate(
        {
            "result_query_id": "market_direction",
            "result_kind": "table",
            "outputs": [{"field": "ts_code", "description": "Security identifier."}],
            "required_result_ids": ["market_direction", "missing_result"],
        }
    )

    with pytest.raises(
        PlanValidationError,
        match="dependencies do not match planned results: missing_result",
    ):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_planner_stops_after_three_identical_valid_complex_candidates():
    candidate = make_daily_plan()
    candidate.requirements.append(
        candidate.requirements[0].model_copy(
            update={"requirement": "Return the related market measure."}
        )
    )
    candidate.answer_contract = AnswerContract.model_validate(
        {
            "result_query_id": "market_direction",
            "result_kind": "table",
            "outputs": [
                {"field": "ts_code", "description": "A-share security code."}
            ],
        }
    )
    session = SequenceFakeSession(
        [
            FakeResponse(
                {"choices": [{"message": {"content": candidate.model_dump_json()}}]}
            )
            for _ in range(3)
        ]
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan_validated(
        AnalysisRequest(prompt="Return two related market measures."),
        [DataOperation(name="daily", description="Daily prices.")],
        lambda plan: plan,
    )

    assert result == candidate
    assert len(session.calls) == 3


def test_planner_final_retry_can_return_contextual_clarification_options():
    invalid_plan = make_daily_plan()
    clarification_plan = QueryPlan(
        interpretation="The requested metric has multiple material definitions.",
        feasibility="unsupported",
        requirements=[
            {
                "requirement": "Choose one precise metric definition.",
                "status": "unsupported",
                "evidence": "The original wording does not identify one definition.",
            }
        ],
        limitations=["A material metric definition is unresolved."],
        clarification_options=[
            "Rank the full market by the latest disclosed metric A.",
            "Rank the full market by the latest disclosed metric B.",
        ],
    )
    session = SequenceFakeSession(
        [
            *[
                FakeResponse(
                    {
                        "choices": [
                            {"message": {"content": invalid_plan.model_dump_json()}}
                        ]
                    }
                )
                for _ in range(19)
            ],
            FakeResponse(
                {
                    "choices": [
                        {"message": {"content": clarification_plan.model_dump_json()}}
                    ]
                }
            ),
        ]
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan_validated(
        AnalysisRequest(prompt="Rank one ambiguous metric."),
        [DataOperation(name="daily", description="Daily prices.")],
        lambda plan: plan,
    )

    assert result.feasibility == "unsupported"
    assert len(result.clarification_options) == 2
    final_feedback = session.calls[-1][1]["json"]["messages"][-1]["content"]
    assert "two or three contextual clarification_options" in final_feedback
    assert "Do not expose the validator error as the answer" in final_feedback


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


def test_validator_rejects_distinct_outputs_with_identical_summaries():
    plan = make_daily_plan()
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "market_direction",
            "output_query_id": "market-direction-summary",
            "steps": [
                {
                    "operation": "summarize",
                    "aggregations": [
                        {
                            "output_field": "positive_count",
                            "field": "change",
                            "function": "count",
                        },
                        {
                            "output_field": "negative_count",
                            "field": "change",
                            "function": "count",
                        },
                    ],
                }
            ],
        }
    )

    with pytest.raises(
        PlanValidationError,
        match="distinct summary outputs use identical unconditional aggregations",
    ):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_validator_rejects_distinct_summaries_of_identical_derived_expressions():
    plan = make_daily_plan()
    plan.queries[0].fields = ["ts_code", "pct_chg"]
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": plan.queries[0].query_id,
            "output_query_id": "market-direction-summary",
            "steps": [
                {
                    "operation": "derive",
                    "field": "pct_chg",
                    "output_field": "up_flag",
                    "arithmetic_operator": "subtract",
                    "value": 0,
                },
                {
                    "operation": "derive",
                    "field": "pct_chg",
                    "output_field": "down_flag",
                    "arithmetic_operator": "subtract",
                    "value": 0,
                },
                {
                    "operation": "summarize",
                    "aggregations": [
                        {
                            "output_field": "up_count",
                            "field": "up_flag",
                            "function": "sum",
                        },
                        {
                            "output_field": "down_count",
                            "field": "down_flag",
                            "function": "sum",
                        },
                    ],
                },
            ],
        }
    )

    with pytest.raises(
        PlanValidationError,
        match="distinct summary outputs use identical unconditional aggregations",
    ):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_planner_compiles_materially_different_conditional_counts():
    plan = make_daily_plan().model_dump(mode="json")
    plan["queries"][0]["fields"] = ["ts_code", "change"]
    plan["result_pipeline"] = {
        "source_query_id": "market_direction",
        "output_query_id": "market-direction-summary",
        "steps": [
            {
                "operation": "summarize",
                "aggregations": [
                    {
                        "output_field": "positive_count",
                        "field": "change",
                        "function": "count",
                        "condition": {"operator": "gt", "value": 1},
                    },
                    {
                        "output_field": "negative_count",
                        "field": "change",
                        "function": "count",
                        "condition": {"operator": "lt", "value": -2},
                    },
                ],
            }
        ],
    }

    normalized = DeepSeekQueryPlanner("test-key").normalize_and_validate_plan(
        json.dumps(plan)
    )

    assert [step.operation for step in normalized.result_pipeline.steps] == [
        "compare_scalar",
        "compare_scalar",
        "summarize",
    ]
    summary = normalized.result_pipeline.steps[-1]
    assert [item.function for item in summary.aggregations] == ["sum", "sum"]
    assert summary.aggregations[0].field != summary.aggregations[1].field


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


def test_planner_retries_when_answer_contract_is_omitted(caplog):
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

    with caplog.at_level(logging.INFO):
        result = DeepSeekQueryPlanner(
            "test-key",
            session=session,
        ).plan_validated(
            AnalysisRequest(prompt="Return the requested fields."),
            [DataOperation(name="daily", description="Daily prices.")],
            ASharePlanValidator(FakeMarketDataProvider()).validate,
        )

    assert result == valid_plan
    feedback_message = session.calls[1][1]["json"]["messages"][-1]["content"]
    assert '"decision": "REVISE"' in feedback_message
    assert '"phase": "contract_validation"' in feedback_message
    retry_messages = session.calls[1][1]["json"]["messages"]
    assert "must include answer_contract" in retry_messages[-1]["content"]
    events = [
        getattr(record, "structured_fields", {})
        for record in caplog.records
    ]
    assert sum(event.get("event") == "planner_raw_output" for event in events) == 2
    assert any(
        event.get("event") == "planner_intent_normalized"
        and event.get("model") == "deepseek-v4-flash"
        for event in events
    )


def test_vertex_planner_fallback_preserves_semantic_validation(caplog):
    invalid_plan = make_daily_plan()
    valid_plan = make_daily_plan()
    planner = VertexClaudeQueryPlanner("test-key")
    planner._plan_with_claude = Mock(return_value=invalid_plan)
    planner._fallback = Mock()
    planner._fallback.plan_validated.return_value = valid_plan
    validator = Mock(side_effect=ValueError("invalid field lineage"))
    request = AnalysisRequest(prompt="Count stocks.")
    operations = [DataOperation(name="daily", description="Daily prices.")]

    with caplog.at_level(logging.WARNING):
        result = planner.plan_validated(request, operations, validator)

    assert result is valid_plan
    planner._fallback.plan_validated.assert_called_once_with(
        request,
        operations,
        validator,
    )
    fallback_event = next(
        getattr(record, "structured_fields", {})
        for record in caplog.records
        if getattr(record, "structured_fields", {}).get("event")
        == "planner_fallback"
    )
    assert fallback_event["from_model"] == "claude-3-5-sonnet-v2@20241022"
    assert fallback_event["to_provider"] == "deepseek"


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


def test_workflow_clamps_snapshot_intent_and_compiled_query_together():
    plan = QueryPlan(
        interpretation="Rank a point-in-time metric.",
        intent=AnalysisIntent.model_validate(
            {
                "analysis_type": "rank_metric",
                "metric": {"type": "pe", "as_of": "20260809"},
                "ranking": {"direction": "asc", "limit": 10},
            }
        ),
        requirements=[
            {
                "requirement": "Rank securities by PE.",
                "status": "covered",
                "evidence": "daily_basic provides PE.",
            }
        ],
    )
    compiled = AnalysisService._compile_intent(plan)

    AnalysisService._normalize_latest_plan_dates(compiled, date(2026, 8, 7))

    assert compiled.intent.metric.as_of == "20260806"
    assert compiled.queries[0].params["trade_date"] == "20260806"


def test_workflow_uses_completed_date_for_natively_published_daily_operations():
    plan = make_daily_plan()
    plan.queries[0].params["trade_date"] = "20260809"
    plan.queries.extend(
        [
            DataQuery(
                query_id="limit-ups",
                operation="limit_list_d",
                params={"trade_date": "20260809"},
                fields=["ts_code", "trade_date"],
                purpose="Retrieve the latest limit-up list.",
            ),
            DataQuery(
                query_id="money-flow",
                operation="moneyflow",
                params={"trade_date": "20260809"},
                fields=["ts_code", "trade_date"],
                purpose="Retrieve the latest security money flow.",
            ),
        ]
    )

    AnalysisService._normalize_latest_plan_dates(plan, date(2026, 8, 7))

    assert plan.queries[0].params["trade_date"] == "20260807"
    assert plan.queries[1].params["trade_date"] == "20260807"
    assert plan.queries[2].params["trade_date"] == "20260807"


def test_workflow_normalizes_date_constraint_with_executable_query():
    plan = make_daily_plan()
    plan.queries[0].operation = "daily_basic"
    plan.queries[0].params["trade_date"] = "20260809"
    plan.constraints = [
        QueryConstraint(
            constraint_id="valuation_date",
            scope="universe",
            query_id=plan.queries[0].query_id,
            field="trade_date",
            operator="eq",
            value="20260809",
        )
    ]

    AnalysisService._normalize_latest_plan_dates(plan, date(2026, 8, 7))

    assert plan.queries[0].params["trade_date"] == "20260806"
    assert plan.constraints[0].value == "20260806"


def test_security_scoped_disclosure_range_uses_native_provider_query():
    query = DataQuery(
        query_id="holder-history",
        operation="stk_holdernumber",
        params={
            "ts_code": "000001.SZ",
            "start_date": "20240101",
            "end_date": "20261231",
        },
        fields=["ts_code", "ann_date", "end_date", "holder_num"],
        purpose="Retrieve one security's shareholder-count history.",
    )

    assert ASharePlanValidator._uses_bounded_date_fanout(query) is False


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


def test_workflow_compiles_generic_provider_field_ranking_intent():
    intent = AnalysisIntent.model_validate(
        {
            "analysis_type": "field_analysis",
            "operation": "forecast",
            "params": {
                "start_date": "20260101",
                "end_date": "20260810",
                "period": "20260630",
            },
            "fields": ["ts_code", "end_date", "p_change_max"],
            "analysis_field": "p_change_max",
            "group_by": ["ts_code"],
            "aggregations": [
                {
                    "output_field": "max_p_change_max",
                    "field": "p_change_max",
                    "function": "max",
                }
            ],
            "ranking": {"direction": "desc", "limit": 10},
        }
    )
    plan = QueryPlan(
        interpretation="Rank disclosed profit growth guidance.",
        intent=intent,
        requirements=[
            {
                "requirement": "Rank the largest disclosed profit increase.",
                "status": "covered",
                "evidence": "forecast provides p_change_max.",
            }
        ],
    )

    compiled = AnalysisService._compile_intent(plan)

    assert compiled.queries[0].operation == "forecast"
    assert compiled.queries[0].params["period"] == "20260630"
    assert [step.operation for step in compiled.result_pipeline.steps] == [
        "aggregate",
        "drop_missing",
        "sort",
        "limit",
    ]
    assert compiled.result_pipeline.steps[2].field == "max_p_change_max"
    assert compiled.answer_contract.result_query_id == "field_analysis_output"
    assert {output.field for output in compiled.answer_contract.outputs} == {
        "ts_code",
        "max_p_change_max",
    }


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


def test_deepseek_raw_normalization_canonicalizes_single_final_result_id():
    raw_plan = make_daily_plan().model_dump(mode="json")
    raw_plan["result_pipeline"] = {
        "source_query_id": "market_direction",
        "output_query_id": "market_summary",
        "steps": [
            {
                "operation": "summarize",
                "aggregations": [
                    {
                        "output_field": "security_count",
                        "field": "ts_code",
                        "function": "count_distinct",
                    }
                ],
            }
        ],
    }
    raw_plan["answer_contract"] = {
        "result_query_id": "model_local_name",
        "result_kind": "summary",
        "outputs": [
            {
                "field": "security_count",
                "description": "Distinct security count.",
            }
        ],
    }

    DeepSeekQueryPlanner._normalize_raw_query_defaults(raw_plan)

    assert raw_plan["answer_contract"]["result_query_id"] == "market_summary"


def test_deepseek_raw_normalization_uses_canonical_pipeline_fields_key():
    raw_plan = make_daily_plan().model_dump(mode="json")
    raw_plan["result_pipeline"] = {
        "source_query_id": "market_direction",
        "output_query_id": "renamed",
        "steps": [
            {
                "operation": "rename_fields",
                "fields_obj": {"change": "price_change"},
            }
        ],
    }

    DeepSeekQueryPlanner._normalize_raw_query_defaults(raw_plan)

    step = raw_plan["result_pipeline"]["steps"][0]
    assert step["fields"] == {"change": "price_change"}
    assert "fields_obj" not in step


def test_deepseek_raw_normalization_canonicalizes_unambiguous_step_defaults():
    raw_plan = make_daily_plan().model_dump(mode="json")
    raw_plan["result_pipeline"] = {
        "source_query_id": "market_direction",
        "output_query_id": "normalized",
        "steps": [
            {"operation": "distinct", "field": "ts_code"},
            {
                "operation": "diff",
                "field": "close",
                "output_field": "close_change",
                "group_by": ["ts_code"],
                "order_by": "trade_date",
            },
            {
                "operation": "inner_join",
                "right_source_query_id": "other",
                "join_on": ["ts_code"],
                "cardinality": "many_to_one",
            },
        ],
    }

    DeepSeekQueryPlanner._normalize_raw_query_defaults(raw_plan)

    steps = raw_plan["result_pipeline"]["steps"]
    assert steps[0]["fields"] == ["ts_code"]
    assert steps[1]["periods"] == 1
    assert steps[2]["fields"] == {}


def test_deepseek_raw_normalization_applies_same_adapter_to_execution_nodes():
    raw_plan = make_daily_plan().model_dump(mode="json")
    raw_plan["execution_plan"] = {
        "nodes": [
            {
                "node_id": "left",
                "kind": "query",
                "input_result_ids": [],
                "query": raw_plan["queries"][0],
            },
            {
                "node_id": "right",
                "kind": "query",
                "input_result_ids": [],
                "query": {**raw_plan["queries"][0], "query_id": "right"},
            },
            {
                "node_id": "joined",
                "kind": "compute",
                "input_result_ids": ["left", "right"],
                "step": {
                    "operation": "join_fields",
                    "join_on": ["ts_code"],
                    "fields": {"ts_code": "ts_code"},
                    "cardinality": "many_to_one",
                },
            },
        ],
        "result_node_id": "joined",
    }

    DeepSeekQueryPlanner._normalize_raw_query_defaults(raw_plan)
    normalized_plan = QueryPlan.model_validate(raw_plan)
    normalized_plan.execution_plan.nodes[1].query.fields = []
    normalized_plan.execution_plan.nodes[1].query.params["fields"] = [
        "ts_code",
        "trade_date",
    ]
    DeepSeekQueryPlanner._normalize_fields(normalized_plan)

    step = raw_plan["execution_plan"]["nodes"][2]["step"]
    assert raw_plan["execution_plan"]["nodes"][0]["query"]["query_id"] == "left"
    assert step["operation"] == "inner_join"
    assert step["right_source_query_id"] == "right"
    assert step["fields"] == {}
    assert normalized_plan.execution_plan.nodes[1].query.fields == [
        "ts_code",
        "trade_date",
    ]
    assert "fields" not in normalized_plan.execution_plan.nodes[1].query.params


def test_deepseek_raw_normalization_binds_constraint_alias_to_unique_parameter():
    raw_plan = make_daily_plan().model_dump(mode="json")
    raw_plan["queries"][0]["params"] = {
        "trade_date": "20260806",
        "limit_type": "U",
    }
    raw_plan["constraints"] = [
        {
            "constraint_id": "event_date",
            "scope": "result",
            "field": "event_date",
            "operator": "eq",
            "value": "20260806",
            "query_id": "market_direction",
        }
    ]

    DeepSeekQueryPlanner._normalize_raw_query_defaults(raw_plan)

    assert raw_plan["constraints"][0]["field"] == "trade_date"


def test_planner_prefers_typed_ranking_intent_over_model_authored_execution_graph():
    raw_plan = {
        "queries": [
            {
                "query_id": "legacy-universe",
                "operation": "stock_basic",
                "params": {"list_status": "L"},
                "fields": ["ts_code"],
                "purpose": "Duplicate legacy universe query.",
            }
        ],
        "result_pipeline": {
            "source_query_id": "legacy-universe",
            "output_query_id": "legacy-result",
            "steps": [{"operation": "limit", "count": 10}],
        },
        "intent": {
            "analysis_type": "rank_metric",
            "metric": {"type": "pe", "as_of": "20260807"},
            "ranking": {"direction": "desc", "limit": 10},
        },
        "execution_plan": {
            "result_node_id": "universe",
            "nodes": [
                {
                    "node_id": "universe",
                    "kind": "query",
                    "query": {
                        "query_id": "universe-query",
                        "operation": "stock_basic",
                        "params": {"list_status": "L"},
                        "fields": ["ts_code"],
                        "purpose": "Retrieve the listed A-share universe.",
                    },
                }
            ],
        },
    }

    DeepSeekQueryPlanner._normalize_raw_query_defaults(raw_plan)

    assert raw_plan["queries"] == []
    assert raw_plan["result_pipeline"] is None
    assert raw_plan["intent"]["metric"]["type"] == "pe"
    assert raw_plan["execution_plan"] is None


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


def test_workflow_compiles_single_day_return_intent_as_daily_change_snapshot():
    plan = make_daily_plan()
    plan.queries = []
    plan.result_pipeline = None
    plan.answer_contract = None
    plan.intent = AnalysisIntent.model_validate(
        {
            "metric": {
                "type": "period_return",
                "window": {"start": "20260618", "end": "20260618"},
            },
            "ranking": {"direction": "desc", "limit": 15},
        }
    )

    result = AnalysisService._compile_intent(plan)

    assert result.queries[0].operation == "daily"
    assert result.queries[0].params == {"trade_date": "20260618"}
    sort_step = next(
        step for step in result.result_pipeline.steps if step.operation == "sort"
    )
    assert sort_step.field == "pct_chg"


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
    service._align_answer_contract_result_id(result)

    assert result.result_pipeline.output_query_id == "period_return_output"
    assert result.answer_contract.result_query_id == "period_return_output"
    assert result.answer_contract.required_result_ids == ["period_return_output"]
    assert result.answer_contract.required_completeness == "complete"
    ASharePlanValidator(FakeMarketDataProvider()).validate(result)


@pytest.mark.parametrize(
    ("universe_field", "universe_value", "metric", "direction", "limit"),
    [
        ("industry", "Automotive", "pe", "desc", 10),
        ("area", "Shanghai", "total_mv", "asc", 7),
    ],
)
def test_workflow_compiles_generic_classified_metric_ranking(
    universe_field,
    universe_value,
    metric,
    direction,
    limit,
):
    intent = AnalysisIntent.model_validate(
        {
            "analysis_type": "rank_metric",
            "universe": {
                "filters": [
                    {
                        "field": universe_field,
                        "operator": "eq",
                        "value": universe_value,
                    }
                ]
            },
            "metric": {"type": metric, "as_of": "20260807"},
            "ranking": {"direction": direction, "limit": limit},
        }
    )
    plan = QueryPlan(
        interpretation="Rank one metric inside a classified universe.",
        intent=intent,
        requirements=[
            {
                "requirement": "Apply the universe before ranking.",
                "status": "covered",
                "implementation": "Compile the typed ranking intent locally.",
                "evidence": "The intent declares the universe, metric, and ranking.",
            }
        ],
    )

    compiled = AnalysisService._compile_intent(plan)
    validated = ASharePlanValidator(
        FakeMarketDataProvider(stock_frame=pd.DataFrame())
    ).validate(compiled)

    assert [query.operation for query in validated.queries] == [
        "daily_basic",
        "stock_basic",
    ]
    assert validated.queries[0].fields == ["ts_code", "trade_date", metric]
    assert validated.queries[1].filters[0].field == universe_field
    assert [step.operation for step in validated.result_pipeline.steps] == [
        "drop_missing",
        "match_source",
        "filter",
        "sort",
        "limit",
    ]
    assert validated.result_pipeline.steps[-2].field == metric
    assert validated.result_pipeline.steps[-2].direction == direction
    assert validated.result_pipeline.steps[-1].count == limit
    assert validated.constraints[0].scope == "universe"


def test_validator_rejects_typed_ranking_with_unbound_intent_predicate():
    intent = AnalysisIntent.model_validate(
        {
            "analysis_type": "rank_metric",
            "universe": {
                "filters": [
                    {"field": "industry", "operator": "eq", "value": "Energy"}
                ]
            },
            "metric": {
                "type": "pb",
                "as_of": "20260807",
                "filters": [{"field": "pb", "operator": "gt", "value": 0}],
            },
            "ranking": {"direction": "asc", "limit": 8},
        }
    )
    plan = AnalysisService._compile_intent(
        QueryPlan(
            interpretation="Rank positive valuation inside one industry.",
            intent=intent,
            requirements=[
                {
                    "requirement": "Apply every typed predicate.",
                    "status": "covered",
                    "implementation": "Compile predicates locally.",
                    "evidence": "Typed intent contains both predicates.",
                }
            ],
        )
    )
    plan.constraints.pop()

    with pytest.raises(
        PlanValidationError,
        match="exact constraint coverage",
    ):
        ASharePlanValidator(
            FakeMarketDataProvider(stock_frame=pd.DataFrame())
        ).validate(plan)


@pytest.mark.parametrize(
    ("prompt", "intent_payload"),
    [
        (
            "A股汽车行业市盈率前十的公司",
            {
                "analysis_type": "rank_metric",
                "universe": {
                    "filters": [
                        {"field": "industry", "operator": "eq", "value": "汽车"}
                    ]
                },
                "metric": {"type": "pe", "as_of": "20260807"},
                "ranking": {"direction": "desc", "limit": 10},
            },
        ),
        (
            "在能源行业中找市净率最低的7家公司",
            {
                "analysis_type": "rank_metric",
                "universe": {
                    "filters": [
                        {"field": "industry", "operator": "eq", "value": "能源"}
                    ]
                },
                "metric": {"type": "pb", "as_of": "20260807"},
                "ranking": {"direction": "asc", "limit": 7},
            },
        ),
    ],
)
def test_prompt_intent_reconciliation_accepts_matching_atomic_facts(
    prompt,
    intent_payload,
):
    plan = QueryPlan(
        interpretation="Rank the requested metric.",
        intent=AnalysisIntent.model_validate(intent_payload),
    )

    reconciled = ASharePlanValidator.validate_prompt_intent_coverage(prompt, plan)

    assert reconciled is plan


def test_prompt_intent_reconciliation_inverts_negative_magnitude_ranking():
    plan = QueryPlan(
        interpretation="Rank the largest declines.",
        intent=AnalysisIntent.model_validate(
            {
                "analysis_type": "rank_metric",
                "metric": {
                    "type": "period_return",
                    "window": {"start": "20260101", "end": "20260807"},
                },
                "ranking": {"direction": "asc", "limit": 10},
            }
        ),
    )

    reconciled = ASharePlanValidator.validate_prompt_intent_coverage(
        "2026年A股跌幅最大的前10只",
        plan,
    )

    assert reconciled is plan


def test_constraint_lineage_accepts_exact_native_parameter_enforcement():
    plan = QueryPlan(
        interpretation="List native limit-up rows.",
        requirements=[
            {
                "requirement": "Restrict the native list to limit-up rows.",
                "status": "covered",
                "evidence": "limit_type=U selects limit-up rows.",
            }
        ],
        constraints=[
            {
                "constraint_id": "native_limit_type",
                "scope": "result",
                "field": "limit_type",
                "operator": "eq",
                "value": "U",
                "query_id": "limit_rows",
            }
        ],
        queries=[
            DataQuery(
                query_id="limit_rows",
                operation="limit_list_d",
                params={"trade_date": "20260807", "limit_type": "U"},
                fields=["ts_code", "name"],
                purpose="Retrieve native limit-up rows.",
            )
        ],
        answer_contract=AnswerContract.model_validate(
            {
                "result_query_id": "limit_rows",
                "result_kind": "table",
                "outputs": [
                    {"field": "ts_code", "description": "Security code."}
                ],
            }
        ),
    )

    ASharePlanValidator._validate_constraint_lineage(plan)


def test_execution_graph_constraints_bind_to_query_filters():
    plan = QueryPlan(
        interpretation="Filter one graph query.",
        requirements=[
            {
                "requirement": "Keep positive daily changes.",
                "status": "covered",
                "evidence": "The daily query exposes pct_chg.",
            }
        ],
        constraints=[
            {
                "constraint_id": "positive_change",
                "scope": "result",
                "field": "pct_chg",
                "operator": "gt",
                "value": 0,
                "query_id": "positive_rows",
            }
        ],
        answer_contract=AnswerContract.model_validate(
            {
                "result_query_id": "positive_rows",
                "result_kind": "table",
                "outputs": [
                    {"field": "ts_code", "description": "Security code."}
                ],
            }
        ),
        execution_plan=ExecutionPlan(
            result_node_id="positive_rows",
            nodes=[
                ExecutionNode(
                    node_id="positive_rows",
                    kind="query",
                    query=DataQuery(
                        query_id="positive_rows",
                        operation="daily",
                        params={"trade_date": "20260807"},
                        fields=["ts_code", "pct_chg"],
                        purpose="Retrieve positive daily changes.",
                        filters=[{"field": "pct_chg", "operator": "gt", "value": 0}],
                    ),
                )
            ],
        ),
    )

    validated = ASharePlanValidator(FakeMarketDataProvider()).validate(plan)

    assert validated is plan


def test_broad_industry_prompt_normalizes_to_descendant_matching():
    plan = QueryPlan(
        interpretation="Rank one broad industry.",
        intent=AnalysisIntent.model_validate(
            {
                "analysis_type": "rank_metric",
                "universe": {
                    "filters": [
                        {"field": "industry", "operator": "eq", "value": "汽车"}
                    ]
                },
                "metric": {"type": "pe", "as_of": "20260601"},
                "ranking": {"direction": "desc", "limit": 10},
            }
        ),
    )

    normalized = ASharePlanValidator.normalize_prompt_classifications(
        "A股汽车行业20260601市盈率前十的公司",
        plan,
    )

    assert normalized.intent.universe.filters[0].operator == "contains"


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("A股2026年汽车行业，查询市盈率", {"汽车"}),
        ("A股2025年下半年电池行业，查询分红", {"电池"}),
        ("沪深两市今年半导体行业，查询市净率", {"半导体"}),
    ],
)
def test_prompt_industry_extraction_excludes_calendar_qualifiers(prompt, expected):
    assert ASharePlanValidator._extract_prompt_industries(prompt) == expected


def test_explicit_exact_industry_prompt_preserves_exact_matching():
    plan = QueryPlan(
        interpretation="Rank one exact taxonomy label.",
        intent=AnalysisIntent.model_validate(
            {
                "analysis_type": "rank_metric",
                "universe": {
                    "filters": [
                        {
                            "field": "industry",
                            "operator": "eq",
                            "value": "汽车整车",
                        }
                    ]
                },
                "metric": {"type": "pe", "as_of": "20260601"},
                "ranking": {"direction": "desc", "limit": 10},
            }
        ),
    )

    normalized = ASharePlanValidator.normalize_prompt_classifications(
        "行业字段精确等于汽车整车行业时，查询市盈率前10",
        plan,
    )

    assert normalized.intent.universe.filters[0].operator == "eq"


@pytest.mark.parametrize(
    ("intent_update", "message"),
    [
        ({"universe": {"filters": []}}, "omitted explicit industry"),
        ({"metric": {"type": "pb", "as_of": "20260807"}}, "prompt metric"),
        ({"ranking": {"direction": "desc", "limit": 9}}, "ranking limit"),
        ({"ranking": {"direction": "asc", "limit": 10}}, "ranking direction"),
    ],
)
def test_prompt_intent_reconciliation_rejects_dropped_atomic_facts(
    intent_update,
    message,
):
    payload = {
        "analysis_type": "rank_metric",
        "universe": {
            "filters": [
                {"field": "industry", "operator": "eq", "value": "汽车"}
            ]
        },
        "metric": {"type": "pe", "as_of": "20260807"},
        "ranking": {"direction": "desc", "limit": 10},
    }
    payload.update(intent_update)
    plan = QueryPlan(
        interpretation="Rank the requested metric.",
        intent=AnalysisIntent.model_validate(payload),
    )

    with pytest.raises(PlanValidationError, match=message):
        ASharePlanValidator.validate_prompt_intent_coverage(
            "A股汽车行业市盈率前十的公司",
            plan,
        )


def test_prompt_intent_reconciliation_requires_special_treatment_exclusion():
    payload = {
        "analysis_type": "rank_metric",
        "universe": {
            "filters": [
                {"field": "industry", "operator": "eq", "value": "汽车"}
            ]
        },
        "metric": {
            "type": "pe",
            "as_of": "20260601",
            "filters": [{"field": "pe", "operator": "gt", "value": 0}],
        },
        "ranking": {"direction": "asc", "limit": 10},
    }
    missing_exclusion = QueryPlan(
        interpretation="Rank listed automotive securities.",
        intent=AnalysisIntent.model_validate(payload),
    )

    with pytest.raises(PlanValidationError, match="special-treatment exclusion"):
        ASharePlanValidator.validate_prompt_intent_coverage(
            "A股汽车行业排除ST和退市股票后，市盈率最低的10家公司",
            missing_exclusion,
        )

    payload["universe"]["filters"].append(
        {"field": "name", "operator": "not_contains", "value": "ST"}
    )
    complete = QueryPlan(
        interpretation="Rank listed non-ST automotive securities.",
        intent=AnalysisIntent.model_validate(payload),
    )

    assert (
        ASharePlanValidator.validate_prompt_intent_coverage(
            "A股汽车行业排除ST和退市股票后，市盈率最低的10家公司",
            complete,
        )
        is complete
    )


def test_compiler_binds_generic_negative_universe_filters_before_ranking():
    intent = AnalysisIntent.model_validate(
        {
            "analysis_type": "rank_metric",
            "universe": {
                "filters": [
                    {"field": "industry", "operator": "eq", "value": "汽车"},
                    {
                        "field": "name",
                        "operator": "not_contains",
                        "value": "ST",
                    },
                ]
            },
            "metric": {
                "type": "pe",
                "as_of": "20260601",
                "filters": [{"field": "pe", "operator": "gt", "value": 0}],
            },
            "ranking": {"direction": "asc", "limit": 10},
        }
    )
    plan = AnalysisService._compile_intent(
        QueryPlan(
            interpretation="Rank one listed non-ST industry universe.",
            intent=intent,
            requirements=[
                {
                    "requirement": "Apply all universe and metric predicates.",
                    "status": "covered",
                    "implementation": "Compile typed predicates locally.",
                    "evidence": "The intent declares each predicate.",
                }
            ],
        )
    )

    validated = ASharePlanValidator(
        FakeMarketDataProvider(stock_frame=pd.DataFrame())
    ).validate(plan)

    universe_query = validated.queries[1]
    assert universe_query.params == {"list_status": "L"}
    assert [row_filter.model_dump() for row_filter in universe_query.filters] == [
        {"field": "industry", "operator": "eq", "value": "汽车"},
        {"field": "name", "operator": "not_contains", "value": "ST"},
    ]
    assert [constraint.operator for constraint in validated.constraints] == [
        "gt",
        "eq",
        "not_contains",
    ]
    assert validated.constraints[-1].enforcement_step_index < 3


def test_validator_rejects_typed_ranking_with_wrong_snapshot_or_order():
    intent = AnalysisIntent.model_validate(
        {
            "analysis_type": "rank_metric",
            "metric": {"type": "total_mv", "as_of": "20260807"},
            "ranking": {"direction": "desc", "limit": 6},
        }
    )
    base_plan = AnalysisService._compile_intent(
        QueryPlan(
            interpretation="Rank one snapshot metric.",
            intent=intent,
            requirements=[
                {
                    "requirement": "Use one declared snapshot.",
                    "status": "covered",
                    "implementation": "Compile the snapshot locally.",
                    "evidence": "The intent declares its as-of date.",
                }
            ],
        )
    )
    wrong_snapshot = base_plan.model_copy(deep=True)
    wrong_snapshot.queries[0].params["trade_date"] = "20260806"

    with pytest.raises(PlanValidationError, match="typed as-of date"):
        ASharePlanValidator(
            FakeMarketDataProvider(stock_frame=pd.DataFrame())
        ).validate(wrong_snapshot)

    wrong_order = base_plan.model_copy(deep=True)
    wrong_order.result_pipeline.steps[-2:] = list(
        reversed(wrong_order.result_pipeline.steps[-2:])
    )

    with pytest.raises(PlanValidationError, match="adjacent sort and limit"):
        ASharePlanValidator(
            FakeMarketDataProvider(stock_frame=pd.DataFrame())
        ).validate(wrong_order)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                {"ts_code": "A", "trade_date": "20260807", "pe": 20.0},
                {"ts_code": "A", "trade_date": "20260807", "pe": 10.0},
            ],
            "duplicate security identifiers",
        ),
        (
            [
                {"ts_code": "A", "trade_date": "20260807", "pe": 20.0},
                {"ts_code": "B", "trade_date": "20260806", "pe": 10.0},
            ],
            "mixes multiple trading snapshots",
        ),
    ],
)
def test_ranking_result_invariants_reject_invalid_security_grain(rows, message):
    source = QueryResult(
        query_id="source",
        provider="test",
        operation="source",
        status="success",
        rows=rows,
        row_count=len(rows),
    )
    pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "source",
            "output_query_id": "ranked",
            "steps": [
                {"operation": "drop_missing", "fields": ["pe"]},
                {"operation": "sort", "field": "pe", "direction": "desc"},
                {"operation": "limit", "count": 10},
            ],
        }
    )

    with pytest.raises(ValueError, match=message):
        ResultPipelineExecutor().execute(pipeline, source)


def test_compiled_metric_ranking_excludes_nonmembers_before_limit():
    intent = AnalysisIntent.model_validate(
        {
            "analysis_type": "rank_metric",
            "universe": {
                "filters": [
                    {
                        "field": "market",
                        "operator": "eq",
                        "value": "Growth",
                    }
                ]
            },
            "metric": {"type": "pb", "as_of": "20260807"},
            "ranking": {"direction": "desc", "limit": 2},
        }
    )
    plan = AnalysisService._compile_intent(
        QueryPlan(
            interpretation="Rank valuation inside one market segment.",
            intent=intent,
        )
    )
    metric_result = QueryResult(
        query_id="ranking_metric_snapshot",
        provider="tushare",
        operation="daily_basic",
        status="success",
        rows=[
            {"ts_code": "A", "trade_date": "20260807", "pb": 100.0},
            {"ts_code": "B", "trade_date": "20260807", "pb": 30.0},
            {"ts_code": "C", "trade_date": "20260807", "pb": 20.0},
            {"ts_code": "D", "trade_date": "20260807", "pb": 10.0},
        ],
        row_count=4,
    )
    universe_result = QueryResult(
        query_id="ranking_security_universe",
        provider="tushare",
        operation="stock_basic",
        status="success",
        rows=[
            {"ts_code": "B", "market": "Growth"},
            {"ts_code": "D", "market": "Growth"},
        ],
        row_count=2,
    )

    result = ResultPipelineExecutor().execute(
        plan.result_pipeline,
        metric_result,
        {universe_result.query_id: universe_result},
    )

    assert [row["ts_code"] for row in result.rows] == ["B", "D"]


def test_workflow_applies_classified_universe_to_period_return_ranking():
    intent = AnalysisIntent.model_validate(
        {
            "analysis_type": "rank_metric",
            "universe": {
                "filters": [
                    {
                        "field": "exchange",
                        "operator": "eq",
                        "value": "SSE",
                    }
                ]
            },
            "metric": {
                "type": "period_return",
                "window": {"start": "20260701", "end": "20260731"},
            },
            "ranking": {"direction": "desc", "limit": 5},
        }
    )
    compiled = AnalysisService._compile_intent(
        QueryPlan(
            interpretation="Rank interval returns inside one exchange.",
            intent=intent,
            requirements=[
                {
                    "requirement": "Restrict the exchange before ranking returns.",
                    "status": "covered",
                    "implementation": "Compile typed universe membership locally.",
                    "evidence": "The intent declares an exchange predicate.",
                }
            ],
        )
    )

    validated = ASharePlanValidator(
        FakeMarketDataProvider(stock_frame=pd.DataFrame())
    ).validate(compiled)

    assert [query.operation for query in validated.queries] == [
        "daily",
        "stock_basic",
    ]
    assert [step.operation for step in validated.result_pipeline.steps] == [
        "drop_missing",
        "match_source",
        "filter",
        "sort",
        "limit",
    ]
    assert validated.result_pipeline.steps[-2].field == "period_return_pct"
    assert validated.constraints[0].field == "exchange"


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


def test_workflow_preserves_valuation_annotation_after_period_return_ranking():
    plan = QueryPlan.model_validate(
        {
            "interpretation": (
                "Rank 20260601 through 20260630 A-shares by return and attach PE "
                "to the top ten."
            ),
            "intent": {
                "analysis_type": "rank_metric",
                "metric": {
                    "type": "period_return",
                    "window": {"start": "20260601", "end": "20260630"},
                },
                "ranking": {"direction": "desc", "limit": 10},
            },
            "requirements": [
                {
                    "requirement": "Rank June returns.",
                    "status": "covered",
                    "evidence": "daily provides closing prices.",
                },
                {
                    "requirement": "Attach PE to the ranked securities.",
                    "status": "covered",
                    "evidence": "daily_basic provides pe.",
                },
            ],
        }
    )

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "大A在6月上涨最多的股票前十，对应的市盈率也标记下\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260601\n"
        "event_end_date=20260630\n"
        "</trusted_analysis_window>",
    )

    assert [query.operation for query in result.queries] == ["daily", "daily_basic"]
    assert result.result_pipeline.source_query_id == "valuation_period_prices"
    assert [step.operation for step in result.result_pipeline.steps] == [
        "sort",
        "limit",
        "join_fields",
    ]
    assert result.result_pipeline.steps[0].field == "period_return_pct"
    assert result.result_pipeline.steps[1].count == 10
    assert result.result_pipeline.steps[2].right_source_query_id == (
        "valuation_snapshot"
    )
    assert {output.field for output in result.answer_contract.outputs} == {
        "ts_code",
        "period_return_pct",
        "pe",
    }
    ASharePlanValidator(FakeMarketDataProvider()).validate(result)


def test_workflow_precompiles_period_return_ranking_before_valuation_annotation():
    result = AnalysisService._compile_known_request(
        "大A在6月上涨最多的股票前十，对应的市盈率也标记下\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260601\n"
        "event_end_date=20260630\n"
        "</trusted_analysis_window>"
    )

    assert result is not None
    assert [query.operation for query in result.queries] == ["daily", "daily_basic"]
    assert result.queries[0].params == {
        "start_date": "20260601",
        "end_date": "20260630",
    }
    assert result.queries[1].params == {"trade_date": "20260630"}
    assert [step.operation for step in result.result_pipeline.steps] == [
        "sort",
        "limit",
        "join_fields",
    ]
    assert result.result_pipeline.steps[0].field == "period_return_pct"
    ASharePlanValidator(FakeMarketDataProvider()).validate(result)


def test_workflow_precompiles_period_return_ranking_before_market_cap_annotation():
    result = AnalysisService._compile_known_request(
        "大A在今年6月上涨最多的股票前十，对应的总市值也标记下\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260601\n"
        "event_end_date=20260630\n"
        "</trusted_analysis_window>"
    )

    assert result is not None
    assert [query.operation for query in result.queries] == ["daily", "daily_basic"]
    assert result.queries[1].fields == ["ts_code", "total_mv"]
    sort_step, limit_step, join_step = result.result_pipeline.steps
    assert (sort_step.field, sort_step.direction) == ("period_return_pct", "desc")
    assert limit_step.count == 10
    assert join_step.fields == {"total_mv": "total_mv"}
    assert {output.field for output in result.answer_contract.outputs} == {
        "ts_code",
        "period_return_pct",
        "total_mv",
    }
    ASharePlanValidator(FakeMarketDataProvider()).validate(result)


def test_workflow_keeps_explicit_market_cap_selection_as_snapshot_ranking():
    result = AnalysisService._compile_known_request(
        "总市值最高的前10只股票最近一个月涨了多少\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260710\n"
        "event_end_date=20260810\n"
        "</trusted_analysis_window>"
    )

    assert result is not None
    assert result.result_pipeline.source_query_id == "valuation_snapshot"
    sort_step, limit_step, join_step = result.result_pipeline.steps
    assert (sort_step.field, sort_step.direction) == ("total_mv", "desc")
    assert limit_step.count == 10
    assert join_step.right_source_query_id == "valuation_period_prices"
    ASharePlanValidator(FakeMarketDataProvider()).validate(result)


def test_workflow_preserves_precompiled_return_ranking_during_normalization():
    prompt = (
        "\u5927A\u5728\u4eca\u5e746\u6708\u4e0a\u6da8\u6700\u591a\u7684\u80a1\u7968\u524d\u5341\uff0c\u5bf9\u5e94\u7684\u5e02\u76c8\u7387\u4e5f\u6807\u8bb0\u4e0b\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260601\n"
        "event_end_date=20260630\n"
        "</trusted_analysis_window>"
    )
    precompiled = AnalysisService._compile_known_request(prompt)

    result = AnalysisService._normalize_plan_for_request(precompiled, prompt)

    assert result.result_pipeline.output_query_id == "period_return_valuation"
    assert result.result_pipeline.source_query_id == "valuation_period_prices"
    sort_step, limit_step, join_step = result.result_pipeline.steps
    assert (sort_step.field, sort_step.direction) == ("period_return_pct", "desc")
    assert limit_step.count == 10
    assert join_step.right_source_query_id == "valuation_snapshot"


def test_workflow_uses_return_ranking_when_valuation_annotation_comes_first():
    result = AnalysisService._compile_known_request(
        "\u5e02\u76c8\u7387\u4e5f\u6807\u8bb0\u4e00\u4e0b\uff0c\u5217\u51fa\u5927A 6\u6708\u4e0a\u6da8\u6700\u591a\u7684\u524d10\u53ea\u80a1\u7968\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260601\n"
        "event_end_date=20260630\n"
        "</trusted_analysis_window>"
    )

    assert result is not None
    assert result.result_pipeline.source_query_id == "valuation_period_prices"
    sort_step, limit_step, join_step = result.result_pipeline.steps
    assert (sort_step.operation, sort_step.field, sort_step.direction) == (
        "sort",
        "period_return_pct",
        "desc",
    )
    assert (limit_step.operation, limit_step.count) == ("limit", 10)
    assert join_step.right_source_query_id == "valuation_snapshot"
    ASharePlanValidator(FakeMarketDataProvider()).validate(result)


def test_workflow_keeps_explicit_high_pe_selection_as_valuation_ranking():
    result = AnalysisService._compile_known_request(
        "\u9ad8PE\u7684\u524d20\u53ea\u80a1\u7968\u6700\u8fd1\u4e00\u4e2a\u6708\u6da8\u4e86\u591a\u5c11\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260710\n"
        "event_end_date=20260810\n"
        "</trusted_analysis_window>"
    )

    assert result is not None
    assert result.result_pipeline.source_query_id == "valuation_snapshot"
    sort_step, limit_step, join_step = result.result_pipeline.steps
    assert (sort_step.operation, sort_step.field, sort_step.direction) == (
        "sort",
        "pe",
        "desc",
    )
    assert (limit_step.operation, limit_step.count) == ("limit", 20)
    assert join_step.right_source_query_id == "valuation_period_prices"
    ASharePlanValidator(FakeMarketDataProvider()).validate(result)


def test_workflow_completes_valuation_return_plan_from_trusted_window():
    plan = make_daily_plan()
    plan.queries[0].operation = "daily_basic"
    plan.queries[0].fields = ["ts_code", "pe"]
    plan.queries[0].params = {"trade_date": "20260807"}

    result = AnalysisService._normalize_plan_for_request(
        plan,
        (
            "市盈率p90线上的选10家公司，看看最近半年的涨跌幅\n"
            "<trusted_analysis_window>\n"
            "event_start_date=20260207\n"
            "event_end_date=20260807\n"
            "</trusted_analysis_window>"
        ),
    )

    assert [query.operation for query in result.queries] == ["daily_basic", "daily"]
    assert result.queries[1].params == {
        "start_date": "20260207",
        "end_date": "20260807",
    }
    assert result.queries[1].transform == "period_return_by_ts_code"
    assert result.answer_contract.result_query_id == "valuation_period_return"


def test_workflow_precompiles_known_valuation_return_family():
    result = AnalysisService._compile_known_request(
        (
            "市净率最低的50家公司今年以来的收益率\n"
            "<trusted_analysis_window>\n"
            "event_start_date=20260101\n"
            "event_end_date=20260807\n"
            "</trusted_analysis_window>"
        )
    )

    assert result is not None
    assert [query.operation for query in result.queries] == ["daily_basic", "daily"]
    sort_step, limit_step, join_step = result.result_pipeline.steps
    assert (sort_step.field, sort_step.direction) == ("pb", "asc")
    assert limit_step.count == 50
    assert join_step.cardinality == "many_to_one"
    ASharePlanValidator(FakeMarketDataProvider()).validate(result)


def test_workflow_precompiles_multi_factor_valuation_screen():
    result = AnalysisService._compile_known_request(
        (
            "找低PE、低PB、高股息率的十只股票\n"
            "<trusted_analysis_window>\n"
            "event_start_date=20260807\n"
            "event_end_date=20260807\n"
            "</trusted_analysis_window>"
        )
    )

    assert result is not None
    assert [query.operation for query in result.queries] == ["daily_basic"]
    assert [step.operation for step in result.result_pipeline.steps] == [
        "drop_missing",
        "filter",
        "filter",
        "quantile_filter",
        "quantile_filter",
        "sort",
        "limit",
    ]
    assert result.result_pipeline.steps[-1].count == 10
    ASharePlanValidator(FakeMarketDataProvider()).validate(result)


def test_workflow_precompiles_unchanged_market_count():
    result = AnalysisService._compile_known_request(
        "How many A-shares closed unchanged on 2026-04-30?"
    )

    assert result is not None
    assert result.queries[0].params == {"trade_date": "20260430"}
    assert [step.operation for step in result.result_pipeline.steps] == [
        "filter",
        "summarize",
    ]
    ASharePlanValidator(FakeMarketDataProvider()).validate(result)


def test_workflow_aligns_moneyflow_summary_answer_contract():
    plan = make_daily_plan()
    plan.queries[0].operation = "moneyflow"
    plan.queries[0].fields = ["ts_code", "trade_date"]

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "贵州茅台近一月大单小单资金流向",
    )

    assert result.answer_contract.result_query_id == (
        "security_moneyflow_comparison"
    )
    assert {output.field for output in result.answer_contract.outputs} == {
        "large_order_net_amount",
        "small_order_net_amount",
        "trading_day_count",
    }


def test_workflow_precompiles_security_moneyflow_comparison():
    result = AnalysisService._compile_known_request(
        (
            "贵州茅台近一月大单小单资金流向\n"
            "<trusted_analysis_window>\n"
            "event_start_date=20260707\n"
            "event_end_date=20260807\n"
            "</trusted_analysis_window>"
        )
    )

    assert result is not None
    assert result.queries[0].params == {
        "ts_code": "600519.SH",
        "start_date": "20260707",
        "end_date": "20260807",
    }
    assert result.answer_contract.result_query_id == (
        "security_moneyflow_comparison"
    )


def test_workflow_precompiles_cross_statement_financial_comparison():
    class FinancialProvider(FakeMarketDataProvider):
        def supports(self, operation):
            return operation in {"fina_indicator", "cashflow"}

    result = AnalysisService._compile_known_request(
        (
            "比较中国平安近三年ROE和经营现金流\n"
            "<trusted_analysis_window>\n"
            "event_start_date=20230807\n"
            "event_end_date=20260807\n"
            "</trusted_analysis_window>"
        )
    )

    assert result is not None
    assert result.execution_plan is not None
    query_nodes = [
        node for node in result.execution_plan.nodes if node.kind == "query"
    ]
    assert len(query_nodes) == 6
    assert {node.query.operation for node in query_nodes} == {
        "fina_indicator",
        "cashflow",
    }
    assert result.execution_plan.result_node_id == "financial_metric_comparison"
    ASharePlanValidator(FinancialProvider()).validate(result)


def test_workflow_precompiles_suspension_count_ranking():
    result = AnalysisService._compile_known_request(
        (
            "过去一个月停牌天数最多的股票\n"
            "<trusted_analysis_window>\n"
            "event_start_date=20260707\n"
            "event_end_date=20260807\n"
            "</trusted_analysis_window>"
        )
    )

    assert result is not None
    assert result.queries[0].operation == "suspend_d"
    assert [step.operation for step in result.result_pipeline.steps] == [
        "aggregate",
        "sort",
        "limit",
    ]
    assert result.result_pipeline.steps[0].group_by == ["ts_code"]


def test_workflow_precompiles_english_resumption_request():
    result = AnalysisService._compile_known_request(
        "Which stocks resumed trading on 2026-05-08?\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260508\n"
        "event_end_date=20260508\n"
        "</trusted_analysis_window>"
    )

    assert result is not None
    assert result.queries[0].operation == "suspend_d"
    assert result.queries[0].params == {"trade_date": "20260508"}


def test_workflow_precompiles_limit_up_streak_before_model_planning():
    result = AnalysisService._compile_known_request(
        "统计20250101至20251231三连板事件未来三个月的收益表现\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20250101\n"
        "event_end_date=20251231\n"
        "outcome_offset_value=3\n"
        "outcome_offset_unit=month\n"
        "</trusted_analysis_window>"
    )

    assert result is not None
    assert {query.operation for query in result.queries} == {
        "daily",
        "limit_list_d",
    }
    assert any(
        step.operation == "match_at_offset"
        and step.offset_value == 3
        and step.offset_unit == "month"
        for step in result.result_pipeline.steps
    )


def test_workflow_precompiles_industry_valuation_and_dividend_view():
    result = AnalysisService._compile_known_request(
        "A股2026年电池行业，市盈率和分红数据\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260101\n"
        "event_end_date=20260807\n"
        "</trusted_analysis_window>"
    )

    assert result is not None
    assert {query.operation for query in result.queries} == {
        "stock_basic",
        "daily_basic",
        "dividend",
    }
    assert result.result_pipeline.output_query_id == (
        "industry_valuation_dividend_result"
    )


def test_workflow_binds_market_wide_unlocks_to_the_resolved_window():
    plan = make_daily_plan()
    plan.queries[0].operation = "share_float"
    plan.queries[0].params = {}
    plan.queries[0].fields = ["ts_code", "float_date", "float_ratio"]

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "列出2026年解禁股数占总股本比例最高的10只股票\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260101\n"
        "event_end_date=20261231\n"
        "</trusted_analysis_window>",
    )

    assert result.queries[0].params == {
        "start_date": "20260101",
        "end_date": "20261231",
    }
    assert ASharePlanValidator._uses_bounded_date_fanout(result.queries[0])


def test_workflow_precompiles_repurchase_ranking_at_security_grain():
    result = AnalysisService._compile_known_request(
        "Rank 2026 A-share repurchase plans by announced upper amount."
    )

    assert result is not None
    assert [step.operation for step in result.result_pipeline.steps] == [
        "drop_missing",
        "aggregate",
        "sort",
    ]
    assert result.result_pipeline.steps[1].group_by == ["ts_code"]


def test_workflow_precompiles_positive_dividend_yield_ranking():
    result = AnalysisService._compile_known_request(
        (
            "Top 20 A-shares by dividend yield, excluding missing or zero yields.\n"
            "<trusted_analysis_window>\n"
            "event_start_date=20260807\n"
            "event_end_date=20260807\n"
            "</trusted_analysis_window>"
        )
    )

    assert result is not None
    assert result.intent.analysis_type == "field_analysis"
    assert result.intent.filters[0].field == "dv_ttm"
    assert result.intent.ranking.limit == 20


def test_workflow_precompiles_market_cap_filtered_pb_ranking():
    result = AnalysisService._compile_known_request(
        (
            "找出总市值超过1000亿且PB最低的前10只\n"
            "<trusted_analysis_window>\n"
            "event_start_date=20260807\n"
            "event_end_date=20260807\n"
            "</trusted_analysis_window>"
        )
    )

    assert result is not None
    assert result.intent.analysis_type == "field_analysis"
    assert result.intent.filters[0].value == 10_000_000
    assert result.intent.analysis_field == "pb"


def test_workflow_precompiles_product_segment_ranking():
    result = AnalysisService._compile_known_request(
        (
            "哪个产品占贵州茅台营业收入比例最高\n"
            "<trusted_analysis_window>\n"
            "event_start_date=20260807\n"
            "event_end_date=20260807\n"
            "</trusted_analysis_window>"
        )
    )

    assert result is not None
    assert result.queries[0].params == {
        "ts_code": "600519.SH",
        "period": "20251231",
        "type": "P",
    }
    assert result.result_pipeline.steps[-1].count == 1


def test_workflow_precompiles_geographic_business_segments():
    result = AnalysisService._compile_known_request(
        "List Ping An Bank's domestic and overseas segment revenue for 2025."
    )

    assert result is not None
    assert result.queries[0].params == {
        "ts_code": "000001.SZ",
        "period": "20251231",
        "type": "D",
    }


def test_full_market_event_horizon_requires_background_execution():
    plan = make_daily_plan()
    plan.queries[0].params = {
        "start_date": "20250101",
        "end_date": "20251231",
    }
    plan.queries[0].fields = ["ts_code", "trade_date", "close"]
    plan.result_pipeline = ResultPipeline(
        source_query_id=plan.queries[0].query_id,
        output_query_id="event_outcome",
        steps=[
            ResultPipelineStep(
                operation="match_at_offset",
                field="close",
                output_field="future_close",
                matched_date_output_field="future_trade_date",
                group_by=["ts_code"],
                order_by="trade_date",
                offset_value=1,
                offset_unit="month",
            )
        ],
    )

    assert AnalysisService._requires_background_execution(plan) is True


def test_workflow_precompiles_market_breadth_categories():
    result = AnalysisService._compile_known_request(
        "昨天全市场红盘和绿盘股票数量\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260807\n"
        "event_end_date=20260807\n"
        "</trusted_analysis_window>"
    )

    assert result is not None
    assert result.intent is None
    assert result.queries[0].operation == "daily"
    assert result.queries[0].params == {"trade_date": "20260807"}
    assert [step.output_field for step in result.result_pipeline.steps[:-1]] == [
        "up_count",
        "down_count",
    ]
    assert {
        aggregation.output_field
        for aggregation in result.result_pipeline.steps[-1].aggregations
    } == {"up_count", "down_count"}


def test_market_breadth_precompiler_does_not_capture_limit_up_event_studies():
    result = AnalysisService._compile_known_request(
        "A股20260102～20260106连续涨停三天的情况下，"
        "接下来一个月的上涨情况数据分析\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260102\n"
        "event_end_date=20260106\n"
        "outcome_offset_value=1\n"
        "outcome_offset_unit=month\n"
        "</trusted_analysis_window>"
    )

    assert result is not None
    assert {query.operation for query in result.queries} == {
        "daily",
        "limit_list_d",
    }
    assert result.result_pipeline.output_query_id == "limit_up_streak_outcome"


def test_workflow_precompiles_block_trade_amount_ranking():
    result = AnalysisService._compile_known_request(
        "本月大宗交易成交金额最多的20只股票\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260801\n"
        "event_end_date=20260810\n"
        "</trusted_analysis_window>"
    )

    assert result is not None
    assert result.queries[0].operation == "block_trade"
    assert result.queries[0].params == {
        "start_date": "20260801",
        "end_date": "20260810",
    }
    assert result.result_pipeline.steps[1].aggregations[0].output_field == (
        "total_amount"
    )
    assert result.result_pipeline.steps[-1].count == 20


def test_workflow_precompiles_large_order_buy_ranking():
    result = AnalysisService._compile_known_request(
        "最近交易日A股大单买入金额排名\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260807\n"
        "event_end_date=20260807\n"
        "</trusted_analysis_window>"
    )

    assert result is not None
    assert result.queries[0].operation == "moneyflow"
    assert result.queries[0].fields == [
        "ts_code",
        "trade_date",
        "buy_lg_amount",
        "buy_elg_amount",
    ]
    derive = result.result_pipeline.steps[0]
    assert (derive.field, derive.right_field, derive.output_field) == (
        "buy_lg_amount",
        "buy_elg_amount",
        "large_buy_amount",
    )


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


def test_validator_rejects_model_authored_snapshot_metric_ranking():
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

    with pytest.raises(
        PlanValidationError,
        match="must use rank_metric intent",
    ):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


def test_validator_tracks_fields_across_inner_join_union_and_projection():
    plan = QueryPlan(
        interpretation="Combine validated tabular sources.",
        requirements=[
            {
                "requirement": "Join and append compatible result rows.",
                "status": "covered",
                "implementation": "Use validated relational operators.",
                "evidence": "Every source declares an exact field contract.",
            }
        ],
        queries=[
            DataQuery(
                query_id="left",
                operation="daily",
                params={"trade_date": "20260807"},
                fields=["ts_code", "close"],
                purpose="Retrieve the primary rows.",
            ),
            DataQuery(
                query_id="labels",
                operation="daily_basic",
                params={"trade_date": "20260807"},
                fields=["ts_code", "pe"],
                purpose="Retrieve one joined field.",
            ),
            DataQuery(
                query_id="appended",
                operation="daily",
                params={"trade_date": "20260806"},
                fields=["ts_code", "close", "valuation"],
                purpose="Retrieve schema-compatible appended rows.",
            ),
        ],
        result_pipeline={
            "source_query_id": "left",
            "output_query_id": "combined",
            "steps": [
                {
                    "operation": "inner_join",
                    "right_source_query_id": "labels",
                    "join_on": ["ts_code"],
                    "fields": {"pe": "valuation"},
                    "cardinality": "many_to_one",
                },
                {
                    "operation": "union_all",
                    "right_source_query_id": "appended",
                },
                {"operation": "distinct", "fields": ["ts_code"]},
                {
                    "operation": "select_fields",
                    "fields": ["ts_code", "valuation"],
                },
            ],
        },
    )

    validated = ASharePlanValidator(FakeMarketDataProvider()).validate(plan)

    assert validated.result_pipeline.output_query_id == "combined"


def test_validator_rejects_having_without_aggregate():
    plan = make_daily_plan()
    plan.result_pipeline = ResultPipeline.model_validate(
        {
            "source_query_id": "market_direction",
            "output_query_id": "invalid-having",
            "steps": [
                {
                    "operation": "having",
                    "field": "change",
                    "comparison": "gt",
                    "value": 0,
                }
            ],
        }
    )

    with pytest.raises(
        PlanValidationError,
        match="having requires an earlier aggregate",
    ):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


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


def test_workflow_preserves_planned_disclosure_operation():
    plan = make_daily_plan()
    plan.queries[0].operation = "income"

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "\u67e5\u770b600519.SH\u8fd1\u4e09\u5e74\u4e3b\u8425\u4e1a\u52a1\u6bdb\u5229\u7387\u53d8\u5316",
    )

    assert result.queries[0].operation == "income"


def test_workflow_preserves_market_wide_forecast_for_fanout_validation():
    plan = make_daily_plan()
    plan.queries[0].operation = "forecast"
    plan.queries[0].params = {"period": "20260630"}

    result = AnalysisService._normalize_plan_for_request(
        plan,
        "\u7edf\u8ba12026\u5e74\u4e0a\u534a\u5e74\u9884\u4e8f\u516c\u53f8\u6570\u91cf",
    )

    assert result.feasibility == "supported"
    assert result.queries[0].operation == "forecast"
    assert result.queries[0].params == {"period": "20260630"}


def test_deepseek_normalizes_identity_projection_mapping():
    raw_plan = {
        "queries": [],
        "execution_plan": {
            "nodes": [
                {
                    "node_id": "projected",
                    "kind": "compute",
                    "input_result_ids": ["source"],
                    "step": {
                        "operation": "select_fields",
                        "fields": {"ts_code": "ts_code", "name": "name"},
                    },
                }
            ],
            "result_node_id": "projected",
        },
    }

    DeepSeekQueryPlanner._normalize_raw_query_defaults(raw_plan)

    assert raw_plan["execution_plan"]["nodes"][0]["step"]["fields"] == [
        "ts_code",
        "name",
    ]


def test_deepseek_rejects_requirement_evidence_missing_from_execution():
    plan = make_daily_plan()
    plan.requirements[0].evidence = "The share_float operation supplies unlocks."

    with pytest.raises(ValueError, match="share_float"):
        DeepSeekQueryPlanner._validate_requirement_operation_lineage(
            plan,
            [
                DataOperation(name="daily", description="Daily prices."),
                DataOperation(name="share_float", description="Unlock schedules."),
            ],
        )


def test_deepseek_binds_constraint_to_unique_query_identifier():
    raw_plan = {
        "queries": [
            {
                "query_id": "actual_query",
                "operation": "daily",
                "params": {"trade_date": "20260807"},
                "fields": ["ts_code", "trade_date"],
            }
        ],
        "constraints": [
            {
                "constraint_id": "date_constraint",
                "scope": "result",
                "field": "trade_date",
                "operator": "eq",
                "value": "20260807",
                "query_id": "stale_alias",
            }
        ],
    }

    DeepSeekQueryPlanner._normalize_raw_query_defaults(raw_plan)

    assert raw_plan["constraints"][0]["query_id"] == "actual_query"


def test_validator_accepts_year_constraint_as_complete_date_range():
    query = DataQuery(
        query_id="annual_disclosures",
        operation="share_float",
        params={"start_date": "20260101", "end_date": "20261231"},
        fields=["ts_code", "float_date", "float_ratio"],
        purpose="Read one calendar year of disclosures.",
    )
    constraint = QueryConstraint(
        constraint_id="calendar_year",
        scope="result",
        field="float_year",
        operator="eq",
        value=2026,
        query_id=query.query_id,
    )

    assert ASharePlanValidator._constraint_enforced_by_query(constraint, query)


def test_validator_rejects_registered_operation_with_partial_date_range():
    plan = make_daily_plan()
    plan.queries[0].operation = "share_float"
    plan.queries[0].params = {"start_date": "20261001"}
    plan.queries[0].fields = ["ts_code", "float_date"]

    with pytest.raises(
        PlanValidationError,
        match="share_float requires start_date and end_date together",
    ):
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)


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
    assert result.clarification_options == []


def test_planner_removes_redundant_join_key_identity_mapping():
    plan = QueryPlan(
        interpretation="Join the latest shareholder disclosure to company names.",
        queries=[
            DataQuery(
                query_id="holders",
                operation="stk_holdernumber",
                fields=["ts_code", "ann_date", "end_date", "holder_num"],
                purpose="Retrieve shareholder counts.",
            ),
            DataQuery(
                query_id="companies",
                operation="stock_basic",
                fields=["ts_code", "name"],
                purpose="Retrieve company names.",
            ),
        ],
        result_pipeline={
            "source_query_id": "holders",
            "output_query_id": "ranked-holders",
            "steps": [
                {
                    "operation": "join_fields",
                    "right_source_query_id": "companies",
                    "join_on": ["ts_code"],
                    "fields": {"ts_code": "ts_code", "name": "name"},
                    "cardinality": "many_to_one",
                }
            ],
        },
    )

    DeepSeekQueryPlanner._normalize_join_field_mappings(plan)

    assert plan.result_pipeline.steps[0].fields == {"name": "name"}


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


@pytest.mark.parametrize(
    "operation",
    ["top10_floatholders", "share_float", "stk_holdertrade", "forecast"],
)
def test_validator_rejects_security_fanout_template_without_universe(operation):
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
                operation=operation,
                params=(
                    {"ann_date": "20260727"}
                    if operation == "share_float"
                    else {"end_date": "20260727"}
                ),
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


def test_analysis_labels_result_validation_failures():
    plan = QueryPlan(
        interpretation="Summarize one validated source.",
        requirements=[
            {
                "requirement": "Return one summary.",
                "status": "covered",
                "implementation": "Use a deterministic result pipeline.",
                "evidence": "The pipeline summarizes the source field.",
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
            "output_query_id": "summary",
            "steps": [
                {
                    "operation": "summarize",
                    "aggregations": [
                        {
                            "output_field": "value_count",
                            "field": "value",
                            "function": "count",
                        }
                    ],
                }
            ],
        },
    )

    class StaticPlanner:
        name = "static"

        def plan(self, request, candidate_operations):
            return plan

    class FailingValidationExecutor:
        def execute(self, pipeline, source, sources=None):
            raise ResultValidationError("independent result check failed")

    provider = FakeMarketDataProvider(frame=pd.DataFrame([{"value": 1.0}]))
    service = AnalysisService(
        planner=StaticPlanner(),
        provider=provider,
        validator=ASharePlanValidator(provider),
        executor=DataQueryExecutor(provider),
        result_pipeline_executor=FailingValidationExecutor(),
    )

    response = service.analyze(
        "request-validation-failure",
        AnalysisRequest(prompt="Return one summary."),
        api_route="/api/analysis",
    )

    assert response.status == "error"
    assert response.results[0].error.code == "RESULT_VALIDATION_FAILED"
    assert response.results[0].error.message == "independent result check failed"


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
    assert response.error.code == "BACKGROUND_TASK_REQUIRED"
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


def test_executor_applies_generic_negative_text_and_membership_filters():
    frame = pd.DataFrame(
        [
            {"ts_code": "A", "name": "Alpha Auto", "area": "Shanghai"},
            {"ts_code": "B", "name": "ST Beta Auto", "area": "Shenzhen"},
            {"ts_code": "C", "name": "Gamma Auto", "area": "Beijing"},
        ]
    )
    query = DataQuery(
        query_id="excluded-universe",
        operation="stock_basic",
        fields=["ts_code", "name", "area"],
        purpose="Apply controlled categorical exclusions.",
        filters=[
            {"field": "name", "operator": "not_contains", "value": "ST"},
            {"field": "area", "operator": "not_in", "value": ["Beijing"]},
        ],
    )

    result = DataQueryExecutor(
        FakeMarketDataProvider(stock_frame=frame)
    ).execute(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "success"
    assert result.rows == [{"ts_code": "A", "name": "Alpha Auto", "area": "Shanghai"}]


def test_executor_matches_broad_classification_descendants():
    frame = pd.DataFrame(
        [
            {"ts_code": "A", "name": "Alpha", "industry": "汽车整车"},
            {"ts_code": "B", "name": "Beta", "industry": "汽车零部件"},
            {"ts_code": "C", "name": "Gamma", "industry": "银行"},
        ]
    )
    query = DataQuery(
        query_id="broad-industry",
        operation="stock_basic",
        fields=["ts_code", "name", "industry"],
        purpose="Match one broad classification and its child labels.",
        filters=[
            {"field": "industry", "operator": "contains", "value": "汽车"}
        ],
    )

    result = DataQueryExecutor(
        FakeMarketDataProvider(stock_frame=frame)
    ).execute(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "success"
    assert [row["ts_code"] for row in result.rows] == ["A", "B"]


def test_negative_membership_filter_still_rejects_an_empty_set():
    with pytest.raises(ValueError, match="must not be empty"):
        DataFilter(
            field="area",
            operator="not_in",
            value=[],
        )


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


def test_disclosure_range_uses_exact_calendar_date_queries():
    class DisclosureProvider(FakeMarketDataProvider):
        def supports(self, operation):
            return operation == "share_float"

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
            return pd.DataFrame(
                [{"ts_code": "000001.SZ", "float_date": params["float_date"]}]
            )

    provider = DisclosureProvider()
    service = AnalysisService(
        planner=None,
        provider=provider,
        validator=ASharePlanValidator(provider),
        executor=DataQueryExecutor(provider),
    )
    query = DataQuery(
        query_id="unlock-range",
        operation="share_float",
        params={"start_date": "20261001", "end_date": "20261003"},
        fields=["ts_code", "float_date"],
        purpose="Read a bounded unlock schedule.",
    )

    result = service._execute_disclosure_range_by_date(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "success"
    assert result.row_count == 3
    assert result.completeness == "complete"
    assert result.retrieval_partition_count == 3
    assert "query_shape=bounded_unlock_range" in result.completeness_evidence
    assert "covered_dates=20261001..20261003" in result.completeness_evidence
    assert provider.calls == [
        ("share_float", {"float_date": "20261001"}),
        ("share_float", {"float_date": "20261002"}),
        ("share_float", {"float_date": "20261003"}),
    ]


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
    assert result.completeness == "complete"
    assert result.retrieval_partition_count == 2
    assert (
        "execution_strategy=full_market_boundary_snapshots"
        in result.completeness_evidence
    )
    assert "covered_boundaries=20260601..20260630" in result.completeness_evidence
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

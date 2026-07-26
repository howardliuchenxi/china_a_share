import json
from datetime import datetime, time as ClockTime, timedelta
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
    AnalysisRequest,
    DataFilter,
    DataOperation,
    DataQuery,
    QueryPlan,
    QueryResult,
    RequirementCoverage,
)
from china_a_share.core.errors import PlannerError
from china_a_share.planners.deepseek import DeepSeekQueryPlanner
from china_a_share.registry import TushareOperationCatalog


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
            "ths_member",
            "top10_floatholders",
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


def test_validator_rejects_operation_outside_provider_catalog():
    plan = make_daily_plan()
    plan.queries[0].operation = "us_daily"

    try:
        ASharePlanValidator(FakeMarketDataProvider()).validate(plan)
    except PlanValidationError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("Expected a plan validation error.")


def test_validator_downgrades_undeclared_derived_calculation():
    plan = make_daily_plan()
    plan.queries[0].aggregations = []
    plan.requirements[0].implementation = (
        "Join locally and calculate the average result."
    )

    result = ASharePlanValidator(FakeMarketDataProvider()).validate(plan)

    assert result.feasibility == "unsupported"
    assert result.queries == []
    assert result.requirements[0].status == "unsupported"
    assert "no deterministic local transform" in result.limitations[0]


def test_validator_preserves_covered_requirements_when_ranking_is_unsupported():
    plan = make_daily_plan()
    plan.queries[0].aggregations = []
    plan.requirements = [
        RequirementCoverage.model_validate(
            {
                "requirement": "Retrieve the complete A-share security universe.",
                "status": "covered",
                "implementation": "Use stock_basic.",
                "evidence": "stock_basic returns A-share security codes.",
            }
        ),
        RequirementCoverage.model_validate(
            {
                "requirement": "Calculate the approved retail holding proxy.",
                "status": "covered",
                "implementation": "Use top10_floatholders with cr10_float_trend.",
                "evidence": "The transform returns non_top10_float_ratio.",
            }
        ),
        RequirementCoverage.model_validate(
            {
                "requirement": "Rank securities and return the top ten.",
                "status": "covered",
                "implementation": "Apply local aggregation and ranking.",
                "evidence": "Sort the calculated ratios locally and take the top ten.",
            }
        ),
    ]

    result = ASharePlanValidator(FakeMarketDataProvider()).validate(plan)

    assert result.feasibility == "unsupported"
    assert [requirement.status for requirement in result.requirements] == [
        "covered",
        "covered",
        "unsupported",
    ]
    assert result.queries == []


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
    assert "limit_list_d with the native parameter limit_type='U'" in system_prompt


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


def test_planner_repairs_unsupported_two_limit_up_probability_plan():
    raw_plan = {
        "market": "A_SHARE",
        "interpretation": "Analyze the probability after two limit-up days.",
        "feasibility": "unsupported",
        "requirements": [
            {
                "requirement": "Find consecutive limit-up trading days.",
                "status": "unsupported",
                "evidence": "The model claimed a local join was unavailable.",
            }
        ],
        "limitations": ["The model claimed multiple steps were unavailable."],
        "queries": [
            {
                "query_id": "limit-ups",
                "operation": "limit_list_d",
                "params": {
                    "start_date": "20260624",
                    "end_date": "20260724",
                    "limit_type": "U",
                },
                "fields": ["trade_date", "ts_code", "name"],
                "purpose": "Retrieve limit-up rows.",
            }
        ],
    }
    session = FakeSession(
        FakeResponse(
            {"choices": [{"message": {"content": json.dumps(raw_plan)}}]}
        )
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(
            prompt="分析之前一个月，连续两天涨停后第三天上涨的概率。"
        ),
        [
            DataOperation(name="limit_list_d", description="Daily limit list."),
            DataOperation(name="daily", description="Daily price changes."),
        ],
    )

    assert result.feasibility == "supported"
    assert result.result_transform == "two_limit_up_next_day_probability"
    assert [query.operation for query in result.queries] == [
        "limit_list_d",
        "daily",
    ]
    assert all(
        requirement.status == "covered"
        for requirement in result.requirements
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "筛选最新市盈率低于十的股票。",
        "筛选低市盈率、低市净率、高股息率的十只股票。",
    ],
)
def test_planner_moves_today_daily_basic_query_to_completed_market_date(prompt):
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    completed_date = now.date()
    if now.time() < ClockTime(17, 10):
        completed_date -= timedelta(days=1)
    while completed_date.weekday() >= 5:
        completed_date -= timedelta(days=1)
    plan = make_daily_plan()
    plan.queries[0].operation = "daily_basic"
    plan.queries[0].params["trade_date"] = now.strftime("%Y%m%d")
    session = FakeSession(
        FakeResponse({"choices": [{"message": {"content": plan.model_dump_json()}}]})
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt=prompt),
        [DataOperation(name="daily_basic", description="Daily valuation data.")],
    )

    assert result.queries[0].params["trade_date"] == completed_date.strftime(
        "%Y%m%d"
    )


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
    assert result.limitations == []

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


def test_executor_groups_and_limits_security_counts():
    frame = pd.DataFrame(
        [
            {"trade_date": "20260701", "ts_code": f"{index:06d}.SZ"}
            for index in range(12)
        ]
        + [
            {"trade_date": "20260702", "ts_code": f"{index:06d}.SZ"}
            for index in range(5)
        ]
    )
    query = DataQuery(
        query_id="top-limit-counts",
        operation="daily",
        fields=["trade_date", "ts_code"],
        purpose="Rank securities by occurrence count.",
        transform="top_10_count_by_ts_code",
    )

    result = DataQueryExecutor(FakeMarketDataProvider(frame=frame)).execute(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.status == "success"
    assert result.row_count == 10
    assert result.rows[0] == {"ts_code": "000000.SZ", "count": 2}


def test_executor_filters_then_ranks_top_dividend_yields():
    frame = pd.DataFrame(
        [
            {
                "ts_code": f"{index:06d}.SZ",
                "pe": index + 1,
                "pb": 1.0,
                "dv_ratio": float(index),
            }
            for index in range(15)
        ]
    )
    query = DataQuery(
        query_id="top-dividend",
        operation="daily",
        fields=["ts_code", "pe", "pb", "dv_ratio"],
        purpose="Filter valuations and rank dividend yield.",
        transform="top_10_by_dv_ratio",
        filters=[
            {"field": "pe", "operator": "lt", "value": 12},
            {"field": "pb", "operator": "lt", "value": 2},
        ],
    )

    result = DataQueryExecutor(FakeMarketDataProvider(frame=frame)).execute(
        query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert result.row_count == 10
    assert result.rows[0]["dv_ratio"] == 10.0
    assert result.rows[-1]["dv_ratio"] == 1.0
    assert all(row["pe"] < 12 for row in result.rows)


def test_executor_ranks_turnover_and_aggregated_security_amount():
    turnover_frame = pd.DataFrame(
        [
            {"ts_code": f"{index:06d}.SZ", "turnover_rate": float(index)}
            for index in range(25)
        ]
    )
    turnover_query = DataQuery(
        query_id="top-turnover",
        operation="daily_basic",
        fields=["ts_code", "turnover_rate"],
        purpose="Rank turnover.",
        transform="top_20_by_turnover_rate",
    )
    turnover_result = DataQueryExecutor(
        FakeMarketDataProvider(frame=turnover_frame)
    ).execute(
        turnover_query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    block_frame = pd.DataFrame(
        [
            {"ts_code": "000001.SZ", "amount": 10},
            {"ts_code": "000001.SZ", "amount": 20},
            {"ts_code": "600000.SH", "amount": 25},
        ]
    )
    block_query = DataQuery(
        query_id="top-block-amount",
        operation="block_trade",
        fields=["ts_code", "amount"],
        purpose="Aggregate and rank block-trade amount.",
        transform="top_20_total_amount_by_ts_code",
    )
    block_result = DataQueryExecutor(
        FakeMarketDataProvider(frame=block_frame)
    ).execute(
        block_query,
        api_route="/api/analysis",
        request_id="request-1",
    )

    assert turnover_result.row_count == 20
    assert turnover_result.rows[0]["turnover_rate"] == 24.0
    assert block_result.rows == [
        {"ts_code": "000001.SZ", "total_amount": 30},
        {"ts_code": "600000.SH", "total_amount": 25},
    ]


def test_executor_ranks_daily_amount_and_calculates_period_returns():
    amount_frame = pd.DataFrame(
        [
            {"ts_code": f"{index:06d}.SZ", "amount": float(index)}
            for index in range(25)
        ]
    )
    amount_query = DataQuery(
        query_id="top-amount",
        operation="daily",
        fields=["ts_code", "amount"],
        purpose="Rank daily trading amount.",
        transform="top_20_by_amount",
    )
    amount_result = DataQueryExecutor(
        FakeMarketDataProvider(frame=amount_frame)
    ).execute(
        amount_query,
        api_route="/api/analysis",
        request_id="request-1",
    )

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

    assert amount_result.row_count == 20
    assert amount_result.rows[0]["amount"] == 24.0
    assert return_result.rows[0]["period_return_pct"] == 20.0


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

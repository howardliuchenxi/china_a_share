import json

import pandas as pd
import requests

from china_a_share.client import TushareApiError
from china_a_share.contracts import AnalysisRequest, QueryPlan, TushareQuery
from china_a_share.executor import TushareQueryExecutor
from china_a_share.planner import DeepSeekApiError, DeepSeekQueryPlanner
from china_a_share.registry import StockApiRegistry
from china_a_share.validation import ASharePlanValidator, PlanValidationError


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


class FakeTushareClient:
    def __init__(self, frame=None, error=None):
        self.frame = frame
        self.error = error

    def query(self, api_name, params, fields):
        if self.error:
            raise self.error
        return self.frame


def make_daily_plan():
    return QueryPlan(
        interpretation="Count daily market direction.",
        queries=[
            TushareQuery(
                query_id="market_direction",
                api_name="daily",
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


def test_registry_and_validator_accept_stock_api_plan():
    registry = StockApiRegistry()

    assert "daily" in registry.search("Count stocks.")
    assert ASharePlanValidator(registry).validate(make_daily_plan()).queries[0].api_name == "daily"


def test_validator_rejects_api_outside_stock_catalog():
    plan = make_daily_plan()
    plan.queries[0].api_name = "us_daily"

    try:
        ASharePlanValidator(StockApiRegistry()).validate(plan)
    except PlanValidationError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("Expected a plan validation error.")


def test_planner_parses_deepseek_json_plan():
    plan = make_daily_plan()
    session = FakeSession(
        FakeResponse({"choices": [{"message": {"content": plan.model_dump_json()}}]})
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="Count stocks."), ["daily"]
    )

    assert result.queries[0].params["trade_date"] == "20260717"
    sent = session.calls[0][1]
    assert sent["headers"]["Authorization"] == "Bearer test-key"
    assert sent["json"]["thinking"] == {"type": "disabled"}


def test_planner_normalizes_fields_misplaced_in_params():
    plan = make_daily_plan()
    plan.queries[0].params["fields"] = ["ts_code", "change"]
    plan.queries[0].fields = []
    session = FakeSession(
        FakeResponse({"choices": [{"message": {"content": plan.model_dump_json()}}]})
    )

    result = DeepSeekQueryPlanner("test-key", session=session).plan(
        AnalysisRequest(prompt="Count stocks."), ["daily"]
    )

    assert result.queries[0].fields == ["ts_code", "change"]
    assert "fields" not in result.queries[0].params


def test_planner_converts_network_failure_to_displayable_error():
    session = FakeSession(exception=requests.ConnectionError("network unavailable"))

    try:
        DeepSeekQueryPlanner("test-key", session=session).plan(
            AnalysisRequest(prompt="Count stocks."), ["daily"]
        )
    except DeepSeekApiError as exc:
        assert "network unavailable" in str(exc)
    else:
        raise AssertionError("Expected a DeepSeek API error.")


def test_executor_computes_controlled_counts():
    frame = pd.DataFrame(
        [{"ts_code": "000001.SZ", "change": 1.2}, {"ts_code": "600000.SH", "change": -0.4}]
    )

    result = TushareQueryExecutor(FakeTushareClient(frame=frame)).execute(
        make_daily_plan().queries[0]
    )

    assert result.status == "success"
    assert result.summary == {"Advanced": 1, "Declined": 1}
    assert result.row_count == 2


def test_executor_preserves_tushare_permission_error():
    error = TushareApiError(
        "No permission", code=2002, http_status=200, raw_response={"code": 2002, "msg": "No permission"}
    )

    result = TushareQueryExecutor(FakeTushareClient(error=error)).execute(
        make_daily_plan().queries[0]
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.source == "tushare"
    assert result.error.raw_response == {"code": 2002, "msg": "No permission"}

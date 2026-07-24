from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from china_a_share.application.workflow import (
    ASharePlanValidator,
    AnalysisService,
    DataQueryExecutor,
)
from china_a_share.bootstrap import create_analysis_service
from china_a_share.config import ConfigurationError, Settings
from china_a_share.core.contracts import (
    AnalysisRequest,
    DataOperation,
    DataQuery,
    QueryPlan,
)
from china_a_share.planners.deepseek import DeepSeekQueryPlanner
from china_a_share.providers.tushare import (
    TushareCacheExpirationPolicy,
    TushareDataProvider,
)


class FakeCache:
    def get_or_fetch(
        self,
        provider,
        operation,
        params,
        fields,
        fetch,
        *,
        api_route,
        request_id,
        query_id,
    ):
        return fetch()


class FakePlanner:
    @property
    def name(self):
        return "test-planner"

    def plan(self, request, candidate_operations):
        return QueryPlan(
            interpretation=request.prompt,
            requirements=[
                {
                    "requirement": "Retrieve daily prices.",
                    "status": "covered",
                    "implementation": "Use the daily operation.",
                    "evidence": "The candidate catalog documents daily prices.",
                }
            ],
            queries=[
                DataQuery(
                    query_id="q1",
                    operation=candidate_operations[0].name,
                    purpose="Retrieve daily prices.",
                )
            ],
        )


class FakeUnsupportedPlanner:
    @property
    def name(self):
        return "test-planner"

    def plan(self, request, candidate_operations):
        return QueryPlan(
            interpretation=request.prompt,
            feasibility="unsupported",
            requirements=[
                {
                    "requirement": "Retrieve an unavailable field.",
                    "status": "unsupported",
                    "evidence": "No candidate operation documents the field.",
                }
            ],
            limitations=["The requested field is not available."],
        )


class FakeProvider:
    def __init__(self):
        self.query_calls = 0

    @property
    def name(self):
        return "test-provider"

    def search_operations(self, prompt):
        return [DataOperation(name="daily", description="Daily prices.")]

    def supports(self, operation):
        return operation == "daily"

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
        self.query_calls += 1
        return pd.DataFrame([{"ts_code": "000001.SZ", "close": 10.0}])


def test_deepseek_skeleton_exposes_stable_name():
    planner = DeepSeekQueryPlanner("test-key")

    assert planner.name == "deepseek"


def test_unsupported_plan_stops_before_provider_execution():
    provider = FakeProvider()
    service = AnalysisService(
        FakeUnsupportedPlanner(),
        provider,
        ASharePlanValidator(provider),
        DataQueryExecutor(provider),
    )

    response = service.analyze(
        "request-unsupported",
        AnalysisRequest(prompt="Retrieve an unavailable field."),
        api_route="/api/analysis",
    )

    assert response.status == "error"
    assert response.plan is not None
    assert response.plan.feasibility == "unsupported"
    assert provider.query_calls == 0
    assert response.decision_trace[-2].status == "skipped"


def test_tushare_provider_exposes_catalog_through_generic_operations():
    provider = TushareDataProvider("test-token", FakeCache())

    assert provider.name == "tushare"
    assert any(
        operation.name == "daily"
        for operation in provider.search_operations("Retrieve daily data.")
    )


def test_tushare_provider_paginates_capped_daily_results():
    provider = TushareDataProvider("test-token", FakeCache())
    calls = []

    class FakeTransport:
        def query(self, operation, params, fields):
            calls.append(dict(params))
            row_count = 6000 if "offset" not in params else 2
            return pd.DataFrame(
                [
                    {"ts_code": f"{index:06d}.SZ", "close": 10.0}
                    for index in range(row_count)
                ]
            )

    provider._transport = FakeTransport()

    frame = provider._fetch_complete(
        "daily",
        {"start_date": "20260701", "end_date": "20260723"},
        ["ts_code", "close"],
    )

    assert len(frame) == 6002
    assert calls[1]["limit"] == 6000
    assert calls[1]["offset"] == 6000


def test_tushare_provider_uses_operation_specific_page_limit():
    provider = TushareDataProvider("test-token", FakeCache())
    calls = []

    class FakeTransport:
        def query(self, operation, params, fields):
            calls.append(dict(params))
            row_count = 1000 if "offset" not in params else 1
            return pd.DataFrame(
                [
                    {"ts_code": f"{index:06d}.SZ", "st_type": "ST"}
                    for index in range(row_count)
                ]
            )

    provider._transport = FakeTransport()

    frame = provider._fetch_complete("stock_st", {}, ["ts_code", "st_type"])

    assert len(frame) == 1001
    assert calls[1]["limit"] == 1000
    assert calls[1]["offset"] == 1000


def test_tushare_policy_has_provider_specific_boundary():
    policy = TushareCacheExpirationPolicy()
    fetched_at = datetime(2026, 7, 17, 17, 15, tzinfo=timezone.utc)

    assert policy.resolve("trade_cal", {}, fetched_at) == fetched_at + timedelta(
        days=30
    )


def test_application_workflow_uses_replaceable_dependencies():
    planner = FakePlanner()
    provider = FakeProvider()
    validator = ASharePlanValidator(provider)
    executor = DataQueryExecutor(provider)
    service = AnalysisService(planner, provider, validator, executor)

    response = service.analyze(
        "request-1",
        AnalysisRequest(prompt="Retrieve daily data."),
        api_route="/api/analysis",
    )

    assert response.status == "success"
    assert response.planner == "test-planner"
    assert response.data_provider == "test-provider"
    assert response.results[0].operation == "daily"


def test_bootstrap_requires_persistent_cache_bucket():
    settings = Settings("test-token", "test-deepseek-key", "")

    with pytest.raises(ConfigurationError, match="TUSHARE_CACHE_BUCKET"):
        create_analysis_service(settings)

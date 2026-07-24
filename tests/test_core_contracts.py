from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from china_a_share.core.contracts import (
    AnalysisResponse,
    DataCacheRecord,
    DataOperation,
    DataQuery,
    QueryPlan,
    QueryResult,
    TradingCalendarBreadth,
)
from china_a_share.core.errors import DataProviderError, PlannerError


def test_provider_neutral_query_plan_uses_operation_names():
    operation = DataOperation(
        name="daily",
        description="Retrieve unadjusted daily prices by date or security code.",
    )
    query = DataQuery(
        query_id="q1",
        operation=operation.name,
        params={"trade_date": "20260717"},
        fields=["ts_code", "close"],
        purpose="Retrieve closing prices.",
    )

    plan = QueryPlan(interpretation="Retrieve one trading day.", queries=[query])

    assert plan.queries[0].operation == "daily"
    assert "api_name" not in plan.queries[0].model_dump()


def test_query_plan_records_requirement_coverage_and_local_filters():
    query = DataQuery(
        query_id="low_pe",
        operation="daily_basic",
        params={"trade_date": "20260717"},
        fields=["ts_code", "trade_date", "pe"],
        purpose="Retrieve and filter daily valuation metrics.",
        filters=[{"field": "pe", "operator": "le", "value": 10}],
    )

    plan = QueryPlan(
        interpretation="Find stocks with PE no greater than 10.",
        requirements=[
            {
                "requirement": "Return only rows where pe is at most 10.",
                "status": "covered",
                "implementation": "Apply a deterministic local pe <= 10 filter.",
                "evidence": "daily_basic returns pe but does not accept a PE threshold.",
            }
        ],
        queries=[query],
    )

    assert plan.feasibility == "supported"
    assert plan.queries[0].filters[0].field == "pe"
    assert plan.requirements[0].status == "covered"


def test_unsupported_query_plan_must_fail_without_external_queries():
    plan = QueryPlan(
        interpretation="Retrieve an unavailable metric.",
        feasibility="unsupported",
        requirements=[
            {
                "requirement": "Retrieve the unavailable metric.",
                "status": "unsupported",
                "evidence": "No provider field or deterministic calculation is available.",
            }
        ],
        limitations=["The requested metric is not present in the provider catalog."],
    )

    assert plan.queries == []
    with pytest.raises(ValidationError, match="unsupported plans must not contain queries"):
        QueryPlan(
            interpretation="Do not execute an unsupported request.",
            feasibility="unsupported",
            limitations=["The requested metric is unavailable."],
            queries=[
                DataQuery(
                    query_id="invalid",
                    operation="daily_basic",
                    purpose="This query must be rejected.",
                )
            ],
        )


def test_supported_query_plan_requires_an_executable_query():
    with pytest.raises(ValidationError, match="supported plans must contain"):
        QueryPlan(interpretation="An empty plan must not claim support.")


def test_analysis_response_records_planner_and_data_provider():
    result = QueryResult(
        query_id="q1",
        provider="tushare",
        operation="daily",
        status="success",
        columns=["ts_code"],
        rows=[{"ts_code": "000001.SZ"}],
        row_count=1,
    )

    response = AnalysisResponse(
        request_id="request-1",
        planner="deepseek",
        data_provider="tushare",
        status="success",
        results=[result],
    )

    assert response.planner == "deepseek"
    assert response.results[0].provider == "tushare"


def test_cache_record_namespace_requires_provider_and_operation():
    fetched_at = datetime(2026, 7, 19, tzinfo=timezone.utc)
    record = DataCacheRecord(
        provider="tushare",
        operation="daily",
        fetched_at=fetched_at,
        expires_at=fetched_at + timedelta(minutes=5),
        columns=["trade_date"],
        rows=[["20260717"]],
    )

    assert record.schema_version == 4
    assert record.provider == "tushare"
    assert record.operation == "daily"


def test_cache_record_rejects_non_future_expiration():
    fetched_at = datetime(2026, 7, 19, tzinfo=timezone.utc)

    with pytest.raises(ValidationError, match="expires_at must be later"):
        DataCacheRecord(
            provider="tushare",
            operation="daily",
            fetched_at=fetched_at,
            expires_at=fetched_at,
        )


def test_external_errors_keep_dynamic_source_names():
    planner_error = PlannerError(source="deepseek", message="Planner failed.")
    provider_error = DataProviderError(
        source="tushare",
        message="Provider failed.",
        code=40203,
    )

    assert planner_error.source == "deepseek"
    assert provider_error.source == "tushare"
    assert provider_error.code == 40203


def test_trading_calendar_breadth_accepts_consistent_counts():
    breadth = TradingCalendarBreadth(
        advanced=3126,
        declined=1842,
        unchanged=96,
        traded=5064,
        advance_decline_ratio=1.6971,
    )

    assert breadth.traded == 5064


def test_trading_calendar_breadth_rejects_inconsistent_traded_count():
    with pytest.raises(
        ValidationError,
        match=r"traded must equal advanced \+ declined \+ unchanged",
    ):
        TradingCalendarBreadth(
            advanced=10,
            declined=5,
            unchanged=1,
            traded=15,
            advance_decline_ratio=2,
        )

"""Opt-in quality coverage against the real DeepSeek and Tushare APIs."""

import os
from uuid import uuid4

import pytest

from china_a_share.application.workflow import (
    ASharePlanValidator,
    AnalysisService,
    DataQueryExecutor,
)
from china_a_share.cache import (
    DEFAULT_L1_MAX_BYTES,
    DEFAULT_L1_MAX_ENTRIES,
    LayeredDataResponseCache,
    MemoryDataCacheStore,
    NoopDataCacheStore,
)
from china_a_share.config import Settings
from china_a_share.core.contracts import AnalysisRequest, AnalysisStatus
from china_a_share.planners.deepseek import DeepSeekQueryPlanner
from china_a_share.providers.tushare import (
    TushareCacheExpirationPolicy,
    TushareDataProvider,
)
from china_a_share.time_range import (
    resolve_consecutive_session_count,
    resolve_future_horizon,
)

from golden_questions import GOLDEN_QUESTION_FAMILIES


LIVE_ANALYSIS_ENVIRONMENT_VARIABLE = "RUN_LIVE_ANALYSIS"
LIVE_SUPPORTED_FAMILIES = (
    "market_breadth",
    "limit_up_list",
    "limit_up_trend",
    "two_limit_up_probability",
    "limit_up_forward_horizon",
    "market_period_return",
    "valuation_period_return",
    "valuation_screen",
    "liquidity_ranking",
    "block_trade",
    "holder_count",
    "security_moneyflow",
    "margin_financing",
    "financial_statements",
    "dividend",
)
LIVE_UNSUPPORTED_CASE_COUNTS = {
    "verified_retail_ownership": 2,
    "future_price_prediction": 2,
    "investor_demographics": 1,
}
GOLDEN_FAMILY_BY_NAME = {
    family["family"]: family for family in GOLDEN_QUESTION_FAMILIES
}
LIVE_ANALYSIS_CASES = [
    (family_name, prompt)
    for family_name in LIVE_SUPPORTED_FAMILIES
    for prompt in GOLDEN_FAMILY_BY_NAME[family_name]["prompts"]
] + [
    (family_name, prompt)
    for family_name, count in LIVE_UNSUPPORTED_CASE_COUNTS.items()
    for prompt in GOLDEN_FAMILY_BY_NAME[family_name]["prompts"][:count]
]


@pytest.fixture(scope="module")
def live_analysis_service() -> AnalysisService:
    """Build one real service so all cases share only local market-data cache."""
    settings = Settings.from_env()
    response_cache = LayeredDataResponseCache(
        memory_store=MemoryDataCacheStore(
            max_entries=DEFAULT_L1_MAX_ENTRIES,
            max_bytes=DEFAULT_L1_MAX_BYTES,
        ),
        # Local live validation must not require Google Cloud credentials.
        persistent_store=NoopDataCacheStore(),
        expiration_policy=TushareCacheExpirationPolicy(),
    )
    provider = TushareDataProvider(
        token=settings.tushare_token,
        response_cache=response_cache,
    )
    return AnalysisService(
        planner=DeepSeekQueryPlanner(settings.deepseek_api_key),
        provider=provider,
        validator=ASharePlanValidator(provider),
        executor=DataQueryExecutor(provider),
    )


def _failure_message(response) -> str:
    """Return the most specific available failure for one live assertion."""
    if response.error is not None:
        return response.error.message
    return "; ".join(
        result.error.message
        for result in response.results
        if result.error is not None
    )


def _assert_pipeline_result_succeeded(response) -> None:
    """Verify the declared result pipeline produced one successful result."""
    if response.plan.result_pipeline is None:
        return
    output_query_id = response.plan.result_pipeline.output_query_id
    pipeline_result = next(
        result for result in response.results if result.query_id == output_query_id
    )
    assert pipeline_result.status.value == "success"
    assert pipeline_result.error is None


def _assert_quality_invariants(response, prompt, invariants) -> None:
    """Check stable business meaning without binding exact planner JSON."""
    plan = response.plan
    operations = {query.operation for query in plan.queries}
    steps = plan.result_pipeline.steps if plan.result_pipeline else []

    if "native_limit_up_source" in invariants:
        assert "limit_list_d" in operations
    if "consecutive_session_count" in invariants:
        expected_count = resolve_consecutive_session_count(prompt)
        assert expected_count is not None
        rolling_step = next(step for step in steps if step.operation == "rolling_sum")
        assert rolling_step.window == expected_count
        assert rolling_step.min_periods == expected_count
        assert rolling_step.require_consecutive is True
    if "future_horizon" in invariants:
        expected_horizon = resolve_future_horizon(prompt)
        assert expected_horizon is not None
        assert any(
            step.operation == "match_at_offset"
            and (step.offset_value, step.offset_unit) == expected_horizon
            for step in steps
        )
    if "valid_sample_count" in invariants:
        summary_step = next(step for step in steps if step.operation == "summarize")
        assert any(
            aggregation.output_field == "event_count"
            and aggregation.function == "count"
            for aggregation in summary_step.aggregations
        )
    if "period_return_direction" in invariants:
        assert any(
            query.transform == "period_return_by_ts_code"
            for query in plan.queries
        )
        sort_step = next(step for step in steps if step.operation == "sort")
        expected_direction = "asc" if "跌" in prompt else "desc"
        assert sort_step.direction == expected_direction
    if "sort_before_limit" in invariants:
        operations = [step.operation for step in steps]
        assert operations.index("sort") < operations.index("limit")


def test_live_analysis_matrix_contains_exactly_50_questions() -> None:
    """Keep the paid external regression suite at the reviewed case count."""
    assert len(LIVE_ANALYSIS_CASES) == 50
    assert len(set(LIVE_ANALYSIS_CASES)) == 50


@pytest.mark.parametrize(
    ("family_name", "prompt"),
    LIVE_ANALYSIS_CASES,
    ids=[
        f"{family}-{index + 1}"
        for index, (family, _) in enumerate(LIVE_ANALYSIS_CASES)
    ],
)
@pytest.mark.live
@pytest.mark.skipif(
    os.getenv(LIVE_ANALYSIS_ENVIRONMENT_VARIABLE) != "1",
    reason=(
        f"Set {LIVE_ANALYSIS_ENVIRONMENT_VARIABLE}=1 to call the real "
        "DeepSeek and Tushare APIs."
    ),
)
def test_live_analysis_question(
    live_analysis_service,
    family_name,
    prompt,
) -> None:
    """Run one curated question through real planning, data, and result execution."""
    family = GOLDEN_FAMILY_BY_NAME[family_name]
    response = live_analysis_service.analyze(
        request_id=f"live-{family_name}-{uuid4()}",
        request=AnalysisRequest(prompt=prompt),
        api_route="/local-live-analysis",
        progress_callback=(lambda completed, total: None),
    )

    if family["tier"] == "unsupported":
        assert response.status is AnalysisStatus.ERROR
        assert response.plan is not None
        assert response.plan.feasibility == "unsupported"
        assert response.results == []
        return

    assert response.status is AnalysisStatus.SUCCESS, _failure_message(response)
    assert response.error is None
    assert response.plan is not None
    assert response.plan.feasibility == "supported"
    assert all(
        requirement.status == "covered"
        for requirement in response.plan.requirements
    )
    expected_operations = set(family["operations"])
    actual_operations = {query.operation for query in response.plan.queries}
    assert actual_operations.intersection(expected_operations)
    assert actual_operations.issubset(expected_operations | {"stock_basic"})
    assert response.results
    assert all(result.status.value == "success" for result in response.results), (
        _failure_message(response)
    )
    _assert_pipeline_result_succeeded(response)
    _assert_quality_invariants(
        response,
        prompt,
        set(family.get("quality_invariants", [])),
    )

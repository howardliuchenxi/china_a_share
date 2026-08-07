"""Second opt-in matrix against the real DeepSeek and Tushare APIs."""

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

from secondary_golden_questions import SECONDARY_LIVE_ANALYSIS_CASES


LIVE_ANALYSIS_ENVIRONMENT_VARIABLE = "RUN_LIVE_ANALYSIS"


@pytest.fixture(scope="module")
def secondary_live_analysis_service() -> AnalysisService:
    """Build one real service with a shared in-memory market-data cache."""
    settings = Settings.from_env()
    response_cache = LayeredDataResponseCache(
        memory_store=MemoryDataCacheStore(
            max_entries=DEFAULT_L1_MAX_ENTRIES,
            max_bytes=DEFAULT_L1_MAX_BYTES,
        ),
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
    """Return the most specific available failure message."""
    if response.error is not None:
        return response.error.message
    return "; ".join(
        result.error.message
        for result in response.results
        if result.error is not None
    )


def test_secondary_live_analysis_matrix_contains_exactly_50_questions() -> None:
    """Keep the second paid matrix at its reviewed case count."""
    prompts = [case["prompt"] for case in SECONDARY_LIVE_ANALYSIS_CASES]
    assert len(SECONDARY_LIVE_ANALYSIS_CASES) == 50
    assert len(set(prompts)) == 50


@pytest.mark.parametrize(
    "case",
    SECONDARY_LIVE_ANALYSIS_CASES,
    ids=[
        f"{case['family']}-{index + 1}"
        for index, case in enumerate(SECONDARY_LIVE_ANALYSIS_CASES)
    ],
)
@pytest.mark.live
@pytest.mark.skipif(
    os.getenv(LIVE_ANALYSIS_ENVIRONMENT_VARIABLE) != "1",
    reason="Set RUN_LIVE_ANALYSIS=1 to call DeepSeek and Tushare.",
)
def test_secondary_live_analysis_question(
    secondary_live_analysis_service,
    case,
) -> None:
    """Execute one second-wave case and enforce its capability boundary."""
    response = secondary_live_analysis_service.analyze(
        request_id=f"secondary-{case['family']}-{uuid4()}",
        request=AnalysisRequest(prompt=case["prompt"]),
        api_route="/local-secondary-live-analysis",
        progress_callback=(lambda completed, total: None),
    )
    if case["tier"] == "unsupported":
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
    expected_operations = set(case["operations"])
    actual_operations = {query.operation for query in response.plan.queries}
    assert actual_operations.intersection(expected_operations)
    assert actual_operations.issubset(
        expected_operations | {"stock_basic", "trade_cal"}
    )
    assert response.results
    assert all(
        result.status.value == "success" for result in response.results
    ), _failure_message(response)
    if response.plan.result_pipeline is not None:
        output_query_id = response.plan.result_pipeline.output_query_id
        pipeline_result = next(
            result
            for result in response.results
            if result.query_id == output_query_id
        )
        assert pipeline_result.status.value == "success"
        assert pipeline_result.error is None

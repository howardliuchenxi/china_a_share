"""Opt-in integration coverage against the real DeepSeek and Tushare APIs."""

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


LIVE_ANALYSIS_ENVIRONMENT_VARIABLE = "RUN_LIVE_ANALYSIS"
REGRESSION_PROMPT = (
    "A股20260101～20260601连续涨停三天的情况下，"
    "接下来一个月的上涨情况数据分析"
)
STREAK_PROMPTS = [
    (REGRESSION_PROMPT, 1),
    (
        "A股20260101～20260601连续涨停四个交易日的情况下，"
        "接下来一个月的上涨情况数据分析",
        0,
    ),
    (
        "A股20260101～20260601连续涨停一周（明确按五个连续交易日）的情况下，"
        "接下来一个月的上涨情况数据分析",
        0,
    ),
]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv(LIVE_ANALYSIS_ENVIRONMENT_VARIABLE) != "1",
        reason=(
            f"Set {LIVE_ANALYSIS_ENVIRONMENT_VARIABLE}=1 to call the real "
            "DeepSeek and Tushare APIs."
        ),
    ),
]


@pytest.fixture(scope="module")
def live_analysis_service() -> AnalysisService:
    """Build one real service so repeated prompts share only local market-data cache."""
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


@pytest.mark.parametrize(("prompt", "minimum_rows"), STREAK_PROMPTS)
def test_live_limit_up_event_study_completes(
    live_analysis_service,
    prompt,
    minimum_rows,
) -> None:
    """Run parameterized streak prompts through real upstream APIs locally."""
    response = live_analysis_service.analyze(
        request_id=f"local-live-{uuid4()}",
        request=AnalysisRequest(prompt=prompt),
        api_route="/local-live-analysis",
    )

    failure = (
        response.error.message
        if response.error
        else "; ".join(
            result.error.message
            for result in response.results
            if result.error is not None
        )
    )
    assert response.status is AnalysisStatus.SUCCESS, failure
    assert response.error is None
    assert response.plan is not None
    assert response.plan.result_pipeline is not None
    pipeline_output_id = response.plan.result_pipeline.output_query_id
    pipeline_result = next(
        result for result in response.results if result.query_id == pipeline_output_id
    )
    assert pipeline_result.status.value == "success"
    assert pipeline_result.row_count >= minimum_rows


def test_live_april_decline_top_10(live_analysis_service) -> None:
    """Run real April decline top 10 query through actual upstream APIs and verify correctness."""
    response = live_analysis_service.analyze(
        request_id=f"local-live-{uuid4()}",
        request=AnalysisRequest(prompt="A股4月一整月单月跌幅最大的公司是top10"),
        api_route="/local-live-analysis",
    )

    assert response.status is AnalysisStatus.SUCCESS, (response.error.message if response.error else "Unknown error")
    assert response.error is None
    assert response.plan is not None
    assert response.plan.intent is not None
    assert response.plan.intent.analysis_type == "rank_metric"
    assert response.plan.intent.metric.type == "period_return"

    pipeline_output_id = response.plan.result_pipeline.output_query_id
    pipeline_result = next(
        result for result in response.results if result.query_id == pipeline_output_id
    )
    assert pipeline_result.status.value == "success"
    assert pipeline_result.row_count == 10

    # Assert monotonic increasing order of returns (drops/losses are increasing from lowest/most negative to less negative)
    returns = [float(row["period_return_pct"]) for row in pipeline_result.rows]
    for i in range(len(returns) - 1):
        assert returns[i] <= returns[i+1], f"Monotonicity violated: {returns}"


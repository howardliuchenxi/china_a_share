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


def test_live_limit_up_event_study_completes() -> None:
    """Run the production regression prompt through real upstream APIs locally."""
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
    service = AnalysisService(
        planner=DeepSeekQueryPlanner(settings.deepseek_api_key),
        provider=provider,
        validator=ASharePlanValidator(provider),
        executor=DataQueryExecutor(provider),
    )

    response = service.analyze(
        request_id=f"local-live-{uuid4()}",
        request=AnalysisRequest(prompt=REGRESSION_PROMPT),
        api_route="/local-live-analysis",
    )

    assert response.status is AnalysisStatus.SUCCESS, response.model_dump_json(
        indent=2
    )
    assert response.error is None
    assert response.plan is not None
    assert response.plan.result_pipeline is not None
    pipeline_output_id = response.plan.result_pipeline.output_query_id
    pipeline_result = next(
        result for result in response.results if result.query_id == pipeline_output_id
    )
    assert pipeline_result.status.value == "success"
    assert pipeline_result.row_count > 0

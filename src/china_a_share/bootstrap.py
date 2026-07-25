"""Dependency assembly for the provider-neutral application."""

from china_a_share.application.workflow import (
    ASharePlanValidator,
    AnalysisService,
    DataQueryExecutor,
)
from china_a_share.application.stock_catalog import StockCatalogService
from china_a_share.cache import (
    DEFAULT_L1_MAX_BYTES,
    DEFAULT_L1_MAX_ENTRIES,
    CloudStorageDataCacheStore,
    LayeredDataResponseCache,
    MemoryDataCacheStore,
    NoopDataCacheStore,
)
from china_a_share.config import ConfigurationError, Settings
from china_a_share.feedback import (
    CloudStorageUiFeedbackStore,
    DeepSeekUiFeedbackAssistant,
    GitHubUiFeedbackDispatcher,
    GoogleAdminVerifier,
    UiFeedbackService,
)
from china_a_share.tasks import (
    AnalysisTaskCoordinator,
    CloudRunJobDispatcher,
    CloudStorageAnalysisTaskStore,
)
from china_a_share.planners.deepseek import DeepSeekQueryPlanner
from china_a_share.providers.tushare import (
    TushareCacheExpirationPolicy,
    TushareDataProvider,
)
from china_a_share.vision.glm import GLMVisionAnalyzer


def create_analysis_service(settings: Settings) -> AnalysisService:
    """Assemble the configured planner, provider, cache, validator, and executor."""
    provider = _create_data_provider(settings)
    planner = DeepSeekQueryPlanner(settings.deepseek_api_key)
    validator = ASharePlanValidator(provider)
    executor = DataQueryExecutor(provider)
    vision_analyzer = (
        GLMVisionAnalyzer(settings.zai_api_key)
        if settings.zai_api_key
        else None
    )
    return AnalysisService(
        planner=planner,
        provider=provider,
        validator=validator,
        executor=executor,
        vision_analyzer=vision_analyzer,
    )


def create_stock_catalog_service(settings: Settings) -> StockCatalogService:
    """Assemble deterministic stock catalog access through the shared cache design."""
    return StockCatalogService(_create_data_provider(settings))


def create_analysis_task_coordinator(
    settings: Settings,
) -> AnalysisTaskCoordinator:
    """Assemble persistent task storage and Cloud Run Job dispatch."""
    if not settings.google_cloud_project:
        raise ConfigurationError(
            "GOOGLE_CLOUD_PROJECT is required for asynchronous analysis."
        )
    if not settings.tushare_cache_bucket:
        raise ConfigurationError(
            "TUSHARE_CACHE_BUCKET is required for asynchronous analysis."
        )
    return AnalysisTaskCoordinator(
        CloudStorageAnalysisTaskStore(settings.tushare_cache_bucket),
        CloudRunJobDispatcher(
            settings.google_cloud_project,
            settings.cloud_run_region,
            settings.analysis_job_name,
        ),
    )


def create_ui_feedback_service(settings: Settings) -> UiFeedbackService:
    """Assemble the private administrator UI feedback workflow."""
    required_settings = {
        "ADMIN_EMAIL": settings.admin_email,
        "GOOGLE_OAUTH_CLIENT_ID": settings.google_oauth_client_id,
        "GITHUB_FIX_REPO": settings.github_fix_repo,
        "GITHUB_FIX_TOKEN": settings.github_fix_token,
        "TUSHARE_CACHE_BUCKET": settings.tushare_cache_bucket,
        "APP_GIT_BRANCH": settings.app_git_branch,
        "APP_GIT_SHA": settings.app_git_sha,
    }
    missing = [name for name, value in required_settings.items() if not value]
    if missing:
        raise ConfigurationError(
            "UI feedback is disabled because required settings are missing: "
            + ", ".join(missing)
        )
    return UiFeedbackService(
        GoogleAdminVerifier(
            settings.google_oauth_client_id,
            settings.admin_email,
        ),
        CloudStorageUiFeedbackStore(settings.tushare_cache_bucket),
        GitHubUiFeedbackDispatcher(
            settings.github_fix_repo,
            settings.github_fix_token,
        ),
        DeepSeekUiFeedbackAssistant(settings.deepseek_api_key),
        google_client_id=settings.google_oauth_client_id,
        git_branch=settings.app_git_branch,
        git_sha=settings.app_git_sha,
    )


def _create_data_provider(settings: Settings) -> TushareDataProvider:
    """Build the configured Tushare provider and its layered response cache."""
    if not settings.tushare_cache_bucket:
        raise ConfigurationError(
            "TUSHARE_CACHE_BUCKET is missing. Configure the private Cloud Storage "
            "bucket used for persistent market-data caching."
        )
    response_cache = LayeredDataResponseCache(
        memory_store=MemoryDataCacheStore(
            max_entries=DEFAULT_L1_MAX_ENTRIES,
            max_bytes=DEFAULT_L1_MAX_BYTES,
        ),
        persistent_store=CloudStorageDataCacheStore(
            settings.tushare_cache_bucket
        ),
        expiration_policy=TushareCacheExpirationPolicy(),
    )
    provider = TushareDataProvider(
        token=settings.tushare_token,
        response_cache=response_cache,
    )
    return provider

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
from china_a_share.e2e_cases import (
    CloudStorageLiveCaseChangeStore,
    GitHubLiveCaseDispatcher,
    LiveCaseService,
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
from china_a_share.feishu import (
    CloudStorageConversationStore,
    FeishuOpenApiClient,
    FeishuResearchBot,
)
from china_a_share.feishu_agent import (
    FeishuAgentCoordinator,
)
from china_a_share.codex_agent import CodexFeishuAgentRuntime


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


from china_a_share.discovery.backtester import FactorBacktester
from china_a_share.discovery.evolution_loop import EvolutionLoop

from china_a_share.core.ports import AnalysisTaskStore
from china_a_share.core.ports import MarketDataProvider
from china_a_share.providers.composite import CompositeMarketDataProvider
from china_a_share.providers.us_market import (
    USMarketCacheExpirationPolicy,
    USMarketDataProvider,
)

def create_evolution_loop(settings: Settings, store: AnalysisTaskStore) -> EvolutionLoop:
    """Assemble the configured discovery engine components."""
    provider = _create_data_provider(settings)
    executor = DataQueryExecutor(provider)
    backtester = FactorBacktester(executor)
    return EvolutionLoop(store, backtester)

def create_stock_catalog_service(settings: Settings) -> StockCatalogService:
    """Assemble deterministic stock catalog access through the shared cache design."""
    return StockCatalogService(_create_data_provider(settings))


def create_feishu_research_bot(settings: Settings) -> FeishuResearchBot:
    """Assemble the private Feishu ingress around the validated analysis core."""
    allowed_open_ids = {
        value.strip()
        for value in settings.feishu_allowed_open_ids.split(",")
        if value.strip()
    }
    task_coordinator = create_analysis_task_coordinator(settings)
    agent_coordinator = (
        FeishuAgentCoordinator(task_coordinator.store, task_coordinator.dispatcher)
        if isinstance(task_coordinator, AnalysisTaskCoordinator)
        else None
    )
    return FeishuResearchBot(
        task_coordinator,
        FeishuOpenApiClient(settings.feishu_app_id, settings.feishu_app_secret),
        CloudStorageConversationStore(settings.tushare_cache_bucket),
        verification_token=settings.feishu_verification_token,
        encrypt_key=settings.feishu_encrypt_key,
        allowed_open_ids=allowed_open_ids,
        agent_coordinator=agent_coordinator,
    )


def create_feishu_agent_runtime(settings: Settings) -> CodexFeishuAgentRuntime:
    """Assemble the Feishu channel around the full Codex agent harness."""
    required_settings = {
        "LLM_BASE_URL": settings.llm_base_url,
        "LLM_MODEL": settings.llm_model,
        "LLM_API_KEY": settings.llm_api_key,
        "RESEARCH_SANDBOX_URL": settings.research_sandbox_url,
        "TUSHARE_CACHE_BUCKET": settings.tushare_cache_bucket,
    }
    missing = [name for name, value in required_settings.items() if not value]
    if missing:
        raise ConfigurationError(
            "Feishu research agent is missing required settings: "
            + ", ".join(missing)
        )
    return CodexFeishuAgentRuntime(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        tushare_token=settings.tushare_token,
        massive_api_key=settings.massive_api_key,
        finnhub_api_key=settings.finnhub_api_key,
        cache_bucket=settings.tushare_cache_bucket,
        sandbox_url=settings.research_sandbox_url,
        google_cloud_project=settings.google_cloud_project,
    )


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
        DeepSeekUiFeedbackAssistant(
            settings.deepseek_api_key,
            git_sha=settings.app_git_sha,
        ),
        google_client_id=settings.google_oauth_client_id,
        git_branch=settings.app_git_branch,
        git_sha=settings.app_git_sha,
    )


def create_live_case_service(settings: Settings) -> LiveCaseService:
    """Assemble the authenticated Git-backed live-case management workflow."""
    required_settings = {
        "ADMIN_EMAIL": settings.admin_email,
        "GOOGLE_OAUTH_CLIENT_ID": settings.google_oauth_client_id,
        "GITHUB_FIX_REPO": settings.github_fix_repo,
        "GITHUB_FIX_TOKEN": settings.github_fix_token,
        "TUSHARE_CACHE_BUCKET": settings.tushare_cache_bucket,
        "APP_GIT_SHA": settings.app_git_sha,
    }
    missing = [name for name, value in required_settings.items() if not value]
    if missing:
        raise ConfigurationError(
            "Live-case management is disabled because required settings are missing: "
            + ", ".join(missing)
        )
    return LiveCaseService(
        GoogleAdminVerifier(
            settings.google_oauth_client_id,
            settings.admin_email,
        ),
        CloudStorageLiveCaseChangeStore(settings.tushare_cache_bucket),
        GitHubLiveCaseDispatcher(
            settings.github_fix_repo,
            settings.github_fix_token,
        ),
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


def _create_feishu_data_provider(settings: Settings) -> MarketDataProvider:
    """Add optional U.S. capabilities without changing the A-share core workflow."""
    tushare_provider = _create_data_provider(settings)
    if not settings.massive_api_key and not settings.finnhub_api_key:
        return tushare_provider
    us_response_cache = LayeredDataResponseCache(
        memory_store=MemoryDataCacheStore(
            max_entries=DEFAULT_L1_MAX_ENTRIES,
            max_bytes=DEFAULT_L1_MAX_BYTES,
        ),
        persistent_store=CloudStorageDataCacheStore(settings.tushare_cache_bucket),
        expiration_policy=USMarketCacheExpirationPolicy(),
    )
    us_provider = USMarketDataProvider(
        settings.massive_api_key,
        settings.finnhub_api_key,
        us_response_cache,
    )
    return CompositeMarketDataProvider((tushare_provider, us_provider))

"""Dependency assembly for the provider-neutral application."""

from typing import Optional

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
from china_a_share.llm_preference import (
    CloudStorageLlmPreferenceStore,
    LlmPreferenceController,
    resolve_active_provider,
)
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
from china_a_share.planners.glm import (
    GLM_CODING_API_URL,
    GLM_FALLBACK_MODEL,
    GLM_MODEL,
    GlmQueryPlanner,
)
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
    FeishuAgentRuntime,
)


def _glm_chat_config(settings: Settings) -> dict:
    """Resolve GLM chat endpoint overrides once for every LLM call site."""
    return {
        "api_key": settings.zai_api_key,
        "api_url": settings.glm_api_url or GLM_CODING_API_URL,
        "model": settings.glm_model or GLM_MODEL,
        "fallback_model": settings.glm_fallback_model or GLM_FALLBACK_MODEL,
    }


def create_llm_preference_controller(
    settings: Settings,
) -> Optional[LlmPreferenceController]:
    """Build the chat model-switch controller when persistence is available."""
    if not settings.tushare_cache_bucket:
        return None
    return LlmPreferenceController(
        settings,
        CloudStorageLlmPreferenceStore(settings.tushare_cache_bucket),
    )


def create_analysis_service(
    settings: Settings,
    *,
    llm_preference: Optional[str] = None,
) -> AnalysisService:
    """Assemble the configured planner, provider, cache, validator, and executor."""
    provider = _create_data_provider(settings)
    if resolve_active_provider(settings, llm_preference) == "glm":
        glm = _glm_chat_config(settings)
        planner = GlmQueryPlanner(
            glm["api_key"],
            api_url=glm["api_url"],
            model=glm["model"],
            fallback_model=glm["fallback_model"],
        )
    else:
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
    from china_a_share.strategy.persistence import StrategyStore
    from china_a_share.strategy.scanner import StrategyScanner
    from china_a_share.strategy.data_loader import QFQDataLoader
    from china_a_share.strategy.engine import RuleEngine
    
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
    
    # Initialize Strategy Components
    strategy_store = StrategyStore(settings.tushare_cache_bucket) if settings.tushare_cache_bucket else None
    strategy_scanner = None
    if strategy_store:
        provider = _create_data_provider(settings)
        data_loader = QFQDataLoader(provider)
        engine = RuleEngine(data_loader)
        sender = FeishuOpenApiClient(settings.feishu_app_id, settings.feishu_app_secret)
        strategy_scanner = StrategyScanner(strategy_store, data_loader, engine, sender)
        
    return FeishuResearchBot(
        task_coordinator,
        FeishuOpenApiClient(settings.feishu_app_id, settings.feishu_app_secret),
        CloudStorageConversationStore(settings.tushare_cache_bucket),
        verification_token=settings.feishu_verification_token,
        encrypt_key=settings.feishu_encrypt_key,
        allowed_open_ids=allowed_open_ids,
        agent_coordinator=agent_coordinator,
        strategy_store=strategy_store,
        strategy_scanner=strategy_scanner,
        llm_switcher=create_llm_preference_controller(settings),
    )


def create_feishu_agent_runtime(
    settings: Settings,
    *,
    llm_preference: Optional[str] = None,
) -> FeishuAgentRuntime:
    """Assemble the independent Feishu agent around read-only provider tools."""
    data_provider = _create_feishu_data_provider(settings)
    if resolve_active_provider(settings, llm_preference) == "glm":
        glm = _glm_chat_config(settings)
        return FeishuAgentRuntime(
            glm["api_key"],
            data_provider,
            api_url=glm["api_url"],
            model=glm["model"],
            fallback_model=glm["fallback_model"],
            label="GLM",
        )
    return FeishuAgentRuntime(settings.deepseek_api_key, data_provider)


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
    if settings.llm_provider == "glm":
        glm = _glm_chat_config(settings)
        feedback_assistant: DeepSeekUiFeedbackAssistant = (
            DeepSeekUiFeedbackAssistant(
                glm["api_key"],
                git_sha=settings.app_git_sha,
                api_url=glm["api_url"],
                model=glm["model"],
            )
        )
    else:
        feedback_assistant = DeepSeekUiFeedbackAssistant(
            settings.deepseek_api_key,
            git_sha=settings.app_git_sha,
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
        feedback_assistant,
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

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

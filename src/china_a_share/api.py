"""FastAPI application for the local analysis system."""

import os
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .cache import (
    DEFAULT_L1_MAX_BYTES,
    DEFAULT_L1_MAX_ENTRIES,
    CloudStorageTushareCacheStore,
    LayeredTushareResponseCache,
    MemoryTushareCacheStore,
)
from .client import TushareClient
from .config import ConfigurationError, Settings
from .contracts import AnalysisRequest, AnalysisResponse
from .executor import TushareQueryExecutor
from .planner import DeepSeekQueryPlanner
from .registry import StockApiRegistry
from .service import AnalysisService
from .validation import ASharePlanValidator


FRONTEND_DIST = Path(
    os.getenv(
        "FRONTEND_DIST",
        str(Path(__file__).resolve().parents[2] / "frontend" / "dist"),
    )
)


def create_analysis_service() -> AnalysisService:
    """Create production dependencies from local environment credentials."""
    settings = Settings.from_env()
    if not settings.tushare_cache_bucket:
        raise ConfigurationError(
            "TUSHARE_CACHE_BUCKET is missing. Configure the private Cloud Storage "
            "bucket used for persistent Tushare caching."
        )
    response_cache = LayeredTushareResponseCache(
        memory_store=MemoryTushareCacheStore(
            max_entries=DEFAULT_L1_MAX_ENTRIES,
            max_bytes=DEFAULT_L1_MAX_BYTES,
        ),
        persistent_store=CloudStorageTushareCacheStore(settings.tushare_cache_bucket),
    )
    registry = StockApiRegistry()
    return AnalysisService(
        registry=registry,
        planner=DeepSeekQueryPlanner(settings.deepseek_api_key),
        validator=ASharePlanValidator(registry),
        executor=TushareQueryExecutor(TushareClient(settings, response_cache=response_cache)),
    )


def create_app(service: Optional[AnalysisService] = None) -> FastAPI:
    """Create the local HTTP application."""
    application = FastAPI(
        title="A-Share Data Assistant",
        version="0.1.0",
    )

    @application.get("/api/health")
    def health() -> dict:
        """Report that the local backend process is available."""
        return {"status": "ok"}

    active_service = service

    @application.post("/api/analysis", response_model=AnalysisResponse)
    def analyze(request: AnalysisRequest) -> AnalysisResponse:
        """Accept a natural-language request for A-share data."""
        nonlocal active_service
        if active_service is None:
            active_service = create_analysis_service()
        return active_service.analyze(str(uuid4()), request)

    if FRONTEND_DIST.is_dir():
        application.mount(
            "/",
            StaticFiles(directory=FRONTEND_DIST, html=True),
            name="frontend",
        )

    return application


app = create_app()

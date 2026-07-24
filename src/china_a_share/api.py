"""FastAPI application for the local analysis system."""

import logging
import os
from pathlib import Path
from time import perf_counter
from typing import Literal, Optional
from uuid import uuid4

from fastapi import FastAPI, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .application.workflow import AnalysisService
from .application.stock_catalog import StockCatalogService
from .bootstrap import create_analysis_service as build_analysis_service
from .bootstrap import create_stock_catalog_service as build_stock_catalog_service
from .config import Settings
from .core.contracts import (
    AnalysisRequest,
    AnalysisResponse,
    ServiceError,
    StockListErrorResponse,
    StockListResponse,
)
from .core.errors import DataProviderError
from .observability import log_event


FRONTEND_DIST = Path(
    os.getenv(
        "FRONTEND_DIST",
        str(Path(__file__).resolve().parents[2] / "frontend" / "dist"),
    )
)
ANALYSIS_API_ROUTE = "/api/analysis"
HEALTH_API_ROUTE = "/api/health"
STOCKS_API_ROUTE = "/api/stocks"
ANALYSIS_PAGE_ROUTE = "/analysis"
BASIC_PAGE_ROUTE = "/basic"
MONITORED_API_ROUTES = {ANALYSIS_API_ROUTE, HEALTH_API_ROUTE, STOCKS_API_ROUTE}
MILLISECONDS_PER_SECOND = 1_000
DEFAULT_STOCK_PAGE_SIZE = 20
MAX_STOCK_PAGE_SIZE = 100
MAX_STOCK_FILTER_LENGTH = 100


logger = logging.getLogger(__name__)


def create_analysis_service() -> AnalysisService:
    """Create production dependencies from local environment credentials."""
    return build_analysis_service(Settings.from_env())


def create_stock_catalog_service() -> StockCatalogService:
    """Create production stock catalog dependencies from local credentials."""
    return build_stock_catalog_service(Settings.from_env())


def create_app(
    service: Optional[AnalysisService] = None,
    stock_catalog_service: Optional[StockCatalogService] = None,
) -> FastAPI:
    """Create the local HTTP application."""
    application = FastAPI(
        title="A-Share Data Assistant",
        version="0.1.0",
    )

    @application.middleware("http")
    async def log_api_request(http_request: Request, call_next):
        """Record one bounded structured event for each known API request."""
        request_id = str(uuid4())
        http_request.state.request_id = request_id
        started_at = perf_counter()
        status_code = 500
        try:
            response = await call_next(http_request)
            status_code = response.status_code
            return response
        finally:
            api_route = http_request.url.path
            if api_route in MONITORED_API_ROUTES:
                # Route allowlisting prevents user-controlled paths from becoming labels.
                log_event(
                    logger,
                    logging.ERROR if status_code >= 500 else logging.INFO,
                    "http_request_completed",
                    api_route=api_route,
                    method=http_request.method,
                    status_code=status_code,
                    status_class=f"{status_code // 100}xx",
                    duration_ms=int(
                        (perf_counter() - started_at) * MILLISECONDS_PER_SECOND
                    ),
                    request_id=request_id,
                )

    @application.get(HEALTH_API_ROUTE)
    def health() -> dict:
        """Report that the local backend process is available."""
        return {"status": "ok"}

    active_service = service
    active_stock_catalog_service = stock_catalog_service

    @application.post(ANALYSIS_API_ROUTE, response_model=AnalysisResponse)
    def analyze(
        request: AnalysisRequest,
        http_request: Request,
    ) -> AnalysisResponse:
        """Accept a natural-language request for A-share data."""
        nonlocal active_service
        if active_service is None:
            active_service = create_analysis_service()
        return active_service.analyze(
            http_request.state.request_id,
            request,
            api_route=ANALYSIS_API_ROUTE,
        )

    @application.get(STOCKS_API_ROUTE, response_model=StockListResponse)
    def list_stocks(
        http_request: Request,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(
            default=DEFAULT_STOCK_PAGE_SIZE,
            ge=1,
            le=MAX_STOCK_PAGE_SIZE,
        ),
        search: str = Query(default="", max_length=MAX_STOCK_FILTER_LENGTH),
        exchange: Literal["", "SSE", "SZSE", "BSE"] = Query(default=""),
        industry: str = Query(default="", max_length=MAX_STOCK_FILTER_LENGTH),
    ) -> StockListResponse:
        """Return one filtered page of currently listed A-share securities."""
        nonlocal active_stock_catalog_service
        if active_stock_catalog_service is None:
            active_stock_catalog_service = create_stock_catalog_service()
        try:
            return active_stock_catalog_service.list_stocks(
                http_request.state.request_id,
                page=page,
                page_size=page_size,
                search=search,
                exchange=exchange,
                industry=industry,
                api_route=STOCKS_API_ROUTE,
            )
        except IndexError as exc:
            log_event(
                logger,
                logging.WARNING,
                "stock_catalog_page_rejected",
                api_route=STOCKS_API_ROUTE,
                request_id=http_request.state.request_id,
                page=page,
                page_size=page_size,
            )
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content=StockListErrorResponse(
                    request_id=http_request.state.request_id,
                    error=ServiceError(source="system", message=str(exc)),
                ).model_dump(mode="json"),
            )
        except DataProviderError as exc:
            log_event(
                logger,
                logging.ERROR,
                "stock_catalog_provider_failed",
                api_route=STOCKS_API_ROUTE,
                request_id=http_request.state.request_id,
                source=exc.source,
            )
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content=StockListErrorResponse(
                    request_id=http_request.state.request_id,
                    error=ServiceError(
                        source=exc.source,
                        code=exc.code,
                        message=str(exc),
                        http_status=exc.http_status,
                        raw_response=exc.raw_response,
                    ),
                ).model_dump(mode="json"),
            )
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "stock_catalog_failed",
                api_route=STOCKS_API_ROUTE,
                request_id=http_request.state.request_id,
                source="system",
                exc_info=True,
            )
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content=StockListErrorResponse(
                    request_id=http_request.state.request_id,
                    error=ServiceError(source="system", message=str(exc)),
                ).model_dump(mode="json"),
            )

    if FRONTEND_DIST.is_dir():
        @application.get("/", include_in_schema=False)
        def redirect_to_analysis() -> RedirectResponse:
            """Redirect the site root to the primary analysis page."""
            return RedirectResponse(
                url=ANALYSIS_PAGE_ROUTE,
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            )

        @application.get(BASIC_PAGE_ROUTE, include_in_schema=False)
        def redirect_basic_to_analysis() -> RedirectResponse:
            """Redirect the legacy basic-data path to the unified page."""
            return RedirectResponse(
                url=ANALYSIS_PAGE_ROUTE,
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            )

        @application.get(ANALYSIS_PAGE_ROUTE, include_in_schema=False)
        def frontend_page() -> FileResponse:
            """Serve the unified frontend entry point."""
            return FileResponse(FRONTEND_DIST / "index.html")

        application.mount(
            "/",
            StaticFiles(directory=FRONTEND_DIST, html=True),
            name="frontend",
        )

    return application


app = create_app()

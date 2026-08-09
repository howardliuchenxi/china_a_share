"""FastAPI application for the local analysis system."""

import logging
import os
from pathlib import Path
from time import perf_counter
from typing import Literal, Optional, Union
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .application.workflow import (
    BACKGROUND_TASK_REQUIRED_ERROR_CODE,
    AnalysisService,
)
from .application.stock_catalog import StockCatalogService
from .bootstrap import create_analysis_service as build_analysis_service
from .bootstrap import create_analysis_task_coordinator
from .bootstrap import create_stock_catalog_service as build_stock_catalog_service
from .bootstrap import create_ui_feedback_service as build_ui_feedback_service
from .bootstrap import create_live_case_service as build_live_case_service
from .config import ConfigurationError, Settings
from .core.contracts import (
    AnalysisRequest,
    AnalysisResponse,
    AnalysisTaskStatusResponse,
    AnalysisTaskSubmission,
    DiscoveryResearchConfig,
    DiscoveryTaskRequest,
    DiscoveryTaskStatusResponse,
    ServiceError,
    StockListErrorResponse,
    StockListResponse,
    UiFeedbackConfig,
    UiFeedbackChatRequest,
    UiFeedbackChatResponse,
    UiFeedbackRequest,
    UiFeedbackSubmission,
)
from .core.errors import DataProviderError
from .feedback import UiFeedbackAuthenticationError, UiFeedbackService
from .e2e_cases import (
    LiveCaseChangeRequest,
    LiveCaseChangeSubmission,
    LiveCaseListResponse,
    LiveCaseService,
)
from .observability import log_event
from .tasks import AnalysisTaskCoordinator


FRONTEND_DIST = Path(
    os.getenv(
        "FRONTEND_DIST",
        str(Path(__file__).resolve().parents[2] / "frontend" / "dist"),
    )
)
ANALYSIS_API_ROUTE = "/api/analysis"
ANALYSIS_TASK_API_ROUTE = "/api/analysis/tasks"
HEALTH_API_ROUTE = "/api/health"
CAPABILITIES_API_ROUTE = "/api/capabilities"
STOCKS_API_ROUTE = "/api/stocks"
UI_FEEDBACK_API_ROUTE = "/api/ui-feedback"
UI_FEEDBACK_CONFIG_API_ROUTE = "/api/ui-feedback/config"
UI_FEEDBACK_CHAT_API_ROUTE = "/api/ui-feedback/chat"
LIVE_CASES_API_ROUTE = "/api/e2e-cases"
ANALYSIS_PAGE_ROUTE = "/analysis"
BASIC_PAGE_ROUTE = "/basic"
MONITORED_API_ROUTES = {
    ANALYSIS_API_ROUTE,
    HEALTH_API_ROUTE,
    CAPABILITIES_API_ROUTE,
    STOCKS_API_ROUTE,
    UI_FEEDBACK_API_ROUTE,
    UI_FEEDBACK_CONFIG_API_ROUTE,
    UI_FEEDBACK_CHAT_API_ROUTE,
    LIVE_CASES_API_ROUTE,
}
MILLISECONDS_PER_SECOND = 1_000
DEFAULT_STOCK_PAGE_SIZE = 20
MAX_STOCK_PAGE_SIZE = 100
MAX_STOCK_FILTER_LENGTH = 100


logger = logging.getLogger(__name__)


def _administrator_bearer_token(authorization: str) -> str:
    """Extract a required administrator bearer token from one API request."""
    if not authorization.startswith("Bearer ") or not authorization[7:].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Administrator authentication is required.",
        )
    return authorization[7:].strip()


def create_analysis_service() -> AnalysisService:
    """Create production dependencies from local environment credentials."""
    return build_analysis_service(Settings.from_env())


def create_stock_catalog_service() -> StockCatalogService:
    """Create production stock catalog dependencies from local credentials."""
    return build_stock_catalog_service(Settings.from_env())


DISCOVERY_TASK_API_ROUTE = "/api/discovery/tasks"

def create_app(
    service: Optional[AnalysisService] = None,
    stock_catalog_service: Optional[StockCatalogService] = None,
    task_coordinator: Optional[AnalysisTaskCoordinator] = None,
    ui_feedback_service: Optional[UiFeedbackService] = None,
    live_case_service: Optional[LiveCaseService] = None,
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
            if (
                api_route in MONITORED_API_ROUTES
                or api_route.startswith(f"{ANALYSIS_TASK_API_ROUTE}/")
                or api_route.startswith(f"{LIVE_CASES_API_ROUTE}/")
            ):
                response_size = 0
                try:
                    if hasattr(response, "body") and response.body:
                        response_size = len(response.body)
                except Exception:
                    pass
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
                    response_size=response_size,
                    request_id=request_id,
                )

    @application.get(HEALTH_API_ROUTE)
    def health() -> dict:
        """Report service availability and configured AI components."""
        planner_name = service.planner.name if service else "unknown"
        vision_provider = "glm" if service and service._vision_analyzer else "none"
        return {
            "status": "ok",
            "planner": planner_name,
            "vision_provider": vision_provider,
        }

    @application.get(CAPABILITIES_API_ROUTE)
    def capabilities() -> dict:
        """Expose the manifest generated by the active executable provider code."""
        if service is None or not hasattr(service, "capability_manifest"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The runtime capability manifest is unavailable.",
            )
        return service.capability_manifest

    active_service = service
    active_stock_catalog_service = stock_catalog_service
    active_task_coordinator = task_coordinator
    active_ui_feedback_service = ui_feedback_service
    active_live_case_service = live_case_service

    def get_ui_feedback_service() -> UiFeedbackService:
        """Build the optional administrator workflow only when it is requested."""
        nonlocal active_ui_feedback_service
        if active_ui_feedback_service is None:
            active_ui_feedback_service = build_ui_feedback_service(Settings.from_env())
        return active_ui_feedback_service

    def get_live_case_service() -> LiveCaseService:
        """Build live-case dependencies only when the administrator opens the tab."""
        nonlocal active_live_case_service
        if active_live_case_service is None:
            active_live_case_service = build_live_case_service(Settings.from_env())
        return active_live_case_service

    @application.get(LIVE_CASES_API_ROUTE, response_model=LiveCaseListResponse)
    def list_live_cases(
        authorization: str = Header(default=""),
    ) -> LiveCaseListResponse:
        """Return the canonical catalog with refresh-safe pending overlays."""
        token = _administrator_bearer_token(authorization)
        try:
            return get_live_case_service().list_cases(token)
        except UiFeedbackAuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except ConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "live_case_list_failed",
                api_route=LIVE_CASES_API_ROUTE,
                source="system",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The live-case catalog is temporarily unavailable.",
            ) from exc

    @application.post(
        f"{LIVE_CASES_API_ROUTE}/changes",
        response_model=LiveCaseChangeSubmission,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def submit_live_case_change(
        request: LiveCaseChangeRequest,
        authorization: str = Header(default=""),
    ) -> LiveCaseChangeSubmission:
        """Persist and dispatch one administrator-authored Git mutation."""
        token = _administrator_bearer_token(authorization)
        try:
            return get_live_case_service().submit(token, request)
        except UiFeedbackAuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except ConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "live_case_change_failed",
                api_route=LIVE_CASES_API_ROUTE,
                source="system",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The live-case change could not be dispatched.",
            ) from exc

    @application.get(
        UI_FEEDBACK_CONFIG_API_ROUTE,
        response_model=UiFeedbackConfig,
    )
    def get_ui_feedback_config() -> UiFeedbackConfig:
        """Expose only the public configuration needed by Google Identity Services."""
        try:
            return get_ui_feedback_service().config()
        except ConfigurationError as exc:
            # The private control must disappear entirely until every dependency exists.
            log_event(
                logger,
                logging.INFO,
                "ui_feedback_disabled",
                api_route=UI_FEEDBACK_CONFIG_API_ROUTE,
                reason=str(exc),
            )
            return UiFeedbackConfig(enabled=False)

    @application.post(
        UI_FEEDBACK_CHAT_API_ROUTE,
        response_model=UiFeedbackChatResponse,
    )
    def chat_about_ui_feedback(
        request: UiFeedbackChatRequest,
        authorization: str = Header(default=""),
    ) -> UiFeedbackChatResponse:
        """Answer one authenticated question about selected production UI."""
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Administrator authentication is required.",
            )
        try:
            return get_ui_feedback_service().chat(
                authorization[7:].strip(),
                request,
            )
        except UiFeedbackAuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "ui_feedback_chat_failed",
                api_route=UI_FEEDBACK_CHAT_API_ROUTE,
                source="system",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="UI feedback discussion is temporarily unavailable.",
            ) from exc

    @application.post(
        UI_FEEDBACK_API_ROUTE,
        response_model=UiFeedbackSubmission,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def submit_ui_feedback(
        request: UiFeedbackRequest,
        authorization: str = Header(default=""),
    ) -> UiFeedbackSubmission:
        """Authenticate and dispatch one administrator UI improvement request."""
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Administrator authentication is required.",
            )
        try:
            return get_ui_feedback_service().submit(
                authorization[7:].strip(),
                request,
            )
        except UiFeedbackAuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except ConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "ui_feedback_submission_failed",
                api_route=UI_FEEDBACK_API_ROUTE,
                source="system",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="UI feedback could not be dispatched.",
            ) from exc

    @application.post(
        DISCOVERY_TASK_API_ROUTE,
        response_model=AnalysisTaskSubmission,
    )
    def submit_discovery(
        request: DiscoveryTaskRequest,
        http_request: Request,
    ):
        """Accept an automated alpha discovery task."""
        nonlocal active_task_coordinator
        if active_task_coordinator is None:
            active_task_coordinator = create_analysis_task_coordinator(
                Settings.from_env()
            )
        try:
            submission = active_task_coordinator.submit_discovery(request)
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "discovery_task_submission_failed",
                api_route=DISCOVERY_TASK_API_ROUTE,
                request_id=http_request.state.request_id,
                source="system",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Discovery task submission is temporarily unavailable.",
            ) from exc
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=submission.model_dump(mode="json"),
        )

    @application.get(
        f"{DISCOVERY_TASK_API_ROUTE}/{{task_id}}",
        response_model=DiscoveryTaskStatusResponse,
    )
    def get_discovery_task(
        task_id: str,
        http_request: Request,
    ) -> DiscoveryTaskStatusResponse:
        """Return current progress or the terminal result for one discovery task."""
        nonlocal active_task_coordinator
        if active_task_coordinator is None:
            active_task_coordinator = create_analysis_task_coordinator(
                Settings.from_env()
            )
        try:
            task = active_task_coordinator.get(task_id)
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "discovery_task_status_failed",
                api_route=DISCOVERY_TASK_API_ROUTE,
                request_id=http_request.state.request_id,
                source="system",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Discovery task status is temporarily unavailable.",
            ) from exc
        if task is None or getattr(task, "task_type", "") != "discovery":
            raise HTTPException(status_code=404, detail="Discovery task was not found.")
        return DiscoveryTaskStatusResponse(
            task_id=task.task_id,
            status=task.status,
            research_config=DiscoveryResearchConfig(
                target_pool=task.request.target_pool,
                train_start=task.request.train_start,
                train_end=task.request.train_end,
                val_start=task.request.val_start,
                val_end=task.request.val_end,
                factors=task.request.factors,
                forward_days=task.request.forward_days,
                target_return_pct=task.request.target_return_pct,
                minimum_samples=task.request.minimum_samples,
                minimum_trading_days=task.request.minimum_trading_days,
                minimum_securities=task.request.minimum_securities,
                minimum_outcome_coverage_pct=(
                    task.request.minimum_outcome_coverage_pct
                ),
                max_conditions=task.request.max_conditions,
            ),
            progress=task.progress,
            error=task.error,
        )

    @application.post(
        ANALYSIS_API_ROUTE,
        response_model=Union[AnalysisResponse, AnalysisTaskSubmission],
    )
    def analyze(
        request: AnalysisRequest,
        http_request: Request,
    ):
        """Accept a natural-language request for A-share data."""
        nonlocal active_service, active_task_coordinator
        if active_service is None:
            active_service = create_analysis_service()
        try:
            response = active_service.analyze(
                http_request.state.request_id,
                request,
                api_route=ANALYSIS_API_ROUTE,
            )
            if (
                response.error is not None
                and response.error.code == BACKGROUND_TASK_REQUIRED_ERROR_CODE
            ):
                try:
                    if active_task_coordinator is None:
                        active_task_coordinator = create_analysis_task_coordinator(
                            Settings.from_env()
                        )
                    submission = active_task_coordinator.submit(request)
                except Exception as exc:
                    log_event(
                        logger,
                        logging.ERROR,
                        "analysis_background_task_submission_failed",
                        api_route=ANALYSIS_API_ROUTE,
                        request_id=http_request.state.request_id,
                        source="system",
                        exc_info=True,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail=(
                            "Background analysis task submission is temporarily "
                            "unavailable."
                        ),
                    ) from exc
                log_event(
                    logger,
                    logging.INFO,
                    "analysis_background_task_submitted",
                    api_route=ANALYSIS_API_ROUTE,
                    request_id=http_request.state.request_id,
                    task_id=submission.task_id,
                )
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content=submission.model_dump(mode="json"),
                )
            return response
        except HTTPException:
            raise
        except Exception:
            # This API boundary prevents unexpected execution or transformation
            # defects from degrading into an untraceable plain-text HTTP 500.
            log_event(
                logger,
                logging.ERROR,
                "analysis_request_failed",
                api_route=ANALYSIS_API_ROUTE,
                request_id=http_request.state.request_id,
                source="system",
                exc_info=True,
            )
            return AnalysisResponse(
                request_id=http_request.state.request_id,
                planner=active_service.planner.name,
                data_provider=getattr(
                    active_service,
                    "data_provider_name",
                    "unknown",
                ),
                status="error",
                error=ServiceError(
                    source="system",
                    code="ANALYSIS_EXECUTION_FAILED",
                    message=(
                        "The analysis could not be completed. Use the request ID "
                        "to locate the server-side error."
                    ),
                ),
            )

    @application.get(
        f"{ANALYSIS_TASK_API_ROUTE}/{{task_id}}",
        response_model=AnalysisTaskStatusResponse,
    )
    def get_analysis_task(task_id: str) -> AnalysisTaskStatusResponse:
        """Return current progress or the terminal result for one task."""
        nonlocal active_task_coordinator
        if active_task_coordinator is None:
            active_task_coordinator = create_analysis_task_coordinator(
                Settings.from_env()
            )
        task = active_task_coordinator.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Analysis task was not found.")
        return AnalysisTaskStatusResponse(
            task_id=task.task_id,
            status=task.status,
            completed_items=task.completed_items,
            total_items=task.total_items,
            response=task.response,
            error=task.error,
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

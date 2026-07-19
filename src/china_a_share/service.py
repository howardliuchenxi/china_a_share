"""Analysis application orchestration."""

import logging

from .contracts import AnalysisRequest, AnalysisResponse
from .executor import TushareQueryExecutor
from .planner import DeepSeekQueryPlanner
from .planner import DeepSeekApiError
from .registry import StockApiRegistry
from .validation import ASharePlanValidator, PlanValidationError


logger = logging.getLogger(__name__)


class AnalysisService:
    """Coordinate planning, validation, execution, and response aggregation."""

    def __init__(
        self,
        registry: StockApiRegistry,
        planner: DeepSeekQueryPlanner,
        validator: ASharePlanValidator,
        executor: TushareQueryExecutor,
    ) -> None:
        self.registry = registry
        self.planner = planner
        self.validator = validator
        self.executor = executor

    def analyze(self, request_id: str, request: AnalysisRequest) -> AnalysisResponse:
        """Run the complete analysis workflow for one client request."""
        logger.info("analysis_started request_id=%s", request_id)
        try:
            candidates = self.registry.search(request.prompt)
            plan = self.planner.plan(request, candidates)
            validated_plan = self.validator.validate(plan)
        except DeepSeekApiError as exc:
            logger.error("planning_failed request_id=%s source=deepseek", request_id)
            return AnalysisResponse(
                request_id=request_id,
                status="error",
                error={
                    "source": "deepseek",
                    "code": exc.code,
                    "message": str(exc),
                    "http_status": exc.http_status,
                    "raw_response": exc.raw_response,
                },
            )
        except PlanValidationError as exc:
            logger.error("planning_failed request_id=%s source=system", request_id)
            return AnalysisResponse(
                request_id=request_id,
                status="error",
                error={"source": "system", "message": str(exc)},
            )
        except Exception as exc:
            logger.exception("planning_failed request_id=%s source=system", request_id)
            return AnalysisResponse(
                request_id=request_id,
                status="error",
                error={"source": "system", "message": str(exc)},
            )

        results = [self.executor.execute(query) for query in validated_plan.queries]
        success_count = sum(result.status == "success" for result in results)
        if success_count == len(results):
            overall_status = "success"
        elif success_count:
            overall_status = "partial_success"
        else:
            overall_status = "error"
        logger.info(
            "analysis_completed request_id=%s status=%s query_count=%s",
            request_id,
            overall_status,
            len(results),
        )
        return AnalysisResponse(
            request_id=request_id,
            status=overall_status,
            plan=validated_plan,
            results=results,
        )

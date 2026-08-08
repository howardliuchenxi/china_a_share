"""Vertex AI Claude implementation of the provider-neutral query-planner port."""

import json
import logging
from time import sleep
from typing import Any, Callable, Optional, Sequence

from pydantic import ValidationError

from china_a_share.core.contracts import (
    AnalysisRequest,
    DataOperation,
    QueryPlan,
)
from china_a_share.core.errors import PlannerError
from china_a_share.planners.deepseek import (
    DeepSeekQueryPlanner,
    build_query_plan_system_prompt,
)
from china_a_share.observability import ANALYSIS_REQUEST_ID, log_event


VERTEX_PROJECT = "china-a-share-lab"
VERTEX_REGION = "asia-east2"
VERTEX_MODEL = "claude-3-5-sonnet-v2@20241022"
VERTEX_API_URL = (
    f"https://{VERTEX_REGION}-aiplatform.googleapis.com/v1/projects/"
    f"{VERTEX_PROJECT}/locations/{VERTEX_REGION}/publishers/anthropic/models/"
    f"{VERTEX_MODEL}:rawPredict"
)
VERTEX_TIMEOUT_SECONDS = 60
VERTEX_MAX_OUTPUT_TOKENS = 4_000
VERTEX_MAX_ATTEMPTS = 2
VERTEX_RETRY_DELAY_SECONDS = 1


logger = logging.getLogger(__name__)


class VertexClaudeQueryPlanner:
    """Convert natural language into a query plan using Vertex AI Claude.

    Falls back to DeepSeek when Vertex AI is unavailable.
    """

    def __init__(
        self,
        deepseek_api_key: str,
        session: Optional[Any] = None,
    ) -> None:
        """Store credentials and the fallback DeepSeek planner."""
        self._fallback = DeepSeekQueryPlanner(deepseek_api_key, session=session)

    @property
    def name(self) -> str:
        """Return the stable planner identifier exposed in analysis responses."""
        return "vertex-claude"

    def plan(
        self,
        request: AnalysisRequest,
        candidate_operations: Sequence[DataOperation],
    ) -> QueryPlan:
        """Build a query plan using Vertex AI Claude, falling back to DeepSeek."""
        try:
            return self._plan_with_claude(request, candidate_operations)
        except PlannerError as exc:
            log_event(
                logger,
                logging.WARNING,
                "planner_fallback",
                request_id=ANALYSIS_REQUEST_ID.get(),
                from_provider="vertex",
                from_model=VERTEX_MODEL,
                to_provider="deepseek",
                reason=str(exc),
            )
            return self._fallback.plan(request, candidate_operations)

    def plan_validated(
        self,
        request: AnalysisRequest,
        candidate_operations: Sequence[DataOperation],
        validator: Callable[[QueryPlan], QueryPlan],
    ) -> QueryPlan:
        """Validate Claude output and give DeepSeek corrective retry feedback."""
        try:
            plan = self._plan_with_claude(request, candidate_operations)
            if plan.feasibility == "supported" and plan.answer_contract is None:
                raise ValueError(
                    "A supported model-generated plan must include answer_contract "
                    "with every user-requested final output field."
                )
            return validator(plan)
        except (PlannerError, ValueError) as exc:
            log_event(
                logger,
                logging.WARNING,
                "planner_fallback",
                request_id=ANALYSIS_REQUEST_ID.get(),
                from_provider="vertex",
                from_model=VERTEX_MODEL,
                to_provider="deepseek",
                reason=str(exc),
            )
            return self._fallback.plan_validated(
                request,
                candidate_operations,
                validator,
            )

    def generate_text(self, prompt: str) -> str:
        """Generate arbitrary text using the underlying LLM."""
        return self._fallback.generate_text(prompt)

    def _plan_with_claude(
        self,
        request: AnalysisRequest,
        candidate_operations: Sequence[DataOperation],
    ) -> QueryPlan:
        """Call Vertex AI Claude and return a validated query plan."""
        import google.auth
        import google.auth.transport.requests
        import requests as http_requests

        guidance = "\n".join(
            f"- {operation.name}: {operation.description}"
            for operation in candidate_operations
        )
        allowed_operations = ",".join(
            operation.name for operation in candidate_operations
        )
        system_prompt = build_query_plan_system_prompt(
            guidance,
            allowed_operations,
        )

        credentials, _ = google.auth.default()
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)
        access_token = credentials.token

        request_payload = {
            "anthropic_version": "vertex-2023-10-16",
            "messages": [
                {"role": "user", "content": request.prompt},
            ],
            "system": system_prompt,
            "max_tokens": VERTEX_MAX_OUTPUT_TOKENS,
            "temperature": 0,
        }

        last_exception = None
        for attempt in range(VERTEX_MAX_ATTEMPTS):
            log_event(
                logger,
                logging.INFO,
                "planner_model_attempt",
                request_id=ANALYSIS_REQUEST_ID.get(),
                provider="vertex",
                model=VERTEX_MODEL,
                attempt=attempt + 1,
                fallback=False,
            )
            try:
                response = http_requests.post(
                    VERTEX_API_URL,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=request_payload,
                    timeout=VERTEX_TIMEOUT_SECONDS,
                )
                if response.status_code >= 400:
                    error_body = response.text[:500]
                    raise PlannerError(
                        source=self.name,
                        message=f"Vertex AI returned HTTP {response.status_code}: {error_body}",
                        http_status=response.status_code,
                    )

                payload = response.json()
                content = "".join(
                    block.get("text", "")
                    for block in payload.get("content", [])
                    if block.get("type") == "text"
                )
                if not content:
                    raise PlannerError(
                        source=self.name,
                        message="Vertex AI returned an empty response.",
                    )

                log_event(
                    logger,
                    logging.INFO,
                    "planner_raw_output",
                    request_id=ANALYSIS_REQUEST_ID.get(),
                    provider="vertex",
                    model=VERTEX_MODEL,
                    content=content,
                )

                # Delegate normalization to the DeepSeek planner
                plan = self._fallback.normalize_and_validate_plan(content)
                return plan

            except (http_requests.RequestException, json.JSONDecodeError) as exc:
                last_exception = exc
                if attempt + 1 < VERTEX_MAX_ATTEMPTS:
                    sleep(VERTEX_RETRY_DELAY_SECONDS)
                    continue
                raise PlannerError(source=self.name, message=str(exc)) from exc
            except PlannerError:
                raise
            except Exception as exc:
                last_exception = exc
                if attempt + 1 < VERTEX_MAX_ATTEMPTS:
                    sleep(VERTEX_RETRY_DELAY_SECONDS)
                    continue
                raise PlannerError(source=self.name, message=str(exc)) from exc

        raise PlannerError(
            source=self.name,
            message=f"Vertex AI failed after {VERTEX_MAX_ATTEMPTS} attempts: {last_exception}",
        )

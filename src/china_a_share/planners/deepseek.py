"""DeepSeek implementation of the provider-neutral query-planner port."""

from datetime import datetime, time as ClockTime, timedelta
import json
import re
from time import sleep
from typing import Any, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from pydantic import ValidationError
import requests

from china_a_share.core.contracts import (
    AnalysisRequest,
    DataQuery,
    DataOperation,
    QueryPlan,
    ResultPipeline,
    ResultPipelineStep,
)
from china_a_share.core.errors import PlannerError


DEEPSEEK_PLANNER_NAME = "deepseek"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_TIMEOUT_SECONDS = 180
DEEPSEEK_MAX_OUTPUT_TOKENS = 2_000
DEEPSEEK_MAX_ATTEMPTS = 3
DEEPSEEK_RETRY_DELAY_SECONDS = 1
RETAIL_PROXY_DISCLOSURE = (
    "This result uses non_top10_float_ratio as a holding-dispersion proxy. "
    "It includes retail holders and institutions outside the disclosed top ten "
    "and is not a verified individual-investor ownership percentage."
)


def build_query_plan_system_prompt(
    guidance: str,
    allowed_operations: str,
) -> str:
    """Build provider-neutral planning instructions shared by every LLM."""
    return (
        "You plan read-only market-data queries for mainland China A-shares. "
        "Return one valid JSON object matching the supplied schema and use only "
        "operations from the active provider catalog. Resolve relative or partial "
        "dates using the current date "
        f"{datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()} and "
        "Asia/Shanghai semantics. Use the latest completed trading day for end-of-day "
        "data. Security codes must end in .SH, .SZ, or .BJ. Follow every operation's "
        "documented parameters and fields exactly. "
        "Decompose the request into atomic requirements and provide catalog evidence "
        "for each requirement. Preserve every numeric value and comparison direction "
        "from the user request; do not replace them with fixed thresholds or counts. "
        "Use result_pipeline for deterministic calculations instead of inventing "
        "specialized transforms. Pipelines may compose latest_by_group, derive, "
        "drop_missing, filter, sort, limit, quantile_filter, aggregate, rolling_mean, "
        "rolling_sum, shift, match_source, compare_fields, compare_scalar, and "
        "summarize. Use right_source_query_id and join_on for multi-source matching. "
        "For ordered calculations, provide group_by and order_by. Fetch enough source "
        "history to initialize rolling windows before filtering to the requested "
        "measurement interval. Drop rows whose required future outcome is unavailable. "
        "Use filters only for row conditions and provider params only for parameters "
        "explicitly documented by the catalog. "
        "Mark feasibility as supported only when every requirement maps to a documented "
        "provider field or parameter, a declared transform, or a valid result_pipeline "
        "step. Otherwise mark the unsupported requirements, state a concrete limitation, "
        "and return no queries. Do not substitute a similar metric or proxy unless the "
        "user explicitly requested that metric. Never infer unavailable values or "
        "invent data.\n\n"
        f"Operation catalog:\n{guidance}\n\n"
        f"Allowed operation names:\n{allowed_operations}\n\n"
        f"JSON schema:\n{json.dumps(QueryPlan.model_json_schema())}"
    )


class DeepSeekQueryPlanner:
    """Convert natural language into a query plan using DeepSeek."""

    def __init__(self, api_key: str, session: Optional[Any] = None) -> None:
        """Store the required credential and an optional injectable HTTP session."""
        self._api_key = api_key
        self._session = session if session is not None else requests.Session()

    @property
    def name(self) -> str:
        """Return the stable planner identifier exposed in analysis responses."""
        return DEEPSEEK_PLANNER_NAME

    def plan(
        self,
        request: AnalysisRequest,
        candidate_operations: Sequence[DataOperation],
    ) -> QueryPlan:
        """Build a query plan using only the active provider's catalog."""
        return self._plan_with_validation(
            request,
            candidate_operations,
            validator=None,
        )

    def plan_validated(
        self,
        request: AnalysisRequest,
        candidate_operations: Sequence[DataOperation],
        validator: Callable[[QueryPlan], QueryPlan],
    ) -> QueryPlan:
        """Build and locally validate a plan with bounded corrective retries."""
        return self._plan_with_validation(
            request,
            candidate_operations,
            validator=validator,
        )

    def _plan_with_validation(
        self,
        request: AnalysisRequest,
        candidate_operations: Sequence[DataOperation],
        validator: Optional[Callable[[QueryPlan], QueryPlan]],
    ) -> QueryPlan:
        """Retry planning with concrete validation feedback before audited fallback."""
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
        feedback = None
        last_error: Optional[Exception] = None
        for attempt in range(DEEPSEEK_MAX_ATTEMPTS):
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.prompt},
            ]
            if feedback:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "The previous query plan was rejected by the trusted "
                            "local validator. Return a complete corrected plan and "
                            "do not repeat this error:\n"
                            f"{feedback}"
                        ),
                    }
                )
            request_payload = {
                "model": DEEPSEEK_MODEL,
                "messages": messages,
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
                "max_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
                "stream": False,
            }
            try:
                response = self._request_once(request_payload)
                plan = self._decode_plan_response(response)
            except PlannerError as exc:
                if exc.http_status in {400, 401, 403, 404}:
                    raise
                last_error = exc
                feedback = str(exc)
                if attempt + 1 < DEEPSEEK_MAX_ATTEMPTS:
                    sleep(DEEPSEEK_RETRY_DELAY_SECONDS)
                    continue
                break

            self._finalize_plan(plan)
            if validator is not None:
                try:
                    validator(plan)
                    return plan
                except ValueError as exc:
                    last_error = exc
                    feedback = str(exc)
            else:
                return plan

            if attempt + 1 < DEEPSEEK_MAX_ATTEMPTS:
                sleep(DEEPSEEK_RETRY_DELAY_SECONDS)

        if isinstance(last_error, PlannerError):
            raise last_error
        raise PlannerError(
            source=self.name,
            message=(
                "DeepSeek could not produce a valid query plan after "
                f"{DEEPSEEK_MAX_ATTEMPTS} attempts: {last_error}"
            ),
        ) from last_error

    def _finalize_plan(self, plan: QueryPlan) -> None:
        """Apply deterministic normalization before semantic validation."""
        self._normalize_fields(plan)
        self._normalize_limit_list_queries(plan)
        self._normalize_latest_completed_date(plan)
        self._downgrade_unexecutable_plan(plan)
        self._split_multi_security_float_holder_queries(plan)
        self._append_audited_disclosures(plan)

    def normalize_and_validate_plan(self, raw_content: str) -> QueryPlan:
        """Parse, normalize, and validate a raw plan JSON from an external planner."""
        import json
        try:
            raw_plan = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            raise PlannerError(
                source=self.name,
                message="Planner returned non-JSON content.",
            ) from exc
        self._normalize_raw_query_defaults(raw_plan)
        plan = QueryPlan.model_validate(raw_plan)
        self._finalize_plan(plan)
        return plan

    @staticmethod
    def _append_audited_disclosures(plan: QueryPlan) -> None:
        """Attach user-visible caveats for declared approximation transforms."""
        if plan.feasibility != "supported":
            return

        if any(
            query.transform == "cr10_float_trend"
            for query in plan.queries
        ) and RETAIL_PROXY_DISCLOSURE not in plan.limitations:
            plan.limitations.append(RETAIL_PROXY_DISCLOSURE)

    def _decode_plan_response(self, response: Any) -> QueryPlan:
        """Validate one planner response before any deterministic normalization."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise PlannerError(
                source=self.name,
                message="DeepSeek returned a non-JSON response.",
                http_status=response.status_code,
                raw_response={"text": response.text},
            ) from exc

        if response.status_code >= 400 or payload.get("error"):
            upstream_error = payload.get("error") or {}
            raise PlannerError(
                source=self.name,
                message=str(upstream_error.get("message") or "DeepSeek request failed."),
                code=upstream_error.get("code"),
                http_status=response.status_code,
                raw_response=payload,
            )

        content = ((payload.get("choices") or [{}])[0].get("message") or {}).get(
            "content", ""
        )
        if not content:
            raise PlannerError(
                source=self.name,
                message="DeepSeek returned an empty query plan.",
                http_status=response.status_code,
                raw_response=payload,
            )
        try:
            raw_plan = json.loads(content)
            self._normalize_raw_query_defaults(raw_plan)
            plan = QueryPlan.model_validate(raw_plan)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise PlannerError(
                source=self.name,
                message="DeepSeek returned a query plan that violates the contract.",
                http_status=response.status_code,
                raw_response={"content": content},
            ) from exc
        return plan

    def generate_text(self, prompt: str) -> str:
        """Generate arbitrary text using the underlying LLM."""
        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
        }
        try:
            response = self._session.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=DEEPSEEK_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except requests.RequestException as exc:
            raise PlannerError(source=self.name, message=str(exc)) from exc

    def _request_once(self, request_payload: dict) -> Any:
        """Issue one planner request so the outer loop bounds total model calls."""
        try:
            return self._session.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=request_payload,
                timeout=DEEPSEEK_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise PlannerError(source=self.name, message=str(exc)) from exc

    @staticmethod
    def _normalize_raw_query_defaults(raw_plan: Any) -> None:
        """Replace nullable query list fields before contract validation."""
        if not isinstance(raw_plan, dict):
            return
        queries = raw_plan.get("queries")
        if not isinstance(queries, list):
            return
        for query in queries:
            if not isinstance(query, dict):
                continue
            if query.get("filters") is None:
                query["filters"] = []
            if query.get("aggregations") is None:
                query["aggregations"] = []

    @staticmethod
    def _normalize_fields(plan: QueryPlan) -> None:
        """Move a model-generated reserved fields parameter into the contract slot."""
        for query in plan.queries:
            misplaced_fields = query.params.pop("fields", None)
            if query.fields or misplaced_fields is None:
                continue
            if isinstance(misplaced_fields, str):
                query.fields = [
                    field.strip()
                    for field in misplaced_fields.split(",")
                    if field.strip()
                ]
            elif isinstance(misplaced_fields, list) and all(
                isinstance(field, str) for field in misplaced_fields
            ):
                query.fields = misplaced_fields

    @staticmethod
    def _normalize_limit_list_queries(plan: QueryPlan) -> None:
        """Use the native limit-list category and its result row count."""
        for query in plan.queries:
            if query.operation != "limit_list_d":
                continue

            retained_filters = []
            for row_filter in query.filters:
                if (
                    row_filter.field == "limit_type"
                    and row_filter.operator == "eq"
                    and isinstance(row_filter.value, str)
                ):
                    query.params.setdefault("limit_type", row_filter.value)
                    continue
                retained_filters.append(row_filter)
            query.filters = retained_filters
            query.fields = [
                field for field in query.fields if field != "limit_type"
            ]
            # Tushare codes are identifiers, not numeric measures. QueryResult.row_count
            # already provides the exact count requested alongside the returned rows.
            query.aggregations = [
                aggregation
                for aggregation in query.aggregations
                if aggregation.field != "ts_code"
            ]

    @staticmethod
    def _normalize_latest_completed_date(plan: QueryPlan) -> None:
        """Keep latest end-of-day queries behind the provider publication cutoff."""
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        completed_date = now.date()
        if now.time() < ClockTime(17, 10):
            completed_date -= timedelta(days=1)
        while completed_date.weekday() >= 5:
            completed_date -= timedelta(days=1)
        today = now.strftime("%Y%m%d")
        completed = completed_date.strftime("%Y%m%d")
        for query in plan.queries:
            if (
                query.operation
                in {"daily", "daily_basic", "margin", "margin_detail"}
                and query.params.get("trade_date") == today
            ):
                query.params["trade_date"] = completed
            if query.operation == "stock_st" and (
                query.params.get("trade_date") == today
                or query.params.get("end_date") == today
            ):
                query.params = {"trade_date": completed}
                query.fields = [
                    field for field in query.fields if field != "status"
                ]
                query.filters = [
                    row_filter
                    for row_filter in query.filters
                    if row_filter.field != "status"
                ]

    @staticmethod
    def _downgrade_unexecutable_plan(plan: QueryPlan) -> None:
        """Turn known missing native parameters and guessed proxies into limitations."""
        limitation = None
        for query in plan.queries:
            if query.operation == "top_list" and not query.params.get("trade_date"):
                limitation = (
                    "top_list requires one trade_date and does not support the "
                    "requested range aggregation."
                )
                break
            if any(
                isinstance(value, str)
                and (
                    "待填充" in value
                    or "placeholder" in value.lower()
                    or "${" in value
                )
                for value in query.params.values()
            ):
                limitation = "The plan contains an unresolved provider parameter."
                break
            if (
                query.operation == "daily_basic"
                and "pct_chg" in query.fields
            ):
                limitation = (
                    "daily_basic does not provide pct_chg; this screen requires an "
                    "unsupported cross-operation join with daily prices."
                )
                break
            if (
                query.operation == "new_share"
                and "pct_chg" in query.fields
            ):
                limitation = (
                    "new_share does not provide first-day price change; calculating "
                    "it requires a separate listing-day price join."
                )
                break
            if (
                query.operation == "dividend"
                and (
                    query.params.get("start_date")
                    or query.params.get("end_date")
                )
            ):
                limitation = (
                    "dividend does not provide the requested full-market date-range "
                    "screen through these parameters."
                )
                break

        if limitation is None:
            if (
                len(plan.queries) > 1
            ):
                non_catalog_queries = [
                    query
                    for query in plan.queries
                    if query.operation != "stock_basic"
                ]
                if non_catalog_queries and all(
                    "ts_code" in query.fields
                    and query.params.get("ts_code")
                    for query in non_catalog_queries
                ):
                    plan.queries = non_catalog_queries
            return
        plan.feasibility = "unsupported"
        plan.limitations = [limitation]
        plan.queries = []
        for requirement in plan.requirements:
            requirement.status = "unsupported"

    @staticmethod
    def _split_multi_security_float_holder_queries(plan: QueryPlan) -> None:
        """Split float-holder reads because Tushare accepts one security per call."""
        normalized_queries = []
        for query in plan.queries:
            raw_codes = query.params.get("ts_code")
            if query.operation != "top10_floatholders" or not isinstance(
                raw_codes, str
            ):
                normalized_queries.append(query)
                continue

            security_codes = [
                code.strip()
                for code in re.split(r"[,，]", raw_codes)
                if code.strip()
            ]
            if len(security_codes) <= 1:
                normalized_queries.append(query)
                continue

            for index, security_code in enumerate(security_codes, start=1):
                split_query: DataQuery = query.model_copy(deep=True)
                split_query.query_id = f"{query.query_id}-{index}"
                split_query.params["ts_code"] = security_code
                split_query.purpose = f"{query.purpose} Security: {security_code}."
                normalized_queries.append(split_query)

        plan.queries = normalized_queries

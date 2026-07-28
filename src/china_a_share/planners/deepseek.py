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
        system_prompt = (
            "You plan read-only market-data queries for mainland China A-shares. "
            "Return one valid JSON object matching the supplied schema. Use only "
            "operations from the active provider catalog. Resolve relative or partial "
            "dates using the current date "
            f"{datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()} and "
            "Asia/Shanghai semantics. For 'latest' end-of-day data, use the latest "
            "completed trading day rather than an uncompleted current date. "
            "Security codes must end in .SH, .SZ, or .BJ. "
            "Follow each operation's native parameter semantics. For requests that "
            "count advancing or declining stocks, use the full-market daily operation "
            "for one trade date, include ts_code, change, and pct_chg in fields, and "
            "add local conditional "
            "counts for change gt 0, lt 0, and eq 0. Decompose every user request "
            "For requests asking which stocks hit limit up, use limit_list_d with "
            "the native parameter limit_type='U'; request ts_code and name, and use "
            "the returned row count as the total. Do not filter the output limit_type "
            "field and do not create a conditional count over ts_code. "
            "For requests asking for the probability that the next trading day "
            "rises after N consecutive limit-up trading days, create exactly two "
            "range queries over the same requested window: limit_list_d with native "
            "limit_type='U' and fields trade_date,ts_code,name; and daily with fields "
            "trade_date,ts_code,pct_chg. Build a result_pipeline sourced from daily: "
            "match limit_list_d on trade_date and ts_code, apply a rolling_sum window "
            "of N, shift the outcome, filter complete streaks, and summarize. This "
            "deterministic pipeline "
            "joins securities across consecutive market trading dates, excludes "
            "signals without next-session data, and computes the requested probability. "
            "Mark all such requirements covered. "
            "Use one range query plus result_pipeline for common ranking and grouping "
            "requests. Compose aggregate, sort, and limit with the exact user-requested "
            "count; never encode Top N into a transform name. Group by trade_date for "
            "daily limit-up trends, by ts_code for security counts, and by industry for "
            "industry counts. Sort amount or turnover_rate and apply limit for rankings. "
            "For block-trade totals, aggregate amount by ts_code, sort total_amount, "
            "and apply the requested limit. "
            "period_return_by_ts_code for multi-security period return comparisons. "
            "For a market-wide or industry-wide return ranking over a month, year, "
            "or arbitrary date range, use daily with start_date and end_date, request "
            "ts_code,trade_date,close, and set transform=period_return_by_ts_code. "
            "The executor reads only the first and last available full-market trading "
            "day snapshots and calculates returns locally. Never use monthly or weekly "
            "without ts_code or trade_date. "
            "For dividend rankings, apply valuation filters, sort dv_ratio, and limit "
            "to the exact requested count. "
            "Never expand a date range into one query per day. "
            "When a user asks for retail ownership, retail holding ratio, retail trend, "
            "shareholding dispersion, or CR10, treat retail ratio as the project's "
            "fixed non_top10_float_ratio proxy: 100% minus the sum of the disclosed "
            "top ten unrestricted float-holder ratios. Use top10_floatholders with "
            "transform=cr10_float_trend. Describe non_top10_float_ratio as a holding "
            "dispersion proxy that includes both retail holders and institutions outside "
            "the top ten; never describe it as a verified account-level percentage held "
            "by individual investors. This proxy is explicitly approved for retail-ratio "
            "requests and must not by itself make a plan unsupported. For "
            "top10_floatholders, period must be a reporting quarter end "
            "(YYYY0331, YYYY0630, YYYY0930, or YYYY1231). When the user supplies an "
            "arbitrary date, interpret it as an as-of date and use end_date without "
            "period so the latest disclosed snapshot can be selected. The transformation "
            "requires ten unique disclosed holders per reporting snapshot and returns "
            "an explicitly partial result when a source ratio is missing. "
            "Create one top10_floatholders query per security. Never combine multiple "
            "security codes in one top10_floatholders ts_code parameter. "
            "For newly listed stocks (IPO within 6 months), CR10 concentration ratios "
            "are unreliable due to lockup periods. In those cases, pair "
            "top10_floatholders with stk_holdernumber to retrieve shareholder count "
            "(holder_num). Combine with daily_basic.float_share to compute average "
            "holding per shareholder: lower shareholder count and higher average "
            "holding suggest institutional concentration regardless of CR10. For "
            "generic retail-ranking requests, include both data sources so the "
            "consumer can apply a composite score."
            "For deterministic post-query calculations, prefer result_pipeline over "
            "inventing a specialized transform. A result pipeline consumes exactly one "
            "query result and may compose latest_by_group, derive, drop_missing, filter, "
            "sort, limit, quantile_filter, aggregate, rolling_mean, shift, "
            "compare_fields, compare_scalar, and summarize steps. Use sort followed by "
            "limit for Top N. Use quantile_filter with a quantile between 0 and 1 for "
            "percentile requests. For ordered time-series analysis, use rolling_mean "
            "and shift with group_by security identifiers and order_by trade_date. "
            "Positive shift periods read prior rows and negative periods read future "
            "rows. Use compare_fields or compare_scalar to create boolean event fields, "
            "filter to the requested event cohort, drop outcomes without a future row, "
            "and summarize count and mean to calculate an event probability. Fetch "
            "enough observations before the requested measurement window to initialize "
            "rolling calculations, then filter event dates to the requested window. "
            "For a full-market retail-proxy ranking, query "
            "stock_basic as the universe, add one top10_floatholders template without "
            "ts_code using transform=cr10_float_trend, then apply latest_by_group on "
            "ts_code ordered by end_date, drop missing non_top10_float_ratio, and apply "
            "the requested sort/limit or quantile_filter steps. "
            "Decompose every user request "
            "into atomic requirements and provide concrete catalog evidence for each "
            "one. Mark feasibility as supported only when every requirement maps to "
            "an explicitly documented provider parameter or field, or to a deterministic "
            "local filter or aggregation in the schema. Use filters when the user asks "
            "to find, screen, or return only rows matching a numeric condition. Never "
            "place a row-filter threshold in provider params unless the catalog explicitly "
            "documents that native parameter. If any required field, operation, join, "
            "calculation, or filter cannot be verified from the supplied catalog, mark "
            "feasibility as unsupported, include at least one unsupported requirement "
            "and a concrete limitation, and return no queries. Do not substitute a similar "
            "metric, infer unavailable values, or claim support based on general knowledge. "
            "Do not invent data.\n\n"
            f"Operation catalog:\n{guidance}\n\n"
            f"Allowed operation names:\n{allowed_operations}\n\n"
            f"JSON schema:\n{json.dumps(QueryPlan.model_json_schema())}"
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
                plan = self._decode_plan_response(response, request.prompt)
            except PlannerError as exc:
                if exc.http_status in {400, 401, 403, 404}:
                    raise
                last_error = exc
                feedback = str(exc)
                if attempt + 1 < DEEPSEEK_MAX_ATTEMPTS:
                    sleep(DEEPSEEK_RETRY_DELAY_SECONDS)
                    continue
                break

            self._finalize_plan(plan, request.prompt)
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

    def _finalize_plan(self, plan: QueryPlan, prompt: str) -> None:
        """Apply deterministic normalization before semantic validation."""
        self._normalize_fields(plan)
        self._normalize_limit_list_queries(plan)
        self._normalize_latest_completed_date(plan, prompt)
        self._downgrade_unexecutable_plan(plan, prompt)
        self._split_multi_security_float_holder_queries(plan)
        self._append_audited_disclosures(plan, prompt)

    def normalize_and_validate_plan(
        self, raw_content: str, prompt: str
    ) -> QueryPlan:
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
        self._finalize_plan(plan, prompt)
        return plan

    @staticmethod
    def _append_audited_disclosures(plan: QueryPlan, prompt: str) -> None:
        """Attach user-visible caveats for approved approximations and assumptions."""
        if plan.feasibility != "supported":
            return

        disclosures = []
        if any(
            query.transform == "cr10_float_trend"
            for query in plan.queries
        ):
            disclosures.append(RETAIL_PROXY_DISCLOSURE)

        normalized = prompt.replace(" ", "")
        month_match = re.search(r"(?:(\d{4})年)?(\d{1,2})月", normalized)
        if month_match and month_match.group(1) is None:
            period_query = next(
                (
                    query
                    for query in plan.queries
                    if query.transform == "period_return_by_ts_code"
                    and isinstance(query.params.get("start_date"), str)
                ),
                None,
            )
            if period_query is not None:
                resolved_year = period_query.params["start_date"][:4]
                disclosures.append(
                    "The omitted year was resolved to "
                    f"{resolved_year} using Asia/Shanghai semantics."
                )

        for disclosure in disclosures:
            if disclosure not in plan.limitations:
                plan.limitations.append(disclosure)

    def _decode_plan_response(self, response: Any, prompt: str) -> QueryPlan:
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
    def _normalize_latest_completed_date(plan: QueryPlan, prompt: str) -> None:
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
                or "当前" in prompt
                or "最新" in prompt
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
    def _downgrade_unexecutable_plan(plan: QueryPlan, prompt: str) -> None:
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

        if (
            "股票" in prompt
            and any(query.operation == "margin" for query in plan.queries)
            and not any(query.operation == "margin_detail" for query in plan.queries)
        ):
            limitation = (
                "margin is market-level data and cannot rank individual securities."
            )
        if (
            "且" in prompt
            and any(query.operation == "daily" for query in plan.queries)
            and any(query.operation == "daily_basic" for query in plan.queries)
        ):
            limitation = (
                "The requested intersection requires an unsupported cross-operation "
                "join between daily and daily_basic."
            )

        limitation_text = " ".join(plan.limitations)
        if plan.feasibility == "supported" and any(
            marker in limitation_text
            for marker in ("无法", "不提供", "缺少", "不可用", "未明确")
        ):
            limitation = limitation_text
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

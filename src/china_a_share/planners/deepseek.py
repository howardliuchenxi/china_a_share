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
DEEPSEEK_TIMEOUT_SECONDS = 60
DEEPSEEK_MAX_OUTPUT_TOKENS = 2_000
DEEPSEEK_MAX_ATTEMPTS = 3
DEEPSEEK_RETRY_DELAY_SECONDS = 1
PERIOD_RETURN_FIELD_ALIASES = {
    "period_return": "period_return_pct",
    "return_pct": "period_return_pct",
    "pct_return": "period_return_pct",
}
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
            "For requests asking for the probability that the third trading day "
            "rises after two consecutive limit-up trading days, create exactly two "
            "range queries over the same requested window: limit_list_d with native "
            "limit_type='U' and fields trade_date,ts_code,name; and daily with fields "
            "trade_date,ts_code,pct_chg. Set result_transform to "
            "two_limit_up_next_day_probability. This deterministic local transform "
            "joins securities across consecutive market trading dates, excludes "
            "signals without third-day data, and computes the requested probability. "
            "Mark all such requirements covered. "
            "Use one range query plus a deterministic query transform for common "
            "ranking and grouping requests. Use count_by_trade_date for daily limit-up "
            "trends, top_count_by_trade_date for the date with the most limit-ups, "
            "top_10_count_by_ts_code for the ten securities with the most limit-ups, "
            "count_by_ts_code for complete security counts, and count_by_industry for "
            "limit-ups by industry. Use "
            "top_20_by_amount for the twenty highest daily amounts, and "
            "top_20_by_turnover_rate for the twenty highest turnover rates. Use "
            "top_20_total_amount_by_ts_code to aggregate and rank block-trade amount "
            "over a date range. "
            "period_return_by_ts_code for multi-security period return comparisons. "
            "For a market-wide or industry-wide return ranking over a month, year, "
            "or arbitrary date range, use daily with start_date and end_date, request "
            "ts_code,trade_date,close, and set transform=period_return_by_ts_code. "
            "The executor reads only the first and last available full-market trading "
            "day snapshots and calculates returns locally. Never use monthly or weekly "
            "without ts_code or trade_date. "
            "Use top_10_by_dv_ratio after valuation filters when the user asks for "
            "ten high-dividend securities. "
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
            "sort, limit, quantile_filter, and aggregate steps. Use sort followed by "
            "limit for Top N. Use quantile_filter with a quantile between 0 and 1 for "
            "percentile requests. For a full-market retail-proxy ranking, query "
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
        known_retail_ranking_plan = self._build_known_retail_ranking_plan(
            request.prompt,
            candidate_operations,
        )
        known_market_period_ranking_plan = (
            self._build_known_market_period_ranking_plan(
                request.prompt,
                candidate_operations,
            )
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
            known_error = self._known_plan_error(
                plan,
                known_retail_ranking_plan=known_retail_ranking_plan,
                known_market_period_ranking_plan=known_market_period_ranking_plan,
            )
            if known_error is not None:
                last_error = ValueError(known_error)
                feedback = known_error
            elif validator is not None:
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

        fallback_plan = (
            known_retail_ranking_plan or known_market_period_ranking_plan
        )
        if fallback_plan is not None:
            self._append_audited_disclosures(fallback_plan, request.prompt)
            if validator is not None:
                validator(fallback_plan)
            return fallback_plan
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
        self._normalize_common_analytics(plan, prompt)
        self._normalize_market_period_returns(plan, prompt)
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
        self._normalize_two_limit_up_analysis(raw_plan, prompt)
        plan = QueryPlan.model_validate(raw_plan)
        self._finalize_plan(plan, prompt)
        return plan

    @staticmethod
    def _known_plan_error(
        plan: QueryPlan,
        *,
        known_retail_ranking_plan: Optional[QueryPlan],
        known_market_period_ranking_plan: Optional[QueryPlan],
    ) -> Optional[str]:
        """Return a corrective error when an audited intent is planned incorrectly."""
        if known_retail_ranking_plan is not None:
            pipeline_source = (
                plan.result_pipeline.source_query_id
                if plan.result_pipeline is not None
                else None
            )
            has_universe_query = any(
                query.operation in {"stock_basic", "ths_member"}
                for query in plan.queries
            )
            has_retail_template = any(
                query.query_id == pipeline_source
                and query.transform == "cr10_float_trend"
                and not query.params.get("ts_code")
                for query in plan.queries
            )
            if not has_universe_query or not has_retail_template:
                return (
                    "The retail ranking requires a stock_basic or ths_member "
                    "universe and must source its result pipeline from a "
                    "top10_floatholders template using "
                    "transform=cr10_float_trend."
                )
        if known_market_period_ranking_plan is not None:
            pipeline_source = (
                plan.result_pipeline.source_query_id
                if plan.result_pipeline is not None
                else None
            )
            if not any(
                query.query_id == pipeline_source
                and query.operation == "daily"
                and query.transform == "period_return_by_ts_code"
                for query in plan.queries
            ):
                return (
                    "The full-market period ranking must source its result pipeline "
                    "from daily using transform=period_return_by_ts_code."
                )
        return None

    @staticmethod
    def _build_known_retail_ranking_plan(
        prompt: str,
        candidate_operations: Sequence[DataOperation],
    ) -> Optional[QueryPlan]:
        """Build the audited full-market retail-proxy ranking after model drift."""
        normalized = prompt.replace(" ", "")
        requested_limit = DeepSeekQueryPlanner._parse_requested_limit(normalized)
        asks_for_retail_ranking = (
            "散户" in normalized
            and any(
                marker in normalized
                for marker in ("股票", "A股", "大A", "全市场")
            )
            and (
                requested_limit is not None
                or any(
                    marker in normalized
                    for marker in ("最多", "最少", "最高", "最低", "排名")
                )
            )
        )
        available_operations = {
            operation.name for operation in candidate_operations
        }
        if not asks_for_retail_ranking or not {
            "stock_basic",
            "top10_floatholders",
        }.issubset(available_operations):
            return None

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        month_match = re.search(r"(?:(\d{4})年)?(\d{1,2})月", normalized)
        holder_params: dict[str, str] = {}
        if month_match:
            month = int(month_match.group(2))
            if not 1 <= month <= 12:
                return None
            explicit_year = month_match.group(1)
            year = int(explicit_year or now.year)
            if explicit_year is None and month > now.month:
                year -= 1
            if month in {3, 6, 9, 12}:
                holder_params["period"] = f"{year}{month:02d}{31 if month in {3, 12} else 30}"
            else:
                if month == 12:
                    next_month = datetime(year + 1, 1, 1)
                else:
                    next_month = datetime(year, month + 1, 1)
                holder_params["end_date"] = (
                    next_month - timedelta(days=1)
                ).strftime("%Y%m%d")

        ascending = any(
            marker in normalized for marker in ("最少", "最低")
        )
        return QueryPlan.model_validate(
            {
                "interpretation": (
                    "Rank the A-share market by the approved non-top-ten "
                    "unrestricted float-holder ratio proxy."
                ),
                "requirements": [
                    {
                        "requirement": (
                            "Retrieve the complete listed A-share security universe."
                        ),
                        "status": "covered",
                        "implementation": "Use stock_basic with list_status=L.",
                        "evidence": (
                            "stock_basic returns listed A-share security codes."
                        ),
                    },
                    {
                        "requirement": (
                            "Calculate the approved retail holding proxy for the "
                            "requested reporting period."
                        ),
                        "status": "covered",
                        "implementation": (
                            "Use top10_floatholders with cr10_float_trend."
                        ),
                        "evidence": (
                            "The audited transform returns non_top10_float_ratio."
                        ),
                    },
                    {
                        "requirement": "Return the requested market ranking.",
                        "status": "covered",
                        "implementation": (
                            "Sort the latest complete proxy values and apply Top N."
                        ),
                        "evidence": (
                            "The validated result pipeline performs deterministic "
                            "sorting and limiting."
                        ),
                    },
                ],
                "queries": [
                    {
                        "query_id": "a-share-universe",
                        "operation": "stock_basic",
                        "params": {"list_status": "L"},
                        "fields": ["ts_code", "name"],
                        "purpose": "Retrieve the listed A-share security universe.",
                    },
                    {
                        "query_id": "retail-proxy",
                        "operation": "top10_floatholders",
                        "params": holder_params,
                        "fields": [
                            "ts_code",
                            "ann_date",
                            "end_date",
                            "holder_name",
                            "hold_amount",
                            "hold_float_ratio",
                        ],
                        "purpose": (
                            "Calculate the approved holding-dispersion proxy for "
                            "each listed security."
                        ),
                        "transform": "cr10_float_trend",
                    },
                ],
                "result_pipeline": {
                    "source_query_id": "retail-proxy",
                    "output_query_id": "ranked-retail-proxy",
                    "steps": [
                        {
                            "operation": "latest_by_group",
                            "group_by": ["ts_code"],
                            "order_by": "end_date",
                        },
                        {
                            "operation": "drop_missing",
                            "fields": ["non_top10_float_ratio"],
                        },
                        {
                            "operation": "sort",
                            "field": "non_top10_float_ratio",
                            "direction": "asc" if ascending else "desc",
                        },
                        {
                            "operation": "limit",
                            "count": requested_limit or 10,
                        },
                    ],
                },
            }
        )

    @staticmethod
    def _build_known_market_period_ranking_plan(
        prompt: str,
        candidate_operations: Sequence[DataOperation],
    ) -> Optional[QueryPlan]:
        """Build the supported full-market period-return ranking after model drift."""
        normalized = prompt.replace(" ", "")
        requested_limit = DeepSeekQueryPlanner._parse_requested_limit(normalized)
        has_market_scope = any(
            marker in normalized
            for marker in ("A股", "大A", "全市场")
        )
        has_return_metric = any(
            marker in normalized
            for marker in ("涨幅", "跌幅", "收益率", "上涨", "下跌")
        )
        has_ranking = requested_limit is not None or any(
            marker in normalized
            for marker in ("最大", "最高", "最小", "最低", "排名", "最多")
        )
        has_supported_period = bool(
            re.search(r"(?:(?:\d{4})年)?\d{1,2}月", normalized)
            or re.search(r"\d{4}年", normalized)
            or "今年" in normalized
            or "去年" in normalized
        )
        available_operations = {
            operation.name for operation in candidate_operations
        }
        if not (
            has_market_scope
            and has_return_metric
            and has_ranking
            and has_supported_period
            and "daily" in available_operations
        ):
            return None

        plan = QueryPlan.model_validate(
            {
                "interpretation": (
                    "Rank the full A-share market by return over the requested "
                    "calendar period."
                ),
                "requirements": [
                    {
                        "requirement": (
                            "Calculate each A-share security's return over the "
                            "requested period and return the requested ranking."
                        ),
                        "status": "covered",
                        "implementation": (
                            "Read the first and last available full-market daily "
                            "snapshots, calculate returns locally, sort, and limit."
                        ),
                        "evidence": (
                            "The daily operation provides trade_date, ts_code, and "
                            "close; period_return_by_ts_code performs the audited "
                            "boundary calculation."
                        ),
                    }
                ],
                "queries": [
                    {
                        "query_id": "market-period-return",
                        "operation": "daily",
                        "params": {
                            "start_date": "20000101",
                            "end_date": "20000102",
                        },
                        "fields": ["ts_code", "trade_date", "close"],
                        "purpose": (
                            "Retrieve full-market boundary snapshots for the "
                            "requested return period."
                        ),
                    }
                ],
            }
        )
        DeepSeekQueryPlanner._normalize_market_period_returns(plan, prompt)
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
            self._normalize_two_limit_up_analysis(raw_plan, prompt)
            plan = QueryPlan.model_validate(raw_plan)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise PlannerError(
                source=self.name,
                message="DeepSeek returned a query plan that violates the contract.",
                http_status=response.status_code,
                raw_response={"content": content},
            ) from exc
        return plan

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
        """Replace nullable list fields and move misplaced query transformations."""
        if not isinstance(raw_plan, dict):
            return
        queries = raw_plan.get("queries")
        if not isinstance(queries, list):
            return
        misplaced_transform = raw_plan.get("result_transform")
        query_transforms = {
            "count_by_trade_date",
            "top_count_by_trade_date",
            "count_by_ts_code",
            "top_10_count_by_ts_code",
            "count_by_industry",
            "top_20_by_amount",
            "top_20_by_turnover_rate",
            "top_20_total_amount_by_ts_code",
            "period_return_by_ts_code",
            "top_10_by_dv_ratio",
        }
        for query in queries:
            if not isinstance(query, dict):
                continue
            if query.get("filters") is None:
                query["filters"] = []
            if query.get("aggregations") is None:
                query["aggregations"] = []
            if misplaced_transform in query_transforms:
                query["transform"] = misplaced_transform
                fields = query.setdefault("fields", [])
                if misplaced_transform == "period_return_by_ts_code":
                    for field in ("ts_code", "trade_date", "close"):
                        if field not in fields:
                            fields.append(field)
        if misplaced_transform in query_transforms:
            raw_plan["result_transform"] = None

    @staticmethod
    def _normalize_two_limit_up_analysis(
        raw_plan: Any,
        prompt: str,
    ) -> None:
        """Repair the known two-limit-up study into two deterministic range reads."""
        if not isinstance(raw_plan, dict):
            return
        normalized_prompt = re.sub(r"\s+", "", prompt).lower()
        has_two_day_limit_signal = bool(
            re.search(
                r"(?:连续(?:2|两|二|两个)(?:天|日|个交易日)?涨停|"
                r"前(?:2|两|二)(?:天|日|个交易日)连续涨停|"
                r"(?:2|两|二)连板)",
                normalized_prompt,
            )
        )
        has_next_day_outcome = any(
            marker in normalized_prompt
            for marker in (
                "第三天",
                "第3天",
                "次日",
                "下一天",
                "后一天",
                "下一交易日",
            )
        )
        if not has_two_day_limit_signal or not has_next_day_outcome:
            return
        queries = raw_plan.get("queries")
        if not isinstance(queries, list):
            return
        limit_query = next(
            (
                query
                for query in queries
                if isinstance(query, dict)
                and query.get("operation") == "limit_list_d"
            ),
            None,
        )
        if limit_query is None:
            return
        params = limit_query.get("params")
        if not isinstance(params, dict):
            return
        start_date = params.get("start_date")
        end_date = params.get("end_date")
        if not isinstance(start_date, str) or not isinstance(end_date, str):
            return

        params["limit_type"] = "U"
        limit_query["fields"] = ["trade_date", "ts_code", "name"]
        limit_query["filters"] = []
        limit_query["aggregations"] = []
        daily_query = {
            "query_id": "two-limit-up-daily",
            "operation": "daily",
            "params": {"start_date": start_date, "end_date": end_date},
            "fields": ["trade_date", "ts_code", "pct_chg"],
            "purpose": (
                "Provide the trading-day sequence and third-day price changes."
            ),
            "filters": [],
            "aggregations": [],
        }
        raw_plan["feasibility"] = "supported"
        raw_plan["limitations"] = []
        raw_plan["result_transform"] = "two_limit_up_next_day_probability"
        raw_plan["queries"] = [limit_query, daily_query]
        for requirement in raw_plan.get("requirements", []):
            if isinstance(requirement, dict):
                requirement["status"] = "covered"
                requirement.setdefault(
                    "implementation",
                    "Join limit_list_d and daily rows by security and trading date.",
                )

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
    def _normalize_common_analytics(plan: QueryPlan, prompt: str) -> None:
        """Collapse common rankings and grouped counts into one bounded query."""
        normalized = prompt.replace(" ", "")
        limit_queries = [
            query for query in plan.queries if query.operation == "limit_list_d"
        ]
        requested_transform = None
        if "涨停" in normalized and "哪一天" in normalized:
            requested_transform = "top_count_by_trade_date"
        elif "涨停" in normalized and "每天的涨停数量" in normalized:
            requested_transform = "count_by_trade_date"
        elif "涨停" in normalized and (
            "次数最多" in normalized
            or "涨停最多的股票" in normalized
            or "出现过涨停" in normalized
        ):
            requested_transform = (
                "top_10_count_by_ts_code"
                if "十" in normalized
                else "count_by_ts_code"
            )
        elif "行业" in normalized and "涨停" in normalized:
            requested_transform = "count_by_industry"

        if requested_transform and limit_queries:
            start_dates = []
            end_dates = []
            for query in limit_queries:
                trade_date = query.params.get("trade_date")
                start_date = query.params.get("start_date")
                end_date = query.params.get("end_date")
                if isinstance(trade_date, str):
                    start_dates.append(trade_date)
                    end_dates.append(trade_date)
                if isinstance(start_date, str):
                    start_dates.append(start_date)
                if isinstance(end_date, str):
                    end_dates.append(end_date)
            query = limit_queries[0].model_copy(deep=True)
            if start_dates and end_dates:
                query.params = {
                    "start_date": min(start_dates),
                    "end_date": max(end_dates),
                    "limit_type": "U",
                }
            query.fields = ["trade_date", "ts_code", "name", "industry"]
            query.transform = requested_transform
            query.filters = []
            query.aggregations = []
            plan.queries = [query]

        if "成交额排名前20" in normalized:
            for query in plan.queries:
                query.operation = "daily"
                query.transform = "top_20_by_amount"
                query.fields = ["ts_code", "pct_chg", "amount"]
        if "换手率最高的20" in normalized:
            for query in plan.queries:
                if query.operation == "daily_basic":
                    query.transform = "top_20_by_turnover_rate"
                    for field in ("ts_code", "turnover_rate"):
                        if field not in query.fields:
                            query.fields.append(field)
        if "大宗交易" in normalized and "成交金额最多" in normalized:
            for query in plan.queries:
                if query.operation == "block_trade":
                    query.transform = "top_20_total_amount_by_ts_code"
                    for field in ("ts_code", "amount"):
                        if field not in query.fields:
                            query.fields.append(field)
        if "比较" in normalized and "最近一个月" in normalized and "涨幅" in normalized:
            for query in plan.queries:
                if query.operation == "daily":
                    query.transform = "period_return_by_ts_code"
                    for field in ("ts_code", "trade_date", "close"):
                        if field not in query.fields:
                            query.fields.append(field)
        if "十只" in normalized and "股息率" in normalized:
            valuation_queries = [
                query for query in plan.queries if query.operation == "daily_basic"
            ]
            if valuation_queries:
                query = valuation_queries[0].model_copy(deep=True)
                seen_fields = set(query.fields)
                seen_filters = {
                    (
                        row_filter.field,
                        row_filter.operator,
                        str(row_filter.value),
                    )
                    for row_filter in query.filters
                }
                for additional_query in valuation_queries[1:]:
                    for field in additional_query.fields:
                        if field not in seen_fields:
                            query.fields.append(field)
                            seen_fields.add(field)
                    for row_filter in additional_query.filters:
                        filter_key = (
                            row_filter.field,
                            row_filter.operator,
                            str(row_filter.value),
                        )
                        if filter_key not in seen_filters:
                            query.filters.append(row_filter)
                            seen_filters.add(filter_key)
                query.transform = "top_10_by_dv_ratio"
                if "dv_ratio" not in seen_fields:
                    query.fields.append("dv_ratio")
                plan.queries = [
                    item
                    for item in plan.queries
                    if item.operation != "daily_basic"
                ] + [query]
        for query in plan.queries:
            period_filters = [
                row_filter
                for row_filter in query.filters
                if row_filter.field == "period_return"
            ]
            if query.operation != "daily" or not period_filters:
                continue
            query.transform = "period_return_by_ts_code"
            query.fields = ["ts_code", "trade_date", "close"]
            for row_filter in period_filters:
                row_filter.field = "period_return_pct"
                if isinstance(row_filter.value, float) and abs(row_filter.value) <= 1:
                    row_filter.value *= 100

    @staticmethod
    def _normalize_market_period_returns(plan: QueryPlan, prompt: str) -> None:
        """Route broad period-return rankings through two market snapshots."""
        if plan.result_transform is not None:
            # A validated cross-query intent takes precedence over broad ranking
            # heuristics, which must not reinterpret numbers such as "前2天".
            return
        normalized = prompt.replace(" ", "")
        asks_for_return = any(
            marker in normalized
            for marker in ("涨幅", "跌幅", "收益率", "上涨", "下跌")
        )
        requested_limit = DeepSeekQueryPlanner._parse_requested_limit(normalized)
        asks_for_ranking = requested_limit is not None or any(
            marker in normalized
            for marker in ("最大", "最高", "最小", "最低", "排名", "最多")
        )
        if not (asks_for_return and asks_for_ranking):
            return

        candidates = [
            query
            for query in plan.queries
            if query.operation in {"daily", "weekly", "monthly"}
            and not query.params.get("ts_code")
        ]
        if not candidates:
            return
        query = candidates[0].model_copy(deep=True)
        start_date = query.params.get("start_date")
        end_date = query.params.get("end_date")
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        completed_date = now.date()
        if now.time() < ClockTime(17, 10):
            completed_date -= timedelta(days=1)
        while completed_date.weekday() >= 5:
            completed_date -= timedelta(days=1)
        month_match = re.search(r"(?:(\d{4})年)?(\d{1,2})月", normalized)
        year_match = re.search(r"(\d{4})年", normalized)
        if month_match:
            month = int(month_match.group(2))
            explicit_year = month_match.group(1)
            year = int(explicit_year or now.year)
            if explicit_year is None and month > now.month:
                year -= 1
            period_start = datetime(year, month, 1).date()
            if month == 12:
                next_month = datetime(year + 1, 1, 1).date()
            else:
                next_month = datetime(year, month + 1, 1).date()
            period_end = next_month - timedelta(days=1)
            start_date = period_start.strftime("%Y%m%d")
            end_date = min(period_end, completed_date).strftime("%Y%m%d")
        elif year_match:
            year = int(year_match.group(1))
            start_date = f"{year}0101"
            end_date = min(
                datetime(year, 12, 31).date(),
                completed_date,
            ).strftime("%Y%m%d")
        elif "今年" in normalized:
            start_date = f"{now.year}0101"
            end_date = completed_date.strftime("%Y%m%d")
        elif "去年" in normalized:
            start_date = f"{now.year - 1}0101"
            end_date = f"{now.year - 1}1231"
        if not isinstance(start_date, str) or not isinstance(end_date, str):
            return
        query.operation = "daily"
        query.params = {"start_date": start_date, "end_date": end_date}
        query.fields = ["ts_code", "trade_date", "close"]
        query.transform = "period_return_by_ts_code"
        query.aggregations = []
        if plan.result_pipeline:
            plan.result_pipeline.source_query_id = query.query_id
            has_limit = False
            for step in plan.result_pipeline.steps:
                if step.field in PERIOD_RETURN_FIELD_ALIASES:
                    step.field = PERIOD_RETURN_FIELD_ALIASES[step.field]
                step.fields = [
                    PERIOD_RETURN_FIELD_ALIASES.get(field, field)
                    for field in step.fields
                ]
                if step.order_by in PERIOD_RETURN_FIELD_ALIASES:
                    step.order_by = PERIOD_RETURN_FIELD_ALIASES[step.order_by]
                if step.operation == "sort" and step.field == "period_return_pct":
                    step.direction = (
                        "asc"
                        if any(
                            marker in normalized
                            for marker in (
                                "跌幅最大",
                                "下跌最多",
                                "最小",
                                "最低",
                            )
                        )
                        else "desc"
                    )
                if step.operation == "limit":
                    has_limit = True
                    if requested_limit is not None:
                        step.count = requested_limit
            if requested_limit is not None and not has_limit:
                plan.result_pipeline.steps.append(
                    ResultPipelineStep.model_validate(
                        {"operation": "limit", "count": requested_limit}
                    )
                )
        else:
            direction = (
                "asc"
                if any(
                    marker in normalized
                    for marker in ("跌幅最大", "下跌最多", "最小", "最低")
                )
                else "desc"
            )
            plan.result_pipeline = ResultPipeline.model_validate(
                {
                    "source_query_id": query.query_id,
                    "output_query_id": f"{query.query_id}-ranking",
                    "steps": [
                        {
                            "operation": "drop_missing",
                            "fields": ["period_return_pct"],
                        },
                        {
                            "operation": "sort",
                            "field": "period_return_pct",
                            "direction": direction,
                        },
                        {
                            "operation": "limit",
                            "count": requested_limit or 1,
                        },
                    ],
                }
            )
        plan.queries = [query]

    @staticmethod
    def _parse_requested_limit(prompt: str) -> Optional[int]:
        """Return an explicit Top-N limit from Arabic or common Chinese numerals."""
        match = re.search(
            r"(?<!之)前(\d{1,4}|[一二三四五六七八九十百两]+)",
            prompt,
        )
        if not match:
            match = re.search(r"top\s*(\d{1,4})", prompt, re.IGNORECASE)
        if not match:
            return None
        token = match.group(1)
        if token.isdigit():
            return min(int(token), 1_000)
        digits = {
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
        }
        if token == "十":
            return 10
        if token == "百":
            return 100
        if "十" in token:
            tens, ones = token.split("十", 1)
            return digits.get(tens, 1) * 10 + digits.get(ones, 0)
        return digits.get(token)

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

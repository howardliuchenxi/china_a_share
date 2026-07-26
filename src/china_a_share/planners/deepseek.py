"""DeepSeek implementation of the provider-neutral query-planner port."""

from datetime import datetime, time as ClockTime, timedelta
import json
import re
from time import sleep
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from pydantic import ValidationError
import requests

from china_a_share.core.contracts import (
    AnalysisRequest,
    DataQuery,
    DataOperation,
    QueryPlan,
    ResultPipeline,
)
from china_a_share.core.errors import PlannerError


DEEPSEEK_PLANNER_NAME = "deepseek"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_TIMEOUT_SECONDS = 60
DEEPSEEK_MAX_OUTPUT_TOKENS = 2_000
DEEPSEEK_MAX_ATTEMPTS = 2
DEEPSEEK_RETRY_DELAY_SECONDS = 1
TRANSIENT_ERROR_MARKERS = ("too busy", "temporarily", "timed out")
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
        request_payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.prompt},
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
            "stream": False,
        }
        for attempt in range(DEEPSEEK_MAX_ATTEMPTS):
            response = self._request_with_retry(request_payload)
            try:
                plan = self._decode_plan_response(response, request.prompt)
                break
            except PlannerError as exc:
                if (
                    str(exc)
                    == "DeepSeek returned a query plan that violates the contract."
                    and attempt + 1 < DEEPSEEK_MAX_ATTEMPTS
                ):
                    sleep(DEEPSEEK_RETRY_DELAY_SECONDS)
                    continue
                raise
        else:
            raise PlannerError(
                source=self.name,
                message="DeepSeek returned no usable query plan.",
            )

        self._normalize_fields(plan)
        self._normalize_limit_list_queries(plan)
        self._normalize_common_analytics(plan, request.prompt)
        self._normalize_market_period_returns(plan, request.prompt)
        self._normalize_latest_completed_date(plan, request.prompt)
        self._downgrade_unexecutable_plan(plan, request.prompt)
        self._split_multi_security_float_holder_queries(plan)
        return plan

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

    def _request_with_retry(self, request_payload: dict) -> Any:
        """Retry one transient planner failure without hiding a final error."""
        last_exception = None
        for attempt in range(DEEPSEEK_MAX_ATTEMPTS):
            try:
                response = self._session.post(
                    DEEPSEEK_API_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_payload,
                    timeout=DEEPSEEK_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                last_exception = exc
                if attempt + 1 < DEEPSEEK_MAX_ATTEMPTS:
                    sleep(DEEPSEEK_RETRY_DELAY_SECONDS)
                    continue
                raise PlannerError(source=self.name, message=str(exc)) from exc

            try:
                error = response.json().get("error") or {}
            except (AttributeError, ValueError):
                return response
            message = str(error.get("message") or "").lower()
            if (
                error
                and any(marker in message for marker in TRANSIENT_ERROR_MARKERS)
                and attempt + 1 < DEEPSEEK_MAX_ATTEMPTS
            ):
                sleep(DEEPSEEK_RETRY_DELAY_SECONDS)
                continue
            return response
        raise PlannerError(source=self.name, message=str(last_exception))

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
        normalized_prompt = prompt.replace(" ", "")
        if "连续两天涨停" not in normalized_prompt or "第三天" not in normalized_prompt:
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
        normalized = prompt.replace(" ", "")
        asks_for_return = any(
            marker in normalized for marker in ("涨幅", "跌幅", "收益率")
        )
        asks_for_ranking = any(
            marker in normalized
            for marker in ("最大", "最高", "最小", "最低", "排名", "前")
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
            year = int(month_match.group(1) or now.year)
            month = int(month_match.group(2))
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
        else:
            direction = (
                "asc"
                if any(marker in normalized for marker in ("跌幅最大", "最小", "最低"))
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
                        {"operation": "limit", "count": 1},
                    ],
                }
            )
        plan.queries = [query]

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

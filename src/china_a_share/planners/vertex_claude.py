"""Vertex AI Claude implementation of the provider-neutral query-planner port."""

from datetime import datetime
import json
from time import sleep
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from china_a_share.core.contracts import (
    AnalysisRequest,
    DataOperation,
    QueryPlan,
)
from china_a_share.core.errors import PlannerError
from china_a_share.planners.deepseek import DeepSeekQueryPlanner


VERTEX_PROJECT = "china-a-share-lab"
VERTEX_REGION = "asia-east2"
VERTEX_MODEL = "claude-sonnet-4@20250514"
VERTEX_API_URL = (
    f"https://{VERTEX_REGION}-aiplatform.googleapis.com/v1/projects/"
    f"{VERTEX_PROJECT}/locations/{VERTEX_REGION}/publishers/anthropic/models/"
    f"{VERTEX_MODEL}:rawPredict"
)
VERTEX_TIMEOUT_SECONDS = 60
VERTEX_MAX_OUTPUT_TOKENS = 4_000
VERTEX_MAX_ATTEMPTS = 2
VERTEX_RETRY_DELAY_SECONDS = 1


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
        except PlannerError:
            # Fall back to DeepSeek on any planner error
            return self._fallback.plan(request, candidate_operations)

    def generate_text(self, prompt: str) -> str:
        """Generate arbitrary text using the underlying LLM."""
        import google.auth
        import google.auth.transport.requests
        import requests as http_requests

        credentials, _ = google.auth.default()
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)
        access_token = credentials.token

        payload = {
            "anthropic_version": "vertex-2023-10-16",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": VERTEX_MAX_OUTPUT_TOKENS,
        }

        response = http_requests.post(
            VERTEX_API_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=VERTEX_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise PlannerError(
                source=self.name,
                message=f"Vertex AI returned HTTP {response.status_code}",
                http_status=response.status_code,
                raw_response={"text": response.text},
            )
        data = response.json()
        return data["content"][0]["text"]

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
        system_prompt = _build_system_prompt(guidance, allowed_operations)

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

                # Delegate normalization to the DeepSeek planner
                plan = self._fallback.normalize_and_validate_plan(
                    content, request.prompt
                )
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


def _build_system_prompt(guidance: str, allowed_operations: str) -> str:
    """Build the same system prompt used by the DeepSeek planner."""
    from china_a_share.planners.deepseek import DeepSeekQueryPlanner

    # Reuse the system prompt building logic from DeepSeek planner
    return (
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
        "Use top_10_by_dv_ratio after valuation filters when the user asks for "
        "ten high-dividend securities. "
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
        f"You must respond with ONLY valid JSON. No other text.\n"
        f"JSON schema:\n{json.dumps(QueryPlan.model_json_schema())}"
    )

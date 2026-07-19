"""DeepSeek-backed natural-language query planner."""

from datetime import datetime
import json
from typing import Any, Dict, Optional, Sequence
from zoneinfo import ZoneInfo

from pydantic import ValidationError
import requests

from .contracts import AnalysisRequest, QueryPlan


DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_TIMEOUT_SECONDS = 60
DEEPSEEK_MAX_OUTPUT_TOKENS = 2_000

CORE_API_GUIDANCE = {
    "daily": (
        "Unadjusted A-share daily prices. Use trade_date=YYYYMMDD for the full "
        "market on one date, or ts_code with start_date and end_date. Common "
        "fields: ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount."
    ),
    "daily_basic": (
        "Daily valuation and trading metrics. Parameters include trade_date, "
        "ts_code, start_date, and end_date."
    ),
    "stock_basic": (
        "A-share security master. Parameters include ts_code, exchange, market, "
        "and list_status."
    ),
    "income": "Listed-company income statements by ts_code or reporting dates.",
    "balancesheet": "Listed-company balance sheets by ts_code or reporting dates.",
    "cashflow": "Listed-company cash-flow statements by ts_code or reporting dates.",
    "fina_indicator": "Listed-company financial indicators by security and period.",
    "moneyflow": "A-share security-level daily fund-flow data.",
}


class DeepSeekApiError(RuntimeError):
    """DeepSeek failure containing safe upstream response details."""

    def __init__(
        self,
        message: str,
        code: Optional[Any] = None,
        http_status: Optional[int] = None,
        raw_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.raw_response = raw_response


class DeepSeekQueryPlanner:
    """Convert a natural-language request into a structured query plan."""

    def __init__(
        self,
        api_key: str,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key
        self.session = session if session is not None else requests.Session()

    def plan(
        self,
        request: AnalysisRequest,
        candidate_api_names: Sequence[str],
    ) -> QueryPlan:
        """Build a query plan from the request and relevant API candidates."""
        guidance = "\n".join(
            f"- {name}: {description}"
            for name, description in CORE_API_GUIDANCE.items()
            if name in candidate_api_names
        )
        system_prompt = (
            "You plan read-only Tushare queries for mainland China A-share data. "
            "Return one valid JSON object matching the supplied schema. Never use an "
            "API outside the allowlist. Resolve relative or partial dates using the "
            "current date "
            f"{datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()} and "
            "Asia/Shanghai semantics. "
            "Use YYYYMMDD for Tushare dates. Security codes must end in .SH, .SZ, or "
            ".BJ. For requests that count advancing or declining stocks, query daily "
            "for the full market by trade_date, include change in fields, and add local "
            "conditional counts for change gt 0, lt 0, and eq 0. Do not invent data.\n\n"
            f"Core API guidance:\n{guidance}\n\n"
            f"Allowed API names:\n{','.join(candidate_api_names)}\n\n"
            f"JSON schema:\n{json.dumps(QueryPlan.model_json_schema())}"
        )
        try:
            response = self.session.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": request.prompt},
                    ],
                    "thinking": {"type": "disabled"},
                    "response_format": {"type": "json_object"},
                    "max_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
                    "stream": False,
                },
                timeout=DEEPSEEK_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise DeepSeekApiError(message=str(exc)) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise DeepSeekApiError(
                message="DeepSeek returned a non-JSON response.",
                http_status=response.status_code,
                raw_response={"text": response.text},
            ) from exc

        if response.status_code >= 400 or payload.get("error"):
            upstream_error = payload.get("error") or {}
            raise DeepSeekApiError(
                message=str(upstream_error.get("message") or "DeepSeek request failed."),
                code=upstream_error.get("code"),
                http_status=response.status_code,
                raw_response=payload,
            )

        content = ((payload.get("choices") or [{}])[0].get("message") or {}).get(
            "content", ""
        )
        if not content:
            raise DeepSeekApiError(
                message="DeepSeek returned an empty query plan.",
                http_status=response.status_code,
                raw_response=payload,
            )
        try:
            plan = QueryPlan.model_validate_json(content)
        except ValidationError as exc:
            raise DeepSeekApiError(
                message="DeepSeek returned a query plan that violates the contract.",
                http_status=response.status_code,
                raw_response={"content": content},
            ) from exc
        self._normalize_fields(plan)
        return plan

    def _normalize_fields(self, plan: QueryPlan) -> None:
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

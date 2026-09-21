"""Eastmoney broker research-report provider through the neutral data port."""

from datetime import datetime, timedelta
import logging
import time
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from china_a_share.capabilities import get_operation_capability, resolve_query_shape
from china_a_share.core.contracts import DataOperation
from china_a_share.core.errors import DataProviderError
from china_a_share.core.ports import DataResponseCache


EASTMONEY_PROVIDER_NAME = "eastmoney"
EASTMONEY_REPORT_LIST_URL = "https://reportapi.eastmoney.com/report/list"
EASTMONEY_REQUEST_TIMEOUT_SECONDS = 20
EASTMONEY_MAX_ATTEMPTS = 3
EASTMONEY_RETRY_DELAY_SECONDS = 1
# One call returns the newest reports first; the page is deliberately bounded so
# agents filter windows locally on the retained dataset instead of paginating.
EASTMONEY_BROKER_REPORTS_PAGE_SIZE = 100
EASTMONEY_REPORTS_CACHE_TTL = timedelta(hours=24)
# The upstream endpoint REQUIRES beginTime/endTime; without an explicit window
# the provider falls back to the most recent three years of publications.
EASTMONEY_DEFAULT_REPORT_WINDOW_DAYS = 3 * 365
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")

EASTMONEY_OPERATION_GUIDANCE: Dict[str, str] = {
    "broker_reports": (
        "Broker research reports (券商研报) for A-share securities — the operation "
        "behind questions about analyst views, broker ratings (评级), target prices "
        "(目标价), or broker earnings forecasts (盈利预测). Parameters: ts_code for "
        "one security, optionally with start_date and end_date (together) bounding "
        "the publication window in YYYYMMDD; without a window the provider returns "
        "the most recent three years of publications. Rows come newest first. Common fields "
        "include ts_code, stock_name, report_date, report_title, org_name (broker), "
        "rating (broker rating text such as 买入 or 增持), max_price and min_price "
        "(target-price bounds, frequently empty), this_year_eps, next_year_eps, "
        "year_after_next_eps, and researcher. Unit note: max_price and min_price "
        "are CNY per share (元), broker target-price bounds; this_year_eps, "
        "next_year_eps, and year_after_next_eps are CNY per share (元), broker "
        "EPS forecasts. One call returns at most the newest 100 reports: issue ONE "
        "query per security, then filter report_date windows and deduplicate "
        "broker titles locally instead of issuing repeated provider calls. "
        "Research reports are analyst opinions rather than company disclosures: "
        "distinguish them from forecast/express guidance when attributing a "
        "statement, and report the broker and report date of every cited view."
    ),
}

# Upstream report-list field mapped onto the provider-facing column name.
BROKER_REPORT_FIELD_MAP: Dict[str, str] = {
    "stockCode": "ts_code",
    "stockName": "stock_name",
    "publishDate": "report_date",
    "title": "report_title",
    "orgSName": "org_name",
    "emRatingName": "rating",
    "indvAimPriceT": "max_price",
    "indvAimPriceL": "min_price",
    "predictThisYearEps": "this_year_eps",
    "predictNextYearEps": "next_year_eps",
    "predictNextTwoYearEps": "year_after_next_eps",
    "researcher": "researcher",
}
BROKER_REPORT_NUMERIC_FIELDS = frozenset(
    {
        "max_price",
        "min_price",
        "this_year_eps",
        "next_year_eps",
        "year_after_next_eps",
    }
)

logger = logging.getLogger(__name__)


def _normalize_report_value(column: str, value: Any, ts_code: str) -> Any:
    """Normalize one upstream cell into the provider's stable contract."""
    if value is None:
        return None
    if column == "ts_code":
        return ts_code
    text = str(value).strip()
    if not text:
        return None
    if column == "report_date":
        digits = text[:10].replace("-", "")
        return digits if len(digits) == 8 and digits.isdigit() else None
    if column in BROKER_REPORT_NUMERIC_FIELDS:
        try:
            return float(text)
        except ValueError:
            return None
    return text


class EastmoneyCacheExpirationPolicy:
    """Cache broker-report datasets with a fixed research-refresh horizon."""

    def resolve(
        self,
        operation: str,
        params: Dict[str, Any],
        fetched_at: datetime,
    ) -> Optional[datetime]:
        """Return a bounded expiration for every supported operation."""
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        if operation not in EASTMONEY_OPERATION_GUIDANCE:
            raise ValueError(
                f"Eastmoney operation has no cache profile: {operation}"
            )
        return fetched_at + EASTMONEY_REPORTS_CACHE_TTL


class EastmoneyDataProvider:
    """Expose the Eastmoney broker-report catalog through the provider port."""

    def __init__(
        self,
        response_cache: DataResponseCache,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        """Store cache access and an injectable HTTP transport."""
        self._response_cache = response_cache
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://data.eastmoney.com/",
            }
        )

    @property
    def name(self) -> str:
        """Return the stable provider identifier used in results and cache keys."""
        return EASTMONEY_PROVIDER_NAME

    @property
    def operation_names(self) -> Sequence[str]:
        """Return every Eastmoney operation connected through this provider."""
        return tuple(EASTMONEY_OPERATION_GUIDANCE)

    def search_operations(self, prompt: str) -> Sequence[DataOperation]:
        """Return connected operations with complete planner guidance."""
        if not prompt.strip():
            return ()
        return tuple(
            DataOperation(name=name, description=description)
            for name, description in EASTMONEY_OPERATION_GUIDANCE.items()
        )

    def supports(self, operation: str) -> bool:
        """Return whether the Eastmoney catalog contains the operation."""
        return operation in EASTMONEY_OPERATION_GUIDANCE

    def describe_query_shapes(self, operation: str) -> Sequence[Dict[str, Any]]:
        """Return audited request shapes available to autonomous agents."""
        capability = get_operation_capability(operation)
        if capability is None:
            return ()
        return tuple(
            {
                "shape_id": shape.shape_id,
                "required_params": list(shape.required_params),
            }
            for shape in capability.query_shapes
        )

    def validate_query(
        self,
        operation: str,
        params: Dict[str, Any],
        fields: Sequence[str],
    ) -> None:
        """Require one audited request shape before agent execution."""
        if not self.supports(operation):
            raise ValueError(f"Unsupported Eastmoney operation: {operation}")
        if resolve_query_shape(operation, params) is None:
            raise ValueError(
                f"Operation lacks an audited Eastmoney query shape: {operation}"
            )

    def describe_result_completeness(
        self,
        operation: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Describe completeness guaranteed by one successful audited request."""
        shape = resolve_query_shape(operation, params)
        if shape is None or shape.execution_strategy != "provider_query":
            return {
                "completeness": "unknown",
                "completeness_evidence": [],
            }
        return {
            "completeness": "complete",
            "completeness_evidence": [
                f"query_shape={shape.shape_id}",
                f"execution_strategy={shape.execution_strategy}",
                f"completeness_policy={shape.completeness_policy}",
            ],
        }

    def query(
        self,
        operation: str,
        params: Dict[str, Any],
        fields: Sequence[str],
        *,
        api_route: str,
        request_id: str,
        query_id: str,
    ) -> pd.DataFrame:
        """Execute one cached Eastmoney read and return a normalized table."""
        if not self.supports(operation):
            raise ValueError(f"Unsupported Eastmoney operation: {operation}")
        return self._response_cache.get_or_fetch(
            self.name,
            operation,
            dict(params),
            list(fields or BROKER_REPORT_FIELD_MAP.values()),
            lambda: self._fetch(operation, params),
            api_route=api_route,
            request_id=request_id,
            query_id=query_id,
        )

    def _fetch(self, operation: str, params: Dict[str, Any]) -> pd.DataFrame:
        """Fetch every mapped row for one audited broker-report request."""
        if operation != "broker_reports":
            raise ValueError(f"Unsupported Eastmoney operation: {operation}")
        ts_code = str(params["ts_code"]).strip()
        request_params = {
            "pageSize": EASTMONEY_BROKER_REPORTS_PAGE_SIZE,
            "pageNo": 1,
            "qType": 0,
            "code": ts_code.split(".")[0],
        }
        if params.get("start_date") and params.get("end_date"):
            request_params["beginTime"] = _iso_date(params["start_date"])
            request_params["endTime"] = _iso_date(params["end_date"])
        else:
            today = datetime.now(BEIJING_TIMEZONE).date()
            request_params["endTime"] = today.isoformat()
            request_params["beginTime"] = (
                today - timedelta(days=EASTMONEY_DEFAULT_REPORT_WINDOW_DAYS)
            ).isoformat()
        payload = self._request_json(request_params)
        rows: List[Dict[str, Any]] = []
        for record in payload.get("data") or []:
            if not isinstance(record, dict):
                continue
            rows.append(
                {
                    column: _normalize_report_value(
                        column, record.get(upstream), ts_code
                    )
                    for upstream, column in BROKER_REPORT_FIELD_MAP.items()
                }
            )
        columns = list(BROKER_REPORT_FIELD_MAP.values())
        if not rows:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(rows, columns=columns)

    def _request_json(self, request_params: Dict[str, Any]) -> Dict[str, Any]:
        """Retry transient transport failures and preserve upstream errors."""
        response = None
        for attempt in range(EASTMONEY_MAX_ATTEMPTS):
            try:
                response = self._session.get(
                    EASTMONEY_REPORT_LIST_URL,
                    params=request_params,
                    timeout=EASTMONEY_REQUEST_TIMEOUT_SECONDS,
                )
                break
            except requests.RequestException as exc:
                logger.warning(
                    "eastmoney_request_failed attempt=%s max_attempts=%s error=%s",
                    attempt + 1,
                    EASTMONEY_MAX_ATTEMPTS,
                    exc,
                )
                if attempt + 1 == EASTMONEY_MAX_ATTEMPTS:
                    raise DataProviderError(
                        source=EASTMONEY_PROVIDER_NAME,
                        message=str(exc),
                    ) from exc
                time.sleep(EASTMONEY_RETRY_DELAY_SECONDS)
        if response is None:
            raise RuntimeError(
                "Eastmoney request loop completed without a response."
            )
        if response.status_code >= 400:
            raise DataProviderError(
                source=EASTMONEY_PROVIDER_NAME,
                message=(
                    f"Eastmoney report request failed with HTTP "
                    f"{response.status_code}."
                ),
                http_status=response.status_code,
                raw_response={"text": response.text[:300]},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DataProviderError(
                source=EASTMONEY_PROVIDER_NAME,
                message="Eastmoney returned a non-JSON response.",
                http_status=response.status_code,
                raw_response={"text": response.text[:300]},
            ) from exc
        if not isinstance(payload, dict):
            raise DataProviderError(
                source=EASTMONEY_PROVIDER_NAME,
                message="Eastmoney returned an unexpected report payload.",
            )
        return payload


def _iso_date(compact: Any) -> str:
    """Convert one YYYYMMDD parameter into the upstream YYYY-MM-DD form."""
    digits = str(compact).strip().replace("-", "")[:8]
    if len(digits) != 8 or not digits.isdigit():
        raise ValueError(f"Invalid Eastmoney date parameter: {compact}")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"

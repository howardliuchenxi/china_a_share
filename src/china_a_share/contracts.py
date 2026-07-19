"""Public request and response contracts for the analysis API."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


MAX_ANALYSIS_PROMPT_LENGTH = 4_000
CACHE_SCHEMA_VERSION = 1


class AnalysisStatus(str, Enum):
    """Overall completion state of an analysis request."""

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    ERROR = "error"


class QueryStatus(str, Enum):
    """Execution state of one Tushare query."""

    SUCCESS = "success"
    ERROR = "error"


class TushareCacheRecord(BaseModel):
    """Serializable successful Tushare response stored in persistent cache."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[CACHE_SCHEMA_VERSION] = Field(
        default=CACHE_SCHEMA_VERSION,
        description="Cache payload version included in compatibility checks and cache keys.",
    )
    api_name: str = Field(
        min_length=1,
        description="Tushare API name that produced the cached response.",
    )
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Normalized Tushare request parameters used to identify the response.",
    )
    fields: List[str] = Field(
        default_factory=list,
        description="Ordered output fields requested from Tushare.",
    )
    fetched_at: AwareDatetime = Field(
        description="Timezone-aware instant when Tushare returned the response.",
    )
    expires_at: AwareDatetime = Field(
        description="Timezone-aware instant after which the response must not be served.",
    )
    columns: List[str] = Field(
        default_factory=list,
        description="Ordered table columns returned by Tushare.",
    )
    rows: List[List[Any]] = Field(
        default_factory=list,
        description="JSON-compatible table rows aligned with the ordered columns.",
    )

    @model_validator(mode="after")
    def validate_expiration(self) -> "TushareCacheRecord":
        """Reject records that are already invalid at their fetch instant."""
        if self.expires_at <= self.fetched_at:
            raise ValueError("expires_at must be later than fetched_at")
        return self


class ConditionalCount(BaseModel):
    """Controlled count over a numeric column in one Tushare result."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        min_length=1,
        description="Human-readable label displayed with the computed count.",
    )
    field: str = Field(
        min_length=1,
        description="Numeric result column evaluated by the condition.",
    )
    operator: Literal["gt", "ge", "eq", "le", "lt"] = Field(
        description="Comparison operator applied to the numeric result column.",
    )
    value: float = Field(
        description="Numeric threshold used by the comparison operator.",
    )


class AnalysisRequest(BaseModel):
    """Natural-language request submitted by the local web client."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(
        min_length=1,
        max_length=MAX_ANALYSIS_PROMPT_LENGTH,
        description="Natural-language description of the requested A-share data.",
    )


class TushareQuery(BaseModel):
    """One validated Tushare API call selected by the query planner."""

    model_config = ConfigDict(extra="forbid")

    query_id: str = Field(
        min_length=1,
        description="Request-local identifier used to match a result to this query.",
    )
    api_name: str = Field(
        min_length=1,
        description="Allowlisted Tushare stock API name.",
    )
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Validated keyword arguments passed to the Tushare API.",
    )
    fields: List[str] = Field(
        default_factory=list,
        description="Requested output fields; an empty list uses the API defaults.",
    )
    purpose: str = Field(
        min_length=1,
        description="Short explanation of why this query is required.",
    )
    aggregations: List[ConditionalCount] = Field(
        default_factory=list,
        description="Optional local conditional counts computed without another model call.",
    )


class QueryPlan(BaseModel):
    """Structured A-share retrieval plan produced from a user prompt."""

    model_config = ConfigDict(extra="forbid")

    market: Literal["A_SHARE"] = Field(
        default="A_SHARE",
        description="Fixed market boundary enforced for every analysis request.",
    )
    interpretation: str = Field(
        min_length=1,
        description="Concise interpretation of the user's data request.",
    )
    queries: List[TushareQuery] = Field(
        min_length=1,
        description="Ordered Tushare calls required to satisfy the request.",
    )


class ServiceError(BaseModel):
    """Error information safe to display in the local web client."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["tushare", "deepseek", "system"] = Field(
        description="Service or application layer that produced the error.",
    )
    code: Optional[Union[int, str]] = Field(
        default=None,
        description="Original upstream error code when one is available.",
    )
    message: str = Field(
        min_length=1,
        description="Original upstream message or a safe system error message.",
    )
    http_status: Optional[int] = Field(
        default=None,
        ge=100,
        le=599,
        description="HTTP response status returned by an upstream service.",
    )
    raw_response: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Original upstream error body after credentials, headers, and local "
            "stack details have been removed."
        ),
    )


class QueryResult(BaseModel):
    """Tabular result or upstream failure for one Tushare query."""

    model_config = ConfigDict(extra="forbid")

    query_id: str = Field(
        min_length=1,
        description="Identifier of the Tushare query that produced this result.",
    )
    api_name: str = Field(
        min_length=1,
        description="Tushare API name used for this result.",
    )
    status: QueryStatus = Field(
        description="Whether this individual query succeeded or failed.",
    )
    columns: List[str] = Field(
        default_factory=list,
        description="Ordered table column names returned by Tushare.",
    )
    rows: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="JSON-compatible table rows returned by Tushare.",
    )
    row_count: int = Field(
        default=0,
        ge=0,
        description="Number of rows returned for this query.",
    )
    summary: Dict[str, int] = Field(
        default_factory=dict,
        description="Local conditional counts requested by the validated query plan.",
    )
    error: Optional[ServiceError] = Field(
        default=None,
        description="Upstream error details when this query failed.",
    )


class AnalysisResponse(BaseModel):
    """Complete response returned to the local web client."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=1,
        description="Identifier used to correlate client requests and server logs.",
    )
    status: AnalysisStatus = Field(
        description="Overall request status across planning and query execution.",
    )
    plan: Optional[QueryPlan] = Field(
        default=None,
        description="Validated query plan when planning completed successfully.",
    )
    results: List[QueryResult] = Field(
        default_factory=list,
        description="Ordered results corresponding to the plan queries.",
    )
    error: Optional[ServiceError] = Field(
        default=None,
        description="Planning or system error when no query-level result applies.",
    )

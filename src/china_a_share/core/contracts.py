"""Provider-neutral request, plan, result, and cache contracts."""

import base64
import binascii
from datetime import date as CalendarDate, datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


MAX_ANALYSIS_PROMPT_LENGTH = 4_000
MAX_ANALYSIS_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ANALYSIS_IMAGE_BASE64_LENGTH = 4 * ((MAX_ANALYSIS_IMAGE_BYTES + 2) // 3)
DATA_CACHE_SCHEMA_VERSION = 4
PROVIDER_NAME_PATTERN = r"^[a-z][a-z0-9_-]*$"
OPERATION_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class AnalysisStatus(str, Enum):
    """Overall completion state of an analysis request."""

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    ERROR = "error"


class AnalysisTaskStatus(str, Enum):
    """Persisted lifecycle state for one asynchronous analysis."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class UiFeedbackStatus(str, Enum):
    """Lifecycle state exposed for one administrator UI improvement request."""

    SUBMITTED = "submitted"
    DISPATCH_FAILED = "dispatch_failed"


class QueryStatus(str, Enum):
    """Execution state of one market-data query."""

    SUCCESS = "success"
    ERROR = "error"


class AnalysisImage(BaseModel):
    """One screenshot supplied as context for an analysis request."""

    model_config = ConfigDict(extra="forbid")

    media_type: Literal["image/png", "image/jpeg", "image/webp"] = Field(
        description="Validated MIME type used to reconstruct the screenshot data URL.",
    )
    base64_data: str = Field(
        min_length=1,
        max_length=MAX_ANALYSIS_IMAGE_BASE64_LENGTH,
        description=(
            "Base64-encoded screenshot bytes, limited to 10 MiB before encoding."
        ),
    )

    @field_validator("base64_data")
    @classmethod
    def validate_base64_data(cls, value: str) -> str:
        """Reject malformed or oversized decoded screenshot payloads."""
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Screenshot data must be valid Base64.") from exc
        if len(decoded) > MAX_ANALYSIS_IMAGE_BYTES:
            raise ValueError("Screenshot data may not exceed 10 MiB.")
        return value


class AnalysisRequest(BaseModel):
    """Natural-language request submitted by the web client."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(
        min_length=1,
        max_length=MAX_ANALYSIS_PROMPT_LENGTH,
        description="Natural-language description of the requested A-share data.",
    )
    image: Optional[AnalysisImage] = Field(
        default=None,
        description="Optional screenshot interpreted before the query plan is generated.",
    )


class DataOperation(BaseModel):
    """One provider-native read operation available to a query planner."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        pattern=OPERATION_NAME_PATTERN,
        description="Provider-native operation name used to retrieve market data.",
    )
    description: str = Field(
        min_length=1,
        description=(
            "Planner guidance covering the operation purpose, important parameters, "
            "and relevant output fields."
        ),
    )


class ConditionalCount(BaseModel):
    """Controlled count over a numeric column in one query result."""

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


class DataFilter(BaseModel):
    """One deterministic scalar filter applied to provider result rows."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(
        min_length=1,
        description="Numeric result column evaluated by the filter.",
    )
    operator: Literal["gt", "ge", "eq", "le", "lt", "in"] = Field(
        description="Comparison operator applied to the numeric result column.",
    )
    value: Union[float, str, List[str]] = Field(
        description=(
            "Numeric threshold, exact string, or string set for membership filtering."
        ),
    )

    @model_validator(mode="after")
    def validate_string_operator(self) -> "DataFilter":
        """Keep text and membership predicates explicit and type-safe."""
        if isinstance(self.value, str) and self.operator != "eq":
            raise ValueError("string filter values require the eq operator")
        if isinstance(self.value, list):
            if self.operator != "in":
                raise ValueError("list filter values require the in operator")
            if not self.value:
                raise ValueError("membership filter values must not be empty")
        if self.operator == "in" and not isinstance(self.value, list):
            raise ValueError("the in operator requires a list value")
        return self


class DataQuery(BaseModel):
    """One provider-native read operation selected by a query planner."""

    model_config = ConfigDict(extra="forbid")

    query_id: str = Field(
        min_length=1,
        description="Request-local identifier used to match a result to this query.",
    )
    operation: str = Field(
        min_length=1,
        pattern=OPERATION_NAME_PATTERN,
        description="Provider-native operation selected from the active catalog.",
    )
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Validated keyword arguments passed to the active data provider.",
    )
    fields: List[str] = Field(
        default_factory=list,
        description="Requested output fields; an empty list uses provider defaults.",
    )
    purpose: str = Field(
        min_length=1,
        description="Short explanation of why this query is required.",
    )
    transform: Optional[
        Literal[
            "cr10_float_trend",
            "period_return_by_ts_code",
        ]
    ] = Field(
        default=None,
        description=(
            "Optional deterministic transformation applied to one provider result; "
            "Supported transforms provide audited concentration calculations, grouped "
            "audited concentration calculations or period returns."
        ),
    )
    filters: List[DataFilter] = Field(
        default_factory=list,
        description=(
            "Deterministic local row filters applied after provider retrieval; "
            "missing or nonnumeric values do not match numeric filters."
        ),
    )
    aggregations: List[ConditionalCount] = Field(
        default_factory=list,
        description="Optional local conditional counts computed without another model call.",
    )


class RequirementCoverage(BaseModel):
    """Auditable mapping from one user requirement to its implementation."""

    model_config = ConfigDict(extra="forbid")

    requirement: str = Field(
        min_length=1,
        description="Atomic user requirement extracted from the natural-language request.",
    )
    status: Literal["covered", "unsupported"] = Field(
        description="Whether available provider data and local operations satisfy it.",
    )
    implementation: Optional[str] = Field(
        default=None,
        description="Provider or deterministic local operation used to satisfy it.",
    )
    evidence: str = Field(
        min_length=1,
        description="Concrete capability evidence supporting the coverage decision.",
    )


class ResultAggregation(BaseModel):
    """One allowlisted grouped aggregation in a deterministic result pipeline."""

    model_config = ConfigDict(extra="forbid")

    output_field: str = Field(
        min_length=1,
        pattern=OPERATION_NAME_PATTERN,
        description="Output column receiving the aggregated value.",
    )
    label: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=80,
        description="Optional reader-facing label for a summary metric.",
    )
    field: str = Field(
        min_length=1,
        pattern=OPERATION_NAME_PATTERN,
        description="Existing input column aggregated within each group.",
    )
    function: Literal["count", "sum", "mean", "min", "max"] = Field(
        description="Allowlisted deterministic aggregation function.",
    )


class ResultPipelineStep(BaseModel):
    """One validated relational operation applied to a query result."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "latest_by_group",
        "derive",
        "drop_missing",
        "filter",
        "sort",
        "limit",
        "quantile_filter",
        "aggregate",
        "rolling_mean",
        "rolling_sum",
        "shift",
        "match_source",
        "compare_fields",
        "compare_scalar",
        "summarize",
    ] = Field(description="Allowlisted relational operation executed by the backend.")
    field: Optional[str] = Field(
        default=None,
        pattern=OPERATION_NAME_PATTERN,
        description="Primary input field used by this operation.",
    )
    output_field: Optional[str] = Field(
        default=None,
        pattern=OPERATION_NAME_PATTERN,
        description="New field produced by a derive operation.",
    )
    right_field: Optional[str] = Field(
        default=None,
        pattern=OPERATION_NAME_PATTERN,
        description="Right-hand input field used by a field comparison.",
    )
    right_source_query_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Planned query matched against the current pipeline frame.",
    )
    join_on: List[str] = Field(
        default_factory=list,
        description="Shared key fields used to match another planned query.",
    )
    fields: List[str] = Field(
        default_factory=list,
        description="Input fields required to be non-null.",
    )
    group_by: List[str] = Field(
        default_factory=list,
        description="Columns defining independent groups.",
    )
    order_by: Optional[str] = Field(
        default=None,
        pattern=OPERATION_NAME_PATTERN,
        description="Column used to select the latest row in each group.",
    )
    direction: Literal["asc", "desc"] = Field(
        default="asc",
        description="Stable ordering direction for latest selection or sorting.",
    )
    arithmetic_operator: Optional[
        Literal["add", "subtract", "multiply", "divide", "constant_minus"]
    ] = Field(
        default=None,
        description="Allowlisted scalar arithmetic applied by a derive operation.",
    )
    comparison: Optional[Literal["gt", "ge", "eq", "le", "lt"]] = Field(
        default=None,
        description="Comparison used by filter and quantile-filter operations.",
    )
    value: Optional[Union[int, float, str]] = Field(
        default=None,
        description="Bounded scalar used by arithmetic or row filtering.",
    )
    count: Optional[int] = Field(
        default=None,
        ge=1,
        le=1_000,
        description="Maximum rows retained by a limit operation.",
    )
    quantile: Optional[float] = Field(
        default=None,
        ge=0,
        le=1,
        description="Quantile threshold between zero and one.",
    )
    window: Optional[int] = Field(
        default=None,
        ge=2,
        le=250,
        description="Bounded trailing row window used by a rolling operation.",
    )
    min_periods: Optional[int] = Field(
        default=None,
        ge=1,
        le=250,
        description="Minimum observations required for a rolling result.",
    )
    periods: Optional[int] = Field(
        default=None,
        ge=-250,
        le=250,
        description="Signed row offset used within an ordered group.",
    )
    require_consecutive: bool = Field(
        default=False,
        description=(
            "Whether shifted rows must be adjacent in the global order sequence."
        ),
    )
    aggregations: List[ResultAggregation] = Field(
        default_factory=list,
        description="Named grouped aggregations executed in one aggregate step.",
    )

    @model_validator(mode="after")
    def validate_operation_arguments(self) -> "ResultPipelineStep":
        """Require the arguments needed by the selected allowlisted operation."""
        required = {
            "latest_by_group": bool(self.group_by and self.order_by),
            "derive": bool(
                self.field
                and self.output_field
                and self.arithmetic_operator
                and self.value is not None
            ),
            "drop_missing": bool(self.fields),
            "filter": bool(
                self.field and self.comparison and self.value is not None
            ),
            "sort": bool(self.field),
            "limit": self.count is not None,
            "quantile_filter": bool(
                self.field
                and self.comparison
                and self.quantile is not None
            ),
            "aggregate": bool(self.group_by and self.aggregations),
            "rolling_mean": bool(
                self.field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.window
            ),
            "rolling_sum": bool(
                self.field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.window
            ),
            "shift": bool(
                self.field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.periods
            ),
            "match_source": bool(
                self.right_source_query_id
                and self.join_on
                and self.output_field
            ),
            "compare_fields": bool(
                self.field
                and self.right_field
                and self.output_field
                and self.comparison
            ),
            "compare_scalar": bool(
                self.field
                and self.output_field
                and self.comparison
                and self.value is not None
            ),
            "summarize": bool(self.aggregations),
        }
        if not required[self.operation]:
            raise ValueError(f"Missing required arguments for {self.operation}")
        if self.operation == "derive" and (
            isinstance(self.value, bool)
            or not isinstance(self.value, (int, float))
        ):
            raise ValueError("derive requires a numeric scalar value")
        if (
            self.operation in {"rolling_mean", "rolling_sum"}
            and self.min_periods is not None
            and self.min_periods > self.window
        ):
            raise ValueError("min_periods cannot exceed the rolling window")
        return self


class ResultPipeline(BaseModel):
    """Linear deterministic plan applied to one normalized query result."""

    model_config = ConfigDict(extra="forbid")

    source_query_id: str = Field(
        min_length=1,
        description="Query result consumed as the pipeline input.",
    )
    output_query_id: str = Field(
        min_length=1,
        description="Stable identifier assigned to the transformed result.",
    )
    steps: List[ResultPipelineStep] = Field(
        min_length=1,
        max_length=12,
        description="Ordered allowlisted relational operations.",
    )


class QueryPlan(BaseModel):
    """Structured A-share retrieval plan produced from one user request."""

    model_config = ConfigDict(extra="forbid")

    market: Literal["A_SHARE"] = Field(
        default="A_SHARE",
        description="Fixed market boundary enforced for every analysis request.",
    )
    interpretation: str = Field(
        min_length=1,
        description="Concise interpretation of the user's data request.",
    )
    feasibility: Literal["supported", "unsupported"] = Field(
        default="supported",
        description="Whether the complete request can be fulfilled without guessing.",
    )
    requirements: List[RequirementCoverage] = Field(
        default_factory=list,
        description="Coverage evidence for each atomic user requirement.",
    )
    limitations: List[str] = Field(
        default_factory=list,
        description="Concrete missing capabilities that prevent faithful execution.",
    )
    result_pipeline: Optional[ResultPipeline] = Field(
        default=None,
        description="Optional deterministic relational pipeline over one query result.",
    )
    queries: List[DataQuery] = Field(
        default_factory=list,
        description="Ordered provider-native reads required to satisfy the request.",
    )

    @model_validator(mode="after")
    def validate_feasibility(self) -> "QueryPlan":
        """Reject executable unsupported plans and empty supported plans."""
        if self.feasibility == "supported" and not self.queries:
            raise ValueError("supported plans must contain at least one query")
        if self.feasibility == "unsupported":
            if self.queries:
                raise ValueError("unsupported plans must not contain queries")
            if not self.limitations:
                raise ValueError("unsupported plans must explain their limitations")
        return self


class DecisionTraceStep(BaseModel):
    """One structured and displayable decision in the analysis workflow."""

    model_config = ConfigDict(extra="forbid")

    stage: Literal[
        "requirements", "capability", "planning", "validation", "execution", "result"
    ] = Field(
        description="Stable workflow stage that produced this decision.",
    )
    status: Literal["success", "warning", "error", "skipped"] = Field(
        description="Display status of this workflow decision.",
    )
    title: str = Field(
        min_length=1,
        description="Short user-facing label for the decision.",
    )
    detail: str = Field(
        min_length=1,
        description="Concise explanation of what was decided and why.",
    )
    evidence: List[str] = Field(
        default_factory=list,
        description="Concrete parameters, fields, rules, or outcomes supporting the decision.",
    )
    external_call: bool = Field(
        default=False,
        description="Whether this workflow step issued a billable external API call.",
    )


class ServiceError(BaseModel):
    """Safe error details produced by a planner, data provider, or the application."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        min_length=1,
        pattern=PROVIDER_NAME_PATTERN,
        description="Stable planner, data-provider, or system identifier.",
    )
    code: Optional[Union[int, str]] = Field(
        default=None,
        description="Original upstream error code when one is available.",
    )
    message: str = Field(
        min_length=1,
        description="Original upstream message or a safe application message.",
    )
    http_status: Optional[int] = Field(
        default=None,
        ge=100,
        le=599,
        description="HTTP status returned by an upstream service when available.",
    )
    raw_response: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Safe upstream body with credentials and private headers removed.",
    )


class QueryResult(BaseModel):
    """Normalized table result or provider error for one data query."""

    model_config = ConfigDict(extra="forbid")

    query_id: str = Field(
        min_length=1,
        description="Identifier of the query that produced this result.",
    )
    provider: str = Field(
        min_length=1,
        pattern=PROVIDER_NAME_PATTERN,
        description="Data-provider identifier that executed the query.",
    )
    operation: str = Field(
        min_length=1,
        pattern=OPERATION_NAME_PATTERN,
        description="Provider-native operation used to retrieve the result.",
    )
    status: QueryStatus = Field(
        description="Whether this individual query succeeded or failed.",
    )
    columns: List[str] = Field(
        default_factory=list,
        description="Ordered table column names returned by the provider.",
    )
    rows: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="JSON-compatible table rows returned by the provider.",
    )
    row_count: int = Field(
        default=0,
        ge=0,
        description="Number of rows returned for this query.",
    )
    summary: Dict[str, Optional[Union[int, float]]] = Field(
        default_factory=dict,
        description=(
            "Local counts or rates requested by the validated query plan; null "
            "means the value is not computable from an empty valid sample."
        ),
    )
    error: Optional[ServiceError] = Field(
        default=None,
        description="Provider error details when this query failed.",
    )


class AnalysisResponse(BaseModel):
    """Complete provider-neutral response returned to the web client."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=1,
        description="Identifier used to correlate client requests and server logs.",
    )
    planner: str = Field(
        min_length=1,
        pattern=PROVIDER_NAME_PATTERN,
        description="Planner implementation that interpreted the natural-language request.",
    )
    data_provider: str = Field(
        min_length=1,
        pattern=PROVIDER_NAME_PATTERN,
        description="Market-data provider selected for all queries in this response.",
    )
    status: AnalysisStatus = Field(
        description="Overall completion state across planning and query execution.",
    )
    plan: Optional[QueryPlan] = Field(
        default=None,
        description="Validated query plan when planning completed successfully.",
    )
    results: List[QueryResult] = Field(
        default_factory=list,
        description="Ordered results corresponding to the plan queries.",
    )
    decision_trace: List[DecisionTraceStep] = Field(
        default_factory=list,
        description="Ordered, auditable workflow decisions rendered by the client.",
    )
    error: Optional[ServiceError] = Field(
        default=None,
        description="Planning or system error when no query-level result applies.",
    )
    cache_metrics: Optional[Dict[str, int]] = Field(
        default=None,
        description="Optional metrics tracking cache hits, misses, and bypasses during execution.",
    )


class AnalysisTask(BaseModel):
    """Persisted request, progress, and terminal result for one analysis task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(description="Stable identifier used to poll task state.")
    status: AnalysisTaskStatus = Field(description="Current asynchronous task state.")
    request: AnalysisRequest = Field(description="Original validated analysis request.")
    created_at: datetime = Field(description="UTC task creation timestamp.")
    updated_at: datetime = Field(description="UTC timestamp of the latest state change.")
    completed_items: int = Field(
        default=0,
        ge=0,
        description="Number of security-specific work items completed.",
    )
    total_items: int = Field(
        default=0,
        ge=0,
        description="Total security-specific work items discovered.",
    )
    response: Optional[AnalysisResponse] = Field(
        default=None,
        description="Terminal analysis response when execution finishes.",
    )
    error: Optional[ServiceError] = Field(
        default=None,
        description="Terminal worker error when no analysis response could be produced.",
    )


class AnalysisTaskSubmission(BaseModel):
    """Immediate response returned after an asynchronous task is accepted."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(description="Identifier used by the polling endpoint.")
    status: AnalysisTaskStatus = Field(description="Initial or reused task state.")
    status_url: str = Field(description="Relative endpoint used to poll task state.")


class DiscoveryTaskRequest(BaseModel):
    """Configuration for an automated alpha discovery task."""

    model_config = ConfigDict(extra="forbid")

    target_pool: str = Field(description="Universe constraint (e.g., 'A_SHARE').")
    train_start: str = Field(description="Start date for training (YYYYMMDD).")
    train_end: str = Field(description="End date for training (YYYYMMDD).")
    val_start: str = Field(description="Start date for validation blind test (YYYYMMDD).")
    val_end: str = Field(description="End date for validation blind test (YYYYMMDD).")
    factors: List[str] = Field(description="Selected base factor fields to use.", default_factory=list)
    prompt: str = Field(description="User guidance prompt.", default="")
    max_generations: int = Field(default=3, description="Number of evolutionary generations.")


class BacktestResult(BaseModel):
    """Performance evaluation of a single factor formula."""
    
    model_config = ConfigDict(extra="forbid")

    win_rate: float
    mean_return: float
    max_drawdown: float
    eval_time_ms: int


class FactorHypothesis(BaseModel):
    """A generated formula and its rationale."""
    
    model_config = ConfigDict(extra="forbid")

    formula: str
    description: str
    reasoning: str
    train_result: Optional[BacktestResult] = None
    val_result: Optional[BacktestResult] = None


class DiscoveryTaskProgress(BaseModel):
    """Live progress data for the evolution loop."""
    
    model_config = ConfigDict(extra="forbid")

    current_generation: int = 0
    total_generations: int = 0
    formulas_tested: int = 0
    current_log: str = ""
    leaderboard: List[FactorHypothesis] = Field(default_factory=list)


class DiscoveryTask(BaseModel):
    """Persisted request, progress, and terminal result for one discovery task."""

    model_config = ConfigDict(extra="forbid")

    task_type: Literal["discovery"] = "discovery"
    task_id: str = Field(description="Stable identifier used to poll task state.")
    status: AnalysisTaskStatus = Field(description="Current asynchronous task state.")
    request: DiscoveryTaskRequest = Field(description="Original validated discovery request.")
    created_at: datetime = Field(description="UTC task creation timestamp.")
    updated_at: datetime = Field(description="UTC timestamp of the latest state change.")
    progress: DiscoveryTaskProgress = Field(default_factory=DiscoveryTaskProgress)
    error: Optional[ServiceError] = None


class DiscoveryTaskStatusResponse(BaseModel):
    """Public task state that excludes the persisted input and screenshot."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(description="Stable task identifier.")
    status: AnalysisTaskStatus = Field(description="Current task lifecycle state.")
    progress: DiscoveryTaskProgress
    error: Optional[ServiceError] = None


class AnalysisTaskStatusResponse(BaseModel):
    """Public task state that excludes the persisted input and screenshot."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(description="Stable task identifier.")
    status: AnalysisTaskStatus = Field(description="Current task lifecycle state.")
    completed_items: int = Field(description="Completed work-item count.")
    total_items: int = Field(description="Total discovered work-item count.")
    response: Optional[AnalysisResponse] = Field(
        default=None,
        description="Terminal analysis response when available.",
    )
    error: Optional[ServiceError] = Field(
        default=None,
        description="Terminal worker error when available.",
    )


class StockListItem(BaseModel):
    """Normalized reference data for one currently listed A-share security."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        min_length=1,
        description="Tushare security code including its exchange suffix.",
    )
    symbol: str = Field(
        min_length=1,
        description="Six-digit security symbol without an exchange suffix.",
    )
    name: str = Field(
        min_length=1,
        description="Official short company name returned by Tushare.",
    )
    area: Optional[str] = Field(
        default=None,
        description="Registration area returned by Tushare when available.",
    )
    industry: Optional[str] = Field(
        default=None,
        description="Industry classification returned by Tushare when available.",
    )
    board: Optional[str] = Field(
        default=None,
        description="Listing board returned in the Tushare market field.",
    )
    exchange: Literal["SSE", "SZSE", "BSE"] = Field(
        description="Tushare exchange identifier for the listed security.",
    )
    listed_on: CalendarDate = Field(
        description="Initial listing date normalized to an ISO calendar date.",
    )


class StockListResponse(BaseModel):
    """One deterministic page of currently listed A-share securities."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=1,
        description="Identifier used to correlate client requests and server logs.",
    )
    page: int = Field(
        ge=1,
        description="Current one-based page number.",
    )
    page_size: int = Field(
        ge=1,
        le=100,
        description="Maximum number of securities returned on one page.",
    )
    total: int = Field(
        ge=0,
        description="Number of securities matching the active filters.",
    )
    total_pages: int = Field(
        ge=1,
        description="Number of pages matching the active filters, with one empty page.",
    )
    available_industries: List[str] = Field(
        default_factory=list,
        description="Sorted industries available across all currently listed securities.",
    )
    items: List[StockListItem] = Field(
        default_factory=list,
        description="Normalized securities contained in the current page.",
    )


class StockListErrorResponse(BaseModel):
    """Structured failure returned by the stock catalog endpoint."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=1,
        description="Identifier used to correlate client requests and server logs.",
    )
    error: ServiceError = Field(
        description="Safe provider or application error details.",
    )


class TradingCalendarBreadth(BaseModel):
    """End-of-day advancing and declining security counts for one market scope."""

    model_config = ConfigDict(extra="forbid")

    advanced: int = Field(
        ge=0,
        description="Securities with a positive daily percentage change.",
    )
    declined: int = Field(
        ge=0,
        description="Securities with a negative daily percentage change.",
    )
    unchanged: int = Field(
        ge=0,
        description="Securities with a zero daily percentage change.",
    )
    traded: int = Field(
        ge=0,
        description="Securities with valid daily market data included in the counts.",
    )
    advance_decline_ratio: Optional[float] = Field(
        default=None,
        ge=0,
        description=(
            "Advanced securities divided by declined securities, or null when no "
            "security declined."
        ),
    )

    @model_validator(mode="after")
    def validate_traded_count(self) -> "TradingCalendarBreadth":
        """Reject breadth totals that disagree with their component counts."""
        if self.traded != self.advanced + self.declined + self.unchanged:
            raise ValueError("traded must equal advanced + declined + unchanged")
        return self


class TradingCalendarDay(BaseModel):
    """Normalized trading status for one mainland A-share calendar date."""

    model_config = ConfigDict(extra="forbid")

    date: CalendarDate = Field(
        description="Calendar date represented by this trading status.",
    )
    is_open: bool = Field(
        description="Whether the reference exchange is open for trading on this date.",
    )
    previous_trading_date: Optional[CalendarDate] = Field(
        default=None,
        description="Most recent preceding open trading date when supplied by Tushare.",
    )
    breadth: Optional[TradingCalendarBreadth] = Field(
        default=None,
        description=(
            "End-of-day market breadth for an open historical date, or null for "
            "closed, future, or unavailable dates."
        ),
    )


class TradingCalendarResponse(BaseModel):
    """One complete month of mainland A-share trading-calendar data."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=1,
        description="Identifier used to correlate client requests and server logs.",
    )
    market: Literal["A_SHARE"] = Field(
        default="A_SHARE",
        description="Fixed mainland A-share market boundary represented by this calendar.",
    )
    month: str = Field(
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="Requested calendar month formatted as YYYY-MM.",
    )
    exchange: Literal["ALL", "SSE", "SZSE", "BSE"] = Field(
        description="Exchange scope selected for the calendar and breadth counts.",
    )
    source_exchanges: List[Literal["SSE", "SZSE", "BSE"]] = Field(
        min_length=1,
        max_length=3,
        description="Exchange calendars and securities included in the response.",
    )
    days: List[TradingCalendarDay] = Field(
        min_length=28,
        max_length=31,
        description="Chronological trading status for every calendar date in the month.",
    )


class TradingCalendarErrorResponse(BaseModel):
    """Structured failure returned by the trading-calendar endpoint."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=1,
        description="Identifier used to correlate client requests and server logs.",
    )
    error: ServiceError = Field(
        description="Safe provider or application error details.",
    )


class DataCacheRecord(BaseModel):
    """Serializable successful provider response stored in the layered cache."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[DATA_CACHE_SCHEMA_VERSION] = Field(
        default=DATA_CACHE_SCHEMA_VERSION,
        description="Cache payload version included in compatibility checks and keys.",
    )
    provider: str = Field(
        min_length=1,
        pattern=PROVIDER_NAME_PATTERN,
        description="Data-provider identifier included in the cache namespace.",
    )
    operation: str = Field(
        min_length=1,
        pattern=OPERATION_NAME_PATTERN,
        description="Provider-native operation that produced the cached response.",
    )
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Normalized provider parameters used to identify the response.",
    )
    fields: List[str] = Field(
        default_factory=list,
        description="Ordered output fields requested from the provider.",
    )
    fetched_at: AwareDatetime = Field(
        description="Timezone-aware instant when the provider returned the response.",
    )
    expires_at: AwareDatetime = Field(
        description="Timezone-aware instant after which the response must not be served.",
    )
    columns: List[str] = Field(
        default_factory=list,
        description="Ordered table columns returned by the provider.",
    )
    rows: List[List[Any]] = Field(
        default_factory=list,
        description="JSON-compatible table rows aligned with the ordered columns.",
    )

    @model_validator(mode="after")
    def validate_expiration(self) -> "DataCacheRecord":
        """Reject records that are invalid at their fetch instant."""
        if self.expires_at <= self.fetched_at:
            raise ValueError("expires_at must be later than fetched_at")
        return self


class UiFeedbackRect(BaseModel):
    """Viewport-relative rectangle associated with selected page content."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(description="Horizontal viewport coordinate in CSS pixels.")
    y: float = Field(description="Vertical viewport coordinate in CSS pixels.")
    width: float = Field(ge=0, description="Selected width in CSS pixels.")
    height: float = Field(ge=0, description="Selected height in CSS pixels.")


class UiFeedbackViewport(BaseModel):
    """Browser viewport dimensions captured with one UI improvement request."""

    model_config = ConfigDict(extra="forbid")

    width: int = Field(ge=1, le=10_000, description="Viewport width in CSS pixels.")
    height: int = Field(ge=1, le=10_000, description="Viewport height in CSS pixels.")
    scroll_x: float = Field(
        ge=0,
        description="Horizontal document scroll offset in CSS pixels.",
    )
    scroll_y: float = Field(
        ge=0,
        description="Vertical document scroll offset in CSS pixels.",
    )


class UiFeedbackRequest(BaseModel):
    """Administrator-authored request to improve selected production UI content."""

    model_config = ConfigDict(extra="forbid")

    page_path: str = Field(
        min_length=1,
        max_length=500,
        description="Application path containing the selected content.",
    )
    feedback_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="Stable frontend component identifier nearest the selection.",
    )
    selected_text: str = Field(
        default="",
        max_length=2_000,
        description="Bounded visible text selected by the administrator.",
    )
    suggestion: str = Field(
        default="",
        max_length=4_000,
        description="Optional administrator instruction describing the desired change.",
    )
    conversation: List["UiFeedbackConversationMessage"] = Field(
        default_factory=list,
        max_length=12,
        description="Bounded administrator and assistant discussion supporting the change.",
    )
    rect: UiFeedbackRect = Field(
        description="Viewport-relative selection or component rectangle.",
    )
    viewport: UiFeedbackViewport = Field(
        description="Browser viewport dimensions used to interpret the rectangle.",
    )

    @model_validator(mode="after")
    def validate_feedback_context(self) -> "UiFeedbackRequest":
        """Require either selected text or an explicit administrator suggestion."""
        if not self.selected_text.strip() and not self.suggestion.strip():
            raise ValueError("selected_text or suggestion is required")
        return self


class UiFeedbackConversationMessage(BaseModel):
    """One bounded message in an administrator UI feedback discussion."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"] = Field(
        description="Participant that authored the feedback discussion message.",
    )
    content: str = Field(
        min_length=1,
        max_length=2_000,
        description="Plain-text discussion content used to refine an improvement.",
    )


class UiFeedbackChatRequest(BaseModel):
    """Authenticated question about one selected production UI region."""

    model_config = ConfigDict(extra="forbid")

    page_path: str = Field(
        min_length=1,
        max_length=500,
        description="Application path containing the selected content.",
    )
    feedback_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="Stable frontend component identifier nearest the selection.",
    )
    selected_text: str = Field(
        min_length=1,
        max_length=2_000,
        description="Bounded visible text captured from the selected region.",
    )
    conversation: List[UiFeedbackConversationMessage] = Field(
        min_length=1,
        max_length=12,
        description="Discussion ending with the administrator question to answer.",
    )

    @model_validator(mode="after")
    def validate_last_chat_message(self) -> "UiFeedbackChatRequest":
        """Require each chat turn to end with a new administrator question."""
        if self.conversation[-1].role != "user":
            raise ValueError("conversation must end with a user message")
        return self


class UiFeedbackChatResponse(BaseModel):
    """Assistant response that helps refine one UI improvement."""

    model_config = ConfigDict(extra="forbid")

    message: UiFeedbackConversationMessage = Field(
        description="Assistant reply to append to the feedback discussion.",
    )


class UiFeedbackConfig(BaseModel):
    """Public configuration required to enable administrator UI feedback."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        description="Whether every required backend and GitHub credential is configured.",
    )
    google_client_id: str = Field(
        default="",
        description="Public Google Web OAuth client identifier used by the frontend.",
    )
    git_branch: str = Field(
        default="",
        description="Git branch recorded for the currently deployed application.",
    )
    git_sha: str = Field(
        default="",
        description="Full Git commit recorded for the currently deployed application.",
    )


class UiFeedbackSubmission(BaseModel):
    """Public acknowledgement returned after a UI request is persisted and dispatched."""

    model_config = ConfigDict(extra="forbid")

    feedback_id: str = Field(
        min_length=1,
        description="Opaque identifier used to correlate the feedback workflow.",
    )
    status: UiFeedbackStatus = Field(
        description="Current durable UI feedback workflow state.",
    )
    actions_url: str = Field(
        min_length=1,
        description="GitHub Actions page where the administrator can inspect progress.",
    )

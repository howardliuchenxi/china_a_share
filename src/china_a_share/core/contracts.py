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
AggregationFunction = Literal[
    "count",
    "count_distinct",
    "sum",
    "mean",
    "median",
    "min",
    "max",
    "std",
    "quantile",
    "first",
    "last",
]
DISCOVERY_SEQUENCE_FACTOR_FIELDS = {
    "amount_5d_to_20d_avg_ratio",
    "amount_to_5d_avg_ratio",
    "amount_to_20d_avg_ratio",
    "close_location_pct",
    "distance_from_5d_ma_pct",
    "distance_from_10d_ma_pct",
    "distance_from_20d_ma_pct",
    "distance_from_10d_peak_pct",
    "distance_from_20d_peak_pct",
    "distance_from_5d_peak_pct",
    "intraday_range_pct",
    "intraday_return_pct",
    "max_drawdown_5d_pct",
    "open_gap_pct",
    "downside_deviation_5d_pct",
    "downside_deviation_10d_pct",
    "downside_deviation_20d_pct",
    "positive_days_3",
    "positive_days_5",
    "positive_days_10",
    "return_10d_pct",
    "return_20d_pct",
    "return_5d_pct",
    "turnover_5d_avg_pct",
    "turnover_20d_avg_pct",
    "turnover_5d_to_20d_avg_ratio",
    "turnover_to_5d_avg_ratio",
    "turnover_to_20d_avg_ratio",
    "volume_5d_to_20d_avg_ratio",
    "volume_to_5d_avg_ratio",
    "volume_to_20d_avg_ratio",
    "volatility_10d_pct",
    "volatility_20d_pct",
    "volatility_5d_pct",
}
DISCOVERY_FACTOR_FIELDS = {
    "amount",
    "circ_mv",
    "close",
    "dv_ratio",
    "dv_ttm",
    "float_share",
    "free_share",
    "open",
    "pb",
    "pct_chg",
    "pe",
    "pe_ttm",
    "ps",
    "ps_ttm",
    "total_mv",
    "total_share",
    "turnover_rate",
    "turnover_rate_f",
    "vol",
    "volume_ratio",
} | DISCOVERY_SEQUENCE_FACTOR_FIELDS
ANALYSIS_UNIVERSE_FILTER_FIELDS = {
    "area",
    "exchange",
    "industry",
    "list_status",
    "market",
    "name",
    "ts_code",
}


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
    operator: Literal[
        "gt",
        "ge",
        "eq",
        "ne",
        "le",
        "lt",
        "in",
        "not_in",
        "contains",
        "not_contains",
    ] = Field(
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
        if isinstance(self.value, str) and self.operator not in {
            "gt",
            "ge",
            "eq",
            "ne",
            "le",
            "lt",
            "contains",
            "not_contains",
        }:
            raise ValueError(
                "string filter values require an ordered, exact, or contains operator"
            )
        if isinstance(self.value, list):
            if self.operator not in {"in", "not_in"}:
                raise ValueError(
                    "list filter values require an in or not_in operator"
                )
            if not self.value:
                raise ValueError("membership filter values must not be empty")
        if self.operator in {"in", "not_in"} and not isinstance(self.value, list):
            raise ValueError("membership operators require a list value")
        if self.operator in {"contains", "not_contains"} and not isinstance(
            self.value,
            str,
        ):
            raise ValueError("contains operators require a string value")
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
    function: AggregationFunction = Field(
        description="Allowlisted deterministic aggregation function.",
    )
    quantile: Optional[float] = Field(
        default=None,
        ge=0,
        le=1,
        description="Required percentile fraction for a quantile aggregation.",
    )

    @model_validator(mode="after")
    def validate_function_arguments(self) -> "ResultAggregation":
        """Require quantile parameters only for quantile aggregation."""
        if (self.function == "quantile") != (self.quantile is not None):
            raise ValueError("quantile aggregation requires exactly one quantile")
        return self


class ResultPipelineStep(BaseModel):
    """One validated relational operation applied to a query result."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "latest_by_group",
        "select_fields",
        "rename_fields",
        "distinct",
        "derive",
        "drop_missing",
        "filter",
        "filter_set",
        "filter_range",
        "filter_null",
        "sort",
        "limit",
        "quantile_filter",
        "aggregate",
        "rolling_mean",
        "rolling_sum",
        "rolling_min",
        "rolling_max",
        "rolling_std",
        "rolling_quantile",
        "rolling_correlation",
        "rolling_covariance",
        "cumulative_sum",
        "expanding_mean",
        "group_transform",
        "normalize",
        "weighted_mean",
        "resample",
        "shift",
        "diff",
        "pct_change",
        "rank",
        "dense_rank",
        "row_number",
        "top_k_by_group",
        "match_at_offset",
        "match_source",
        "exists_in_source",
        "semi_join",
        "anti_join",
        "inner_join",
        "asof_join",
        "intersect_keys",
        "except_keys",
        "union_all",
        "join_fields",
        "having",
        "compare_fields",
        "compare_scalar",
        "coalesce",
        "fill_constant",
        "clip",
        "conditional_value",
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
    matched_date_output_field: Optional[str] = Field(
        default=None,
        pattern=OPERATION_NAME_PATTERN,
        description="Actual market trading date selected by a temporal match.",
    )
    right_field: Optional[str] = Field(
        default=None,
        pattern=OPERATION_NAME_PATTERN,
        description="Right-hand input field used by a field comparison.",
    )
    weight_field: Optional[str] = Field(
        default=None,
        pattern=OPERATION_NAME_PATTERN,
        description="Non-negative weight column used by a weighted aggregation.",
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
    fields: Union[List[str], Dict[str, str]] = Field(
        default_factory=list,
        description="Input fields required to be non-null, or rename mapping dictionary for join_fields.",
    )
    cardinality: Optional[
        Literal["one_to_one", "many_to_one", "many_to_many"]
    ] = Field(
        default=None,
        description="Expected cardinality constraint for join_fields.",
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
    right_order_by: Optional[str] = Field(
        default=None,
        pattern=OPERATION_NAME_PATTERN,
        description="Right-side time field used by an as-of join.",
    )
    asof_direction: Literal["backward", "forward", "nearest"] = Field(
        default="backward",
        description="Temporal search direction used by an as-of join.",
    )
    tolerance: Optional[float] = Field(
        default=None,
        gt=0,
        description="Required finite maximum distance allowed by an as-of join.",
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
    comparison: Optional[
        Literal["gt", "ge", "eq", "ne", "le", "lt", "contains", "not_contains"]
    ] = Field(
        default=None,
        description="Comparison used by filter and quantile-filter operations.",
    )
    value: Optional[Union[int, float, str]] = Field(
        default=None,
        description="Bounded scalar used by arithmetic or row filtering.",
    )
    values: List[Union[int, float, str]] = Field(
        default_factory=list,
        max_length=1_000,
        description="Bounded scalar set used by membership filtering.",
    )
    lower_value: Optional[Union[int, float]] = Field(
        default=None,
        description="Inclusive lower bound used by range filtering.",
    )
    upper_value: Optional[Union[int, float]] = Field(
        default=None,
        description="Inclusive upper bound used by range filtering.",
    )
    true_value: Optional[Union[int, float, str]] = Field(
        default=None,
        description="Scalar emitted when a conditional comparison is true.",
    )
    false_value: Optional[Union[int, float, str]] = Field(
        default=None,
        description="Scalar emitted when a conditional comparison is false.",
    )
    negate: bool = Field(
        default=False,
        description="Whether a set, range, or null predicate is logically negated.",
    )
    keep: Literal["first", "last"] = Field(
        default="first",
        description="Stable duplicate retained by a distinct operation.",
    )
    rank_method: Literal["average", "min", "max", "first"] = Field(
        default="min",
        description="Tie policy used by a rank operation.",
    )
    transform_function: Optional[
        Literal["count", "sum", "mean", "median", "min", "max", "std"]
    ] = Field(
        default=None,
        description="Allowlisted aggregation broadcast to every row in a group.",
    )
    normalization: Optional[Literal["zscore", "minmax", "percentile"]] = Field(
        default=None,
        description="Allowlisted normalization method applied globally or by group.",
    )
    frequency: Optional[Literal["week", "month", "quarter", "year"]] = Field(
        default=None,
        description="Calendar frequency used to resample an ordered time series.",
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
    offset_value: Optional[int] = Field(
        default=None,
        ge=1,
        le=1_000,
        description="Positive calendar offset applied by a temporal match.",
    )
    offset_unit: Optional[
        Literal["day", "week", "month", "year", "trading_session"]
    ] = Field(
        default=None,
        description="Calendar unit applied by a temporal match.",
    )
    require_consecutive: bool = Field(
        default=False,
        description=(
            "Whether rolling or shifted rows must be adjacent in the global "
            "market order sequence."
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
            "select_fields": bool(isinstance(self.fields, list) and self.fields),
            "rename_fields": bool(isinstance(self.fields, dict) and self.fields),
            "distinct": bool(isinstance(self.fields, list) and self.fields),
            "derive": bool(
                self.field
                and self.output_field
                and self.arithmetic_operator
                and (
                    (self.value is not None and not self.right_field)
                    or (self.right_field and self.value is None)
                )
            ),
            "drop_missing": bool(self.fields),
            "filter": bool(
                self.field and self.comparison and self.value is not None
            ),
            "filter_set": bool(self.field and self.values),
            "filter_range": bool(
                self.field
                and self.lower_value is not None
                and self.upper_value is not None
            ),
            "filter_null": bool(self.field),
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
            "rolling_min": bool(
                self.field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.window
            ),
            "rolling_max": bool(
                self.field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.window
            ),
            "rolling_std": bool(
                self.field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.window
            ),
            "rolling_quantile": bool(
                self.field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.window
                and self.quantile is not None
            ),
            "rolling_correlation": bool(
                self.field
                and self.right_field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.window
            ),
            "rolling_covariance": bool(
                self.field
                and self.right_field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.window
            ),
            "cumulative_sum": bool(
                self.field and self.output_field and self.group_by and self.order_by
            ),
            "expanding_mean": bool(
                self.field and self.output_field and self.group_by and self.order_by
            ),
            "group_transform": bool(
                self.field
                and self.output_field
                and self.group_by
                and self.transform_function
            ),
            "normalize": bool(
                self.field and self.output_field and self.normalization
            ),
            "weighted_mean": bool(
                self.field and self.weight_field and self.output_field and self.group_by
            ),
            "resample": bool(
                self.group_by
                and self.order_by
                and self.frequency
                and self.aggregations
            ),
            "shift": bool(
                self.field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.periods
            ),
            "diff": bool(
                self.field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.periods
            ),
            "pct_change": bool(
                self.field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.periods
            ),
            "rank": bool(self.field and self.output_field),
            "dense_rank": bool(self.field and self.output_field),
            "row_number": bool(self.output_field and self.group_by and self.order_by),
            "top_k_by_group": bool(self.field and self.group_by and self.count),
            "match_at_offset": bool(
                self.field
                and self.output_field
                and self.group_by
                and self.order_by
                and self.offset_value
                and self.offset_unit
                and self.matched_date_output_field
            ),
            "match_source": bool(
                self.right_source_query_id
                and self.join_on
                and self.output_field
            ),
            "exists_in_source": bool(
                self.right_source_query_id
                and self.join_on
                and self.output_field
            ),
            "semi_join": bool(self.right_source_query_id and self.join_on),
            "anti_join": bool(self.right_source_query_id and self.join_on),
            "inner_join": bool(
                self.right_source_query_id
                and self.join_on
                and isinstance(self.fields, dict)
                and self.cardinality
            ),
            "asof_join": bool(
                self.right_source_query_id
                and self.group_by
                and self.order_by
                and self.right_order_by
                and isinstance(self.fields, dict)
                and self.fields
                and self.tolerance is not None
            ),
            "intersect_keys": bool(self.right_source_query_id and self.join_on),
            "except_keys": bool(self.right_source_query_id and self.join_on),
            "union_all": bool(self.right_source_query_id),
            "join_fields": bool(
                self.right_source_query_id
                and self.join_on
                and self.fields
                and self.cardinality
            ),
            "having": bool(
                self.field and self.comparison and self.value is not None
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
            "coalesce": bool(
                self.output_field and isinstance(self.fields, list) and self.fields
            ),
            "fill_constant": bool(
                self.field and self.output_field and self.value is not None
            ),
            "clip": bool(
                self.field
                and self.output_field
                and (self.lower_value is not None or self.upper_value is not None)
            ),
            "conditional_value": bool(
                self.field
                and self.output_field
                and self.comparison
                and self.value is not None
                and self.true_value is not None
                and self.false_value is not None
            ),
            "summarize": bool(self.aggregations),
        }
        if not required[self.operation]:
            raise ValueError(f"Missing required arguments for {self.operation}")
        if self.operation == "derive":
            if self.value is not None and (
                isinstance(self.value, bool)
                or not isinstance(self.value, (int, float))
            ):
                raise ValueError("derive requires a numeric scalar value")
            if (
                self.right_field
                and self.arithmetic_operator == "constant_minus"
            ):
                raise ValueError(
                    "constant_minus requires a numeric scalar value"
                )
            if self.right_field == self.field:
                raise ValueError(
                    "derive requires different left and right fields"
                )
        if (
            self.operation == "match_at_offset"
            and self.field == self.order_by
        ):
            raise ValueError(
                "match_at_offset value field must differ from order_by"
            )
        if (
            self.operation == "match_at_offset"
            and self.output_field == self.matched_date_output_field
        ):
            raise ValueError(
                "match_at_offset value and matched-date output fields must differ"
            )
        if (
            self.operation
            in {
                "rolling_mean",
                "rolling_sum",
                "rolling_min",
                "rolling_max",
                "rolling_std",
                "rolling_quantile",
                "rolling_correlation",
                "rolling_covariance",
            }
            and self.min_periods is not None
            and self.min_periods > self.window
        ):
            raise ValueError("min_periods cannot exceed the rolling window")
        if self.operation == "rolling_correlation" and self.field == self.right_field:
            raise ValueError("rolling_correlation requires two different fields")
        if self.operation == "rolling_covariance" and self.field == self.right_field:
            raise ValueError("rolling_covariance requires two different fields")
        if self.operation == "resample":
            reserved_fields = set(self.group_by + [self.order_by])
            if reserved_fields.intersection(
                aggregation.output_field for aggregation in self.aggregations
            ):
                raise ValueError("resample outputs must not replace grouping or time fields")
        if (
            self.operation == "filter_range"
            and self.lower_value > self.upper_value
        ):
            raise ValueError("filter_range lower_value cannot exceed upper_value")
        if (
            self.operation == "clip"
            and self.lower_value is not None
            and self.upper_value is not None
            and self.lower_value > self.upper_value
        ):
            raise ValueError("clip lower_value cannot exceed upper_value")
        if (
            self.operation == "rename_fields"
            and len(set(self.fields.values())) != len(self.fields)
        ):
            raise ValueError("rename_fields outputs must be unique")
        if self.operation == "asof_join":
            if len(set(self.fields.values())) != len(self.fields):
                raise ValueError("asof_join outputs must be unique")
            reserved_right_fields = set(self.group_by + [self.right_order_by])
            if reserved_right_fields.intersection(self.fields):
                raise ValueError(
                    "asof_join copied fields must exclude grouping and time fields"
                )
        aggregation_outputs = [
            aggregation.output_field for aggregation in self.aggregations
        ]
        if len(aggregation_outputs) != len(set(aggregation_outputs)):
            raise ValueError("aggregation output fields must be unique")
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
        max_length=16,
        description="Ordered allowlisted relational operations.",
    )


class AnalysisUniverse(BaseModel):
    """Target universe for high-level semantic analysis intent."""

    model_config = ConfigDict(extra="forbid")

    market: Literal["A_SHARE"] = Field(
        default="A_SHARE",
        description="Target market universe.",
    )
    filters: List[DataFilter] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Security-master predicates that define which securities may enter "
            "the analysis."
        ),
    )

    @model_validator(mode="after")
    def validate_filter_fields(self) -> "AnalysisUniverse":
        """Restrict universe predicates to documented security-master fields."""
        invalid_fields = {
            row_filter.field for row_filter in self.filters
        }.difference(ANALYSIS_UNIVERSE_FILTER_FIELDS)
        if invalid_fields:
            raise ValueError(
                "unsupported universe filter fields: "
                + ", ".join(sorted(invalid_fields))
            )
        return self


class DateWindow(BaseModel):
    """Exclusive or inclusive calendar date range window."""

    model_config = ConfigDict(extra="forbid")

    start: str = Field(
        pattern=r"^\d{8}$",
        description="Start date of the window in YYYYMMDD format.",
    )
    end: str = Field(
        pattern=r"^\d{8}$",
        description="End date of the window in YYYYMMDD format.",
    )


class AnalysisMetric(BaseModel):
    """Metric properties and window configurations for high-level intent."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "period_return",
        "pct_chg",
        "pe",
        "pe_ttm",
        "pb",
        "total_mv",
        "circ_mv",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "dv_ratio",
        "dv_ttm",
    ] = Field(
        default="period_return",
        description="Derived analysis metric type.",
    )
    window: Optional[DateWindow] = Field(
        default=None,
        description="Target date range required by interval-derived metrics.",
    )
    as_of: Optional[str] = Field(
        default=None,
        pattern=r"^\d{8}$",
        description="Trading snapshot date required by point-in-time metrics.",
    )
    filters: List[DataFilter] = Field(
        default_factory=list,
        max_length=8,
        description="Optional predicates applied to the selected metric before ranking.",
    )

    @model_validator(mode="after")
    def validate_metric_inputs(self) -> "AnalysisMetric":
        """Require the temporal input and filter field owned by each metric type."""
        if self.type == "period_return":
            if self.window is None or self.as_of is not None:
                raise ValueError("period_return requires window and prohibits as_of")
        elif self.as_of is None or self.window is not None:
            raise ValueError("snapshot metrics require as_of and prohibit window")
        if any(row_filter.field != self.type for row_filter in self.filters):
            raise ValueError("metric filters must target the selected metric type")
        return self


class AnalysisRanking(BaseModel):
    """Sorting and size boundaries for intent ranking."""

    model_config = ConfigDict(extra="forbid")

    direction: Literal["asc", "desc"] = Field(
        default="asc",
        description="Rank sort direction.",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Total top rows retained.",
    )


class AnalysisIntent(BaseModel):
    """High-level analysis intent parsed by the LLM planner."""

    model_config = ConfigDict(extra="forbid")

    analysis_type: Literal[
        "rank_metric",
        "event_outcome_probability",
        "field_analysis",
    ] = Field(
        default="rank_metric",
        description="Categorized analysis intent type.",
    )
    universe: AnalysisUniverse = Field(
        default_factory=AnalysisUniverse,
        description="Universe boundaries.",
    )
    metric: Optional[AnalysisMetric] = Field(
        default=None,
        description="Target calculation metric for metric-ranking analysis.",
    )
    ranking: Optional[AnalysisRanking] = Field(
        default=None,
        description="Sorting and count constraints for metric-ranking analysis.",
    )
    event_window: Optional[DateWindow] = Field(
        default=None,
        description="Inclusive interval in which qualifying events must end.",
    )
    event_type: Optional[Literal["limit_up"]] = Field(
        default=None,
        description="Provider-neutral market event evaluated by an event study.",
    )
    consecutive_sessions: Optional[int] = Field(
        default=None,
        ge=1,
        le=20,
        description="Required number of consecutive trading sessions for the event.",
    )
    observation_offset: Optional[int] = Field(
        default=None,
        ge=1,
        le=60,
        description="Positive offset from the final event session to the outcome.",
    )
    observation_unit: Optional[
        Literal["trading_session", "day", "week", "month", "year"]
    ] = Field(
        default=None,
        description="Time unit used to locate the post-event outcome.",
    )
    outcomes: List[Literal["up", "down"]] = Field(
        default_factory=list,
        max_length=2,
        description="Requested directional outcomes whose probabilities are returned.",
    )
    aggregation: Optional[Literal["probability"]] = Field(
        default=None,
        description="Aggregate statistic calculated over qualifying event outcomes.",
    )
    operation: Optional[str] = Field(
        default=None,
        pattern=OPERATION_NAME_PATTERN,
        description="Provider operation supplying a generic analyzable field.",
    )
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Provider parameters bounding a generic field analysis.",
    )
    fields: List[str] = Field(
        default_factory=list,
        max_length=30,
        description="Provider fields required by a generic field analysis.",
    )
    filters: List[DataFilter] = Field(
        default_factory=list,
        max_length=12,
        description="Local predicates applied before generic aggregation or ranking.",
    )
    analysis_field: Optional[str] = Field(
        default=None,
        pattern=OPERATION_NAME_PATTERN,
        description="Provider field sorted by a generic field ranking.",
    )
    group_by: List[str] = Field(
        default_factory=list,
        max_length=8,
        description="Grouping keys applied before generic field aggregations.",
    )
    aggregations: List[ResultAggregation] = Field(
        default_factory=list,
        max_length=12,
        description="Deterministic aggregations for a generic field analysis.",
    )

    @model_validator(mode="after")
    def validate_intent_shape(self) -> "AnalysisIntent":
        """Require exactly the semantic inputs needed by the selected analysis type."""
        if self.analysis_type == "rank_metric":
            if self.metric is None or self.ranking is None:
                raise ValueError("rank_metric requires metric and ranking")
            if any(
                value is not None
                for value in (
                    self.event_window,
                    self.event_type,
                    self.consecutive_sessions,
                    self.observation_offset,
                    self.observation_unit,
                    self.aggregation,
                    self.operation,
                    self.analysis_field,
                )
            ) or self.outcomes or any(
                (self.params, self.fields, self.filters, self.group_by, self.aggregations)
            ):
                raise ValueError("rank_metric cannot declare event-study inputs")
            return self
        if self.analysis_type == "field_analysis":
            if not self.operation or not self.fields:
                raise ValueError("field_analysis requires operation and fields")
            if self.ranking is not None and not self.analysis_field:
                raise ValueError(
                    "field_analysis ranking requires analysis_field"
                )
            aggregation_outputs = {
                aggregation.output_field for aggregation in self.aggregations
            }
            required_fields = {
                *self.group_by,
                *(row_filter.field for row_filter in self.filters),
                *(aggregation.field for aggregation in self.aggregations),
            }.difference({None})
            if (
                self.analysis_field is not None
                and self.analysis_field not in aggregation_outputs
            ):
                required_fields.add(self.analysis_field)
            missing_fields = required_fields.difference(self.fields)
            if missing_fields:
                raise ValueError(
                    "field_analysis fields omit required inputs: "
                    + ", ".join(sorted(missing_fields))
                )
            if any(
                value is not None
                for value in (
                    self.metric,
                    self.event_window,
                    self.event_type,
                    self.consecutive_sessions,
                    self.observation_offset,
                    self.observation_unit,
                    self.aggregation,
                )
            ) or self.outcomes:
                raise ValueError(
                    "field_analysis cannot declare metric or event-study inputs"
                )
            if self.ranking is None and not self.aggregations:
                raise ValueError(
                    "field_analysis requires ranking or aggregations"
                )
            return self
        if (
            self.metric is not None
            or self.ranking is not None
            or self.operation is not None
            or self.analysis_field is not None
            or any(
                (self.params, self.fields, self.filters, self.group_by, self.aggregations)
            )
        ):
            raise ValueError(
                "event_outcome_probability cannot declare metric-ranking inputs"
            )
        if not all(
            value is not None
            for value in (
                self.event_window,
                self.event_type,
                self.consecutive_sessions,
                self.observation_offset,
                self.observation_unit,
                self.aggregation,
            )
        ) or not self.outcomes:
            raise ValueError(
                "event_outcome_probability requires a window, event, sequence, "
                "observation, outcomes, and aggregation"
            )
        if len(self.outcomes) != len(set(self.outcomes)):
            raise ValueError("event outcomes must be unique")
        return self


class AnswerOutput(BaseModel):
    """One user-requested field that must exist in the final answer result."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(
        pattern=OPERATION_NAME_PATTERN,
        description="Exact final result field that satisfies one requested output.",
    )
    description: str = Field(
        min_length=1,
        description="Business meaning of the output, including its unit or condition.",
    )


class AnswerContract(BaseModel):
    """Machine-verifiable shape of the result promised to the user."""

    model_config = ConfigDict(extra="forbid")

    result_query_id: str = Field(
        min_length=1,
        description="Query or pipeline output identifier containing the final answer.",
    )
    result_kind: Literal["table", "summary"] = Field(
        description="Whether the answer contains detail rows or aggregate metrics.",
    )
    outputs: List[AnswerOutput] = Field(
        min_length=1,
        max_length=20,
        description="Complete set of fields explicitly requested by the user.",
    )

    @model_validator(mode="after")
    def validate_unique_outputs(self) -> "AnswerContract":
        """Reject ambiguous contracts that promise one field more than once."""
        output_fields = [output.field for output in self.outputs]
        if len(output_fields) != len(set(output_fields)):
            raise ValueError("answer contract output fields must be unique")
        return self


class QueryConstraint(BaseModel):
    """One user selection predicate with a machine-verifiable execution binding."""

    model_config = ConfigDict(extra="forbid")

    constraint_id: str = Field(
        min_length=1,
        pattern=OPERATION_NAME_PATTERN,
        description="Stable identifier for one atomic user selection predicate.",
    )
    scope: Literal["universe", "result"] = Field(
        description=(
            "Whether the predicate restricts eligible securities or computed rows."
        ),
    )
    field: str = Field(
        min_length=1,
        pattern=OPERATION_NAME_PATTERN,
        description="Exact field evaluated by the executable predicate.",
    )
    operator: Literal[
        "gt",
        "ge",
        "eq",
        "ne",
        "le",
        "lt",
        "in",
        "not_in",
        "contains",
        "not_contains",
    ] = Field(
        description="Comparison operator preserved from the user requirement.",
    )
    value: Union[float, str, List[str]] = Field(
        description="Exact scalar or membership values preserved from the request.",
    )
    query_id: str = Field(
        min_length=1,
        description="Provider query whose local row filter applies this predicate.",
    )
    enforcement_step_index: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Pipeline filter step that enforces membership when the constrained "
            "query is not the pipeline source."
        ),
    )

    @model_validator(mode="after")
    def validate_value_operator(self) -> "QueryConstraint":
        """Keep declared constraint values consistent with executable filters."""
        if isinstance(self.value, str) and self.operator not in {
            "gt",
            "ge",
            "eq",
            "ne",
            "le",
            "lt",
            "contains",
            "not_contains",
        }:
            raise ValueError(
                "string constraint values require an ordered, exact, or contains operator"
            )
        if isinstance(self.value, list):
            if self.operator not in {"in", "not_in"} or not self.value:
                raise ValueError(
                    "constraint membership values require a non-empty membership predicate"
                )
        if self.operator in {"in", "not_in"} and not isinstance(self.value, list):
            raise ValueError("constraint membership operators require a list value")
        if self.operator in {"contains", "not_contains"} and not isinstance(
            self.value,
            str,
        ):
            raise ValueError("constraint contains operators require a string value")
        return self


class ExecutionNode(BaseModel):
    """One provider-query or deterministic-compute node in an execution graph."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(
        min_length=1,
        description="Unique result identifier produced by this execution node.",
    )
    purpose: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Human-readable business purpose of this execution node.",
    )
    kind: Literal["query", "compute"] = Field(
        description="Whether the node calls the provider or applies one local operator.",
    )
    input_result_ids: List[str] = Field(
        default_factory=list,
        max_length=8,
        description="Upstream node results required before this node can execute.",
    )
    query: Optional[DataQuery] = Field(
        default=None,
        description="Provider query executed by a query node.",
    )
    step: Optional[ResultPipelineStep] = Field(
        default=None,
        description="Single allowlisted relational operation executed by a compute node.",
    )
    fanout_input_field: Optional[str] = Field(
        default=None,
        pattern=OPERATION_NAME_PATTERN,
        description="Upstream field whose distinct values drive a query-node fan-out.",
    )
    fanout_param: Optional[str] = Field(
        default=None,
        pattern=OPERATION_NAME_PATTERN,
        description="Provider parameter populated from each fan-out input value.",
    )

    @model_validator(mode="after")
    def validate_node_contract(self) -> "ExecutionNode":
        """Require an exact query or compute payload and complete fan-out metadata."""
        if self.kind == "query":
            if self.query is None or self.step is not None:
                raise ValueError("query nodes require query and prohibit step")
            has_fanout_field = self.fanout_input_field is not None
            has_fanout_param = self.fanout_param is not None
            if has_fanout_field != has_fanout_param:
                raise ValueError("query-node fan-out requires both field and parameter")
            if has_fanout_field and len(self.input_result_ids) != 1:
                raise ValueError("fan-out query nodes require exactly one upstream result")
            if not has_fanout_field and self.input_result_ids:
                raise ValueError("ordinary query nodes cannot depend on upstream results")
        else:
            if self.step is None or self.query is not None:
                raise ValueError("compute nodes require step and prohibit query")
            if not self.input_result_ids:
                raise ValueError("compute nodes require at least one upstream result")
            if self.fanout_input_field is not None or self.fanout_param is not None:
                raise ValueError("compute nodes cannot declare provider fan-out")
        return self


class ExecutionPlan(BaseModel):
    """Validated directed acyclic graph of provider and compute nodes."""

    model_config = ConfigDict(extra="forbid")

    nodes: List[ExecutionNode] = Field(
        min_length=1,
        max_length=32,
        description="Execution nodes whose dependencies form one bounded DAG.",
    )
    result_node_id: str = Field(
        min_length=1,
        description="Node identifier containing the final user-visible result.",
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
    intent: Optional[AnalysisIntent] = Field(
        default=None,
        description="High-level intent when compiling deterministic execution paths.",
    )
    answer_contract: Optional[AnswerContract] = Field(
        default=None,
        description="Exact final result fields required to answer the user's request.",
    )
    feasibility: Literal["supported", "unsupported"] = Field(
        default="supported",
        description="Whether the complete request can be fulfilled without guessing.",
    )
    requirements: List[RequirementCoverage] = Field(
        default_factory=list,
        description="Coverage evidence for each atomic user requirement.",
    )
    constraints: List[QueryConstraint] = Field(
        default_factory=list,
        description=(
            "Machine-verifiable bindings for every explicit universe or result "
            "selection predicate in the request."
        ),
    )
    limitations: List[str] = Field(
        default_factory=list,
        description="Concrete missing capabilities that prevent faithful execution.",
    )
    clarification_options: List[str] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "Alternative complete prompts that resolve material ambiguity in the "
            "original request and can be submitted without further editing."
        ),
    )
    result_pipeline: Optional[ResultPipeline] = Field(
        default=None,
        description="Optional deterministic relational pipeline over one query result.",
    )
    queries: List[DataQuery] = Field(
        default_factory=list,
        description="Ordered provider-native reads required to satisfy the request.",
    )
    execution_plan: Optional[ExecutionPlan] = Field(
        default=None,
        description="Optional dependency graph for arbitrary multi-stage analysis.",
    )

    @model_validator(mode="after")
    def validate_feasibility(self) -> "QueryPlan":
        """Reject executable unsupported plans and empty supported plans."""
        if self.feasibility == "supported" and not (
            self.queries or self.execution_plan or self.intent
        ):
            raise ValueError(
                "supported plans must contain queries, an execution plan, or intent"
            )
        if self.feasibility == "unsupported":
            if self.queries or self.execution_plan:
                raise ValueError(
                    "unsupported plans must not contain queries or execution plans"
                )
            if not self.limitations:
                raise ValueError("unsupported plans must explain their limitations")
        if self.execution_plan is not None and (
            self.queries or self.result_pipeline or self.intent
        ):
            raise ValueError(
                "execution plans cannot be combined with legacy queries, pipelines, or intent"
            )
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


class CalculationTraceStep(BaseModel):
    """One deterministic operation that contributes to a displayed result."""

    model_config = ConfigDict(extra="forbid")

    operation: str = Field(
        min_length=1,
        pattern=OPERATION_NAME_PATTERN,
        description="Allowlisted operation that was actually executed.",
    )
    input_fields: List[str] = Field(
        default_factory=list,
        description="Existing fields read by this operation.",
    )
    output_fields: List[str] = Field(
        default_factory=list,
        description="New fields produced by this operation.",
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Validated non-field parameters that control the operation.",
    )


class SummaryMetricMetadata(BaseModel):
    """Calculation and display semantics for one summary metric."""

    model_config = ConfigDict(extra="forbid")

    output_field: str = Field(
        min_length=1,
        pattern=OPERATION_NAME_PATTERN,
        description="Stable machine-readable field containing the summary value.",
    )
    source_field: str = Field(
        min_length=1,
        pattern=OPERATION_NAME_PATTERN,
        description="Input field aggregated to produce the summary value.",
    )
    function: AggregationFunction = Field(
        description="Aggregation applied to the source field.",
    )
    value_format: Literal["number", "percentage_points", "ratio"] = Field(
        description="Formatting and scaling semantics for the numeric value.",
    )
    formula: str = Field(
        default="",
        description="Deterministic expression evaluated to produce the metric.",
    )
    source_fields: List[str] = Field(
        default_factory=list,
        description="Provider or source-result fields used by the expression.",
    )
    calculation_steps: List[CalculationTraceStep] = Field(
        default_factory=list,
        description="Ordered operations executed before the final aggregation.",
    )
    initial_sample_count: Optional[int] = Field(
        default=None,
        ge=0,
        description="Rows available before the result pipeline was executed.",
    )
    valid_sample_count: Optional[int] = Field(
        default=None,
        ge=0,
        description="Non-null observations consumed by the final aggregation.",
    )


class ColumnCalculationMetadata(BaseModel):
    """Deterministic calculation semantics for one generated result column."""

    model_config = ConfigDict(extra="forbid")

    formula: str = Field(
        min_length=1,
        description="Deterministic expression evaluated to produce the column.",
    )
    source_fields: List[str] = Field(
        default_factory=list,
        description="Provider or source-result fields used by the expression.",
    )
    calculation_steps: List[CalculationTraceStep] = Field(
        default_factory=list,
        description="Ordered operations executed to produce the column.",
    )
    value_format: Literal["number", "percentage_points", "ratio"] = Field(
        description="Formatting and scaling semantics for the column values.",
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
    summary_metadata: Dict[str, SummaryMetricMetadata] = Field(
        default_factory=dict,
        description="Display and calculation metadata for each summary entry.",
    )
    column_metadata: Dict[str, ColumnCalculationMetadata] = Field(
        default_factory=dict,
        description="Calculation metadata keyed by generated result column.",
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

    target_pool: Literal["A_SHARE"] = Field(description="Validated research universe.")
    train_start: str = Field(description="Start date for training (YYYYMMDD).")
    train_end: str = Field(description="Last training signal date (YYYYMMDD).")
    val_start: str = Field(description="Start date for validation blind test (YYYYMMDD).")
    val_end: str = Field(
        description=(
            "Last validation signal date (YYYYMMDD); the requested forward-return "
            "horizon must already have settled after this date."
        )
    )
    factors: List[str] = Field(description="Selected base factor fields to use.", default_factory=list)
    prompt: str = Field(
        description="Optional research note stored with the deterministic request.",
        default="",
    )
    max_generations: int = Field(
        default=1,
        ge=1,
        le=1,
        description="Supported deterministic search pass count; currently fixed at one.",
    )
    forward_days: int = Field(
        default=20,
        ge=1,
        le=60,
        description="Trading sessions between the signal close and outcome close.",
    )
    target_return_pct: float = Field(
        default=0.0,
        ge=-100.0,
        le=1000.0,
        description="Forward return threshold, in percentage points, defining a hit.",
    )
    minimum_samples: int = Field(
        default=30,
        ge=5,
        le=10000,
        description="Minimum observations required in each evaluation window.",
    )
    minimum_trading_days: int = Field(
        default=20,
        ge=2,
        le=1000,
        description="Minimum distinct signal dates required for each rule and window.",
    )
    minimum_securities: int = Field(
        default=10,
        ge=2,
        le=1000,
        description="Minimum distinct securities required for each rule and window.",
    )
    minimum_outcome_coverage_pct: float = Field(
        default=95.0,
        ge=50.0,
        le=100.0,
        description="Minimum percentage of matched signals with observable outcomes.",
    )
    max_conditions: int = Field(
        default=2,
        ge=1,
        le=2,
        description="Maximum number of conditions in one discovered rule.",
    )

    @model_validator(mode="after")
    def validate_research_windows(self) -> "DiscoveryTaskRequest":
        """Require ordered, non-overlapping research windows."""
        try:
            train_start = datetime.strptime(self.train_start, "%Y%m%d")
            train_end = datetime.strptime(self.train_end, "%Y%m%d")
            val_start = datetime.strptime(self.val_start, "%Y%m%d")
            val_end = datetime.strptime(self.val_end, "%Y%m%d")
        except ValueError as exc:
            raise ValueError("Discovery dates must use YYYYMMDD format.") from exc
        if train_start > train_end:
            raise ValueError("Training start must not be after training end.")
        if val_start > val_end:
            raise ValueError("Validation start must not be after validation end.")
        if val_start <= train_end:
            raise ValueError("Validation window must start after the training window.")
        if self.minimum_trading_days > self.minimum_samples:
            raise ValueError(
                "Minimum trading days cannot exceed minimum samples."
            )
        if self.minimum_securities > self.minimum_samples:
            raise ValueError(
                "Minimum securities cannot exceed minimum samples."
            )
        if not self.factors:
            raise ValueError("At least one discovery factor is required.")
        if len(self.factors) != len(set(self.factors)):
            raise ValueError("Discovery factors must be unique.")
        unsupported_factors = sorted(set(self.factors) - DISCOVERY_FACTOR_FIELDS)
        if unsupported_factors:
            raise ValueError(
                "Unsupported discovery factors: " + ", ".join(unsupported_factors)
            )
        return self


class DiscoveryResearchConfig(BaseModel):
    """Public immutable configuration used to reproduce a discovery task."""

    model_config = ConfigDict(extra="forbid")

    target_pool: Literal["A_SHARE"] = Field(
        description="Validated research universe."
    )
    train_start: str = Field(description="Training-window start date (YYYYMMDD).")
    train_end: str = Field(description="Last training-window signal date (YYYYMMDD).")
    val_start: str = Field(description="Validation-window start date (YYYYMMDD).")
    val_end: str = Field(
        description=(
            "Last validation-window signal date (YYYYMMDD), before the separate "
            "forward-return settlement horizon."
        )
    )
    factors: List[str] = Field(description="Requested factor fields in search order.")
    forward_days: int = Field(description="Trading-session forward-return horizon.")
    target_return_pct: float = Field(
        description="Return threshold in percentage points."
    )
    minimum_samples: int = Field(description="Minimum event observations per window.")
    minimum_trading_days: int = Field(
        description="Minimum distinct signal dates per window."
    )
    minimum_securities: int = Field(
        description="Minimum distinct securities per window."
    )
    minimum_outcome_coverage_pct: float = Field(
        description="Minimum observable outcome percentage."
    )
    max_conditions: int = Field(description="Maximum conditions in one rule.")


class DiscoveryEventExample(BaseModel):
    """One bounded matched event retained for manual research auditing."""

    model_config = ConfigDict(extra="forbid")

    trade_date: str = Field(
        description="Signal market date in YYYYMMDD format.",
    )
    ts_code: Optional[str] = Field(
        default=None,
        description="Security code when the research dataset contains one.",
    )
    future_trade_date: Optional[str] = Field(
        default=None,
        description="Market date used to settle the forward-return label.",
    )
    forward_return: float = Field(
        description="Observed split-and-dividend-adjusted forward return as a ratio.",
    )
    factor_values: Dict[str, float] = Field(
        default_factory=dict,
        description="Finite signal-date values for only the factors referenced by the rule.",
    )


class BacktestResult(BaseModel):
    """Performance evaluation of a single factor formula."""
    
    model_config = ConfigDict(extra="forbid")

    win_rate: float
    mean_return: float
    max_drawdown: Optional[float] = Field(
        default=None,
        description=(
            "Portfolio drawdown, unavailable for overlapping event-endpoint returns "
            "without a self-financing position path."
        ),
    )
    eval_time_ms: int
    sample_count: int = Field(default=0, ge=0)
    matched_sample_count: int = Field(default=0, ge=0)
    eligible_sample_count: int = Field(
        default=0,
        ge=0,
        description="Events with finite values for every factor referenced by the rule.",
    )
    rule_support_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Share of factor-eligible events matched by the rule.",
    )
    missing_outcome_count: int = Field(default=0, ge=0)
    outcome_coverage_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    positive_count: int = Field(default=0, ge=0)
    median_return: float = 0.0
    return_p05: float = Field(
        default=0.0,
        description="Fifth percentile of event-level forward returns.",
    )
    return_std: float = 0.0
    baseline_win_rate: float = Field(
        default=0.0,
        description=(
            "Hit rate across all factor-comparable events, including events "
            "selected by the rule; this is not an unmatched control-group rate."
        ),
    )
    baseline_sample_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Finite outcome observations across all events with every rule factor "
            "available, including rule-selected observations."
        ),
    )
    baseline_outcome_coverage_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Share of factor-comparable baseline events with observable outcomes."
        ),
    )
    win_rate_lift: float = Field(
        default=0.0,
        description=(
            "Rule hit rate minus the inclusive factor-comparable baseline hit rate."
        ),
    )
    outcome_robust_lift_lower: float = Field(
        default=-1.0,
        ge=-1.0,
        le=1.0,
        description=(
            "Worst-case win-rate lift when selected missing outcomes fail and "
            "non-selected baseline missing outcomes succeed."
        ),
    )
    outcome_robust_lift_upper: float = Field(
        default=1.0,
        ge=-1.0,
        le=1.0,
        description=(
            "Best-case win-rate lift when selected missing outcomes succeed and "
            "non-selected baseline missing outcomes fail."
        ),
    )
    lift_confidence_lower: float = Field(
        default=-1.0,
        ge=-1.0,
        le=1.0,
        description="Conservative lower 95% bound for win-rate lift over baseline.",
    )
    lift_confidence_upper: float = Field(
        default=1.0,
        ge=-1.0,
        le=1.0,
        description="Conservative upper 95% bound for win-rate lift over baseline.",
    )
    confidence_lower: float = 0.0
    confidence_upper: float = 0.0
    target_return: float = 0.0
    trading_day_count: int = Field(default=0, ge=0)
    effective_trading_day_count: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Kish effective count of signal dates after accounting for unequal "
            "event concentration across dates."
        ),
    )
    security_count: int = Field(
        default=0,
        ge=0,
        description="Distinct securities represented by observable rule outcomes.",
    )
    effective_security_count: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Kish effective security count after accounting for unequal event "
            "concentration across securities."
        ),
    )
    max_security_event_share: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Largest share of observable rule events contributed by one security.",
    )
    max_signal_date_event_share: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Largest share of observable rule events contributed by one signal date.",
    )
    cluster_standard_error: float = Field(default=0.0, ge=0.0)
    lift_standard_error: float = Field(default=0.0, ge=0.0)
    dependence_lag_days: int = Field(default=0, ge=0)
    return_price_basis: str = Field(
        default="split_and_dividend_adjusted_close",
        description="Price basis used to calculate forward returns.",
    )
    event_examples: List[DiscoveryEventExample] = Field(
        default_factory=list,
        description="Bounded recent matched events retained for manual auditing.",
    )


class FactorHypothesis(BaseModel):
    """A generated formula and its rationale."""
    
    model_config = ConfigDict(extra="forbid")

    formula: str
    description: str
    reasoning: str
    threshold_source: Literal[
        "unknown",
        "quantile",
        "observed_value",
        "mixed",
    ] = Field(
        default="unknown",
        description="Training-only source used to generate the rule thresholds.",
    )
    train_result: Optional[BacktestResult] = None
    val_result: Optional[BacktestResult] = None
    validation_score: float = Field(
        default=0.0,
        description="Validation lift after one-sided uncertainty and stability penalties.",
    )
    generalization_gap: float = Field(
        default=0.0,
        ge=0.0,
        description="Absolute train-to-validation win-rate-lift difference.",
    )
    support_rate_gap: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Absolute train-to-validation rule-support-rate difference.",
    )
    support_retention_ratio: float = Field(
        default=0.0,
        ge=0.0,
        description="Validation rule support divided by training rule support.",
    )
    p_value: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "One-sided Student-t probability for positive validation lift using "
            "date-clustered uncertainty."
        ),
    )
    q_value: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Benjamini-Yekutieli adjusted validation significance for an "
            "arbitrarily dependent candidate family."
        ),
    )
    fdr_family_size: int = Field(
        default=0,
        ge=0,
        description="Number of frozen candidates included in FDR correction.",
    )
    validation_passed: bool = Field(
        default=False,
        description=(
            "Whether both windows have positive lift, validation satisfies all "
            "evidence thresholds, and validation passes FDR."
        ),
    )
    validation_reason: Literal[
        "not_evaluated",
        "training_lift_not_positive",
        "training_outcome_attrition_not_robust",
        "insufficient_validation_samples",
        "insufficient_validation_days",
        "insufficient_validation_effective_days",
        "insufficient_validation_securities",
        "insufficient_validation_effective_securities",
        "insufficient_validation_coverage",
        "insufficient_validation_baseline_coverage",
        "validation_lift_not_positive",
        "validation_outcome_attrition_not_robust",
        "insufficient_significance_days",
        "fdr_not_passed",
        "passed",
    ] = Field(
        default="not_evaluated",
        description="Machine-readable reason for the validation decision.",
    )


class DiscoveryTaskProgress(BaseModel):
    """Live progress data for the evolution loop."""
    
    model_config = ConfigDict(extra="forbid")

    current_generation: int = 0
    total_generations: int = 0
    formulas_tested: int = 0
    candidates_evaluated: int = Field(default=0, ge=0)
    current_log: str = ""
    current_stage: str = Field(
        default="queued",
        description="Stable research stage displayed while the task runs.",
    )
    training_sample_count: int = Field(default=0, ge=0)
    training_samples_purged: int = Field(
        default=0,
        ge=0,
        description="Training observations removed because labels overlap validation.",
    )
    validation_sample_count: int = Field(default=0, ge=0)
    training_factor_coverage: Dict[str, float] = Field(
        default_factory=dict,
        description="Finite-value coverage by requested factor in training data.",
    )
    validation_factor_coverage: Dict[str, float] = Field(
        default_factory=dict,
        description="Finite-value coverage by requested factor in validation data.",
    )
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
    """Public task state with a non-sensitive reproducibility snapshot."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(description="Stable task identifier.")
    status: AnalysisTaskStatus = Field(description="Current task lifecycle state.")
    research_config: DiscoveryResearchConfig = Field(
        description="Immutable non-sensitive configuration used by the task."
    )
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

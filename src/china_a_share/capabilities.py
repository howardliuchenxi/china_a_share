"""Runtime analysis-capability manifest derived from executable registrations."""

import hashlib
import json
import os
from typing import Any, Dict, Iterable, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from china_a_share.registry import READ_ONLY_API_NAMES, TUSHARE_API_CATEGORIES


class ProviderQueryShape(BaseModel):
    """One provider request shape whose execution semantics are locally audited."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    shape_id: str = Field(description="Stable identifier for the audited request shape.")
    required_params: Tuple[str, ...] = Field(
        description="Provider parameters that must all be present for this shape."
    )
    execution_strategy: str = Field(
        description="Deterministic execution strategy used for this request shape."
    )
    completeness_policy: str = Field(
        description="Rule used to prove that successful retrieval is complete."
    )


class ProviderOperationCapability(BaseModel):
    """Machine-verifiable provider contract for one connected operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str = Field(description="Provider-native operation name.")
    allowed_params: Tuple[str, ...] = Field(
        description="Complete allowlist of provider parameters accepted by the planner."
    )
    date_pair: Optional[Tuple[str, str]] = Field(
        default=None,
        description="Date parameters that must be supplied together when either is used.",
    )
    query_shapes: Tuple[ProviderQueryShape, ...] = Field(
        description="Audited parameter shapes accepted for execution."
    )
    page_size: Optional[int] = Field(
        default=None,
        ge=1,
        description="Documented provider row limit used by fail-closed pagination.",
    )
    unique_key: Tuple[str, ...] = Field(
        default_factory=tuple,
        description="Fields that identify one logical provider row when known.",
    )


COMMON_PAGINATION_PARAMS = ("limit", "offset")


PROVIDER_OPERATION_CAPABILITIES: Dict[str, ProviderOperationCapability] = {
    "stk_holdernumber": ProviderOperationCapability(
        operation="stk_holdernumber",
        allowed_params=(
            "ts_code",
            "ann_date",
            "enddate",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="security_history",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="announcement_snapshot",
                required_params=("ann_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="reporting_snapshot",
                required_params=("enddate",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="bounded_range",
                required_params=("start_date", "end_date"),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=3_000,
        unique_key=("ts_code", "ann_date", "end_date"),
    ),
    "block_trade": ProviderOperationCapability(
        operation="block_trade",
        allowed_params=(
            "ts_code",
            "trade_date",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="market_snapshot",
                required_params=("trade_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="bounded_range",
                required_params=("start_date", "end_date"),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=1_000,
        unique_key=("ts_code", "trade_date", "price", "buyer", "seller"),
    ),
    "stock_basic": ProviderOperationCapability(
        operation="stock_basic",
        allowed_params=(
            "ts_code",
            "name",
            "exchange",
            "market",
            "is_hs",
            "list_status",
            *COMMON_PAGINATION_PARAMS,
        ),
        query_shapes=(
            ProviderQueryShape(
                shape_id="listed_security_universe",
                required_params=("list_status",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="all_security_reference",
                required_params=(),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=6_000,
        unique_key=("ts_code",),
    ),
    "daily": ProviderOperationCapability(
        operation="daily",
        allowed_params=(
            "ts_code",
            "trade_date",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="market_snapshot",
                required_params=("trade_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="bounded_range",
                required_params=("start_date", "end_date"),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=6_000,
        unique_key=("ts_code", "trade_date"),
    ),
    "daily_basic": ProviderOperationCapability(
        operation="daily_basic",
        allowed_params=(
            "ts_code",
            "trade_date",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="market_snapshot",
                required_params=("trade_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="bounded_range",
                required_params=("start_date", "end_date"),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=6_000,
        unique_key=("ts_code", "trade_date"),
    ),
    "limit_list_d": ProviderOperationCapability(
        operation="limit_list_d",
        allowed_params=(
            "trade_date",
            "ts_code",
            "limit_type",
            "exchange",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="market_snapshot",
                required_params=("trade_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="bounded_range",
                required_params=("start_date", "end_date"),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=5_000,
        unique_key=("ts_code", "trade_date", "limit_type"),
    ),
    "moneyflow": ProviderOperationCapability(
        operation="moneyflow",
        allowed_params=(
            "ts_code",
            "trade_date",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="market_snapshot",
                required_params=("trade_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=6_000,
        unique_key=("ts_code", "trade_date"),
    ),
    "weekly": ProviderOperationCapability(
        operation="weekly",
        allowed_params=(
            "ts_code",
            "trade_date",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="market_snapshot",
                required_params=("trade_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=6_000,
        unique_key=("ts_code", "trade_date"),
    ),
    "monthly": ProviderOperationCapability(
        operation="monthly",
        allowed_params=(
            "ts_code",
            "trade_date",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="market_snapshot",
                required_params=("trade_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=6_000,
        unique_key=("ts_code", "trade_date"),
    ),
    "margin_detail": ProviderOperationCapability(
        operation="margin_detail",
        allowed_params=(
            "trade_date",
            "ts_code",
            "exchange",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="market_snapshot",
                required_params=("trade_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=6_000,
        unique_key=("ts_code", "trade_date"),
    ),
    "margin_secs": ProviderOperationCapability(
        operation="margin_secs",
        allowed_params=(
            "trade_date",
            "ts_code",
            "exchange",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="exchange_snapshot",
                required_params=("trade_date", "exchange"),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="market_snapshot",
                required_params=("trade_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=6_000,
        unique_key=("ts_code", "trade_date"),
    ),
    "dividend": ProviderOperationCapability(
        operation="dividend",
        allowed_params=(
            "ts_code",
            "ann_date",
            "record_date",
            "ex_date",
            "imp_ann_date",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="announcement_date",
                required_params=("ann_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="bounded_announcement_date_range",
                required_params=("start_date", "end_date"),
                execution_strategy="exact_ann_date_fanout",
                completeness_policy="all_dates_complete",
            ),
            ProviderQueryShape(
                shape_id="security_fanout_template",
                required_params=(),
                execution_strategy="security_fanout",
                completeness_policy="all_security_queries_complete",
            ),
        ),
        page_size=5_000,
        unique_key=("ts_code", "end_date", "ann_date", "div_proc"),
    ),
    "repurchase": ProviderOperationCapability(
        operation="repurchase",
        allowed_params=(
            "ts_code",
            "ann_date",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        query_shapes=(
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="announcement_date",
                required_params=("ann_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="bounded_range",
                required_params=("start_date", "end_date"),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=2_000,
        unique_key=("ts_code", "ann_date", "end_date", "proc"),
    ),
    "suspend_d": ProviderOperationCapability(
        operation="suspend_d",
        allowed_params=(
            "ts_code",
            "trade_date",
            "suspend_type",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="market_snapshot",
                required_params=("trade_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="bounded_range",
                required_params=("start_date", "end_date"),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=5_000,
        unique_key=("ts_code", "trade_date", "suspend_type"),
    ),
    "stk_holdertrade": ProviderOperationCapability(
        operation="stk_holdertrade",
        allowed_params=(
            "ts_code",
            "ann_date",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        query_shapes=(
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="announcement_date",
                required_params=("ann_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="bounded_range",
                required_params=("start_date", "end_date"),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="security_fanout_template",
                required_params=(),
                execution_strategy="security_fanout",
                completeness_policy="all_security_queries_complete",
            ),
        ),
        page_size=5_000,
        unique_key=("ts_code", "ann_date", "holder_name", "in_de"),
    ),
    "fina_mainbz": ProviderOperationCapability(
        operation="fina_mainbz",
        allowed_params=(
            "ts_code",
            "period",
            "type",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
        ),
        page_size=100,
        unique_key=("ts_code", "end_date", "bz_item", "curr_type"),
    ),
    "share_float": ProviderOperationCapability(
        operation="share_float",
        allowed_params=(
            "ts_code",
            "ann_date",
            "float_date",
            "start_date",
            "end_date",
            *COMMON_PAGINATION_PARAMS,
        ),
        date_pair=("start_date", "end_date"),
        query_shapes=(
            ProviderQueryShape(
                shape_id="security",
                required_params=("ts_code",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="announcement_date",
                required_params=("ann_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="unlock_date",
                required_params=("float_date",),
                execution_strategy="provider_query",
                completeness_policy="paginate_until_short_page",
            ),
            ProviderQueryShape(
                shape_id="bounded_unlock_range",
                required_params=("start_date", "end_date"),
                execution_strategy="exact_float_date_fanout",
                completeness_policy="all_dates_complete",
            ),
        ),
        page_size=5_000,
        unique_key=("ts_code", "float_date", "holder_name", "share_type"),
    ),
}


def get_operation_capability(
    operation: str,
) -> Optional[ProviderOperationCapability]:
    """Return the audited provider contract for an operation when registered."""
    return PROVIDER_OPERATION_CAPABILITIES.get(operation)


def resolve_query_shape(
    operation: str,
    params: Dict[str, Any],
) -> Optional[ProviderQueryShape]:
    """Resolve one audited request shape or reject an invalid registered request."""
    capability = get_operation_capability(operation)
    if capability is None:
        return None
    invalid_params = sorted(set(params).difference(capability.allowed_params))
    if invalid_params:
        raise ValueError(
            f"{operation} uses unsupported provider parameters: "
            + ", ".join(invalid_params)
        )
    if capability.date_pair is not None:
        start_param, end_param = capability.date_pair
        has_start = bool(params.get(start_param))
        has_end = bool(params.get(end_param))
        if has_start != has_end:
            raise ValueError(
                f"{operation} requires {start_param} and {end_param} together."
            )
    matching = [
        shape
        for shape in capability.query_shapes
        if all(params.get(name) not in (None, "") for name in shape.required_params)
    ]
    if not matching:
        if operation in {"weekly", "monthly"}:
            raise ValueError(f"{operation} requires ts_code or trade_date.")
        expected = " or ".join(
            "+".join(shape.required_params) for shape in capability.query_shapes
        )
        raise ValueError(f"{operation} requires one audited query shape: {expected}.")
    return matching[0]


ANALYSIS_CAPABILITIES: Tuple[Dict[str, Any], ...] = (
    {
        "id": "limit_up_streak",
        "version": 1,
        "description": (
            "Detect a variable number of consecutive A-share limit-up trading "
            "sessions and optionally measure a later historical outcome."
        ),
        "compiler": "limit_up_streak",
        "required_operations": ("daily", "limit_list_d"),
        "parameters": {
            "date_range": {"required": True, "type": "date_range"},
            "streak_length": {
                "required": True,
                "type": "positive_integer",
                "minimum": 1,
            },
            "forward_horizon": {
                "required": False,
                "type": "historical_offset",
            },
        },
        "handles": (
            "trading_calendar",
            "suspensions",
            "board_specific_price_limits",
            "special_treatment_price_limits",
        ),
        "constraints": ("historical_data_only",),
    },
)


def build_capability_manifest(
    provider: Any,
    compilers: Dict[str, Any],
) -> Dict[str, Any]:
    """Build and validate the manifest against the active provider instance."""
    declared_operations = getattr(provider, "operation_names", None)
    has_explicit_catalog = declared_operations is not None
    if not has_explicit_catalog:
        declared_operations = tuple(
            operation.name for operation in provider.search_operations("capability audit")
        )
    provider_operations = tuple(
        operation for operation in declared_operations if provider.supports(operation)
    )
    unconnected_operations = tuple(
        operation for operation in declared_operations if operation not in provider_operations
    )
    if has_explicit_catalog and unconnected_operations:
        raise RuntimeError(
            "The active provider declares operations that it cannot execute: "
            f"{list(unconnected_operations)}"
        )

    registered_capabilities = tuple(
        _normalize_capability(capability)
        for capability in ANALYSIS_CAPABILITIES
    )
    _validate_capability_compilers(registered_capabilities, compilers)
    capability_rows = tuple(
        capability
        for capability in registered_capabilities
        if set(capability["required_operations"]).issubset(provider_operations)
    )
    _validate_capability_dependencies(capability_rows, provider_operations)
    content = {
        "schema_version": 1,
        "code_revision": os.getenv("APP_GIT_SHA", "").strip(),
        "provider": provider.name,
        "provider_operations": provider_operations,
        "provider_operation_count": len(provider_operations),
        "tushare_catalog_operation_count": len(READ_ONLY_API_NAMES),
        "tushare_catalog_fully_connected": (
            provider.name != "tushare"
            or set(provider_operations) == set(READ_ONLY_API_NAMES)
        ),
        "tushare_category_coverage": {
            category: {
                "documented": len(operations),
                "connected": sum(
                    operation in provider_operations for operation in operations
                ),
            }
            for category, operations in TUSHARE_API_CATEGORIES.items()
        },
        "provider_operation_capabilities": {
            operation: capability.model_dump(mode="json")
            for operation, capability in PROVIDER_OPERATION_CAPABILITIES.items()
            if operation in provider_operations
        },
        "capabilities": capability_rows,
    }
    serialized = json.dumps(
        content,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **content,
        "fingerprint": f"sha256:{hashlib.sha256(serialized).hexdigest()}",
    }


def build_capability_guidance() -> str:
    """Return compact planner guidance from the executable capability registry."""
    lines = []
    for capability in ANALYSIS_CAPABILITIES:
        parameters = ", ".join(
            f"{name} ({'required' if definition['required'] else 'optional'})"
            for name, definition in capability["parameters"].items()
        )
        lines.append(
            f"- {capability['id']} v{capability['version']}: "
            f"{capability['description']} Parameters: {parameters}. "
            f"Required provider operations: "
            f"{', '.join(capability['required_operations'])}."
        )
    return "\n".join(lines)


def _normalize_capability(capability: Dict[str, Any]) -> Dict[str, Any]:
    """Convert immutable registration values into JSON-stable values."""
    return json.loads(
        json.dumps(capability, ensure_ascii=True, sort_keys=True)
    )


def _validate_capability_dependencies(
    capabilities: Iterable[Dict[str, Any]],
    provider_operations: Iterable[str],
) -> None:
    """Fail fast when one registered capability has an unavailable dependency."""
    available = set(provider_operations)
    identifiers = set()
    for capability in capabilities:
        capability_id = capability["id"]
        if capability_id in identifiers:
            raise RuntimeError(f"Duplicate analysis capability: {capability_id}")
        identifiers.add(capability_id)
        missing = sorted(
            set(capability["required_operations"]).difference(available)
        )
        if missing:
            raise RuntimeError(
                f"Analysis capability {capability_id} has unavailable provider "
                f"operations: {missing}"
            )


def _validate_capability_compilers(
    capabilities: Iterable[Dict[str, Any]],
    compilers: Dict[str, Any],
) -> None:
    """Fail fast when a capability registration has no callable implementation."""
    registered = {capability["compiler"] for capability in capabilities}
    missing = sorted(
        compiler_id
        for compiler_id in registered
        if not callable(compilers.get(compiler_id))
    )
    extra = sorted(set(compilers).difference(registered))
    if missing or extra:
        raise RuntimeError(
            "Analysis capability compiler registration is inconsistent; "
            f"missing={missing}, extra={extra}"
        )

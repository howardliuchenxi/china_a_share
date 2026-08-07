"""Runtime analysis-capability manifest derived from executable registrations."""

import hashlib
import json
import os
from typing import Any, Dict, Iterable, Tuple

from china_a_share.registry import READ_ONLY_API_NAMES, TUSHARE_API_CATEGORIES


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

"""Validation for allowlisted A-share query plans."""

import re
from typing import Any

from .contracts import QueryPlan
from .registry import StockApiRegistry


MAX_QUERIES_PER_ANALYSIS = 8
VALID_SECURITY_SUFFIXES = (".SH", ".SZ", ".BJ")
VALID_EXCHANGES = {"", "SSE", "SZSE", "BSE"}
FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PlanValidationError(ValueError):
    """Raised when a model-generated plan violates local safety constraints."""


class ASharePlanValidator:
    """Enforce the API allowlist and the Shanghai, Shenzhen, and Beijing scope."""

    def __init__(self, registry: StockApiRegistry) -> None:
        self.registry = registry

    def validate(self, plan: QueryPlan) -> QueryPlan:
        """Return the plan after every query passes the A-share constraints."""
        if len(plan.queries) > MAX_QUERIES_PER_ANALYSIS:
            raise PlanValidationError(
                f"A query plan may contain at most {MAX_QUERIES_PER_ANALYSIS} calls."
            )
        query_ids = set()
        for query in plan.queries:
            if query.query_id in query_ids:
                raise PlanValidationError(f"Duplicate query_id: {query.query_id}")
            query_ids.add(query.query_id)
            if not self.registry.contains(query.api_name):
                raise PlanValidationError(
                    f"API is outside the Tushare stock allowlist: {query.api_name}"
                )
            for field in query.fields:
                if not FIELD_NAME_PATTERN.fullmatch(field):
                    raise PlanValidationError(f"Invalid output field: {field}")
            self._validate_params(query.params)
            for aggregation in query.aggregations:
                if query.fields and aggregation.field not in query.fields:
                    raise PlanValidationError(
                        f"Aggregation field is not requested: {aggregation.field}"
                    )
        return plan

    def _validate_params(self, params: Any) -> None:
        if not isinstance(params, dict):
            raise PlanValidationError("Tushare parameters must be a JSON object.")
        for name, value in params.items():
            if name == "exchange" and value not in VALID_EXCHANGES:
                raise PlanValidationError(f"Exchange is outside A-share scope: {value}")
            if name.endswith("ts_code") or name in {"ts_code", "con_code"}:
                self._validate_security_codes(value)

    def _validate_security_codes(self, value: Any) -> None:
        if value in (None, ""):
            return
        if not isinstance(value, str):
            raise PlanValidationError("Security codes must be strings.")
        for code in value.split(","):
            if "." in code and not code.endswith(VALID_SECURITY_SUFFIXES):
                raise PlanValidationError(
                    f"Security code is outside A-share scope: {code}"
                )

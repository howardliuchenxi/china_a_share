"""Date binding and result selection for frozen general strategy plans."""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any

from china_a_share.core.contracts import AnalysisResponse, QueryPlan, QueryResult
from china_a_share.discovery.strategy_models import CompiledStrategyRule


_COMPACT_DATE = re.compile(r"^\d{8}$")
_VARIABLE_DATE_KEYS = {
    "ann_date",
    "as_of",
    "begin_date",
    "cal_date",
    "date",
    "end",
    "end_date",
    "start",
    "start_date",
    "trade_date",
}
_DATE_FIELDS = {"ann_date", "cal_date", "trade_date"}


def _parse_compact_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or _COMPACT_DATE.fullmatch(value) is None:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None


def _iter_variable_dates(value: Any) -> list[str]:
    dates: list[str] = []
    if isinstance(value, dict):
        comparison_field = value.get("field")
        for key, child in value.items():
            parsed = _parse_compact_date(child)
            if parsed is not None and (
                key in _VARIABLE_DATE_KEYS
                or (comparison_field in _DATE_FIELDS and key in {"value", "min_value", "max_value"})
            ):
                dates.append(child)
            else:
                dates.extend(_iter_variable_dates(child))
    elif isinstance(value, list):
        for child in value:
            dates.extend(_iter_variable_dates(child))
    return dates


def plan_anchor_date(plan: QueryPlan, *, fallback: str) -> str:
    """Return the latest variable observation date encoded in a plan."""
    if _parse_compact_date(fallback) is None:
        raise ValueError("fallback must be a valid YYYYMMDD date")
    dates = _iter_variable_dates(plan.model_dump(mode="json"))
    return max(dates, default=fallback)


def _shift_variable_dates(value: Any, delta: timedelta) -> Any:
    if isinstance(value, dict):
        shifted: dict[str, Any] = {}
        comparison_field = value.get("field")
        for key, child in value.items():
            parsed = _parse_compact_date(child)
            if parsed is not None and (
                key in _VARIABLE_DATE_KEYS
                or (comparison_field in _DATE_FIELDS and key in {"value", "min_value", "max_value"})
            ):
                shifted[key] = (parsed + delta).strftime("%Y%m%d")
            else:
                shifted[key] = _shift_variable_dates(child, delta)
        return shifted
    if isinstance(value, list):
        return [_shift_variable_dates(child, delta) for child in value]
    return value


def bind_plan_date(rule: CompiledStrategyRule, target_date: str) -> QueryPlan:
    """Clone a frozen plan while shifting only its declared observation dates."""
    anchor = _parse_compact_date(rule.anchor_date)
    target = _parse_compact_date(target_date)
    if anchor is None or target is None:
        raise ValueError("strategy plan dates must use valid YYYYMMDD values")
    payload = _shift_variable_dates(
        rule.plan.model_dump(mode="json"),
        target - anchor,
    )
    return QueryPlan.model_validate(payload)


def answer_result(response: AnalysisResponse) -> QueryResult | None:
    """Return the exact result declared by a frozen plan's answer contract."""
    if response.plan is None:
        return None
    result_id = None
    if response.plan.answer_contract is not None:
        result_id = response.plan.answer_contract.result_query_id
    elif response.plan.execution_plan is not None:
        result_id = response.plan.execution_plan.result_node_id
    elif response.plan.result_pipeline is not None:
        result_id = response.plan.result_pipeline.output_query_id
    elif len(response.results) == 1:
        return response.results[0]
    if result_id is None:
        return None
    return next(
        (result for result in response.results if result.query_id == result_id),
        None,
    )

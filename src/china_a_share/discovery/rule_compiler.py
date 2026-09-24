"""Compile natural-language strategy rules into validated reusable plans."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Optional, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from china_a_share.application.workflow import AnalysisService
from china_a_share.core.contracts import AnalysisConversationTurn, AnalysisRequest
from china_a_share.discovery.strategy_models import CompiledStrategyRule
from china_a_share.discovery.strategy_plan import plan_anchor_date


logger = logging.getLogger(__name__)
SHANGHAI_TIME_ZONE = ZoneInfo("Asia/Shanghai")
STRATEGY_PLAN_API_ROUTE = "/api/analysis/tasks/strategy:compile"


@dataclass(frozen=True)
class RuleCompileResult:
    """Validated compilation, or concrete reasons why execution is not ready."""

    compiled_rule: Optional[CompiledStrategyRule] = None
    clarification_options: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


class RuleCompiler(Protocol):
    """Compile free text into one reusable execution contract."""

    def compile(
        self,
        text: str,
        conversation: Optional[list[AnalysisConversationTurn]] = None,
    ) -> Optional[RuleCompileResult]:
        """Compile one free-text description or return None when unavailable."""
        ...


class AnalysisRuleCompiler:
    """Reuse the general analysis planner and validator for saved strategies."""

    def __init__(
        self,
        service: AnalysisService,
        *,
        clock=None,
    ) -> None:
        self._service = service
        self._clock = clock or (lambda: datetime.now(SHANGHAI_TIME_ZONE))

    def compile(
        self,
        text: str,
        conversation: Optional[list[AnalysisConversationTurn]] = None,
    ) -> Optional[RuleCompileResult]:
        """Return a frozen validated plan without issuing its market-data queries."""
        request_id = f"strategy-compile-{uuid4().hex}"
        try:
            response = self._service.analyze(
                request_id,
                AnalysisRequest(
                    prompt=text,
                    conversation=conversation or [],
                    mode="plan",
                ),
                api_route=STRATEGY_PLAN_API_ROUTE,
            )
        except Exception:
            logger.exception("strategy_rule_compile_failed request_id=%s", request_id)
            return None
        plan = response.plan
        if plan is None:
            limitations = [response.error.message] if response.error is not None else []
            return RuleCompileResult(limitations=limitations)
        if plan.feasibility != "supported":
            return RuleCompileResult(
                clarification_options=list(plan.clarification_options),
                limitations=list(plan.limitations),
            )
        anchor_date = plan_anchor_date(
            plan,
            fallback=self._clock().astimezone(SHANGHAI_TIME_ZONE).strftime("%Y%m%d"),
        )
        return RuleCompileResult(
            compiled_rule=CompiledStrategyRule.create(
                source_text=text,
                plan=plan,
                anchor_date=anchor_date,
            )
        )

"""Tests for reusable natural-language strategy plan compilation."""

from datetime import datetime
from zoneinfo import ZoneInfo

from china_a_share.core.contracts import (
    AnalysisConversationTurn,
    AnalysisResponse,
    QueryPlan,
)
from china_a_share.discovery.rule_compiler import AnalysisRuleCompiler
from china_a_share.discovery.strategy_plan import bind_plan_date, plan_anchor_date


class FakeAnalysisService:
    """Return one prepared plan while recording the plan-only request."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def analyze(self, request_id, request, *, api_route):
        self.calls.append((request_id, request, api_route))
        return self.response


def executable_plan():
    return QueryPlan.model_validate(
        {
            "interpretation": "Screen the requested universe with a reusable event rule.",
            "feasibility": "supported",
            "queries": [
                {
                    "query_id": "daily_prices",
                    "operation": "daily",
                    "params": {"start_date": "20260901", "end_date": "20260922"},
                    "fields": ["ts_code", "trade_date", "close"],
                    "purpose": "Load the bounded price history.",
                }
            ],
            "answer_contract": {
                "result_query_id": "daily_prices",
                "result_kind": "table",
                "outputs": [
                    {"field": "ts_code", "description": "Security code."},
                    {"field": "trade_date", "description": "Signal date."},
                ],
            },
        }
    )


def response_for(plan):
    return AnalysisResponse(
        request_id="request-1",
        planner="fake-planner",
        data_provider="fake-provider",
        status="success" if plan.feasibility == "supported" else "error",
        plan=plan,
    )


def test_compiler_freezes_validated_general_plan_and_source_text():
    plan = executable_plan()
    service = FakeAnalysisService(response_for(plan))
    compiler = AnalysisRuleCompiler(
        service,
        clock=lambda: datetime(2026, 9, 23, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    conversation = [
        AnalysisConversationTurn(
            prompt="先定义股票集合A",
            interpretation="集合A由前一步筛选结果构成。",
        )
    ]
    result = compiler.compile("任意多阶段自然语言规则", conversation=conversation)

    assert result is not None
    assert result.compiled_rule is not None
    assert result.compiled_rule.source_text == "任意多阶段自然语言规则"
    assert result.compiled_rule.plan == plan
    assert result.compiled_rule.anchor_date == "20260922"
    assert len(result.compiled_rule.plan_fingerprint) == 64
    _, request, api_route = service.calls[0]
    assert request.mode == "plan"
    assert request.prompt == "任意多阶段自然语言规则"
    assert request.conversation == conversation
    assert api_route.endswith("strategy:compile")


def test_compiler_returns_precise_clarification_without_candidate_rules():
    plan = QueryPlan(
        interpretation="The universe is ambiguous.",
        feasibility="unsupported",
        limitations=["The requested set A is not defined in this conversation."],
        clarification_options=["Define set A by an explicit stock list."],
    )
    result = AnalysisRuleCompiler(FakeAnalysisService(response_for(plan))).compile(
        "在A中筛选"
    )

    assert result is not None
    assert result.compiled_rule is None
    assert result.clarification_options == ["Define set A by an explicit stock list."]
    assert result.limitations == [
        "The requested set A is not defined in this conversation."
    ]


def test_plan_date_binding_shifts_observation_dates_but_not_reporting_periods():
    plan = executable_plan().model_copy(deep=True)
    plan.queries[0].params["period"] = "20251231"
    compiled = AnalysisRuleCompiler(
        FakeAnalysisService(response_for(plan))
    ).compile("可复用规则").compiled_rule

    rebound = bind_plan_date(compiled, "20261002")

    assert plan_anchor_date(rebound, fallback="20261002") == "20261002"
    assert rebound.queries[0].params["start_date"] == "20260911"
    assert rebound.queries[0].params["end_date"] == "20261002"
    assert rebound.queries[0].params["period"] == "20251231"


def test_compiler_returns_none_when_analysis_service_fails():
    class FailingService:
        def analyze(self, *_args, **_kwargs):
            raise RuntimeError("planner unavailable")

    assert AnalysisRuleCompiler(FailingService()).compile("任意规则") is None

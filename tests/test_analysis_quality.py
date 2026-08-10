"""Offline end-to-end quality regressions for representative user questions."""

import json

import pandas as pd
import pytest

from china_a_share.application.workflow import (
    ASharePlanValidator,
    AnalysisService,
    DataQueryExecutor,
)
from china_a_share.core.contracts import AnalysisRequest, DataOperation, AnalysisStatus
from china_a_share.planners.deepseek import DeepSeekQueryPlanner


EVENT_STUDY_PROMPT = (
    "A股20260102～20260106连续涨停三天的情况下，"
    "接下来一个月的上涨情况数据分析"
)


class ReplayResponse:
    """Return one captured planner payload through the requests response contract."""

    status_code = 200

    def __init__(self, content):
        self._payload = {"choices": [{"message": {"content": content}}]}

    def json(self):
        """Return the captured DeepSeek response body."""
        return self._payload

    def raise_for_status(self):
        """Represent a successful replay response."""


class ReplaySession:
    """Replay one deterministic DeepSeek response without network access."""

    def __init__(self, plan):
        self._content = json.dumps(plan)

    def post(self, *args, **kwargs):
        """Return the captured plan for every bounded planner attempt."""
        return ReplayResponse(self._content)


class EventStudyProvider:
    """Serve fixed daily and native limit-up rows for offline quality checks."""

    name = "quality-fixture"

    def search_operations(self, prompt):
        """Expose only the two operations needed by the event study."""
        return [
            DataOperation(name="daily", description="Daily A-share prices."),
            DataOperation(
                name="limit_list_d",
                description="Native daily A-share price-limit events.",
            ),
        ]

    def supports(self, operation):
        """Report support for the fixed fixture operations."""
        return operation in {"daily", "limit_list_d"}

    def query(
        self,
        operation,
        params,
        fields,
        *,
        api_route,
        request_id,
        query_id,
    ):
        """Return deterministic rows that contain one three-session streak."""
        if operation == "limit_list_d":
            return pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "trade_date": trade_date}
                    for trade_date in ("20260102", "20260105", "20260106")
                ]
            )
        rows = [
            {"ts_code": "000001.SZ", "trade_date": "20260102", "close": 10.0},
            {"ts_code": "000001.SZ", "trade_date": "20260105", "close": 11.0},
            {"ts_code": "000001.SZ", "trade_date": "20260106", "close": 12.0},
            {"ts_code": "000001.SZ", "trade_date": "20260206", "close": 15.0},
        ]
        trade_date = params.get("trade_date")
        return pd.DataFrame(
            row for row in rows if trade_date is None or row["trade_date"] == trade_date
        )


def event_study_plan():
    """Build a captured model plan containing a historical field-name drift."""
    return {
        "interpretation": "Analyze one-month returns after three limit-up sessions.",
        "requirements": [
            {
                "requirement": "Measure returns after three consecutive limit-up sessions.",
                "status": "covered",
                "implementation": "Match native events to daily prices and summarize returns.",
                "evidence": "limit_list_d and daily expose event dates and closing prices.",
            }
        ],
        "answer_contract": {
            "result_query_id": "limit_up_streak_outcome",
            "result_kind": "summary",
            "outputs": [
                {
                    "field": "event_count",
                    "description": "Number of valid streak outcomes.",
                },
                {
                    "field": "positive_event_ratio",
                    "description": "Share of outcomes with a positive return.",
                },
                {
                    "field": "negative_event_ratio",
                    "description": "Share of outcomes with a negative return.",
                },
                {
                    "field": "average_return_pct",
                    "description": "Mean outcome return in percentage points.",
                },
            ],
        },
        "queries": [
            {
                "query_id": "market_direction",
                "operation": "daily",
                "params": {"start_date": "20260102", "end_date": "20260206"},
                "fields": ["ts_code", "trade_date", "close"],
                "purpose": "Fetch the dense price series.",
            },
            {
                "query_id": "limit-ups",
                "operation": "limit_list_d",
                "params": {
                    "start_date": "20260102",
                    "end_date": "20260106",
                    "limit_type": "U",
                },
                "fields": ["ts_code", "trade_date"],
                "purpose": "Fetch native limit-up events.",
            },
        ],
        "result_pipeline": {
            "source_query_id": "market_direction",
            "output_query_id": "event-study",
            "steps": [
                {
                    "operation": "match_source",
                    "right_source_query_id": "limit-ups",
                    "join_on": ["ts_code", "trade_date"],
                    "output_field": "is_limit_up",
                },
                {
                    "operation": "rolling_sum",
                    "field": "limit_up_flag",
                    "output_field": "streak_count",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "window": 3,
                    "min_periods": 1,
                },
                {
                    "operation": "match_at_offset",
                    "field": "close",
                    "output_field": "future_close",
                    "matched_date_output_field": "future_trade_date",
                    "group_by": ["ts_code"],
                    "order_by": "trade_date",
                    "offset_value": 1,
                    "offset_unit": "month",
                },
                {
                    "operation": "summarize",
                    "aggregations": [
                        {
                            "output_field": "event_count",
                            "field": "future_close",
                            "function": "count",
                        }
                    ],
                },
            ],
        },
    }


def test_replayed_event_study_is_normalized_validated_and_executed_end_to_end():
    provider = EventStudyProvider()
    service = AnalysisService(
        planner=DeepSeekQueryPlanner(
            "test-key",
            session=ReplaySession(event_study_plan()),
        ),
        provider=provider,
        validator=ASharePlanValidator(provider),
        executor=DataQueryExecutor(provider),
    )

    response = service.analyze(
        request_id="quality-event-study",
        request=AnalysisRequest(prompt=EVENT_STUDY_PROMPT),
        api_route="/quality/event-study",
        progress_callback=lambda completed, total: None,
    )

    assert response.status is AnalysisStatus.SUCCESS
    assert response.error is None
    assert response.plan is not None
    steps = response.plan.result_pipeline.steps
    assert steps[1].field == "is_limit_up"
    assert steps[1].window == 3
    assert steps[1].min_periods == 3
    assert steps[1].require_consecutive is True
    result = next(
        item
        for item in response.results
        if item.query_id == response.plan.result_pipeline.output_query_id
    )
    assert result.status.value == "success"
    assert result.rows == [
        {
            "event_count": 1,
            "positive_event_count": 1,
            "positive_event_ratio": 1.0,
            "negative_event_count": 0,
            "negative_event_ratio": 0.0,
            "average_return_pct": 25.0,
            "minimum_return_pct": 25.0,
            "maximum_return_pct": 25.0,
        }
    ]


def test_event_study_rejects_filtering_before_future_value_matching():
    planner = DeepSeekQueryPlanner("test-key")
    plan = planner.normalize_and_validate_plan(json.dumps(event_study_plan()))
    steps = plan.result_pipeline.steps
    steps[2], steps[3] = steps[3], steps[2]

    with pytest.raises(
        ValueError,
        match="outcomes must be matched before filtering",
    ):
        AnalysisService._validate_planned_time_semantics(plan, EVENT_STUDY_PROMPT)

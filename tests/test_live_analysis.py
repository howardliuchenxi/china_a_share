"""Opt-in quality coverage against the real DeepSeek and Tushare APIs."""

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from uuid import uuid4

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from china_a_share.api import create_app
from china_a_share.application.workflow import (
    ASharePlanValidator,
    AnalysisService,
    DataQueryExecutor,
)
from china_a_share.cache import (
    DEFAULT_L1_MAX_BYTES,
    DEFAULT_L1_MAX_ENTRIES,
    LayeredDataResponseCache,
    MemoryDataCacheStore,
    NoopDataCacheStore,
)
from china_a_share.config import Settings
from china_a_share.core.contracts import (
    AnalysisConversationTurn,
    AnalysisRequest,
    AnalysisStatus,
)
from china_a_share.planners.deepseek import DeepSeekQueryPlanner
from china_a_share.providers.tushare import (
    TushareCacheExpirationPolicy,
    TushareDataProvider,
)
from china_a_share.time_range import (
    resolve_consecutive_session_count,
    resolve_future_horizon,
)
from china_a_share.tasks import AnalysisTaskCoordinator, MemoryAnalysisTaskStore

from live_analysis_cases import LIVE_ANALYSIS_CASES, LIVE_REGRESSION_CASES


LIVE_ANALYSIS_ENVIRONMENT_VARIABLE = "RUN_LIVE_ANALYSIS"
LIVE_ANALYSIS_PARALLEL_ENVIRONMENT_VARIABLE = "LIVE_ANALYSIS_PARALLEL"
LIVE_ANALYSIS_MAX_WORKERS = 2
BACKGROUND_FANOUT_REPORTED_PROMPT = "A股2026年汽车行业，市盈率和分红数据"


class RecordingTaskDispatcher:
    """Record local dispatches while the test invokes the worker explicitly."""

    def __init__(self) -> None:
        self.task_ids = []

    def dispatch(self, task_id: str) -> None:
        """Record the durable task that production would send to Cloud Run."""
        self.task_ids.append(task_id)


@pytest.fixture(scope="module")
def live_market_data_provider() -> TushareDataProvider:
    """Build one real provider so live execution and independent checks share cache."""
    settings = Settings.from_env()
    response_cache = LayeredDataResponseCache(
        memory_store=MemoryDataCacheStore(
            max_entries=DEFAULT_L1_MAX_ENTRIES,
            max_bytes=DEFAULT_L1_MAX_BYTES,
        ),
        # Local live validation must not require Google Cloud credentials.
        persistent_store=NoopDataCacheStore(),
        expiration_policy=TushareCacheExpirationPolicy(),
    )
    return TushareDataProvider(
        token=settings.tushare_token,
        response_cache=response_cache,
    )


@pytest.fixture(scope="module")
def live_analysis_service(live_market_data_provider) -> AnalysisService:
    """Build one real service so all cases share only local market-data cache."""
    settings = Settings.from_env()
    return AnalysisService(
        planner=DeepSeekQueryPlanner(settings.deepseek_api_key),
        provider=live_market_data_provider,
        validator=ASharePlanValidator(live_market_data_provider),
        executor=DataQueryExecutor(live_market_data_provider),
    )


def _failure_message(response) -> str:
    """Return the most specific available failure for one live assertion."""
    if response.error is not None:
        return response.error.message
    result_errors = "; ".join(
        result.error.message
        for result in response.results
        if result.error is not None
    )
    if result_errors:
        return result_errors
    if response.plan is not None:
        details = list(response.plan.limitations)
        details.extend(
            requirement.evidence
            for requirement in response.plan.requirements
            if requirement.status == "unsupported"
        )
        if details:
            return "; ".join(details)
    return "Analysis did not satisfy the live regression contract."


def _assert_pipeline_result_succeeded(response) -> None:
    """Verify the declared result pipeline produced one successful result."""
    if response.plan.result_pipeline is None:
        return
    output_query_id = response.plan.result_pipeline.output_query_id
    pipeline_result = next(
        result for result in response.results if result.query_id == output_query_id
    )
    assert pipeline_result.status.value == "success"
    assert pipeline_result.error is None


def _planned_operations(plan) -> set[str]:
    """Return provider operations from both linear and graph execution plans."""
    operations = {query.operation for query in plan.queries}
    if plan.execution_plan is not None:
        operations.update(
            node.query.operation
            for node in plan.execution_plan.nodes
            if node.kind == "query"
        )
    return operations


def _assert_quality_invariants(
    response,
    prompt,
    invariants,
    live_market_data_provider=None,
    live_analysis_service=None,
) -> None:
    """Check stable business meaning without binding exact planner JSON."""
    plan = response.plan
    operations = _planned_operations(plan)
    steps = plan.result_pipeline.steps if plan.result_pipeline else []

    if "native_limit_up_source" in invariants:
        assert "limit_list_d" in operations
    if "consecutive_session_count" in invariants:
        expected_count = resolve_consecutive_session_count(prompt)
        assert expected_count is not None
        rolling_step = next(step for step in steps if step.operation == "rolling_sum")
        assert rolling_step.window == expected_count
        assert rolling_step.min_periods == expected_count
        assert rolling_step.require_consecutive is True
    if "future_horizon" in invariants:
        expected_horizon = resolve_future_horizon(prompt)
        assert expected_horizon is not None
        assert any(
            step.operation == "match_at_offset"
            and (step.offset_value, step.offset_unit) == expected_horizon
            for step in steps
        )
    if "valid_sample_count" in invariants:
        summary_step = next(step for step in steps if step.operation == "summarize")
        assert any(
            aggregation.output_field == "event_count"
            and aggregation.function == "count"
            for aggregation in summary_step.aggregations
        )
    if "period_return_direction" in invariants:
        assert any(
            query.transform == "period_return_by_ts_code"
            for query in plan.queries
        )
        sort_step = next(step for step in steps if step.operation == "sort")
        expected_direction = "asc" if "跌" in prompt else "desc"
        assert sort_step.direction == expected_direction
    if "sort_before_limit" in invariants:
        operations = [step.operation for step in steps]
        assert operations.index("sort") < operations.index("limit")
    if "conditional_category_counts" in invariants:
        comparisons = [step for step in steps if step.operation == "compare_scalar"]
        summary = next(step for step in steps if step.operation == "summarize")
        summary_fields = {item.field for item in summary.aggregations}
        assert len(comparisons) >= 2
        assert len(
            {(step.field, step.comparison, step.value) for step in comparisons}
        ) == len(comparisons)
        assert summary_fields.issubset(
            {step.output_field for step in comparisons}
        )
        assert all(item.function == "sum" for item in summary.aggregations)
        assert live_market_data_provider is not None
        source_query = next(
            query
            for query in plan.queries
            if query.query_id == plan.result_pipeline.source_query_id
        )
        source_frame = live_market_data_provider.query(
            source_query.operation,
            source_query.params,
            source_query.fields,
            api_route="/local-live-independent-validation",
            request_id=f"live-oracle-{uuid4()}",
            query_id=f"{source_query.query_id}-independent-validation",
        )
        result = next(
            item
            for item in response.results
            if item.query_id == plan.result_pipeline.output_query_id
        )
        result_row = result.rows[0]
        comparisons_by_output = {
            step.output_field: step for step in comparisons
        }
        for aggregation in summary.aggregations:
            comparison = comparisons_by_output[aggregation.field]
            source_series = source_frame[comparison.field]
            if comparison.comparison == "eq":
                expected = int((source_series == comparison.value).sum())
                assert result_row[aggregation.output_field] == expected
                continue
            series = pd.to_numeric(source_series, errors="coerce")
            if comparison.comparison == "gt":
                expected = int((series > comparison.value).sum())
            elif comparison.comparison == "ge":
                expected = int((series >= comparison.value).sum())
            elif comparison.comparison == "le":
                expected = int((series <= comparison.value).sum())
            else:
                expected = int((series < comparison.value).sum())
            assert result_row[aggregation.output_field] == expected
        operator_set = {step.comparison for step in comparisons}
        if operator_set in ({"lt", "eq", "gt"}, {"le", "gt"}, {"lt", "ge"}):
            assert sum(
                result_row[aggregation.output_field]
                for aggregation in summary.aggregations
            ) == int(source_frame[comparisons[0].field].notna().sum())
    if "complete_share_float_result" in invariants:
        share_float_results = [
            result
            for result in response.results
            if result.operation in {"share_float", "result_pipeline"}
        ]
        assert share_float_results
        assert all(
            result.completeness == "complete" for result in share_float_results
        )
        assert any(
            any(
                evidence.startswith("query_shape=")
                for evidence in result.completeness_evidence
            )
            for result in share_float_results
        )
    if "distinct_security_count" in invariants:
        assert plan.result_pipeline is not None
        summary = next(
            step
            for step in plan.result_pipeline.steps
            if step.operation == "summarize"
        )
        assert any(
            aggregation.field == "ts_code"
            and aggregation.function == "count_distinct"
            for aggregation in summary.aggregations
        )
    if "industry_valuation_dividend_table" in invariants:
        assert plan.answer_contract is not None
        assert plan.answer_contract.result_kind == "table"
        expected_fields = {"ts_code", "name", "pe", "cash_div_tax"}
        assert {output.field for output in plan.answer_contract.outputs} == (
            expected_fields
        )
        result = next(
            item
            for item in response.results
            if item.query_id == plan.answer_contract.result_query_id
        )
        assert result.rows
        assert result.row_count == len(result.rows)
        assert all(expected_fields.issubset(row) for row in result.rows)
        assert len({row["ts_code"] for row in result.rows}) == len(result.rows)
    if "explicit_ranked_limit" in invariants:
        assert plan.answer_contract is not None
        limit_match = re.search(r"(?:\u524d|top)\s*(\d+)", prompt.casefold())
        assert limit_match is not None
        expected_count = int(limit_match.group(1))
        result = next(
            item
            for item in response.results
            if item.query_id == plan.answer_contract.result_query_id
        )
        values = [float(row["pe"]) for row in result.rows]
        assert len(values) == expected_count
        if any(
            term in prompt.casefold()
            for term in ("\u4ece\u4f4e\u5230\u9ad8", "\u5347\u5e8f", "ascending")
        ):
            assert values == sorted(values)
        elif any(
            term in prompt.casefold()
            for term in ("\u4ece\u9ad8\u5230\u4f4e", "\u964d\u5e8f", "descending")
        ):
            assert values == sorted(values, reverse=True)
        else:
            raise AssertionError("Ranked-limit invariant requires an explicit order.")
    if "list_not_scalar" in invariants:
        assert plan.answer_contract is not None
        assert plan.answer_contract.result_kind == "table"
        assert any(
            output.field == "ts_code"
            for output in plan.answer_contract.outputs
        )
        result = next(
            item
            for item in response.results
            if item.query_id == plan.answer_contract.result_query_id
        )
        assert result.row_count == len(result.rows)
        assert all("ts_code" in row for row in result.rows)
    if "confirmation_dates_match_queries" in invariants:
        valuation_dates = {
            query.params["trade_date"]
            for query in plan.queries
            if query.operation == "daily_basic" and query.params.get("trade_date")
        }
        assert valuation_dates
        assert all(
            trade_date in plan.interpretation
            for trade_date in valuation_dates
        )
    if "distinct_company_list" in invariants:
        assert plan.answer_contract is not None
        result = next(
            item
            for item in response.results
            if item.query_id == plan.answer_contract.result_query_id
        )
        security_codes = [row["ts_code"] for row in result.rows]
        assert len(security_codes) == len(set(security_codes))
    if "explicit_date_order" in invariants:
        assert plan.answer_contract is not None
        result = next(
            item
            for item in response.results
            if item.query_id == plan.answer_contract.result_query_id
        )
        date_field = next(
            field
            for field in ("ann_date", "trade_date", "end_date", "float_date")
            if any(output.field == field for output in plan.answer_contract.outputs)
        )
        descending = any(
            term in prompt.casefold()
            for term in (
                "descending",
                "newest first",
                "latest first",
                "\u4ece\u65b0\u5230\u65e7",
                "\u964d\u5e8f",
            )
        )
        expected_direction = "desc" if descending else "asc"
        assert any(
            step.operation == "sort"
            and step.field == date_field
            and step.direction == expected_direction
            for step in steps
        )
        date_values = [row[date_field] for row in result.rows]
        assert date_values == sorted(date_values, reverse=descending)
    if "explicit_cash_dividend_ranked_limit" in invariants:
        assert plan.answer_contract is not None
        result = next(
            item
            for item in response.results
            if item.query_id == plan.answer_contract.result_query_id
        )
        limit_match = re.search(r"(?:\u524d|top)\s*(\d+)", prompt.casefold())
        assert limit_match is not None
        expected_count = int(limit_match.group(1))
        expected_fields = {"ts_code", "name", "cash_div_tax", "ann_date"}
        assert expected_fields.issubset(
            {output.field for output in plan.answer_contract.outputs}
        )
        values = [row["cash_div_tax"] for row in result.rows]
        assert len(values) == expected_count
        assert all(row["name"] for row in result.rows)
        assert all(value is not None for value in values)
        ascending = any(
            term in prompt.casefold()
            for term in ("lowest", "smallest", "ascending", "\u6700\u4f4e", "\u5347\u5e8f")
        )
        if any(term in prompt.casefold() for term in ("positive", ">0", "\u5927\u4e8e0")):
            assert all(value > 0 for value in values)
        assert values == sorted(values, reverse=not ascending)
    if "holder_concentration_ranked_list" in invariants:
        assert plan.result_pipeline is not None
        expected_fields = {
            "ts_code",
            "name",
            "ann_date",
            "end_date",
            "holder_num",
            "holder_change_pct",
        }
        assert plan.answer_contract is not None
        assert plan.answer_contract.result_kind == "table"
        assert expected_fields == {
            output.field for output in plan.answer_contract.outputs
        }
        limit_match = re.search(r"(?:\u524d|top)\s*(\d+)", prompt.casefold())
        assert limit_match is not None
        expected_count = int(limit_match.group(1))
        source_query = next(
            query
            for query in plan.queries
            if query.query_id == plan.result_pipeline.source_query_id
        )
        assert source_query.operation == "stk_holdernumber"
        assert live_analysis_service is not None
        source_result = live_analysis_service._execute_disclosure_range_by_date(
            source_query,
            api_route="/local-live-independent-validation",
            request_id=f"live-oracle-{uuid4()}",
        )
        assert source_result.completeness == "complete"
        assert source_result.row_count == len(source_result.rows)
        source_frame = pd.DataFrame(source_result.rows)
        ordered = source_frame.sort_values(
            ["ts_code", "end_date"], kind="mergesort"
        ).copy()
        holder_numbers = pd.to_numeric(ordered["holder_num"], errors="coerce")
        ordered["holder_change_pct"] = (
            holder_numbers.groupby(ordered["ts_code"], sort=False, dropna=False)
            .pct_change(periods=1, fill_method=None)
            .multiply(100)
        )
        expected = (
            ordered.sort_values("ann_date", kind="mergesort", na_position="last")
            .drop_duplicates(["ts_code"], keep="last")
            .dropna(subset=["holder_change_pct"])
            .sort_values("holder_change_pct", kind="mergesort")
            .head(expected_count)
        )
        result = next(
            item
            for item in response.results
            if item.query_id == plan.answer_contract.result_query_id
        )
        assert result.row_count == expected_count
        assert len(result.rows) == expected_count
        assert all(row["name"] for row in result.rows)
        assert [row["ts_code"] for row in result.rows] == expected[
            "ts_code"
        ].tolist()
        assert [row["holder_change_pct"] for row in result.rows] == pytest.approx(
            expected["holder_change_pct"].tolist()
        )


def test_live_analysis_matrix_contains_exactly_100_questions() -> None:
    """Keep the paid external regression suite at the reviewed case count."""
    prompts = [case["prompt"] for case in LIVE_ANALYSIS_CASES]

    assert len(LIVE_ANALYSIS_CASES) == 100
    assert len(set(prompts)) == 100


def test_live_regression_matrix_contains_unique_prompts() -> None:
    """Keep production-reported prompts as distinct end-to-end regressions."""
    prompts = [case["prompt"] for case in LIVE_REGRESSION_CASES]

    assert prompts
    assert len(set(prompts)) == len(prompts)


def _run_live_analysis_question(
    live_analysis_service,
    live_market_data_provider,
    case,
) -> None:
    """Run and validate one curated question against both real providers."""
    response = live_analysis_service.analyze(
        request_id=f"live-{case['family']}-{uuid4()}",
        request=AnalysisRequest(prompt=case["prompt"]),
        api_route="/local-live-analysis",
        progress_callback=(lambda completed, total: None),
    )

    if case["tier"] == "unsupported":
        assert response.status is AnalysisStatus.ERROR
        assert response.plan is not None
        assert response.plan.feasibility == "unsupported"
        assert response.results == []
        return

    assert response.status is AnalysisStatus.SUCCESS, _failure_message(response)
    assert response.error is None
    assert response.plan is not None
    assert response.plan.feasibility == "supported"
    assert all(
        requirement.status == "covered"
        for requirement in response.plan.requirements
    )
    expected_operations = set(case["operations"])
    actual_operations = _planned_operations(response.plan)
    assert actual_operations.intersection(expected_operations)
    assert actual_operations.issubset(
        expected_operations | {"stock_basic", "trade_cal", "daily_basic"}
    )
    assert response.results
    assert all(result.status.value == "success" for result in response.results), (
        _failure_message(response)
    )
    _assert_pipeline_result_succeeded(response)
    _assert_quality_invariants(
        response,
        case["prompt"],
        set(case.get("quality_invariants", [])),
        live_market_data_provider,
        live_analysis_service,
    )


@pytest.mark.parametrize(
    "case",
    LIVE_ANALYSIS_CASES,
    ids=[
        f"{case['family']}-{index + 1}"
        for index, case in enumerate(LIVE_ANALYSIS_CASES)
    ],
)
@pytest.mark.live
@pytest.mark.skipif(
    os.getenv(LIVE_ANALYSIS_ENVIRONMENT_VARIABLE) != "1"
    or os.getenv(LIVE_ANALYSIS_PARALLEL_ENVIRONMENT_VARIABLE) == "1",
    reason="Sequential live cases are disabled or replaced by the parallel matrix.",
)
def test_live_analysis_question(
    live_analysis_service,
    live_market_data_provider,
    case,
) -> None:
    """Run one curated question when the parallel matrix is not selected."""
    _run_live_analysis_question(
        live_analysis_service,
        live_market_data_provider,
        case,
    )


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv(LIVE_ANALYSIS_ENVIRONMENT_VARIABLE) != "1"
    or os.getenv(LIVE_ANALYSIS_PARALLEL_ENVIRONMENT_VARIABLE) != "1",
    reason="Set both live-analysis flags to run the bounded parallel matrix.",
)
def test_live_analysis_questions_in_parallel(
    live_analysis_service,
    live_market_data_provider,
) -> None:
    """Run independent paid live cases concurrently with bounded provider load."""
    failures = []
    completed_count = 0
    with ThreadPoolExecutor(max_workers=LIVE_ANALYSIS_MAX_WORKERS) as pool:
        futures = {
            pool.submit(
                _run_live_analysis_question,
                live_analysis_service,
                live_market_data_provider,
                case,
            ): case
            for case in LIVE_ANALYSIS_CASES
        }
        for future in as_completed(futures):
            case = futures[future]
            completed_count += 1
            try:
                future.result()
                outcome = "PASS"
            except Exception as exc:
                outcome = "FAIL"
                failures.append(
                    f"{case['family']} — {case['prompt']}: {exc}"
                )
            print(
                f"LIVE {completed_count}/{len(LIVE_ANALYSIS_CASES)} "
                f"{outcome} — {case['family']}",
                flush=True,
            )
    assert not failures, "\n".join(sorted(failures))


@pytest.mark.parametrize(
    "case",
    LIVE_REGRESSION_CASES,
    ids=[case["name"] for case in LIVE_REGRESSION_CASES],
)
@pytest.mark.live
@pytest.mark.skipif(
    os.getenv(LIVE_ANALYSIS_ENVIRONMENT_VARIABLE) != "1",
    reason=(
        f"Set {LIVE_ANALYSIS_ENVIRONMENT_VARIABLE}=1 to call the real "
        "DeepSeek and Tushare APIs."
    ),
)
def test_live_reported_prompt_regression(
    live_analysis_service,
    live_market_data_provider,
    case,
) -> None:
    """Run one production-reported prompt through the complete public workflow."""
    response = live_analysis_service.analyze(
        request_id=f"regression-{case['name']}-{uuid4()}",
        request=AnalysisRequest(prompt=case["prompt"]),
        api_route="/local-live-regression",
        progress_callback=(lambda completed, total: None),
    )

    assert response.error is None, _failure_message(response)
    assert response.plan is not None
    assert response.plan.feasibility == case["expected_feasibility"]
    assert response.plan.requirements
    assert all(
        requirement.status in {"covered", "unsupported"}
        for requirement in response.plan.requirements
    )
    if response.plan.feasibility == "unsupported":
        assert response.status is AnalysisStatus.ERROR
        assert response.plan.queries == []
        assert response.results == []
        assert response.plan.clarification_options
        return

    assert response.status is AnalysisStatus.SUCCESS, _failure_message(response)
    assert all(
        requirement.status == "covered"
        for requirement in response.plan.requirements
    )
    assert response.results
    assert all(result.status.value == "success" for result in response.results), (
        _failure_message(response)
    )
    _assert_pipeline_result_succeeded(response)
    _assert_quality_invariants(
        response,
        case["prompt"],
        set(case.get("quality_invariants", [])),
        live_market_data_provider,
        live_analysis_service,
    )


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv(LIVE_ANALYSIS_ENVIRONMENT_VARIABLE) != "1",
    reason=(
        f"Set {LIVE_ANALYSIS_ENVIRONMENT_VARIABLE}=1 to call the real "
        "DeepSeek and Tushare APIs."
    ),
)
def test_live_multi_turn_valuation_refinement(live_analysis_service) -> None:
    """Preserve the prior metric definition while applying current Top-N refinements."""
    initial_prompt = "\u627e\u4f4ePE\u3001\u4f4ePB\u3001\u9ad8\u80a1\u606f\u7387\u7684\u5341\u53ea\u80a1\u7968"
    initial = live_analysis_service.analyze(
        request_id=f"live-multi-turn-initial-{uuid4()}",
        request=AnalysisRequest(prompt=initial_prompt),
        api_route="/local-live-multi-turn",
        progress_callback=(lambda completed, total: None),
    )
    assert initial.status is AnalysisStatus.SUCCESS, _failure_message(initial)
    assert initial.plan is not None

    refinement_prompt = (
        "\u53ea\u4fdd\u7559\u80a1\u606f\u7387\u5927\u4e8e0\u7684\uff0c\u6309\u80a1\u606f\u7387\u4ece\u9ad8\u5230\u4f4e\u7ed9\u6211\u524d5\u5bb6\uff0c"
        "\u4fdd\u7559\u4ee3\u7801\u3001\u540d\u79f0\u3001\u5e02\u76c8\u7387\u3001\u5e02\u51c0\u7387\u548c\u80a1\u606f\u7387"
    )
    refined = live_analysis_service.analyze(
        request_id=f"live-multi-turn-refinement-{uuid4()}",
        request=AnalysisRequest(
            prompt=refinement_prompt,
            conversation=[
                AnalysisConversationTurn(
                    prompt=initial_prompt,
                    interpretation=initial.plan.interpretation,
                )
            ],
        ),
        api_route="/local-live-multi-turn",
        progress_callback=(lambda completed, total: None),
    )

    assert refined.status is AnalysisStatus.SUCCESS, _failure_message(refined)
    assert refined.error is None
    assert refined.plan is not None
    assert refined.plan.result_pipeline is not None
    assert [step.operation for step in refined.plan.result_pipeline.steps] == [
        "filter",
        "sort",
        "limit",
        "join_fields",
    ]
    result = next(
        item
        for item in refined.results
        if item.query_id == refined.plan.answer_contract.result_query_id
    )
    expected_fields = {"ts_code", "name", "pe", "pb", "dv_ttm"}
    assert result.row_count == 5
    assert len(result.rows) == 5
    assert all(expected_fields.issubset(row) for row in result.rows)
    values = [row["dv_ttm"] for row in result.rows]
    assert all(value > 0 for value in values)
    assert values == sorted(values, reverse=True)


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv(LIVE_ANALYSIS_ENVIRONMENT_VARIABLE) != "1",
    reason=(
        f"Set {LIVE_ANALYSIS_ENVIRONMENT_VARIABLE}=1 to call the real "
        "DeepSeek and Tushare APIs."
    ),
)
def test_live_background_fanout_through_http_and_worker(
    live_analysis_service,
) -> None:
    """Run the reported fan-out request through HTTP, worker, and task polling."""
    dispatcher = RecordingTaskDispatcher()
    coordinator = AnalysisTaskCoordinator(MemoryAnalysisTaskStore(), dispatcher)
    client = TestClient(
        create_app(live_analysis_service, task_coordinator=coordinator)
    )

    submission_response = client.post(
        "/api/analysis",
        json={"prompt": BACKGROUND_FANOUT_REPORTED_PROMPT},
    )

    assert submission_response.status_code == 202, submission_response.text
    submission = submission_response.json()
    assert submission["status"] == "queued"
    assert submission["status_url"] == (
        f"/api/analysis/tasks/{submission['task_id']}"
    )
    assert dispatcher.task_ids == [submission["task_id"]]

    completed_task = coordinator.run(submission["task_id"], live_analysis_service)
    polled_response = client.get(submission["status_url"])

    assert completed_task.status.value == "succeeded"
    assert polled_response.status_code == 200
    task_status = polled_response.json()
    assert task_status["status"] == "succeeded"
    assert task_status["error"] is None
    analysis = task_status["response"]
    assert analysis is not None
    assert analysis["status"] == "success", analysis.get("error")
    assert analysis["error"] is None
    assert analysis["plan"]["feasibility"] == "supported"
    operations = {
        query["operation"] for query in analysis["plan"]["queries"]
    }
    execution_plan = analysis["plan"]["execution_plan"]
    if execution_plan is not None:
        operations.update(
            node["query"]["operation"]
            for node in execution_plan["nodes"]
            if node["kind"] == "query"
        )
    assert {"stock_basic", "daily_basic", "dividend"}.issubset(operations)
    assert analysis["results"]
    assert all(result["status"] == "success" for result in analysis["results"])


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv(LIVE_ANALYSIS_ENVIRONMENT_VARIABLE) != "1",
    reason=(
        f"Set {LIVE_ANALYSIS_ENVIRONMENT_VARIABLE}=1 to call the real "
        "DeepSeek and Tushare APIs."
    ),
)
def test_live_industry_valuation_followup_returns_ranked_security_list(
    live_analysis_service,
) -> None:
    """Verify a follow-up inherits industry scope and preserves list semantics."""
    response = live_analysis_service.analyze(
        request_id=f"live-industry-followup-{uuid4()}",
        request=AnalysisRequest(
            prompt="只给我市盈率最低的10家公司列表，保留分红数据",
            conversation=[
                AnalysisConversationTurn(
                    prompt="A股2026年手机行业，市盈率和分红数据",
                    interpretation="Return the requested industry table.",
                )
            ],
        ),
        api_route="/local-live-industry-followup",
        progress_callback=(lambda completed, total: None),
    )

    assert response.status is AnalysisStatus.SUCCESS, (
        _failure_message(response),
        [
            (result.query_id, result.completeness)
            for result in response.results
        ],
    )
    assert response.plan is not None
    assert response.plan.answer_contract is not None
    result = next(
        item
        for item in response.results
        if item.query_id == response.plan.answer_contract.result_query_id
    )
    assert result.row_count == 10
    assert len({row["ts_code"] for row in result.rows}) == 10
    pe_values = [row["pe"] for row in result.rows]
    assert all(value is not None for value in pe_values)
    assert pe_values == sorted(pe_values)


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv(LIVE_ANALYSIS_ENVIRONMENT_VARIABLE) != "1",
    reason=(
        f"Set {LIVE_ANALYSIS_ENVIRONMENT_VARIABLE}=1 to call the real "
        "DeepSeek and Tushare APIs."
    ),
)
def test_live_industry_followup_filters_all_requested_nonmissing_metrics(
    live_analysis_service,
) -> None:
    """Verify follow-up metric-presence filters run before ranking and limiting."""
    response = live_analysis_service.analyze(
        request_id=f"live-industry-nonmissing-followup-{uuid4()}",
        request=AnalysisRequest(
            prompt=(
                "\u53ea\u4fdd\u7559\u6709\u5206\u7ea2\u4e14\u5e02\u76c8\u7387\u975e\u7a7a\u7684\u516c\u53f8\uff0c"
                "\u6309\u5e02\u76c8\u7387\u4ece\u4f4e\u5230\u9ad8\u5217\u51fa\u524d12\u5bb6\uff0c"
                "\u4ecd\u4fdd\u7559\u4ee3\u7801\u3001\u540d\u79f0\u3001\u5e02\u76c8\u7387\u548c\u5206\u7ea2"
            ),
            conversation=[
                AnalysisConversationTurn(
                    prompt="A\u80a12026\u5e74\u624b\u673a\u884c\u4e1a\uff0c\u5e02\u76c8\u7387\u548c\u5206\u7ea2\u6570\u636e",
                    interpretation="Return the requested industry table.",
                )
            ],
        ),
        api_route="/local-live-industry-nonmissing-followup",
        progress_callback=(lambda completed, total: None),
    )

    assert response.status is AnalysisStatus.SUCCESS, _failure_message(response)
    assert response.plan is not None
    assert response.plan.answer_contract is not None
    result = next(
        item
        for item in response.results
        if item.query_id == response.plan.answer_contract.result_query_id
    )
    assert result.row_count == 12
    assert all(row["pe"] is not None for row in result.rows)
    assert all(row["cash_div_tax"] is not None for row in result.rows)
    assert [row["pe"] for row in result.rows] == sorted(
        row["pe"] for row in result.rows
    )

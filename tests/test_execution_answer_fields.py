from datetime import date

import pandas as pd
import pytest

from china_a_share.application.workflow import (
    ASharePlanValidator,
    AnalysisService,
    DataQueryExecutor,
)
from china_a_share.core.contracts import (
    AnswerContract,
    AnalysisConversationTurn,
    AnalysisRequest,
    DataFilter,
    DataQuery,
    DataOperation,
    QueryConstraint,
    QueryPlan,
    ResultPipeline,
)
from china_a_share.planners.deepseek import DeepSeekQueryPlanner


class _CatalogProvider:
    name = "tushare"
    operation_names = (
        "stock_basic",
        "daily_basic",
        "dividend",
        "repurchase",
        "moneyflow",
    )

    @staticmethod
    def supports(operation: str) -> bool:
        return operation in {
            "stock_basic",
            "daily_basic",
            "dividend",
            "repurchase",
            "moneyflow",
        }


class _IndustryCatalogProvider:
    name = "tushare"

    @staticmethod
    def supports(operation: str) -> bool:
        return operation == "stock_basic"

    @staticmethod
    def query(operation, params, fields, **context):
        assert operation == "stock_basic"
        assert params == {}
        assert fields == ["industry"]
        assert context["query_id"] == "industry-classification-catalog"
        return pd.DataFrame({"industry": ["通信设备", "银行"]})


class _IndustrySelectingPlanner:
    name = "planner"

    def __init__(self):
        self.prompts = []

    def generate_text(self, prompt):
        self.prompts.append(prompt)
        return '{"industry":"通信设备"}'


def _execution_plan(
    *,
    label_field: str,
    detail_field: str,
) -> QueryPlan:
    return QueryPlan.model_validate(
        {
            "market": "A_SHARE",
            "interpretation": "Combine a filtered universe with two requested data sources.",
            "feasibility": "supported",
            "requirements": [
                {
                    "requirement": "Return fields from every requested source.",
                    "status": "covered",
                    "implementation": "Join validated query results by security code.",
                    "evidence": "Each output field has one exact upstream source.",
                }
            ],
            "answer_contract": {
                "result_query_id": "combined",
                "result_kind": "table",
                "outputs": [
                    {"field": "ts_code", "description": "Security code."},
                    {"field": label_field, "description": "Security label."},
                    {"field": "metric", "description": "Snapshot metric."},
                    {"field": detail_field, "description": "Detail value."},
                ],
            },
            "execution_plan": {
                "result_node_id": "combined",
                "nodes": [
                    {
                        "node_id": "universe",
                        "kind": "query",
                        "query": {
                            "query_id": "universe",
                            "operation": "stock_basic",
                            "purpose": "Define the requested universe.",
                            "fields": ["ts_code", label_field, "industry"],
                        },
                    },
                    {
                        "node_id": "metrics",
                        "kind": "query",
                        "query": {
                            "query_id": "metrics",
                            "operation": "daily_basic",
                            "purpose": "Read the requested snapshot metric.",
                            "fields": ["ts_code", "metric"],
                            "params": {"trade_date": "20260807"},
                        },
                    },
                    {
                        "node_id": "filtered",
                        "kind": "compute",
                        "input_result_ids": ["metrics", "universe"],
                        "step": {
                            "operation": "semi_join",
                            "right_source_query_id": "universe",
                            "join_on": ["ts_code"],
                        },
                    },
                    {
                        "node_id": "details",
                        "kind": "query",
                        "input_result_ids": ["filtered"],
                        "fanout_input_field": "ts_code",
                        "fanout_param": "ts_code",
                        "query": {
                            "query_id": "details",
                            "operation": "dividend",
                            "purpose": "Read one requested detail field.",
                            "fields": ["ts_code", detail_field],
                        },
                    },
                    {
                        "node_id": "combined",
                        "kind": "compute",
                        "input_result_ids": ["filtered", "details"],
                        "step": {
                            "operation": "inner_join",
                            "right_source_query_id": "details",
                            "join_on": ["ts_code"],
                            "fields": {},
                            "cardinality": "one_to_one",
                        },
                    },
                ],
            },
        }
    )


def test_execution_joins_copy_unambiguous_answer_fields_from_each_source():
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")

    DeepSeekQueryPlanner._normalize_execution_answer_fields(plan)

    filtered, combined = plan.execution_plan.nodes[2], plan.execution_plan.nodes[4]
    assert filtered.step.operation == "inner_join"
    assert filtered.step.fields == {"name": "name"}
    assert combined.step.fields == {"cash_div_tax": "cash_div_tax"}
    assert ASharePlanValidator(_CatalogProvider()).validate(plan) is plan


def test_execution_join_normalization_generalizes_to_other_output_fields():
    plan = _execution_plan(label_field="area", detail_field="record_date")

    DeepSeekQueryPlanner._normalize_execution_answer_fields(plan)

    filtered, combined = plan.execution_plan.nodes[2], plan.execution_plan.nodes[4]
    assert filtered.step.fields == {"area": "area"}
    assert combined.step.fields == {"record_date": "record_date"}


def test_inner_join_without_uniqueness_evidence_uses_unrestricted_cardinality():
    steps = [
        {
            "operation": "inner_join",
            "right_source_query_id": "details",
            "join_on": ["ts_code"],
        }
    ]

    DeepSeekQueryPlanner._normalize_pipeline_step_syntax(steps)

    assert steps[0]["fields"] == {}
    assert steps[0]["cardinality"] == "many_to_many"


def test_supported_requirement_with_unsupported_status_is_not_rewritten():
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")
    plan.requirements[0].status = "unsupported"

    DeepSeekQueryPlanner("test-key")._finalize_plan(plan)

    assert plan.requirements[0].status == "unsupported"


def test_requirement_without_implementation_remains_unsupported():
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")
    plan.requirements[0].status = "unsupported"
    plan.requirements[0].implementation = None

    DeepSeekQueryPlanner("test-key")._finalize_plan(plan)

    assert plan.requirements[0].status == "unsupported"


def test_unsupported_plan_rejects_all_requirements_claiming_implementations():
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")
    plan.feasibility = "unsupported"
    plan.requirements[0].status = "unsupported"

    with pytest.raises(
        ValueError,
        match="at least one requirement without an executable implementation",
    ):
        DeepSeekQueryPlanner._validate_requirement_operation_lineage(
            plan,
            [DataOperation(name="stock_basic", description="Security master.")],
        )


@pytest.mark.parametrize(
    ("prompt", "expected_industry", "expected_year"),
    [
        ("A股2026年汽车行业，市盈率和分红数据", "汽车", "2026"),
        ("A股2025年电池行业，PE和分红数据", "电池", "2025"),
    ],
)
def test_industry_valuation_dividend_request_compiles_deterministically(
    prompt,
    expected_industry,
    expected_year,
):
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")

    AnalysisService._normalize_plan_for_request(plan, prompt)

    assert plan.execution_plan is None
    assert [query.operation for query in plan.queries] == [
        "stock_basic",
        "daily_basic",
        "dividend",
    ]
    assert plan.queries[0].params == {"list_status": "L"}
    assert plan.queries[0].filters[0].value == expected_industry
    assert [row_filter.value for row_filter in plan.queries[2].filters] == [
        f"{expected_year}0101",
        f"{expected_year}1231",
    ]
    assert expected_industry in plan.interpretation
    assert expected_year in plan.interpretation
    assert "price-to-earnings ratios" in plan.interpretation
    assert "per-share pre-tax cash dividend" in plan.interpretation
    assert plan.requirements[0].requirement == (
        f"Return {expected_industry} industry securities with valuation and "
        f"{expected_year} dividend data."
    )
    assert "missing value does not imply a zero dividend" in plan.limitations[0]
    assert [step.operation for step in plan.result_pipeline.steps] == [
        "latest_by_group",
        "join_fields",
        "join_fields",
        "select_fields",
    ]
    assert {output.field for output in plan.answer_contract.outputs} == {
        "ts_code",
        "name",
        "pe",
        "cash_div_tax",
    }
    assert ASharePlanValidator(_CatalogProvider()).validate(plan) is plan


def test_industry_resolver_selects_only_a_provider_taxonomy_label():
    planner = _IndustrySelectingPlanner()
    service = AnalysisService.__new__(AnalysisService)
    service._planner = planner
    service._provider = _IndustryCatalogProvider()

    resolved = service._append_resolved_industry(
        "industry-request",
        "A股2026年手机行业，市盈率和分红数据",
        AnalysisRequest(prompt="A股2026年手机行业，市盈率和分红数据"),
    )

    assert "<trusted_industry_classification>" in resolved
    assert "industry=通信设备" in resolved
    assert "year=2026" in resolved
    assert "手机" in planner.prompts[0]
    assert "通信设备" in planner.prompts[0]


def test_industry_resolver_uses_one_direct_taxonomy_match_without_model_call():
    planner = _IndustrySelectingPlanner()
    service = AnalysisService.__new__(AnalysisService)
    service._planner = planner
    service._provider = _IndustryCatalogProvider()

    resolved = service._append_resolved_industry(
        "industry-request",
        "A股2026年银行行业，市盈率和分红数据",
        AnalysisRequest(prompt="A股2026年银行行业，市盈率和分红数据"),
    )

    assert "industry=银行" in resolved
    assert planner.prompts == []


def test_industry_resolver_inherits_the_latest_explicit_conversation_scope():
    planner = _IndustrySelectingPlanner()
    service = AnalysisService.__new__(AnalysisService)
    service._planner = planner
    service._provider = _IndustryCatalogProvider()
    request = AnalysisRequest(
        prompt="只给我市盈率最低的10家公司列表，保留分红数据",
        conversation=[
            AnalysisConversationTurn(
                prompt="A股2026年手机行业，市盈率和分红数据",
                interpretation="Return the requested industry table.",
            )
        ],
    )

    resolved = service._append_resolved_industry(
        "industry-followup",
        request.prompt,
        request,
    )

    assert "industry=通信设备" in resolved
    assert "year=2026" in resolved


def test_industry_valuation_followup_compiles_ranked_table_from_trusted_scope():
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")
    prompt = (
        "只给我市盈率最低的10家公司列表，保留分红数据\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260814\n"
        "event_end_date=20260814\n"
        "</trusted_analysis_window>\n"
        "<trusted_industry_classification>\n"
        "industry=通信设备\n"
        "year=2026\n"
        "</trusted_industry_classification>"
    )

    AnalysisService._normalize_plan_for_request(plan, prompt)

    assert plan.queries[0].filters[0].value == "通信设备"
    assert [step.operation for step in plan.result_pipeline.steps] == [
        "latest_by_group",
        "join_fields",
        "drop_missing",
        "sort",
        "limit",
        "join_fields",
        "select_fields",
    ]
    assert plan.result_pipeline.steps[3].direction == "asc"
    assert plan.result_pipeline.steps[4].count == 10
    assert ASharePlanValidator(_CatalogProvider()).validate(plan) is plan


@pytest.mark.parametrize(
    ("prompt", "expected_direction", "expected_limit"),
    [
        (
            "\u8bf7\u6309\u5e02\u76c8\u7387\u4ece\u4f4e\u5230\u9ad8\u5217\u51fa2026\u5e74\u901a\u4fe1\u8bbe\u5907\u884c\u4e1a\u524d7\u5bb6\u516c\u53f8\uff0c"
            "\u5e76\u4fdd\u7559\u6bcf\u80a1\u7a0e\u524d\u73b0\u91d1\u5206\u7ea2\uff1b\u4e0d\u8981\u53ea\u544a\u8bc9\u6211\u6570\u91cf",
            "asc",
            7,
        ),
        (
            "\u6309\u5e02\u76c8\u7387\u4ece\u9ad8\u5230\u4f4e\u5217\u51fa2025\u5e74\u7535\u6c60\u884c\u4e1a\u524d5\u5bb6\u516c\u53f8\u5e76\u4fdd\u7559\u5206\u7ea2\u6570\u636e",
            "desc",
            5,
        ),
    ],
)
def test_industry_valuation_ranking_accepts_explicit_order_phrases(
    prompt,
    expected_direction,
    expected_limit,
):
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")

    AnalysisService._normalize_plan_for_request(plan, prompt)

    assert [step.operation for step in plan.result_pipeline.steps] == [
        "latest_by_group",
        "join_fields",
        "drop_missing",
        "sort",
        "limit",
        "join_fields",
        "select_fields",
    ]
    assert plan.result_pipeline.steps[3].direction == expected_direction
    assert plan.result_pipeline.steps[4].count == expected_limit
    assert plan.answer_contract.result_kind == "table"


def test_industry_ranking_filters_every_explicitly_required_nonmissing_metric():
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")
    prompt = (
        "\u53ea\u4fdd\u7559\u6709\u5206\u7ea2\u4e14\u5e02\u76c8\u7387\u975e\u7a7a\u7684\u516c\u53f8\uff0c"
        "\u6309\u5e02\u76c8\u7387\u4ece\u4f4e\u5230\u9ad8\u5217\u51fa\u524d12\u5bb6\uff0c"
        "\u4ecd\u4fdd\u7559\u4ee3\u7801\u3001\u540d\u79f0\u3001\u5e02\u76c8\u7387\u548c\u5206\u7ea2\n\n"
        "<conversation_context>\n"
        "A\u80a12026\u5e74\u624b\u673a\u884c\u4e1a\uff0c\u5e02\u76c8\u7387\u548c\u5206\u7ea2\u6570\u636e\n"
        "</conversation_context>"
    )

    AnalysisService._normalize_plan_for_request(plan, prompt)

    missing_step = next(
        step
        for step in plan.result_pipeline.steps
        if step.operation == "drop_missing"
    )
    assert missing_step.fields == ["pe", "cash_div_tax"]
    assert plan.result_pipeline.steps.index(missing_step) < next(
        index
        for index, step in enumerate(plan.result_pipeline.steps)
        if step.operation == "limit"
    )


def test_snapshot_date_normalization_updates_confirmation_text():
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")
    valuation_node = next(
        node
        for node in plan.execution_plan.nodes
        if node.kind == "query" and node.query.operation == "daily_basic"
    )
    valuation_node.query.params = {"trade_date": "20260818"}
    prompt = (
        "A\u80a12026\u5e74\u6c7d\u8f66\u884c\u4e1a\uff0c\u5e02\u76c8\u7387\u548c\u5206\u7ea2\u6570\u636e\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260818\n"
        "event_end_date=20260818\n"
        "</trusted_analysis_window>"
    )
    AnalysisService._normalize_plan_for_request(plan, prompt)

    AnalysisService._normalize_latest_plan_dates(plan, date(2026, 8, 18))

    assert plan.queries[1].params == {"trade_date": "20260817"}
    assert "20260817" in plan.interpretation
    assert "20260818" not in plan.interpretation


def test_latest_margin_security_exchange_snapshot_binds_completed_session():
    plan = QueryPlan(
        interpretation="List the latest SSE margin securities.",
        requirements=[
            {
                "requirement": "List the latest SSE margin securities.",
                "status": "covered",
                "evidence": "margin_secs provides exchange-level snapshots.",
            }
        ],
        queries=[
            DataQuery(
                query_id="margin-securities",
                operation="margin_secs",
                params={"exchange": "SSE"},
                fields=["ts_code", "name"],
                purpose="Retrieve the latest SSE margin securities.",
            )
        ],
    )

    AnalysisService._normalize_latest_plan_dates(plan, date(2026, 8, 17))

    assert plan.queries[0].params == {
        "exchange": "SSE",
        "trade_date": "20260817",
    }


def test_holder_count_history_drops_missing_metric_before_sorting():
    plan = QueryPlan(
        interpretation="Show shareholder-count history.",
        requirements=[
            {
                "requirement": "Show shareholder-count history.",
                "status": "covered",
                "evidence": "stk_holdernumber provides shareholder counts.",
            }
        ],
        queries=[
            DataQuery(
                query_id="holder-history",
                operation="stk_holdernumber",
                params={"ts_code": "000001.SZ"},
                fields=["ts_code", "end_date", "holder_num"],
                purpose="Retrieve shareholder-count history.",
            )
        ],
        result_pipeline=ResultPipeline.model_validate(
            {
                "source_query_id": "holder-history",
                "output_query_id": "holder-trend",
                "steps": [
                    {"operation": "sort", "field": "end_date", "direction": "asc"}
                ],
            }
        ),
        answer_contract=AnswerContract(
            result_query_id="holder-trend",
            result_kind="table",
            outputs=[
                {"field": "end_date", "description": "Reporting date."},
                {"field": "holder_num", "description": "Shareholder count."},
            ],
        ),
    )

    AnalysisService._normalize_plan_for_request(plan, "Show shareholder-count history.")

    assert [step.operation for step in plan.result_pipeline.steps] == [
        "drop_missing",
        "sort",
    ]
    assert plan.result_pipeline.steps[0].fields == ["holder_num"]


def test_date_range_endpoints_do_not_become_membership_filter():
    plan = QueryPlan(
        interpretation="Count shareholder trades in one year.",
        requirements=[
            {
                "requirement": "Count shareholder trades in one year.",
                "status": "covered",
                "evidence": "The provider exposes bounded announcement dates.",
            }
        ],
        constraints=[
            QueryConstraint(
                constraint_id="year_window",
                scope="result",
                query_id="holder-trades",
                field="ann_date",
                operator="in",
                value=["20260101", "20261231"],
            )
        ],
        queries=[
            DataQuery(
                query_id="holder-trades",
                operation="stk_holdertrade",
                params={"start_date": "20260101", "end_date": "20261231"},
                fields=["ts_code", "ann_date", "in_de"],
                filters=[
                    DataFilter(
                        field="ann_date",
                        operator="in",
                        value=["20260101", "20261231"],
                    )
                ],
                purpose="Retrieve shareholder trades in one year.",
            )
        ],
    )

    AnalysisService._normalize_plan_for_request(plan, "Count 2026 shareholder trades.")

    assert plan.queries[0].filters == []
    assert [(item.operator, item.value) for item in plan.constraints] == [
        ("ge", "20260101"),
        ("le", "20261231"),
    ]
    assert ASharePlanValidator._uses_bounded_date_fanout(plan.queries[0]) is False
    assert ASharePlanValidator._uses_bounded_native_range(plan.queries[0]) is True


def test_grouped_category_counts_remain_a_table():
    plan = QueryPlan(
        interpretation="Count purchases and reductions separately.",
        requirements=[
            {
                "requirement": "Count purchases and reductions separately.",
                "status": "covered",
                "evidence": "The provider exposes a transaction direction.",
            }
        ],
        queries=[
            DataQuery(
                query_id="holder-trades",
                operation="stk_holdertrade",
                params={"start_date": "20260101", "end_date": "20261231"},
                fields=["ts_code", "in_de"],
                purpose="Retrieve shareholder trades.",
            )
        ],
        result_pipeline=ResultPipeline.model_validate(
            {
                "source_query_id": "holder-trades",
                "output_query_id": "direction-counts",
                "steps": [
                    {
                        "operation": "aggregate",
                        "group_by": ["in_de"],
                        "aggregations": [
                            {
                                "output_field": "count",
                                "field": "ts_code",
                                "function": "count",
                            }
                        ],
                    },
                    {
                        "operation": "summarize",
                        "aggregations": [
                            {
                                "output_field": "in_de",
                                "field": "in_de",
                                "function": "first",
                            },
                            {
                                "output_field": "count",
                                "field": "count",
                                "function": "sum",
                            },
                        ],
                    },
                ],
            }
        ),
        answer_contract=AnswerContract(
            result_query_id="direction-counts",
            result_kind="summary",
            outputs=[
                {"field": "in_de", "description": "Transaction direction."},
                {"field": "count", "description": "Disclosure count."},
            ],
        ),
    )

    AnalysisService._normalize_plan_for_request(plan, "Count purchases and reductions.")

    assert [step.operation for step in plan.result_pipeline.steps] == ["aggregate"]
    assert plan.answer_contract.result_kind == "table"


@pytest.mark.parametrize(
    ("prompt", "expected_start", "expected_end"),
    [
        ("\u7edf\u8ba12026\u5e74\u80a1\u4e1c\u589e\u6301\u548c\u51cf\u6301\u6b21\u6570", "20260101", "20261231"),
        (
            "Count shareholder purchases and reductions in 2025.",
            "20250101",
            "20251231",
        ),
    ],
)
def test_annual_holder_trade_counts_compile_deterministically(
    prompt,
    expected_start,
    expected_end,
):
    plan = AnalysisService._compile_known_request(prompt)

    assert plan is not None
    assert [query.operation for query in plan.queries] == ["stk_holdertrade"]
    assert plan.queries[0].params == {
        "start_date": expected_start,
        "end_date": expected_end,
    }
    assert ASharePlanValidator._uses_bounded_date_fanout(plan.queries[0]) is False
    assert [step.operation for step in plan.result_pipeline.steps] == [
        "compare_scalar",
        "compare_scalar",
        "summarize",
    ]
    assert [step.value for step in plan.result_pipeline.steps[:2]] == ["IN", "DE"]
    assert {output.field for output in plan.answer_contract.outputs} == {
        "purchase_count",
        "reduction_count",
    }


def test_multi_factor_valuation_confirmation_explains_screen_and_order():
    prompt = (
        "\u627e\u4f4ePE\u3001\u4f4ePB\u3001\u9ad8\u80a1\u606f\u7387\u7684\u5341\u53ea\u80a1\u7968\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260817\n"
        "event_end_date=20260817\n"
        "</trusted_analysis_window>"
    )

    plan = AnalysisService._compile_known_request(prompt)

    assert plan is not None
    assert "20260817" in plan.interpretation
    assert "lower 30 percent" in plan.interpretation
    assert "10 highest trailing dividend yields" in plan.interpretation


def test_multi_turn_valuation_refinement_preserves_metric_and_explicit_order():
    plan = _execution_plan(label_field="name", detail_field="dv_ttm")
    plan.queries = [
        DataQuery(
            query_id="multi_factor_valuation_snapshot",
            operation="daily_basic",
            params={"trade_date": "20260817"},
            fields=["ts_code", "trade_date", "pe", "pb", "dv_ttm"],
            purpose="Retrieve the validated valuation snapshot.",
        )
    ]
    plan.intent = None
    plan.execution_plan = None
    plan.result_pipeline = None
    prompt = (
        "\u53ea\u4fdd\u7559\u80a1\u606f\u7387\u5927\u4e8e0\u7684\uff0c\u6309\u80a1\u606f\u7387\u4ece\u9ad8\u5230\u4f4e\u7ed9\u6211\u524d5\u5bb6\uff0c"
        "\u4fdd\u7559\u4ee3\u7801\u3001\u540d\u79f0\u3001\u5e02\u76c8\u7387\u3001\u5e02\u51c0\u7387\u548c\u80a1\u606f\u7387"
    )
    class StaticPlanner:
        name = "planner"

        def plan_validated(self, request, operations, validate):
            assert "<analysis_conversation_context>" in request.prompt
            return validate(plan.model_copy(deep=True))

    provider = _CatalogProvider()
    service = AnalysisService(
        planner=StaticPlanner(),
        provider=provider,
        validator=ASharePlanValidator(provider),
        executor=DataQueryExecutor(provider),
    )
    planning_prompt = (
        prompt
        + "\n\n<trusted_analysis_window>\n"
        + "event_start_date=20260817\n"
        + "event_end_date=20260817\n"
        + "</trusted_analysis_window>"
    )

    result = service._plan_with_request_context(
        "multi-turn-test",
        AnalysisRequest(
            prompt=planning_prompt,
            conversation=[
                AnalysisConversationTurn(
                    prompt="\u627e\u4f4ePE\u3001\u4f4ePB\u3001\u9ad8\u80a1\u606f\u7387\u7684\u5341\u53ea\u80a1\u7968",
                    interpretation=(
                        "Using the completed 20260817 valuation snapshot."
                    ),
                )
            ],
        ),
        prompt,
        [],
    )

    assert [query.operation for query in result.queries] == [
        "daily_basic",
        "stock_basic",
    ]
    assert result.queries[0].fields == [
        "ts_code",
        "pe",
        "pb",
        "dv_ttm",
    ]
    assert [step.operation for step in result.result_pipeline.steps] == [
        "drop_missing",
        "filter",
        "filter",
        "quantile_filter",
        "quantile_filter",
        "filter",
        "sort",
        "limit",
        "join_fields",
    ]
    filter_step = result.result_pipeline.steps[5]
    sort_step = result.result_pipeline.steps[6]
    limit_step = result.result_pipeline.steps[7]
    join_step = result.result_pipeline.steps[8]
    assert (filter_step.field, filter_step.comparison, filter_step.value) == (
        "dv_ttm",
        "gt",
        0,
    )
    assert (sort_step.field, sort_step.direction) == ("dv_ttm", "desc")
    assert limit_step.count == 5
    assert join_step.right_source_query_id == "conversation_valuation_names"
    assert {output.field for output in result.answer_contract.outputs} == {
        "ts_code",
        "name",
        "pe",
        "pb",
        "dv_ttm",
    }
    assert "previously confirmed valuation cohort" in result.interpretation
    assert "dv_ttm > 0" in result.interpretation
    assert "first 5 rows" in result.interpretation
    ASharePlanValidator(_CatalogProvider()).validate(result)


@pytest.mark.parametrize(
    ("follow_up", "comparison", "threshold", "direction", "limit"),
    [
        (
            "\u53ea\u4fdd\u7559\u6bcf\u80a1\u7a0e\u524d\u73b0\u91d1\u5206\u7ea2\u5927\u4e8e0.2\u7684\uff0c"
            "\u6309\u5206\u7ea2\u4ece\u4f4e\u5230\u9ad8\u7ed9\u6211\u524d6\u5bb6\uff0c\u4ecd\u4fdd\u7559\u4ee3\u7801\u3001\u540d\u79f0\u3001\u5206\u7ea2\u548c\u516c\u544a\u65e5\u671f",
            "gt",
            0.2,
            "asc",
            6,
        ),
        (
            "\u5206\u7ea2\u4e0d\u5c11\u4e8e2\u5143\uff0c\u6309\u5206\u7ea2\u4ece\u9ad8\u5230\u4f4e\u7ed9\u6211\u524d4\u5bb6",
            "ge",
            2.0,
            "desc",
            4,
        ),
    ],
)
def test_multi_turn_cash_dividend_refinement_preserves_confirmed_cohort(
    follow_up,
    comparison,
    threshold,
    direction,
    limit,
):
    candidate = _execution_plan(label_field="name", detail_field="cash_div_tax")
    candidate.intent = None
    candidate.execution_plan = None
    candidate.result_pipeline = None
    context_prompt = (
        "<analysis_conversation_context>\n"
        '<turn index="1">\n'
        "user_request=\"\\u8bf7\\u5217\\u51fa2026\\u5e74\\u7b2c\\u4e8c\\u5b63\\u5ea6\\u6bcf\\u80a1\\u7a0e\\u524d\\u73b0\\u91d1\\u5206\\u7ea2\\u6700\\u9ad8\\u7684\\u524d20\\u5bb6A\\u80a1\\u516c\\u53f8\"\n"
        "validated_interpretation=\"Rank the top 20 A-share companies from 20260401 through 20260630.\"\n"
        "</turn>\n"
        "</analysis_conversation_context>\n"
        "<current_analysis_request>\n"
        f"{follow_up}\n"
        "</current_analysis_request>"
    )

    normalized = AnalysisService._normalize_plan_for_request(
        candidate,
        context_prompt,
    )

    assert normalized.result_pipeline.source_query_id == (
        "ranked_cash_dividend_disclosures"
    )
    assert [step.operation for step in normalized.result_pipeline.steps] == [
        "drop_missing",
        "inner_join",
        "sort",
        "distinct",
        "sort",
        "limit",
        "filter",
        "sort",
        "limit",
        "select_fields",
    ]
    filter_step = normalized.result_pipeline.steps[6]
    sort_step = normalized.result_pipeline.steps[7]
    limit_step = normalized.result_pipeline.steps[8]
    assert (filter_step.field, filter_step.comparison, filter_step.value) == (
        "cash_div_tax",
        comparison,
        threshold,
    )
    assert (sort_step.field, sort_step.direction) == ("cash_div_tax", direction)
    assert limit_step.count == limit
    assert normalized.result_pipeline.steps[5].count == 20
    assert {output.field for output in normalized.answer_contract.outputs} == {
        "ts_code",
        "name",
        "cash_div_tax",
        "ann_date",
    }
    ASharePlanValidator(_CatalogProvider()).validate(normalized)


def test_multi_turn_disclosure_refinement_preserves_cohort_and_date_order():
    candidate = _execution_plan(label_field="name", detail_field="ann_date")
    candidate.intent = None
    candidate.execution_plan = None
    candidate.result_pipeline = None
    current_prompt = (
        "\u53ea\u4fdd\u75592026\u5e746\u6708\u516c\u544a\u7684\uff0c\u6309\u516c\u544a\u65e5\u671f\u4ece\u65e7\u5230\u65b0\u5217\u51fa\u524d15\u5bb6\uff0c"
        "\u4ecd\u4fdd\u7559\u4ee3\u7801\u3001\u540d\u79f0\u548c\u516c\u544a\u65e5\u671f"
    )
    context_prompt = (
        "<analysis_conversation_context>\n"
        '<turn index="1">\n'
        "user_request=\"\\u5217\\u51fa2026\\u5e74\\u7b2c\\u4e8c\\u5b63\\u5ea6\\u5ba3\\u5e03\\u56de\\u8d2d\\u7684\\u5168\\u90e8A\\u80a1\\u516c\\u53f8\\uff0c\\u6309\\u516c\\u544a\\u65e5\\u671f\\u4ece\\u65b0\\u5230\\u65e7\\u7ed9\\u51fa\\u4ee3\\u7801\\u3001\\u540d\\u79f0\\u548c\\u516c\\u544a\\u65e5\\u671f\\uff0c\\u4e0d\\u8981\\u53ea\\u7ed9\\u603b\\u6570\"\n"
        "validated_interpretation=\"List distinct A-share companies with repurchase disclosures announced from 20260401 through 20260630.\"\n"
        "</turn>\n"
        "</analysis_conversation_context>\n"
        "<current_analysis_request>\n"
        f"{current_prompt}\n"
        "</current_analysis_request>"
    )

    normalized = AnalysisService._normalize_plan_for_request(
        candidate,
        context_prompt,
    )

    assert [query.operation for query in normalized.queries] == [
        "repurchase",
        "stock_basic",
    ]
    assert [step.operation for step in normalized.result_pipeline.steps] == [
        "latest_by_group",
        "join_fields",
        "filter",
        "filter",
        "sort",
        "limit",
        "select_fields",
    ]
    start_filter, end_filter = normalized.result_pipeline.steps[2:4]
    assert (start_filter.field, start_filter.comparison, start_filter.value) == (
        "ann_date",
        "ge",
        "20260601",
    )
    assert (end_filter.field, end_filter.comparison, end_filter.value) == (
        "ann_date",
        "le",
        "20260630",
    )
    assert normalized.result_pipeline.steps[4].direction == "asc"
    assert normalized.result_pipeline.steps[5].count == 15
    assert normalized.result_pipeline.steps[6].fields == [
        "ts_code",
        "name",
        "ann_date",
    ]
    ASharePlanValidator(_CatalogProvider()).validate(normalized)


def test_repurchase_count_and_list_compiles_at_distinct_company_grain():
    prompt = (
        "2026\u5e746\u6708\u5ba3\u5e03\u56de\u8d2d\u7684A\u80a1\u516c\u53f8\u6709\u591a\u5c11\u5bb6\uff1f\u8bf7\u5217\u51fa\u5168\u90e8"
        "\u516c\u53f8\u4ee3\u7801\u548c\u540d\u79f0\uff0c\u4e0d\u8981\u53ea\u7ed9\u603b\u6570\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260601\n"
        "event_end_date=20260630\n"
        "</trusted_analysis_window>"
    )

    plan = AnalysisService._compile_known_request(prompt)

    assert plan is not None
    assert [query.operation for query in plan.queries] == ["repurchase", "stock_basic"]
    assert plan.queries[0].params == {
        "start_date": "20260601",
        "end_date": "20260630",
    }
    assert [step.operation for step in plan.result_pipeline.steps] == [
        "latest_by_group",
        "join_fields",
        "select_fields",
    ]
    assert plan.result_pipeline.steps[0].group_by == ["ts_code"]
    assert plan.answer_contract.result_kind == "table"
    assert {output.field for output in plan.answer_contract.outputs} == {
        "ts_code",
        "name",
        "ann_date",
    }


def test_repurchase_company_list_preserves_explicit_announcement_date_order():
    prompt = (
        "\u5217\u51fa2026\u5e74\u7b2c\u4e8c\u5b63\u5ea6\u5ba3\u5e03\u56de\u8d2d\u7684\u5168\u90e8A\u80a1\u516c\u53f8\uff0c"
        "\u6309\u516c\u544a\u65e5\u671f\u4ece\u65b0\u5230\u65e7\u7ed9\u51fa\u4ee3\u7801\u3001\u540d\u79f0\u548c\u65e5\u671f"
    )

    plan = AnalysisService._compile_known_request(prompt)

    assert plan is not None
    assert plan.queries[0].params == {
        "start_date": "20260401",
        "end_date": "20260630",
    }
    sort_step = next(
        step for step in plan.result_pipeline.steps if step.operation == "sort"
    )
    assert sort_step.field == "ann_date"
    assert sort_step.direction == "desc"
    assert "announcement date descending" in plan.interpretation


def test_quarterly_cash_dividend_ranking_keeps_one_highest_disclosure_per_company():
    prompt = (
        "\u8bf7\u5217\u51fa2026\u5e74\u7b2c\u4e8c\u5b63\u5ea6\u6bcf\u80a1\u7a0e\u524d\u73b0\u91d1\u5206\u7ea2\u6700\u9ad8\u7684"
        "\u524d20\u5bb6A\u80a1\u516c\u53f8\uff0c\u7ed9\u51fa\u4ee3\u7801\u3001\u540d\u79f0\u3001\u5206\u7ea2\u548c\u516c\u544a\u65e5\u671f"
    )

    plan = AnalysisService._compile_known_request(prompt)

    assert plan is not None
    assert [query.operation for query in plan.queries] == ["dividend", "stock_basic"]
    assert plan.queries[0].params == {
        "start_date": "20260401",
        "end_date": "20260630",
    }
    assert ASharePlanValidator._uses_bounded_date_fanout(plan.queries[0]) is True
    assert [step.operation for step in plan.result_pipeline.steps] == [
        "drop_missing",
        "inner_join",
        "sort",
        "distinct",
        "sort",
        "limit",
        "select_fields",
    ]
    assert plan.result_pipeline.steps[3].fields == ["ts_code"]
    assert plan.result_pipeline.steps[5].count == 20
    assert {output.field for output in plan.answer_contract.outputs} == {
        "ts_code",
        "name",
        "cash_div_tax",
        "ann_date",
    }


def test_positive_cash_dividend_ranking_filters_before_ascending_company_rank():
    prompt = (
        "\u8bf7\u5217\u51fa2026\u5e74\u7b2c\u4e00\u5b63\u5ea6\u6bcf\u80a1\u7a0e\u524d\u73b0\u91d1\u5206\u7ea2\u6700\u4f4e\u4e14"
        "\u5927\u4e8e0\u7684\u524d8\u5bb6A\u80a1\u516c\u53f8\uff0c\u7ed9\u51fa\u4ee3\u7801\u3001\u540d\u79f0\u3001\u5206\u7ea2\u548c\u516c\u544a\u65e5\u671f"
    )

    plan = AnalysisService._compile_known_request(prompt)

    assert plan is not None
    assert [step.operation for step in plan.result_pipeline.steps] == [
        "drop_missing",
        "filter",
        "inner_join",
        "sort",
        "distinct",
        "sort",
        "limit",
        "select_fields",
    ]
    positive_filter = plan.result_pipeline.steps[1]
    assert (positive_filter.field, positive_filter.comparison, positive_filter.value) == (
        "cash_div_tax",
        "gt",
        0,
    )
    assert plan.result_pipeline.steps[3].direction == "asc"
    assert plan.result_pipeline.steps[5].direction == "asc"
    assert plan.result_pipeline.steps[6].count == 8
    assert "lowest positive" in plan.interpretation


@pytest.mark.parametrize(
    ("prompt", "expected_limit"),
    [
        ("A股2026年最新披露数据，筹码集中度top10公司", 10),
        ("A股2025年筹码集中度前20家公司", 20),
    ],
)
def test_holder_concentration_ranking_compiles_derived_change_contract(
    prompt,
    expected_limit,
):
    plan = _execution_plan(
        label_field="name",
        detail_field="holder_change_pct",
    )

    AnalysisService._normalize_plan_for_request(plan, prompt)

    assert plan.execution_plan is None
    assert [query.operation for query in plan.queries] == [
        "stock_basic",
        "stk_holdernumber",
    ]
    assert plan.queries[1].fields == [
        "ts_code",
        "ann_date",
        "end_date",
        "holder_num",
    ]
    assert ASharePlanValidator._uses_bounded_date_fanout(plan.queries[1]) is True
    assert [step.operation for step in plan.result_pipeline.steps] == [
        "pct_change",
        "derive",
        "latest_by_group",
        "drop_missing",
        "sort",
        "limit",
        "join_fields",
    ]
    assert plan.result_pipeline.steps[0].output_field == "holder_change_ratio"
    assert plan.result_pipeline.steps[1].output_field == "holder_change_pct"
    assert plan.result_pipeline.steps[4].direction == "asc"
    assert plan.result_pipeline.steps[5].count == expected_limit
    assert {output.field for output in plan.answer_contract.outputs} == {
        "ts_code",
        "name",
        "ann_date",
        "end_date",
        "holder_num",
        "holder_change_pct",
    }


def test_trusted_snapshot_date_compiles_without_model_query_nodes():
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")
    plan.execution_plan = None
    plan.answer_contract = None
    plan.queries = []
    plan.feasibility = "unsupported"
    prompt = (
        "A股2026年汽车行业，市盈率和分红数据\n\n"
        "<trusted_analysis_window>\n"
        "event_start_date=20260807\n"
        "event_end_date=20260807\n"
        "</trusted_analysis_window>"
    )

    AnalysisService._normalize_plan_for_request(plan, prompt)

    assert plan.feasibility == "supported"
    assert plan.queries[1].params == {"trade_date": "20260807"}
    assert plan.result_pipeline.output_query_id == "industry_valuation_dividend_result"


def test_single_security_main_moneyflow_compiles_from_trusted_context():
    prompt = (
        "\u67e5\u8be2\u5e73\u5b89\u94f6\u884c\u6628\u5929\u4e3b\u529b\u8d44\u91d1\u51c0\u6d41\u5165\n"
        "<trusted_security>\nname=Ping An Bank\nts_code=000001.SZ\n"
        "</trusted_security>\n<trusted_analysis_window>\n"
        "event_start_date=20260817\nevent_end_date=20260817\n"
        "</trusted_analysis_window>"
    )

    plan = AnalysisService._compile_known_request(prompt)

    assert plan is not None
    assert len(plan.queries) == 1
    query = plan.queries[0]
    assert query.operation == "moneyflow"
    assert query.params == {"ts_code": "000001.SZ", "trade_date": "20260817"}
    assert query.fields == ["ts_code", "trade_date", "net_mf_amount"]
    assert plan.answer_contract.result_kind == "table"
    assert [output.field for output in plan.answer_contract.outputs] == [
        "ts_code",
        "trade_date",
        "net_mf_amount",
    ]
    ASharePlanValidator(_CatalogProvider()).validate(plan)


def test_moneyflow_followup_inherits_security_and_date_and_derives_components():
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")
    contextual_prompt = (
        "<analysis_conversation_context>\n<turn index=\"1\">\n"
        "user_request=\"query\"\n"
        "validated_interpretation=\"Return the native main-fund net inflow for "
        "000001.SZ on 20260817.\"\n</turn>\n"
        "</analysis_conversation_context>\n<current_analysis_request>\n"
        "\u518d\u628a\u5927\u5355\u548c\u5c0f\u5355\u51c0\u6d41\u5165\u4e00\u8d77\u5217\u51fa\u6765\uff0c"
        "\u5e76\u7ed9\u51fa\u4ea4\u6613\u65e5\u671f\n</current_analysis_request>"
    )

    normalized = AnalysisService._normalize_plan_for_request(
        plan,
        contextual_prompt,
    )

    query = normalized.queries[0]
    assert query.operation == "moneyflow"
    assert query.params == {"ts_code": "000001.SZ", "trade_date": "20260817"}
    assert query.fields == [
        "ts_code",
        "trade_date",
        "buy_lg_amount",
        "sell_lg_amount",
        "buy_sm_amount",
        "sell_sm_amount",
    ]
    assert [step.operation for step in normalized.result_pipeline.steps] == [
        "derive",
        "derive",
        "select_fields",
    ]
    assert [output.field for output in normalized.answer_contract.outputs] == [
        "ts_code",
        "trade_date",
        "net_lg_amount",
        "net_sm_amount",
    ]
    ASharePlanValidator(_CatalogProvider()).validate(normalized)

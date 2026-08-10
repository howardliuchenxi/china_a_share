import pytest

from china_a_share.application.workflow import ASharePlanValidator, AnalysisService
from china_a_share.core.contracts import DataOperation, QueryPlan
from china_a_share.planners.deepseek import DeepSeekQueryPlanner


class _CatalogProvider:
    name = "tushare"

    @staticmethod
    def supports(operation: str) -> bool:
        return operation in {"stock_basic", "daily_basic", "dividend"}


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


def test_supported_requirement_with_implementation_normalizes_to_covered():
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")
    plan.requirements[0].status = "unsupported"

    DeepSeekQueryPlanner._normalize_requirement_statuses(plan)

    assert plan.requirements[0].status == "covered"


def test_requirement_without_implementation_remains_unsupported():
    plan = _execution_plan(label_field="name", detail_field="cash_div_tax")
    plan.requirements[0].status = "unsupported"
    plan.requirements[0].implementation = None

    DeepSeekQueryPlanner._normalize_requirement_statuses(plan)

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
    assert plan.queries[0].filters[0].value == expected_industry
    assert [row_filter.value for row_filter in plan.queries[2].filters] == [
        f"{expected_year}0101",
        f"{expected_year}1231",
    ]
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

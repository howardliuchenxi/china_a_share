"""Field-unit facts live in the registry and travel with every dataset.

Unit facts have exactly one machine-readable source (registry.FIELD_UNIT_NOTES).
Dataset payloads returned to every model embed them as field_units, and the
catalog guidance documents the same facts in prose — so no runtime needs
its own unit special cases.
"""

import pandas as pd

from china_a_share.feishu_agent import ResearchToolbox, _result_payload
from china_a_share.glm_agent import GLM_RECOVERY_INSTRUCTIONS
from china_a_share.core.contracts import QueryResult, QueryStatus
from china_a_share.registry import (
    FIELD_UNIT_NOTES,
    TushareOperationCatalog,
    field_unit_notes_for,
)


def guidance_for(operation: str) -> str:
    catalog = TushareOperationCatalog()
    return next(
        item.description
        for item in catalog.search("成交额")
        if item.name == operation
    )


def test_daily_guidance_documents_amount_and_volume_units():
    guidance = guidance_for("daily")

    assert "amount is in thousands of CNY" in guidance
    assert "vol is in lots" in guidance


def test_moneyflow_and_repurchase_guidance_document_wan_units():
    assert "ten thousands of CNY" in guidance_for("moneyflow")
    assert "ten thousands of CNY" in guidance_for("repurchase")


def test_ths_board_fund_flow_guidance_documents_net_field_semantics():
    industry = guidance_for("moneyflow_ind_ths")
    assert "net_amount is" in industry
    assert "GROSS inflow despite its name" in industry
    assert "CNY 100 million units" in industry
    assert "without names" in industry
    assert "ths_index" in industry

    concept = guidance_for("moneyflow_cnt_ths")
    assert "GROSS inflow despite its name" in concept
    assert "CNY 100 million units" in concept
    assert "market-attribute pools" in concept


def test_ths_taxonomy_guidance_documents_hierarchy_and_membership():
    taxonomy = guidance_for("ths_index")
    assert "type='I'" in taxonomy
    assert "881xxx" in taxonomy
    assert "count" in taxonomy

    member = guidance_for("ths_member")
    assert "con_code" in member
    assert ".TI" in member


def test_runtime_prompt_requires_ths_taxonomy_discipline_without_field_facts():
    from china_a_share.codex_agent import _developer_instructions

    instructions = _developer_instructions()

    assert "THS taxonomy contract" in instructions
    assert "同花顺二级行业" in instructions
    assert "同花顺概念板块" in instructions
    # Field-level semantics live only in the catalog guidance.
    assert "net_amount" not in instructions
    assert "net_buy_amount" not in instructions


def test_structured_unit_table_covers_every_documented_operation():
    for operation in FIELD_UNIT_NOTES:
        guidance = guidance_for(operation)
        assert "Unit note" in guidance, (
            f"{operation} has structured units but no prose unit note"
        )


def test_field_unit_notes_filter_to_requested_columns():
    notes = field_unit_notes_for(
        "daily", ["ts_code", "trade_date", "close", "amount"]
    )

    assert notes == {"amount": "thousands of CNY (千元)"}
    assert field_unit_notes_for("stock_basic", ["industry"]) == {}


def _query_result(operation: str, columns: list) -> QueryResult:
    return QueryResult(
        query_id="unit-check",
        provider="tushare",
        operation=operation,
        status=QueryStatus.SUCCESS,
        columns=columns,
        rows=[{column: 1 for column in columns}],
    )


def test_dataset_payload_embeds_field_units_for_every_runtime():
    payload = _result_payload(
        _query_result("daily", ["ts_code", "trade_date", "amount", "vol"])
    )

    assert payload["field_units"] == {
        "amount": "thousands of CNY (千元)",
        "vol": "lots (手)",
    }


def test_dataset_payload_omits_field_units_when_none_apply():
    payload = _result_payload(
        _query_result("stock_basic", ["ts_code", "industry"])
    )

    assert "field_units" not in payload


def test_runtime_prompts_do_not_embed_provider_schema_facts():
    # Unit facts belong to the registry and dataset payloads; a runtime
    # prompt may only require the generic discipline of reading them.
    assert "Tushare daily.amount" not in GLM_RECOVERY_INSTRUCTIONS
    assert "thousands of CNY" not in GLM_RECOVERY_INSTRUCTIONS
    assert "Unit discipline" in GLM_RECOVERY_INSTRUCTIONS
    assert "field_units" in GLM_RECOVERY_INSTRUCTIONS


def test_toolbox_query_result_carries_units_end_to_end():
    class UnitProvider:
        name = "tushare"

        def search_operations(self, prompt):
            return []

        def supports(self, operation):
            return operation == "daily"

        def describe_query_shapes(self, operation):
            return ()

        def validate_query(self, operation, params, fields):
            return None

        def query(self, operation, params, fields, **kwargs):
            return pd.DataFrame(
                [{"ts_code": "000001.SZ", "trade_date": "20260918", "amount": 860000}]
            )

    toolbox = ResearchToolbox(UnitProvider(), "request-units")
    payload = toolbox.call(
        "query_market_data",
        {"operation": "daily", "params": {"trade_date": "20260918"}, "fields": ["ts_code", "trade_date", "amount"]},
        lambda stage, message: None,
    )

    assert payload["field_units"]["amount"] == "thousands of CNY (千元)"

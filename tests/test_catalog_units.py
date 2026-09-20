"""Field-unit documentation lives in the shared operation catalog.

Every runtime (Codex MCP toolbox and the GLM chat loop) reads the same
catalog guidance, so monetary unit facts must be documented here rather
than baked into any single runtime's prompt.
"""

from china_a_share.glm_agent import GLM_RECOVERY_INSTRUCTIONS
from china_a_share.registry import TushareOperationCatalog


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


def test_runtime_prompts_do_not_embed_provider_schema_facts():
    # Unit facts belong to the catalog; a runtime prompt may only require the
    # generic discipline of reading units from the catalog documentation.
    assert "Tushare daily.amount" not in GLM_RECOVERY_INSTRUCTIONS
    assert "Unit discipline" in GLM_RECOVERY_INSTRUCTIONS

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


def test_runtime_prompts_do_not_embed_provider_schema_facts():
    # Unit facts belong to the catalog; a runtime prompt may only require the
    # generic discipline of reading units from the catalog documentation.
    assert "Tushare daily.amount" not in GLM_RECOVERY_INSTRUCTIONS
    assert "Unit discipline" in GLM_RECOVERY_INSTRUCTIONS

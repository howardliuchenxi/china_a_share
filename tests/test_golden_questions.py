"""Validate the breadth and enforced boundaries of the golden question matrix."""

from collections import Counter

from china_a_share.core.contracts import AnalysisRequest
from china_a_share.registry import STOCK_API_NAMES
from china_a_share.tasks import requires_async_analysis

from golden_questions import GOLDEN_QUESTION_FAMILIES


MINIMUM_GOLDEN_QUESTION_COUNT = 60
MINIMUM_GOLDEN_FAMILY_COUNT = 20
VALID_TIERS = {"supported", "approximation", "unsupported"}
VALID_DELIVERY_MODES = {"sync", "async"}


def test_golden_question_matrix_has_broad_unique_coverage():
    prompts = [
        prompt
        for family in GOLDEN_QUESTION_FAMILIES
        for prompt in family["prompts"]
    ]
    family_names = [family["family"] for family in GOLDEN_QUESTION_FAMILIES]

    assert len(prompts) >= MINIMUM_GOLDEN_QUESTION_COUNT
    assert len(set(prompts)) == len(prompts)
    assert len(set(family_names)) >= MINIMUM_GOLDEN_FAMILY_COUNT
    assert len(set(family_names)) == len(family_names)
    assert all(len(family["prompts"]) >= 3 for family in GOLDEN_QUESTION_FAMILIES)


def test_golden_question_matrix_uses_known_contract_values():
    catalog_operations = set(STOCK_API_NAMES)
    tier_counts = Counter()

    for family in GOLDEN_QUESTION_FAMILIES:
        assert family["tier"] in VALID_TIERS
        assert family["delivery"] in VALID_DELIVERY_MODES
        assert set(family["operations"]).issubset(catalog_operations)
        if family["tier"] == "unsupported":
            assert family["operations"] == []
        tier_counts[family["tier"]] += len(family["prompts"])

    assert tier_counts["supported"] > tier_counts["unsupported"]
    assert tier_counts["approximation"] >= 3
    assert tier_counts["unsupported"] >= 9


def test_golden_question_delivery_modes_are_enforced_by_the_router():
    prompt_expectations = [
        (prompt, family["delivery"] == "async")
        for family in GOLDEN_QUESTION_FAMILIES
        for prompt in family["prompts"]
    ]

    assert all(
        requires_async_analysis(AnalysisRequest(prompt=prompt)) == expected
        for prompt, expected in prompt_expectations
    )

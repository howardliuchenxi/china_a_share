"""Unified paid live-analysis cases loaded from the canonical Git catalog."""

from china_a_share.e2e_cases import load_live_case_catalog


LIVE_ANALYSIS_CASES = [
    {
        "family": case.family,
        "tier": case.tier,
        "operations": case.operations,
        "quality_invariants": case.quality_invariants,
        "prompt": case.prompt,
    }
    for case in load_live_case_catalog().cases
    if case.source == "matrix"
]

LIVE_REGRESSION_CASES = [
    {
        "name": case.name,
        "prompt": case.prompt,
        "expected_feasibility": case.expected_feasibility,
    }
    for case in load_live_case_catalog().cases
    if case.source == "reported_regression"
]

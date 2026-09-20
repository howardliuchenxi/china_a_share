"""Tests for the LLM rule compiler that maps free text to validated rules."""

from china_a_share.discovery.rule_compiler import (
    LlmRuleCompiler,
    RuleCompileResult,
    build_rule_compile_prompt,
)
from china_a_share.discovery.strategy_models import (
    CumulativeReturnRule,
    DrawdownRule,
    FirstBullishMARule,
    LimitUpRule,
)


def test_prompt_documents_every_indicator_and_both_output_shapes():
    prompt = build_rule_compile_prompt("最近交易日涨停")
    for marker in (
        "drawdown",
        "cumulative_return",
        "first_bullish_ma",
        "limit_up",
        '"rules"',
        '"candidates"',
        "涨停",
    ):
        assert marker in prompt
    assert "最近交易日涨停" in prompt


def test_parse_response_accepts_compiled_rules():
    raw = (
        '```json\n{"rules": ['
        '{"type": "drawdown", "window": 60, "threshold": 0.30},'
        '{"type": "cumulative_return", "window": 10, "min_return": -0.10, "max_return": 0.10},'
        '{"type": "first_bullish_ma", "fast_window": 5, "slow_window": 10},'
        '{"type": "limit_up"}'
        "]}\n```"
    )
    result = LlmRuleCompiler.parse_response(raw)
    assert result == RuleCompileResult(
        rules=[
            DrawdownRule(window=60, threshold=0.30),
            CumulativeReturnRule(window=10, min_return=-0.10, max_return=0.10),
            FirstBullishMARule(fast_window=5, slow_window=10),
            LimitUpRule(window=1),
        ]
    )


def test_parse_response_rejects_rule_payloads_failing_validation():
    bad_window = '{"rules": [{"type": "drawdown", "window": 60, "threshold": 1.5}]}'
    assert LlmRuleCompiler.parse_response(bad_window) is None
    inverted_ma = (
        '{"rules": [{"type": "first_bullish_ma", "fast_window": 10, "slow_window": 5}]}'
    )
    assert LlmRuleCompiler.parse_response(inverted_ma) is None
    unknown_type = '{"rules": [{"type": "volume_surge", "window": 5}]}'
    assert LlmRuleCompiler.parse_response(unknown_type) is None
    empty_rules = '{"rules": []}'
    assert LlmRuleCompiler.parse_response(empty_rules) is None


def test_parse_response_returns_clean_candidate_strings():
    raw = (
        '{"candidates": ["涨停", "涨停 窗口=3", "", 42, "涨停", "   "]}'
    )
    result = LlmRuleCompiler.parse_response(raw)
    assert result == RuleCompileResult(candidates=["涨停", "涨停 窗口=3"])


def test_parse_response_returns_none_for_unusable_payloads():
    for raw in ("不是JSON", "[]", '{"answer": "不支持"}', '{"rules": "回撤"}'):
        assert LlmRuleCompiler.parse_response(raw) is None


def test_compile_returns_none_when_llm_call_fails():
    def failing(prompt):
        raise RuntimeError("network down")

    assert LlmRuleCompiler(failing).compile("随便什么") is None

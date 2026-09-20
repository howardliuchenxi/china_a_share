"""Compile free-text strategy conditions into validated rules via the LLM."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
from typing import Any, Callable, Optional, Protocol

from pydantic import ValidationError

from china_a_share.discovery.strategy_models import Rule


logger = logging.getLogger(__name__)

MAX_CANDIDATE_INTERPRETATIONS = 3
_JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

_RULE_VOCABULARY = """可用的条件类型（只有这四种，参数为十进制小数，30% 写成 0.30）：
- {"type": "drawdown", "window": 60, "threshold": 0.30}
  含义：window 个交易日内，复权最高价到当日复权收盘的累计回撤大于 threshold（严格大于，0<threshold<1）。
- {"type": "cumulative_return", "window": 10, "min_return": -0.10, "max_return": 0.10}
  含义：window 个交易日前的收盘到当日收盘的累计涨跌幅介于 [min_return, max_return]（闭区间，min<=max）。
- {"type": "first_bullish_ma", "fast_window": 5, "slow_window": 10}
  含义：MA(fast) 与 MA(slow) 首次形成多头向上（当日 MA快>MA慢 且两者均较昨日上升，且昨日不满足），fast<slow。
- {"type": "limit_up", "window": 1}
  含义：近 window 个交易日内出现过收盘涨停（含当日）。window 可省略，默认 1。按交易所口径精确判定，含板块涨跌幅限制。"""

_DSL_GRAMMAR = """候选解释必须写成这种可直接解析的中文 DSL 文本（不要解释、不要加引号）：
- 回撤 窗口=60 阈值=30%
- 涨幅 窗口=10 下限=-10% 上限=10%
- 金叉 快线=5 慢线=10
- 涨停
- 涨停 窗口=3
多条用「；」分隔，例如：回撤 窗口=60 阈值=30%；涨幅 窗口=10 下限=-10% 上限=10%；金叉 快线=5 慢线=10"""


def build_rule_compile_prompt(text: str) -> str:
    """Return the instruction prompt that maps one free-text rule to JSON."""
    return (
        "你是A股策略条件编译器。用户会用一句话描述选股条件，你的任务是判断它能否"
        "完全由下列条件类型表达。\n\n"
        f"{_RULE_VOCABULARY}\n\n"
        "只输出一个 JSON 对象，两种形态二选一：\n"
        '1. 能完全表达：{"rules": [<条件对象>, ...]}，各条件为逻辑与。\n'
        '2. 不能完全表达或存在明显歧义：{"candidates": [<DSL文本>, ...]}，'
        "给出 2-3 个最接近用户意图、且能用条件类型表达的候选解释；"
        "若没有任何接近的表达则给空数组。\n\n"
        f"{_DSL_GRAMMAR}\n\n"
        "规则：忽略与行情条件无关的内容（如持仓建议、仓位管理）；"
        "百分比换算成小数；不要编造无法由上述类型表达的条件；输出只有 JSON，别无其他文字。\n\n"
        f"用户描述：{text}"
    )


@dataclass(frozen=True)
class RuleCompileResult:
    """One compiler outcome: either compiled rules or DSL candidate texts."""

    rules: list[Rule] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)


class RuleCompiler(Protocol):  # noqa: D101 - documented via methods below.
    def compile(self, text: str) -> Optional[RuleCompileResult]:
        """Compile one free-text description or return None when unavailable."""
        ...


class LlmRuleCompiler:
    """Compile free text through an LLM and re-validate everything locally."""

    def __init__(self, generate_text: Callable[[str], str]) -> None:
        self._generate_text = generate_text

    def compile(self, text: str) -> Optional[RuleCompileResult]:
        """Return validated rules, DSL candidates, or None on any failure."""
        try:
            raw = self._generate_text(build_rule_compile_prompt(text))
        except Exception:
            logger.warning("rule compile LLM call failed", exc_info=True)
            return None
        return self.parse_response(raw)

    @staticmethod
    def parse_response(raw: str) -> Optional[RuleCompileResult]:
        """Validate one LLM response into rules or DSL candidates."""
        try:
            payload = json.loads(_JSON_FENCE_PATTERN.sub("", raw.strip()))
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        rules = payload.get("rules")
        if isinstance(rules, list):
            validated: list[Rule] = []
            try:
                for item in rules:
                    if not isinstance(item, dict):
                        return None
                    validated.append(_validate_rule(item))
            except (ValidationError, ValueError):
                return None
            if validated:
                return RuleCompileResult(rules=validated)
            return None
        candidates = payload.get("candidates")
        if isinstance(candidates, list):
            usable: list[str] = []
            for candidate in candidates:
                if not isinstance(candidate, str):
                    continue
                text = candidate.strip()
                if text and text not in usable:
                    usable.append(text)
            return RuleCompileResult(candidates=usable[:MAX_CANDIDATE_INTERPRETATIONS])
        return None


def _validate_rule(item: dict[str, Any]) -> Rule:
    """Build one discriminated rule or raise ValidationError for bad payloads."""
    from china_a_share.discovery.strategy_models import (
        CumulativeReturnRule,
        DrawdownRule,
        FirstBullishMARule,
        LimitUpRule,
    )

    rule_type = item.get("type")
    if rule_type == "drawdown":
        return DrawdownRule.model_validate(item)
    if rule_type == "cumulative_return":
        return CumulativeReturnRule.model_validate(item)
    if rule_type == "first_bullish_ma":
        return FirstBullishMARule.model_validate(item)
    if rule_type == "limit_up":
        return LimitUpRule.model_validate(item)
    raise ValueError(f"unknown rule type: {rule_type}")

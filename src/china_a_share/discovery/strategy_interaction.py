"""Conversational Feishu layer for creating and running custom strategies."""

from __future__ import annotations

from datetime import datetime
import logging
import re
from typing import Any, Callable, Dict, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from china_a_share.discovery.strategy_models import (
    CumulativeReturnRule,
    DraftState,
    DrawdownRule,
    FirstBullishMARule,
    LimitUpRule,
    Rule,
    SignalDirection,
    StrategyConfig,
    StrategyDraft,
)
from china_a_share.discovery.strategy_scanner import (
    StrategyScanner,
    format_strategy_rules,
)
from china_a_share.discovery.strategy_store import StrategyStore
from china_a_share.discovery.rule_compiler import RuleCompileResult, RuleCompiler


logger = logging.getLogger(__name__)
SHANGHAI_TIME_ZONE = ZoneInfo("Asia/Shanghai")

STRATEGY_MENU_COMMAND_PATTERN = re.compile(r"^(?:策略菜单|策略)$")
STRATEGY_CREATE_COMMAND_PATTERN = re.compile(r"^新建策略(?:\s+(?P<name>.+))?$")
STRATEGY_LIST_COMMAND_PATTERN = re.compile(r"^策略列表$")
STRATEGY_RUN_COMMAND_PATTERN = re.compile(r"^运行规则$")
STRATEGY_CANCEL_DRAFT_COMMAND_PATTERN = re.compile(r"^取消草稿$")
STRATEGY_NAME_COMMAND_PATTERN = re.compile(r"^策略名称\s+(?P<name>.+)$")
STRATEGY_RULES_COMMAND_PATTERN = re.compile(r"^规则\s+(?P<spec>.+)$")

STRATEGY_COMMAND_PATTERNS = (
    STRATEGY_MENU_COMMAND_PATTERN,
    STRATEGY_CREATE_COMMAND_PATTERN,
    STRATEGY_LIST_COMMAND_PATTERN,
    STRATEGY_RUN_COMMAND_PATTERN,
    STRATEGY_CANCEL_DRAFT_COMMAND_PATTERN,
    STRATEGY_NAME_COMMAND_PATTERN,
    STRATEGY_RULES_COMMAND_PATTERN,
)

DIRECTION_LABELS = {
    SignalDirection.BUY: "买入",
    SignalDirection.SELL: "卖出",
}

_RULES_EXAMPLE = (
    "规则 回撤 窗口=60 阈值=30%；涨幅 窗口=10 下限=-10% 上限=10%；金叉 快线=5 慢线=10；涨停"
)
_RULES_TYPE_HINT = (
    "规则类型：回撤（窗口/阈值）、涨幅（窗口/下限/上限）、金叉（快线/慢线）、"
    "涨停（无需参数，可选 窗口=N 表示近 N 日内出现过涨停）。"
)


class RuleSpecError(ValueError):
    """Raised when submitted rule text cannot compile into validated rules."""


_RULE_SEPARATOR = re.compile(r"[;；]")
_TOKEN_SEPARATOR = re.compile(r"[\s,，、]+")
_KEY_VALUE = re.compile(r"^(?P<key>[A-Za-z\u4e00-\u9fff]+)[=＝](?P<value>.+)$")

_RULE_TYPE_ALIASES: Dict[str, str] = {
    "回撤": "drawdown",
    "drawdown": "drawdown",
    "涨幅": "cumulative_return",
    "return": "cumulative_return",
    "cumulative_return": "cumulative_return",
    "金叉": "first_bullish_ma",
    "ma": "first_bullish_ma",
    "first_bullish_ma": "first_bullish_ma",
    "涨停": "limit_up",
    "limit_up": "limit_up",
    "limitup": "limit_up",
}

_RULE_KEYS: Dict[str, Dict[str, tuple[str, ...]]] = {
    "drawdown": {
        "window": ("window", "窗口", "w"),
        "threshold": ("threshold", "阈值", "t"),
    },
    "cumulative_return": {
        "window": ("window", "窗口", "w"),
        "min_return": ("min", "min_return", "下限"),
        "max_return": ("max", "max_return", "上限"),
    },
    "first_bullish_ma": {
        "fast_window": ("fast", "fast_window", "快线"),
        "slow_window": ("slow", "slow_window", "慢线"),
    },
    "limit_up": {
        "window": ("window", "窗口", "w"),
    },
}
# Rule fields that keep their model default when the user omits them.
_OPTIONAL_RULE_FIELDS: Dict[str, set[str]] = {
    "limit_up": {"window"},
}
_INT_FIELDS = {"window", "fast_window", "slow_window"}


def _parse_number(raw: str) -> float:
    """Parse one decimal number that may use a trailing percent sign."""
    text = raw.strip()
    percent = text.endswith("%") or text.endswith("％")
    if percent:
        text = text[:-1].strip()
    value = float(text)
    return value / 100.0 if percent else value


def parse_rule_spec(spec: str) -> list[Rule]:
    """Compile one user-submitted rule text into validated rule models."""
    segments = [segment.strip() for segment in _RULE_SEPARATOR.split(spec) if segment.strip()]
    if not segments:
        raise RuleSpecError("规则内容为空，请按示例格式填写。")
    rules: list[Rule] = []
    for segment in segments:
        tokens = [token for token in _TOKEN_SEPARATOR.split(segment) if token]
        head = tokens[0]
        canonical = _RULE_TYPE_ALIASES.get(head) or _RULE_TYPE_ALIASES.get(head.lower())
        if canonical is None:
            raise RuleSpecError(
                f"不支持的规则类型「{head}」，支持的类型：回撤 / 涨幅 / 金叉 / 涨停。"
            )
        values: Dict[str, float] = {}
        for token in tokens[1:]:
            match = _KEY_VALUE.match(token)
            if match is None:
                raise RuleSpecError(f"参数「{token}」格式不正确，应形如 窗口=60。")
            alias = match.group("key").lower()
            field_name = next(
                (
                    name
                    for name, aliases in _RULE_KEYS[canonical].items()
                    if alias in aliases
                ),
                None,
            )
            if field_name is None:
                raise RuleSpecError(f"规则「{head}」不支持参数「{alias}」。")
            try:
                values[field_name] = _parse_number(match.group("value"))
            except ValueError as exc:
                raise RuleSpecError(
                    f"参数「{token}」的数值无法识别，请使用数字或百分比。"
                ) from exc
        missing = sorted(
            set(_RULE_KEYS[canonical])
            - set(values)
            - _OPTIONAL_RULE_FIELDS.get(canonical, set())
        )
        if missing:
            hints = "、".join(
                "/".join(_RULE_KEYS[canonical][field] ) for field in missing
            )
            raise RuleSpecError(f"规则「{head}」缺少参数：{hints}。")
        try:
            if canonical == "drawdown":
                rules.append(
                    DrawdownRule(
                        window=int(values["window"]),
                        threshold=values["threshold"],
                    )
                )
            elif canonical == "cumulative_return":
                rules.append(
                    CumulativeReturnRule(
                        window=int(values["window"]),
                        min_return=values["min_return"],
                        max_return=values["max_return"],
                    )
                )
            elif canonical == "limit_up":
                rules.append(LimitUpRule(window=int(values.get("window", 1))))
            else:
                rules.append(
                    FirstBullishMARule(
                        fast_window=int(values["fast_window"]),
                        slow_window=int(values["slow_window"]),
                    )
                )
        except ValidationError as exc:
            raise RuleSpecError(str(exc)[:500]) from exc
    return rules


def build_notice_card(
    title: str, content: str, *, template: str = "grey"
) -> Dict[str, Any]:
    """Return one minimal informational card."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}}
        ],
    }


def build_rule_options_card(
    draft: StrategyDraft,
    spec: str,
    candidates: list[str],
    *,
    error: str = "",
) -> Dict[str, Any]:
    """Offer candidate interpretations or guided re-entry for unclear rules."""
    elements: list[Dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"你描述的条件：**{spec}**\n"
                    + (f"无法直接解析：{error}\n" if error else "")
                    + "你的意思是不是下面之一？"
                ),
            },
        }
    ]
    if candidates:
        for candidate in candidates:
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": candidate[:20],
                            },
                            "value": {
                                "action": "strategy_rules_candidate",
                                "draft_id": draft.draft_id,
                                "text": candidate,
                            },
                        }
                    ],
                }
            )
    else:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "暂时没有接近的候选。目前支持的条件："
                        "回撤（窗口/阈值）、涨幅（窗口/下限/上限）、"
                        "金叉（快线/慢线）、涨停（可选 窗口=N）。\n"
                        f"标准写法示例：**{_RULES_EXAMPLE}**"
                    ),
                },
            }
        )
    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "都不对？回复「规则 + 你的补充描述」换种说法，或点下方取消。",
                }
            ],
        }
    )
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "取消草稿"},
                    "value": {
                        "action": "strategy_draft_cancel",
                        "draft_id": draft.draft_id,
                    },
                }
            ],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "条件解析"},
        },
        "elements": elements,
    }


def build_strategy_menu_card() -> Dict[str, Any]:
    """Return the strategy management menu shown inside the group chat."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "A股策略助手"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "策略会按交易日自动扫描并推送结果卡片，也可以随时手动运行。",
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "新建策略"},
                        "value": {"action": "strategy_create_draft"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "策略列表"},
                        "value": {"action": "strategy_list"},
                    },
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "运行规则"},
                        "value": {"action": "strategy_run_all"},
                    },
                ],
            },
        ],
    }


def build_draft_card(
    draft: StrategyDraft, *, error: str = ""
) -> Dict[str, Any]:
    """Return the state-aware guidance card for one editing draft."""
    elements: list[Dict[str, Any]] = []
    if error:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**⚠️ {error}**"},
            }
        )
    if draft.state == DraftState.AWAITING_NAME:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "请回复一条消息设置策略名称：\n**策略名称 你的策略名称**",
                },
            }
        )
    elif draft.state == DraftState.AWAITING_DIRECTION:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"策略名称：**{draft.name}**\n请选择规则全部命中时给出的建议：",
                },
            }
        )
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "买入"},
                        "value": {
                            "action": "strategy_draft_direction",
                            "draft_id": draft.draft_id,
                            "direction": SignalDirection.BUY.value,
                        },
                    },
                    {
                        "tag": "button",
                        "type": "danger",
                        "text": {"tag": "plain_text", "content": "卖出"},
                        "value": {
                            "action": "strategy_draft_direction",
                            "draft_id": draft.draft_id,
                            "direction": SignalDirection.SELL.value,
                        },
                    },
                ],
            }
        )
    elif draft.state == DraftState.AWAITING_RULES:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"策略名称：**{draft.name}**\n"
                        f"建议方向：**{DIRECTION_LABELS.get(draft.direction or SignalDirection.BUY)}**\n"
                        "请回复一条消息设置规则，直接用一句话描述即可，例如：\n"
                        "**规则 60天回撤超过30%，近10天横盘，MA5和MA10首次金叉**\n"
                        "或使用标准写法（多个规则用「；」分隔）：\n"
                        f"**{_RULES_EXAMPLE}**\n"
                        f"{_RULES_TYPE_HINT}"
                    ),
                },
            }
        )
    else:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"策略名称：**{draft.name}**\n"
                        f"建议方向：**{DIRECTION_LABELS.get(draft.direction or SignalDirection.BUY)}**\n"
                        f"**规则：**\n{format_strategy_rules(draft_strategy_view(draft))}"
                    ),
                },
            }
        )
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "保存策略"},
                        "value": {
                            "action": "strategy_draft_save",
                            "draft_id": draft.draft_id,
                        },
                    },
                ],
            }
        )
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "取消草稿"},
                    "value": {
                        "action": "strategy_draft_cancel",
                        "draft_id": draft.draft_id,
                    },
                }
            ],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "策略配置"},
        },
        "elements": elements,
    }


def draft_strategy_view(draft: StrategyDraft) -> StrategyConfig:
    """Return one throwaway config that reuses the strategy card formatters."""
    now = datetime.now(SHANGHAI_TIME_ZONE)
    return StrategyConfig(
        id=draft.draft_id,
        name=draft.name or "未命名策略",
        direction=draft.direction or SignalDirection.BUY,
        rules=draft.rules,
        creator_open_id=draft.owner_open_id,
        enabled=False,
        notification_chat_id=draft.chat_id,
        created_at=now,
        updated_at=now,
    )


def build_strategy_list_card(strategies: list[StrategyConfig]) -> Dict[str, Any]:
    """Return one card listing the caller's strategies with inline actions."""
    elements: list[Dict[str, Any]] = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "您已保存的策略："},
        }
    ]
    for strategy in strategies:
        status = "启用" if strategy.enabled else "停用"
        toggle_label = "停用" if strategy.enabled else "启用"
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{strategy.name}**（{DIRECTION_LABELS[strategy.direction]}，{status}）\n"
                        f"{format_strategy_rules(strategy)}"
                    ),
                },
            }
        )
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": toggle_label},
                        "value": {
                            "action": "strategy_toggle",
                            "strategy_id": strategy.id,
                            "enabled": not strategy.enabled,
                        },
                    },
                    {
                        "tag": "button",
                        "type": "danger",
                        "text": {"tag": "plain_text", "content": "删除"},
                        "value": {
                            "action": "strategy_delete",
                            "strategy_id": strategy.id,
                        },
                    },
                ],
            }
        )
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "运行规则"},
                    "value": {"action": "strategy_run_all"},
                }
            ],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "策略列表"},
        },
        "elements": elements,
    }


def build_strategy_saved_card(strategy: StrategyConfig) -> Dict[str, Any]:
    """Return the confirmation card shown after one strategy is saved."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "策略已保存"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"策略 **{strategy.name}** 已保存并启用，"
                        "每日扫描完成后会自动推送结果卡片。"
                    ),
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "立即运行"},
                        "value": {"action": "strategy_run_all"},
                    }
                ],
            },
        ],
    }


class StrategyInteractionCoordinator:
    """Own the strategy menu, guided drafts, and manual runs for one bot."""

    def __init__(
        self,
        store: StrategyStore,
        scanner: Optional[StrategyScanner] = None,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        compiler: Optional[RuleCompiler] = None,
    ) -> None:
        self._store = store
        self._scanner = scanner
        self._clock = clock or (lambda: datetime.now(SHANGHAI_TIME_ZONE))
        self._compiler = compiler

    @property
    def scanner(self) -> Optional[StrategyScanner]:
        """Return the scanner wired for manual runs, if any."""
        return self._scanner

    def handles_prompt(self, prompt: str) -> bool:
        """Return True only when the text is an exact strategy command."""
        text = prompt.strip()
        return any(pattern.match(text) for pattern in STRATEGY_COMMAND_PATTERNS)

    def handle_message(
        self,
        sender_open_id: str,
        chat_id: str,
        prompt: str,
        request_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Handle one strategy text command and return the reply card."""
        text = prompt.strip()
        if STRATEGY_MENU_COMMAND_PATTERN.match(text):
            return build_strategy_menu_card()
        create = STRATEGY_CREATE_COMMAND_PATTERN.match(text)
        if create is not None:
            name = (create.group("name") or "").strip()
            return self._create_draft(sender_open_id, chat_id, name or None)
        if STRATEGY_LIST_COMMAND_PATTERN.match(text):
            return self._list_card(sender_open_id)
        if STRATEGY_RUN_COMMAND_PATTERN.match(text):
            return self._run_all(sender_open_id, request_id)
        if STRATEGY_CANCEL_DRAFT_COMMAND_PATTERN.match(text):
            return self._cancel_latest_draft(sender_open_id)
        name = STRATEGY_NAME_COMMAND_PATTERN.match(text)
        if name is not None:
            return self._apply_name(sender_open_id, name.group("name").strip())
        rules = STRATEGY_RULES_COMMAND_PATTERN.match(text)
        if rules is not None:
            return self._apply_rules(sender_open_id, rules.group("spec").strip())
        return build_notice_card("策略助手", "未识别的策略指令。")

    def handle_card_action(
        self,
        operator_open_id: str,
        chat_id: str,
        action_name: str,
        value: Dict[str, Any],
        request_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Handle one strategy card button click and return the reply card."""
        if not isinstance(value, dict):
            value = {}
        if action_name == "strategy_menu":
            return build_strategy_menu_card()
        if action_name == "strategy_create_draft":
            return self._create_draft(operator_open_id, chat_id, None)
        if action_name == "strategy_list":
            return self._list_card(operator_open_id)
        if action_name == "strategy_run_all":
            return self._run_all(operator_open_id, request_id)
        if action_name == "strategy_draft_direction":
            return self._apply_direction(
                operator_open_id,
                str(value.get("draft_id", "")),
                str(value.get("direction", "")),
            )
        if action_name == "strategy_draft_cancel":
            return self._cancel_draft_by_id(
                operator_open_id, str(value.get("draft_id", ""))
            )
        if action_name == "strategy_draft_save":
            return self._save_draft(operator_open_id, str(value.get("draft_id", "")))
        if action_name == "strategy_rules_candidate":
            return self._apply_rules_candidate(
                operator_open_id,
                str(value.get("draft_id", "")),
                str(value.get("text", "")),
            )
        if action_name == "strategy_toggle":
            return self._toggle_strategy(
                operator_open_id,
                str(value.get("strategy_id", "")),
                bool(value.get("enabled")),
            )
        if action_name == "strategy_delete":
            return self._delete_strategy(
                operator_open_id, str(value.get("strategy_id", ""))
            )
        return None

    def _now(self) -> datetime:
        return self._clock()

    def _create_draft(
        self, owner_open_id: str, chat_id: str, name: Optional[str]
    ) -> Dict[str, Any]:
        if not chat_id:
            return build_notice_card(
                "策略配置",
                "缺少群上下文，请在群里通过策略菜单操作。",
            )
        now = self._now()
        draft = StrategyDraft(
            draft_id=uuid4().hex,
            owner_open_id=owner_open_id,
            chat_id=chat_id,
            state=DraftState.AWAITING_DIRECTION if name else DraftState.AWAITING_NAME,
            name=name,
            created_at=now,
            updated_at=now,
        )
        self._store.put_draft(draft, owner_open_id)
        return build_draft_card(draft)

    def _latest_draft(self, owner_open_id: str) -> Optional[StrategyDraft]:
        drafts = self._store.list_drafts(owner_open_id)
        if not drafts:
            return None
        return max(drafts, key=lambda draft: draft.updated_at)

    def _apply_name(self, owner_open_id: str, name: str) -> Dict[str, Any]:
        draft = self._latest_draft(owner_open_id)
        if draft is None:
            return build_notice_card(
                "策略配置", "还没有进行中的草稿，请先回复「新建策略」。"
            )
        if draft.state != DraftState.AWAITING_NAME:
            return build_draft_card(draft, error="当前步骤不是设置名称。")
        updated = draft.model_copy(
            update={
                "name": name,
                "state": DraftState.AWAITING_DIRECTION,
                "updated_at": self._now(),
            }
        )
        self._store.put_draft(updated, owner_open_id)
        return build_draft_card(updated)

    def _apply_direction(
        self, owner_open_id: str, draft_id: str, direction: str
    ) -> Dict[str, Any]:
        draft = self._store.get_draft(draft_id, owner_open_id)
        if draft is None:
            return build_notice_card("策略配置", "草稿不存在或无权访问。")
        if draft.state != DraftState.AWAITING_DIRECTION:
            return build_draft_card(draft, error="当前步骤不是选择建议方向。")
        try:
            parsed = SignalDirection(direction)
        except ValueError:
            return build_draft_card(draft, error="建议方向无效。")
        updated = draft.model_copy(
            update={
                "direction": parsed,
                "state": DraftState.AWAITING_RULES,
                "updated_at": self._now(),
            }
        )
        self._store.put_draft(updated, owner_open_id)
        return build_draft_card(updated)

    def _apply_rules(self, owner_open_id: str, spec: str) -> Dict[str, Any]:
        draft = self._latest_draft(owner_open_id)
        if draft is None:
            return build_notice_card(
                "策略配置", "还没有进行中的草稿，请先回复「新建策略」。"
            )
        if draft.state not in (DraftState.AWAITING_RULES, DraftState.READY):
            return build_draft_card(draft, error="当前步骤不是设置规则。")
        try:
            rules = parse_rule_spec(spec)
        except RuleSpecError as exc:
            return self._compile_or_suggest(draft, spec, str(exc))
        return self._store_rules(draft, rules)

    def _compile_or_suggest(
        self, draft: StrategyDraft, spec: str, error: str
    ) -> Dict[str, Any]:
        """Fall back from the strict DSL to LLM compilation or option buttons."""
        compiled: Optional[RuleCompileResult] = None
        if self._compiler is not None:
            try:
                compiled = self._compiler.compile(spec)
            except Exception:
                compiled = None
        if compiled is not None and compiled.rules:
            return self._store_rules(draft, compiled.rules)
        candidates: list[str] = []
        if compiled is not None and compiled.candidates:
            # Only offer candidates that the deterministic parser accepts.
            candidates = [
                text
                for text in compiled.candidates
                if self._parses(text)
            ]
        return build_rule_options_card(draft, spec, candidates, error=error)

    @staticmethod
    def _parses(text: str) -> bool:
        try:
            parse_rule_spec(text)
        except RuleSpecError:
            return False
        return True

    def _store_rules(self, draft: StrategyDraft, rules: list) -> Dict[str, Any]:
        updated = draft.model_copy(
            update={
                "rules": rules,
                "state": DraftState.READY,
                "updated_at": self._now(),
            }
        )
        self._store.put_draft(updated, draft.owner_open_id)
        return build_draft_card(updated)

    def _apply_rules_candidate(
        self, owner_open_id: str, draft_id: str, text: str
    ) -> Dict[str, Any]:
        """Adopt one suggested interpretation chosen from the options card."""
        draft = self._store.get_draft(draft_id, owner_open_id)
        if draft is None:
            return build_notice_card("策略配置", "草稿不存在或无权访问。")
        if draft.state not in (DraftState.AWAITING_RULES, DraftState.READY):
            return build_draft_card(draft, error="当前步骤不是设置规则。")
        try:
            rules = parse_rule_spec(text)
        except RuleSpecError as exc:
            return build_rule_options_card(draft, text, [], error=str(exc))
        return self._store_rules(draft, rules)

    def _save_draft(self, owner_open_id: str, draft_id: str) -> Dict[str, Any]:
        draft = self._store.get_draft(draft_id, owner_open_id)
        if draft is None:
            return build_notice_card("策略配置", "草稿不存在或无权访问。")
        if draft.state != DraftState.READY or not draft.name or draft.direction is None:
            return build_draft_card(draft, error="草稿还未完成，无法保存。")
        if not draft.chat_id:
            return build_draft_card(draft, error="缺少通知群，请在群里重新创建。")
        now = self._now()
        strategy = StrategyConfig(
            id=uuid4().hex,
            name=draft.name,
            direction=draft.direction,
            rules=draft.rules,
            creator_open_id=owner_open_id,
            enabled=True,
            notification_chat_id=draft.chat_id,
            created_at=now,
            updated_at=now,
        )
        self._store.put_strategy(strategy, owner_open_id)
        self._store.delete_draft(draft_id, owner_open_id)
        return build_strategy_saved_card(strategy)

    def _cancel_latest_draft(self, owner_open_id: str) -> Dict[str, Any]:
        draft = self._latest_draft(owner_open_id)
        if draft is None:
            return build_notice_card("策略配置", "没有进行中的草稿。")
        self._store.delete_draft(draft.draft_id, owner_open_id)
        return build_notice_card("策略配置", "草稿已取消。")

    def _cancel_draft_by_id(
        self, owner_open_id: str, draft_id: str
    ) -> Dict[str, Any]:
        draft = self._store.get_draft(draft_id, owner_open_id)
        if draft is None:
            return build_notice_card("策略配置", "草稿不存在或无权访问。")
        self._store.delete_draft(draft_id, owner_open_id)
        return build_notice_card("策略配置", "草稿已取消。")

    def _list_card(self, owner_open_id: str) -> Dict[str, Any]:
        strategies = self._store.list_strategies(owner_open_id)
        if not strategies:
            return build_notice_card(
                "策略列表", "您还没有已保存的策略，回复「新建策略」开始创建。"
            )
        return build_strategy_list_card(strategies)

    def _toggle_strategy(
        self, owner_open_id: str, strategy_id: str, enabled: bool
    ) -> Dict[str, Any]:
        strategy = self._store.get_strategy(strategy_id, owner_open_id)
        if strategy is None:
            return build_notice_card("策略列表", "策略不存在或无权访问。")
        updated = strategy.model_copy(
            update={"enabled": enabled, "updated_at": self._now()}
        )
        self._store.put_strategy(updated, owner_open_id)
        return self._list_card(owner_open_id)

    def _delete_strategy(self, owner_open_id: str, strategy_id: str) -> Dict[str, Any]:
        strategy = self._store.get_strategy(strategy_id, owner_open_id)
        if strategy is None:
            return build_notice_card("策略列表", "策略不存在或无权访问。")
        self._store.delete_strategy(strategy_id, owner_open_id)
        return self._list_card(owner_open_id)

    def _run_all(self, owner_open_id: str, request_id: str) -> Optional[Dict[str, Any]]:
        if self._scanner is None:
            return build_notice_card("策略运行", "策略扫描器未配置，无法运行。")
        enabled = self._store.list_strategies(owner_open_id, enabled=True)
        if not enabled:
            return build_notice_card("策略运行", "您还没有启用的策略，请先创建。")
        self._scanner.run_manual_preview(owner_open_id, request_id or "manual")
        return None

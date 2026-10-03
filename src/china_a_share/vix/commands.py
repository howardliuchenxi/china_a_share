"""Chat-command handling for the VIX alert capability.

Command patterns live here (not in feishu.py) so the Feishu ingress only
routes, while parsing, validation, and replies stay in this module. Rule
creation auto-fills the optional expectation and exit from the documented
research context when the chat does not state them; the reply always shows
the filled values and how to change them. No behavioral value is hardcoded:
every threshold, expectation, and exit lives in the stored rule object.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Optional
from uuid import uuid4

from china_a_share.vix.rules import (
    DIRECTION_LABELS,
    VixRule,
    new_vix_rule,
    suggest_expected_return_pct,
)
from china_a_share.vix.scanner import VixAlertService

VIX_RULES_COMMAND_PATTERN = re.compile(r"^VIX规则\s*$")
VIX_ADD_RULE_COMMAND_PATTERN = re.compile(
    r"^VIX提醒\s*(上涨|涨|下跌|跌)\s*([0-9]+(?:\.[0-9]+)?)\s*%?(?P<rest>.*)$"
)
VIX_MODIFY_RULE_COMMAND_PATTERN = re.compile(r"^VIX修改规则\s+(\S+)\s*(?P<rest>.*)$")
VIX_REMOVE_RULE_COMMAND_PATTERN = re.compile(r"^VIX删除规则\s*(\S+)\s*$")
VIX_LOOKBACK_COMMAND_PATTERN = re.compile(
    r"^VIX回溯\s*(\d{4}-\d{2}-\d{2}|\d{8})\s*$"
)

_DIRECTION_TOKEN_PATTERN = re.compile(r"(上涨|涨|下跌|跌)\s*([0-9]+(?:\.[0-9]+)?)\s*%")
_EXPECTED_TOKEN_PATTERN = re.compile(r"预期\s*(-?[0-9]+(?:\.[0-9]+)?)\s*%")
_EXIT_DAYS_TOKEN_PATTERN = re.compile(r"([0-9]+)\s*(?:个?交易日|日|天)后卖出")
_EXIT_VIX_TOKEN_PATTERN = re.compile(r"VIX\s*低于\s*([0-9]+(?:\.[0-9]+)?)\s*卖出")
_EXIT_NONE_TOKEN_PATTERN = re.compile(r"不卖出")

_RULE_USAGE = (
    "用法：VIX提醒 上涨10%（阈值自定，0-100；预期收益与卖出条件缺省时按研究结论自动填入）"
    "；可选后缀：预期0.5%、3日后卖出、VIX低于20卖出、不卖出；"
    "修改：VIX修改规则 <编号> <任选后缀或 上涨12%>；"
    "查看：VIX规则；删除：VIX删除规则 <编号> 或 全部；"
    "回溯：VIX回溯 2026-04-04。"
)

_WORD_DIRECTIONS = {"上涨": "up", "涨": "up", "下跌": "down", "跌": "down"}


class _OptionalFields:
    """Parsed optional suffix tokens (trigger/expectation/exit)."""

    def __init__(self) -> None:
        self.direction: Optional[str] = None
        self.threshold: Optional[float] = None
        self.expected: Optional[float] = None
        self.exit_kind: Optional[str] = None
        self.exit_days: Optional[int] = None
        self.exit_vix_below: Optional[float] = None

    @property
    def any_exit(self) -> bool:
        return (
            self.exit_kind is not None
            or self.exit_days is not None
            or self.exit_vix_below is not None
        )


def _parse_optionals(rest: str) -> _OptionalFields:
    fields = _OptionalFields()
    direction_match = _DIRECTION_TOKEN_PATTERN.search(rest)
    if direction_match:
        fields.direction = _WORD_DIRECTIONS[direction_match.group(1)]
        fields.threshold = float(direction_match.group(2))
    expected_match = _EXPECTED_TOKEN_PATTERN.search(rest)
    if expected_match:
        fields.expected = float(expected_match.group(1))
    days_match = _EXIT_DAYS_TOKEN_PATTERN.search(rest)
    vix_match = _EXIT_VIX_TOKEN_PATTERN.search(rest)
    none_match = _EXIT_NONE_TOKEN_PATTERN.search(rest)
    exit_tokens = sum(1 for m in (days_match, vix_match, none_match) if m)
    if exit_tokens > 1:
        raise ValueError("卖出条件只能指定一种（N日后卖出 / VIX低于X卖出 / 不卖出）。")
    if days_match:
        fields.exit_kind = "days"
        fields.exit_days = int(days_match.group(1))
    elif vix_match:
        fields.exit_kind = "vix_below"
        fields.exit_vix_below = float(vix_match.group(1))
    elif none_match:
        fields.exit_kind = "none"
    return fields


def handles_vix_command(prompt: str) -> bool:
    """Return whether one prompt belongs to the VIX command family."""
    return bool(
        VIX_RULES_COMMAND_PATTERN.match(prompt)
        or VIX_ADD_RULE_COMMAND_PATTERN.match(prompt)
        or VIX_MODIFY_RULE_COMMAND_PATTERN.match(prompt)
        or VIX_REMOVE_RULE_COMMAND_PATTERN.match(prompt)
        or VIX_LOOKBACK_COMMAND_PATTERN.match(prompt)
    )


def handle_vix_command(
    service: VixAlertService,
    chat_id: str,
    prompt: str,
    sender_open_id: str,
) -> Optional[str]:
    """Answer one VIX command prompt; None when it is not a VIX command."""
    if VIX_RULES_COMMAND_PATTERN.match(prompt):
        return _rules_reply(service, chat_id)
    add_match = VIX_ADD_RULE_COMMAND_PATTERN.match(prompt)
    if add_match:
        return _add_rule_reply(service, chat_id, add_match, sender_open_id)
    modify_match = VIX_MODIFY_RULE_COMMAND_PATTERN.match(prompt)
    if modify_match:
        return _modify_rule_reply(service, chat_id, modify_match.group(1), modify_match.group("rest"))
    remove_match = VIX_REMOVE_RULE_COMMAND_PATTERN.match(prompt)
    if remove_match:
        return _remove_rule_reply(service, chat_id, remove_match.group(1))
    lookback_match = VIX_LOOKBACK_COMMAND_PATTERN.match(prompt)
    if lookback_match:
        return _lookback_reply(service, chat_id, lookback_match.group(1))
    return None


def _rules_reply(service: VixAlertService, chat_id: str) -> str:
    rules = service.rules.load(chat_id)
    if not rules:
        return f"本群还没有 VIX 提醒规则。\n{_RULE_USAGE}"
    lines = []
    for index, rule in enumerate(rules, start=1):
        open_note = ""
        if service.positions is not None and service.positions.get(chat_id, rule.id):
            open_note = "（事件进行中，暂不重复提醒）"
        lines.append(
            f"{index}. [{rule.id}] {rule.describe_full()}{open_note}"
            f"（{rule.created_at[:10]} 建立）"
        )
    return f"本群 VIX 提醒规则：\n" + "\n".join(lines) + f"\n{_RULE_USAGE}"


def _add_rule_reply(service: VixAlertService, chat_id: str, match, sender_open_id: str) -> str:
    direction = _WORD_DIRECTIONS[match.group(1)]
    threshold = float(match.group(2))
    try:
        optionals = _parse_optionals(match.group("rest"))
    except ValueError as exc:
        return f"无法添加规则：{exc}\n{_RULE_USAGE}"
    expected = optionals.expected
    auto_filled = []
    if expected is None:
        expected = suggest_expected_return_pct(direction, threshold)
        if expected:
            auto_filled.append(f"预期收益 {expected:+.2f}%（研究结论）")
        else:
            auto_filled.append("预期收益 +0.00%（该档位无研究结论，可修改）")
    exit_kind = optionals.exit_kind or "days"
    exit_days = optionals.exit_days or 1
    exit_vix_below = optionals.exit_vix_below or 0.0
    if not optionals.any_exit:
        auto_filled.append(f"{exit_days}个交易日后卖出（缺省）")
    try:
        rule = new_vix_rule(
            direction,
            threshold,
            sender_open_id,
            uuid4().hex[:8],
            expected_return_pct=expected,
            exit_kind=exit_kind,
            exit_days=exit_days,
            exit_vix_below=exit_vix_below,
        )
    except ValueError as exc:
        return f"无法添加规则：{exc}\n{_RULE_USAGE}"
    rules = service.rules.load(chat_id)
    if any(
        existing.direction == rule.direction
        and existing.threshold_pct == rule.threshold_pct
        for existing in rules
    ):
        existing = next(
            item
            for item in rules
            if item.direction == rule.direction
            and item.threshold_pct == rule.threshold_pct
        )
        return f"该规则已存在：[{existing.id}] {existing.describe_full()}，无需重复添加。"
    rules.append(rule)
    service.rules.save(chat_id, rules)
    filled_note = (
        f"已按群聊上下文自动填入：{'；'.join(auto_filled)}。可用「VIX修改规则 {rule.id} …」调整。"
        if auto_filled
        else ""
    )
    return (
        f"已添加规则 [{rule.id}]：{rule.describe_full()}，本群共 {len(rules)} 条。"
        f"{filled_note}"
        f"每小时巡检一次，命中即在本群通知；同一事件不重复提醒，"
        f"满足卖出条件后自动发对账消息。"
    )


def _modify_rule_reply(service: VixAlertService, chat_id: str, target: str, rest: str) -> str:
    rules = service.rules.load(chat_id)
    index = next(
        (i for i, item in enumerate(rules) if item.id.lower() == target.strip().lower()),
        None,
    )
    if index is None:
        return f"未找到编号 {target} 的规则。\n{_RULE_USAGE}"
    rule = rules[index]
    try:
        optionals = _parse_optionals(rest)
    except ValueError as exc:
        return f"无法修改规则：{exc}\n{_RULE_USAGE}"
    try:
        replacement = _merged_rule(rule, optionals)
    except ValueError as exc:
        return f"无法修改规则：{exc}\n{_RULE_USAGE}"
    rules[index] = replacement
    service.rules.save(chat_id, rules)
    changed = _describe_changes(rule, replacement)
    return (
        f"已修改规则 [{replacement.id}]：{replacement.describe_full()}。\n{changed}"
    )


def _merged_rule(rule: VixRule, optionals: _OptionalFields) -> VixRule:
    """Build the replacement rule by overlaying parsed tokens on the stored one."""
    if (optionals.direction is None) != (optionals.threshold is None):
        raise ValueError("触发条件需成对出现，如「上涨12%」。")
    direction = optionals.direction or rule.direction
    threshold = optionals.threshold if optionals.threshold is not None else rule.threshold_pct
    expected = (
        optionals.expected
        if optionals.expected is not None
        else (
            suggest_expected_return_pct(direction, threshold)
            if optionals.direction is not None and optionals.expected is None
            else rule.expected_return_pct
        )
    )
    exit_kind = optionals.exit_kind or rule.exit_kind
    if optionals.exit_kind == "days":
        exit_days = optionals.exit_days
        exit_vix_below = rule.exit_vix_below
    elif optionals.exit_kind == "vix_below":
        exit_days = rule.exit_days
        exit_vix_below = optionals.exit_vix_below
    else:
        exit_days = optionals.exit_days or rule.exit_days
        exit_vix_below = optionals.exit_vix_below or rule.exit_vix_below
    replacement = new_vix_rule(
        direction,
        threshold,
        rule.created_by,
        rule.id,
        expected_return_pct=expected,
        exit_kind=exit_kind,
        exit_days=exit_days,
        exit_vix_below=exit_vix_below,
    )
    # Preserve the original creation timestamp for listing continuity.
    return VixRule(
        id=replacement.id,
        direction=replacement.direction,
        threshold_pct=replacement.threshold_pct,
        created_by=replacement.created_by,
        created_at=rule.created_at,
        expected_return_pct=replacement.expected_return_pct,
        exit_kind=replacement.exit_kind,
        exit_days=replacement.exit_days,
        exit_vix_below=replacement.exit_vix_below,
    )


def _describe_changes(before: VixRule, after: VixRule) -> str:
    changes = []
    if (before.direction, before.threshold_pct) != (after.direction, after.threshold_pct):
        changes.append(
            f"触发条件 {before.describe()} → {after.describe()}"
        )
    if before.expected_return_pct != after.expected_return_pct:
        changes.append(
            f"预期收益 {before.expected_return_pct:+.2f}% → {after.expected_return_pct:+.2f}%"
        )
    if (
        before.exit_kind,
        before.exit_days,
        before.exit_vix_below,
    ) != (after.exit_kind, after.exit_days, after.exit_vix_below):
        changes.append(f"卖出条件 {before.describe_exit()} → {after.describe_exit()}")
    return "；".join(changes) if changes else "无字段变化。"


def _remove_rule_reply(service: VixAlertService, chat_id: str, target: str) -> str:
    rules = service.rules.load(chat_id)
    if not rules:
        return f"本群没有可删除的 VIX 提醒规则。\n{_RULE_USAGE}"
    if target == "全部":
        if service.positions is not None:
            for rule in rules:
                service.positions.delete(chat_id, rule.id)
        service.rules.save(chat_id, [])
        return f"已删除全部 {len(rules)} 条 VIX 提醒规则。"
    remaining = [rule for rule in rules if rule.id.lower() != target.lower()]
    if len(remaining) == len(rules):
        return f"未找到编号 {target} 的规则。\n{_RULE_USAGE}"
    if service.positions is not None:
        service.positions.delete(chat_id, target)
    service.rules.save(chat_id, remaining)
    return f"已删除规则 {target}，本群剩余 {len(remaining)} 条。"


def _lookback_reply(service: VixAlertService, chat_id: str, raw_date: str) -> str:
    date_str = _normalize_date(raw_date)
    if date_str is None:
        return f"日期格式无法识别：{raw_date}。请用 YYYY-MM-DD 或 YYYYMMDD。"
    data = service.lookback(date_str)
    if data is None:
        return (
            f"{date_str} 没有 VIX 交易数据（可能为非交易日，或早于 1990 年的"
            f"历史起点）。"
        )
    direction = "上涨" if data["change_pct"] >= 0 else "下跌"
    prev_part = (
        f"较前一日（{data['prev_date']}）收盘 {data['prev_close']:.2f} "
        if data["prev_date"]
        else f"较前收 {data['prev_close']:.2f} "
    )
    header = (
        f"VIX 回溯 {data['date']}：收盘 {data['close']:.2f}，{prev_part}"
        f"{direction} {abs(data['change_pct']):.2f}%。"
    )
    rules = service.rules.load(chat_id)
    if not rules:
        return f"{header}\n本群还没有 VIX 提醒规则，无法回溯判定。\n{_RULE_USAGE}"
    lines = []
    for rule in rules:
        if rule.hits(data["change_pct"]):
            threshold_label = DIRECTION_LABELS[rule.direction]
            detail = (
                f"涨幅 {data['change_pct']:.2f}% ≥ {rule.threshold_pct:.2f}%"
                if rule.direction == "up"
                else f"跌幅 {data['change_pct']:.2f}% ≤ -{rule.threshold_pct:.2f}%"
            )
            lines.append(
                f"✓ 命中 [{rule.id}] VIX {threshold_label} ≥{rule.threshold_pct:.2f}%（{detail}）"
            )
        else:
            lines.append(f"✗ 未命中 [{rule.id}] {rule.describe()}")
    return "\n".join([header] + lines)


def _normalize_date(raw: str) -> Optional[str]:
    text = raw.strip()
    compact = text.replace("-", "")
    try:
        parsed = datetime.strptime(compact, "%Y%m%d").date()
    except ValueError:
        return None
    return parsed.isoformat()

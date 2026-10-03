"""Chat-command handling for the VIX alert capability.

Command patterns live here (not in feishu.py) so the Feishu ingress only
routes, while parsing, validation, and replies stay in this module. All
thresholds come from the chat's own configuration; the help text may show an
example number but the code defines none.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Optional
from uuid import uuid4

from china_a_share.vix.rules import DIRECTION_LABELS, new_vix_rule
from china_a_share.vix.scanner import VixAlertService

VIX_RULES_COMMAND_PATTERN = re.compile(r"^VIX规则\s*$")
VIX_ADD_RULE_COMMAND_PATTERN = re.compile(
    r"^VIX提醒\s*(上涨|涨|下跌|跌)\s*([0-9]+(?:\.[0-9]+)?)\s*%?\s*$"
)
VIX_REMOVE_RULE_COMMAND_PATTERN = re.compile(r"^VIX删除规则\s*(\S+)\s*$")
VIX_LOOKBACK_COMMAND_PATTERN = re.compile(
    r"^VIX回溯\s*(\d{4}-\d{2}-\d{2}|\d{8})\s*$"
)

_RULE_USAGE = (
    "用法：VIX提醒 上涨10% 或 VIX提醒 下跌5%（阈值自定，0-100）；"
    "查看：VIX规则；删除：VIX删除规则 <编号> 或 全部；"
    "回溯：VIX回溯 2026-04-04。"
)

_WORD_DIRECTIONS = {"上涨": "up", "涨": "up", "下跌": "down", "跌": "down"}


def handles_vix_command(prompt: str) -> bool:
    """Return whether one prompt belongs to the VIX command family."""
    return bool(
        VIX_RULES_COMMAND_PATTERN.match(prompt)
        or VIX_ADD_RULE_COMMAND_PATTERN.match(prompt)
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
    lines = "\n".join(
        f"{index}. [{rule.id}] {rule.describe()}（{rule.created_at[:10]} 建立）"
        for index, rule in enumerate(rules, start=1)
    )
    return f"本群 VIX 提醒规则：\n{lines}\n{_RULE_USAGE}"


def _add_rule_reply(service: VixAlertService, chat_id: str, match, sender_open_id: str) -> str:
    direction = _WORD_DIRECTIONS[match.group(1)]
    try:
        rule = new_vix_rule(direction, float(match.group(2)), sender_open_id, uuid4().hex[:8])
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
        return f"该规则已存在：[{existing.id}] {existing.describe()}，无需重复添加。"
    rules.append(rule)
    service.rules.save(chat_id, rules)
    return (
        f"已添加规则 [{rule.id}]：{rule.describe()}，本群共 {len(rules)} 条。"
        f"每小时扫描一次，命中即在本群通知。"
    )


def _remove_rule_reply(service: VixAlertService, chat_id: str, target: str) -> str:
    rules = service.rules.load(chat_id)
    if not rules:
        return f"本群没有可删除的 VIX 提醒规则。\n{_RULE_USAGE}"
    if target == "全部":
        service.rules.save(chat_id, [])
        return f"已删除全部 {len(rules)} 条 VIX 提醒规则。"
    remaining = [rule for rule in rules if rule.id.lower() != target.lower()]
    if len(remaining) == len(rules):
        return f"未找到编号 {target} 的规则。\n{_RULE_USAGE}"
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
            lines.append(f"✓ 命中 [{rule.id}] VIX {threshold_label} ≥{rule.threshold_pct:.2f}%（{detail}）")
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

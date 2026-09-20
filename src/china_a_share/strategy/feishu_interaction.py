import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .models import StrategyDraft
from .persistence import StrategyStore
from .scanner import StrategyScanner

logger = logging.getLogger(__name__)

# Manual previews scan the whole market and can run far past Feishu's 3-second
# card-callback deadline, so they must execute off the callback thread and push
# their own result card when finished.
_PREVIEW_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="strategy-preview"
)


def _callback_response(
    toast_type: str, toast_content: str, card: Optional[dict] = None
) -> dict:
    """Build one card-callback response in the envelope Feishu requires.

    Feishu judges any response that is not this shape (or arrives after the
    3-second deadline) as a failed callback and shows the error toast, so every
    handler return value must go through this builder. The updated card is a
    v1 card (config/header/elements) matching the cards we push.
    """
    response: dict = {"toast": {"type": toast_type, "content": toast_content}}
    if card is not None:
        response["card"] = {"type": "raw", "data": card}
    return response


def handle_strategy_interactive_card(
    payload: dict,
    store: StrategyStore,
    scanner: StrategyScanner
) -> Optional[dict]:
    """
    Handle user clicking buttons on Feishu interactive cards related to strategy.
    Must check permissions to prevent modifying someone else's draft or strategy.
    """
    action = payload.get("action", {})
    action_name = action.get("value", {}).get("action")
    user_id = payload.get("open_id")

    if not action_name or not user_id:
        return None

    if action_name == "create_draft":
        return _handle_create_draft(user_id, store)
    elif action_name == "view_drafts":
        return _handle_view_drafts(user_id, store)
    elif action_name == "view_progress":
        draft_id = action.get("value", {}).get("draft_id")
        return _handle_view_progress(user_id, draft_id, store)
    elif action_name == "cancel_draft":
        draft_id = action.get("value", {}).get("draft_id")
        return _handle_cancel_draft(user_id, draft_id, store)
    elif action_name == "save_draft":
        draft_id = action.get("value", {}).get("draft_id")
        return _handle_save_draft(user_id, draft_id, store)
    elif action_name == "run_preview":
        strategy_id = action.get("value", {}).get("strategy_id")
        return _handle_run_preview(user_id, strategy_id, store, scanner)

    return None

def _handle_view_progress(user_id: str, draft_id: str, store: StrategyStore) -> dict:
    draft = store.get_draft(draft_id)
    if not draft or draft.creator_id != user_id:
        return _callback_response("error", "草稿不存在或权限被拒绝。")

    content = f"**草稿ID**: {draft.id}\n**当前进度**: {draft.step}\n"
    content += f"- 名称: {draft.name or '未填写'}\n"
    content += f"- 方向: {draft.direction.value if draft.direction else '未填写'}\n"
    content += f"- 通知目标: {draft.notify_target or '未填写'}\n"
    content += f"- 规则数量: {len(draft.conditions)}\n\n"
    content += "下一步您可以选择完善信息或确认保存。"

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "草稿进度"}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": content},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "确认保存"}, "type": "primary", "value": {"action": "save_draft", "draft_id": draft_id}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "取消/删除"}, "type": "danger", "value": {"action": "cancel_draft", "draft_id": draft_id}}
            ]}
        ]
    }
    return _callback_response("success", "已打开草稿进度。", card)

def _handle_cancel_draft(user_id: str, draft_id: str, store: StrategyStore) -> dict:
    draft = store.get_draft(draft_id)
    if not draft or draft.creator_id != user_id:
        return _callback_response("error", "草稿不存在或权限被拒绝。")
    store.delete_draft(draft_id)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "草稿已取消"}, "template": "grey"},
        "elements": [{"tag": "markdown", "content": f"草稿 {draft_id} 已成功删除。"}]
    }
    return _callback_response("success", "草稿已取消。", card)

def _handle_save_draft(user_id: str, draft_id: str, store: StrategyStore) -> dict:
    draft = store.get_draft(draft_id)
    if not draft or draft.creator_id != user_id:
        return _callback_response("error", "草稿不存在或权限被拒绝。")

    from china_a_share.strategy.models import StrategyConfig, StrategyDirection, RuleCondition, Operator

    # In a full interactive setup, these would be collected dynamically.
    # For now, if missing, we populate with the requested preset defaults so it can be saved and run.
    name = draft.name or "回撤后首次转多"
    direction = draft.direction or StrategyDirection.BUY
    target = draft.notify_target or user_id
    conditions = draft.conditions

    if not conditions:
        conditions = [
            RuleCondition(metric="drawdown", operator=Operator.GT, parameters={"window": 60, "threshold": 0.30}),
            RuleCondition(metric="cumulative_return", operator=Operator.BETWEEN, parameters={"window": 10, "min": -0.10, "max": 0.10}),
            RuleCondition(metric="ma_cross", operator=Operator.MA_CROSS_UP_FIRST, parameters={"fast": 5, "slow": 10})
        ]

    strategy = StrategyConfig(
        id=draft.id,
        name=name,
        creator_id=user_id,
        direction=direction,
        notify_target=target,
        conditions=conditions
    )
    store.put_strategy(strategy)
    store.delete_draft(draft_id)

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "策略已保存"}, "template": "green"},
        "elements": [
            {"tag": "markdown", "content": f"策略 **{strategy.name}** 已保存。每日扫描将自动执行。"},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "立即预览运行"}, "type": "primary", "value": {"action": "run_preview", "strategy_id": strategy.id}}
            ]}
        ]
    }
    return _callback_response("success", "策略已保存。", card)

def _handle_create_draft(user_id: str, store: StrategyStore) -> dict:
    from uuid import uuid4
    draft_id = str(uuid4())
    draft = StrategyDraft(id=draft_id, creator_id=user_id, step="name")
    store.put_draft(draft)

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "新建策略草稿"},
            "template": "blue"
        },
        "elements": [
            {"tag": "markdown", "content": "**第一步：请回复您想设置的策略名称。**\n\n您随时可以发送“取消草稿”来放弃当前会话。"}
        ]
    }
    return _callback_response("success", "已创建策略草稿。", card)

def _handle_view_drafts(user_id: str, store: StrategyStore) -> dict:
    drafts = store.list_drafts(creator_id=user_id)
    if not drafts:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "您的草稿列表"}, "template": "grey"},
            "elements": [{"tag": "markdown", "content": "您当前没有未完成的策略草稿。"}]
        }
        return _callback_response("success", "已打开草稿列表。", card)

    elements = [{"tag": "markdown", "content": "以下是您尚未完成的草稿："}]
    for d in drafts:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": f"**草稿ID**: {d.id}\n**当前进度**: {d.step}\n**已填名称**: {d.name or '未填写'}"
        })
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "您的草稿列表"}, "template": "blue"},
        "elements": elements
    }
    return _callback_response("success", "已打开草稿列表。", card)

def _handle_run_preview(user_id: str, strategy_id: str, store: StrategyStore, scanner: StrategyScanner) -> dict:
    from datetime import datetime, timezone
    strategy = store.get_strategy(strategy_id)
    if not strategy:
        return _callback_response("error", "策略未找到。")
    if strategy.creator_id != user_id:
        return _callback_response("error", "权限被拒绝：您不能运行他人的策略。")

    target_date = datetime.now(timezone.utc).strftime("%Y%m%d")
    try:
        _PREVIEW_EXECUTOR.submit(scanner.run_manual_preview, strategy, target_date)
    except Exception:
        logger.exception("strategy_preview_dispatch_failed strategy_id=%s", strategy_id)
        return _callback_response("error", "预览提交失败，请稍后重试。")

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "预览已提交"}, "template": "green"},
        "elements": [{"tag": "markdown", "content": f"已触发对策略 **{strategy.name}** 的手动预览。扫描在后台执行，结果卡片稍后单独下发（与正式通知隔离）。"}]
    }
    return _callback_response("success", "预览已提交，结果稍后推送。", card)

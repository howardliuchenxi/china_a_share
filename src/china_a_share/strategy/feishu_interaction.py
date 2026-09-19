import json
import logging
from typing import Optional

from .models import StrategyDraft
from .persistence import StrategyStore
from .scanner import StrategyScanner

logger = logging.getLogger(__name__)

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
    elif action_name == "run_preview":
        strategy_id = action.get("value", {}).get("strategy_id")
        return _handle_run_preview(user_id, strategy_id, store, scanner)
        
    return None

def _handle_create_draft(user_id: str, store: StrategyStore) -> dict:
    from uuid import uuid4
    draft_id = str(uuid4())
    draft = StrategyDraft(id=draft_id, creator_id=user_id, step="name")
    store.put_draft(draft)
    
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "新建策略草稿"},
            "template": "blue"
        },
        "elements": [
            {"tag": "markdown", "content": "**第一步：请回复您想设置的策略名称。**\n\n您随时可以发送“取消草稿”来放弃当前会话。"}
        ]
    }

def _handle_view_drafts(user_id: str, store: StrategyStore) -> dict:
    drafts = store.list_drafts(creator_id=user_id)
    if not drafts:
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "您的草稿列表"}, "template": "grey"},
            "elements": [{"tag": "markdown", "content": "您当前没有未完成的策略草稿。"}]
        }
        
    elements = [{"tag": "markdown", "content": "以下是您尚未完成的草稿："}]
    for d in drafts:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": f"**草稿ID**: {d.id}\n**当前进度**: {d.step}\n**已填名称**: {d.name or '未填写'}"
        })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "您的草稿列表"}, "template": "blue"},
        "elements": elements
    }

def _handle_run_preview(user_id: str, strategy_id: str, store: StrategyStore, scanner: StrategyScanner) -> dict:
    from datetime import datetime, timezone
    # Need to trigger a background execution or synchronously depending on timeouts.
    # For now, we simulate returning a card telling them it started.
    strategy = store.get_strategy(strategy_id)
    if not strategy:
        return {"content": "策略未找到。"}
    if strategy.creator_id != user_id:
        return {"content": "权限被拒绝：您不能运行他人的策略。"}
        
    target_date = datetime.now(timezone.utc).strftime("%Y%m%d")
    scanner.run_manual_preview(strategy, target_date)
    
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "规则已运行"}, "template": "green"},
        "elements": [{"tag": "markdown", "content": f"已触发对策略 **{strategy.name}** 的手动预览。结果卡片即将下发（与正式通知隔离）。"}]
    }

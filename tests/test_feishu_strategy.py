from china_a_share.strategy.feishu_interaction import _handle_create_draft, _handle_view_drafts
from china_a_share.strategy.models import StrategyDraft
import uuid
import pytest

class MockStrategyStore:
    def __init__(self):
        self.drafts = {}
    def put_draft(self, draft):
        self.drafts[draft.id] = draft
    def list_drafts(self, creator_id):
        return [d for d in self.drafts.values() if d.creator_id == creator_id]
        
def test_create_draft():
    store = MockStrategyStore()
    user_id = "user123"
    result = _handle_create_draft(user_id, store)
    
    assert result["header"]["template"] == "blue"
    assert len(store.drafts) == 1
    draft = list(store.drafts.values())[0]
    assert draft.creator_id == user_id
    assert draft.step == "name"

def test_view_drafts_isolation():
    store = MockStrategyStore()
    store.put_draft(StrategyDraft(id="1", creator_id="user1", step="name"))
    store.put_draft(StrategyDraft(id="2", creator_id="user2", step="name"))
    
    result = _handle_view_drafts("user1", store)
    # Should only see user1's draft
    content = str(result)
    assert "**草稿ID**: 1" in content
    assert "**草稿ID**: 2" not in content

def test_strategy_menu_trigger_condition(monkeypatch):
    import os
    monkeypatch.setenv("FEISHU_APP_ID", "test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test")
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "test")
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "test")
    
    from china_a_share.feishu import FeishuMessageEvent
    from china_a_share.bootstrap import create_feishu_research_bot
    from china_a_share.config import Settings
    
    bot = create_feishu_research_bot(Settings.from_env())
    
    # Text with mention and no text inside should be preserved
    payload = {
        "header": {"token": "test", "event_id": "1", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "user1"}},
            "message": {"message_type": "text", "content": '{"text": "<at user_id=\\"ou_123\\">bot</at> "}', "chat_id": "c1", "message_id": "m1"}
        }
    }
    
    ev = bot.parse_event(payload)
    assert ev is not None
    assert ev.mentions_bot is True
    assert ev.prompt == ""
    
    # Text without mention but text inside should be preserved
    payload["event"]["message"]["content"] = '{"text": "执行回测"}'
    ev2 = bot.parse_event(payload)
    assert ev2 is not None
    assert ev2.mentions_bot is False
    assert ev2.prompt == "执行回测"

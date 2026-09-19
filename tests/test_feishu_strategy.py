from china_a_share.strategy.feishu_interaction import _handle_create_draft, _handle_view_drafts
from china_a_share.strategy.models import StrategyDraft
import uuid

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

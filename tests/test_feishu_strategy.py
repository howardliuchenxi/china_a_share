import threading
import time
from concurrent.futures import ThreadPoolExecutor

from china_a_share.strategy import feishu_interaction
from china_a_share.strategy.feishu_interaction import (
    _handle_create_draft,
    _handle_run_preview,
    _handle_view_drafts,
    handle_strategy_interactive_card,
)
from china_a_share.strategy.models import (
    Operator,
    RuleCondition,
    StrategyConfig,
    StrategyDirection,
    StrategyDraft,
)
import uuid
import pytest

class MockStrategyStore:
    def __init__(self):
        self.drafts = {}
        self.strategies = {}
    def put_draft(self, draft):
        self.drafts[draft.id] = draft
    def list_drafts(self, creator_id):
        return [d for d in self.drafts.values() if d.creator_id == creator_id]
    def get_draft(self, did):
        return self.drafts.get(did)
    def delete_draft(self, did):
        self.drafts.pop(did, None)
    def get_strategy(self, sid):
        return self.strategies.get(sid)

def _assert_callback_envelope(response, expect_card):
    """Every card-callback reply must use the envelope Feishu accepts.

    A bare card at the top level (or a ``content`` key) is judged a failed
    callback and the client shows the error toast on the clicked card.
    """
    assert set(response) <= {"toast", "card"}
    toast = response["toast"]
    assert toast["type"] in {"info", "success", "error", "warning"}
    assert isinstance(toast["content"], str) and toast["content"]
    if expect_card:
        assert response["card"]["type"] == "raw"
        data = response["card"]["data"]
        assert "config" in data and "header" in data and "elements" in data
    else:
        assert "card" not in response

def test_create_draft():
    store = MockStrategyStore()
    user_id = "user123"
    result = _handle_create_draft(user_id, store)

    _assert_callback_envelope(result, expect_card=True)
    assert result["card"]["data"]["header"]["template"] == "blue"
    assert len(store.drafts) == 1
    draft = list(store.drafts.values())[0]
    assert draft.creator_id == user_id
    assert draft.step == "name"

def test_view_drafts_isolation():
    store = MockStrategyStore()
    store.put_draft(StrategyDraft(id="1", creator_id="user1", step="name"))
    store.put_draft(StrategyDraft(id="2", creator_id="user2", step="name"))

    result = _handle_view_drafts("user1", store)
    _assert_callback_envelope(result, expect_card=True)
    # Should only see user1's draft
    content = str(result["card"]["data"])
    assert "**草稿ID**: 1" in content
    assert "**草稿ID**: 2" not in content

def test_interactive_card_payload_routes_to_envelope_reply():
    store = MockStrategyStore()
    payload = {"open_id": "user123", "action": {"value": {"action": "create_draft"}}}
    result = handle_strategy_interactive_card(payload, store, scanner=None)
    _assert_callback_envelope(result, expect_card=True)
    assert len(store.drafts) == 1

    # A payload without an operator id cannot be attributed; it must not
    # mutate anything and yields no reply so the caller answers "unknown".
    assert handle_strategy_interactive_card(
        {"action": {"value": {"action": "create_draft"}}}, MockStrategyStore(), scanner=None
    ) is None

def test_run_preview_missing_strategy_and_permission_use_error_toast():
    store = MockStrategyStore()
    strategy = StrategyConfig(
        id="s1",
        name="test",
        creator_id="owner",
        direction=StrategyDirection.BUY,
        notify_target="owner",
        conditions=[
            RuleCondition(
                metric="drawdown",
                operator=Operator.GT,
                parameters={"window": 1, "threshold": 0.1},
            )
        ],
    )
    store.strategies["s1"] = strategy

    missing = _handle_run_preview("owner", "missing", store, scanner=object())
    _assert_callback_envelope(missing, expect_card=False)
    assert missing["toast"]["type"] == "error"

    denied = _handle_run_preview("intruder", "s1", store, scanner=object())
    _assert_callback_envelope(denied, expect_card=False)
    assert denied["toast"]["type"] == "error"

def test_run_preview_returns_fast_and_scans_in_background(monkeypatch):
    store = MockStrategyStore()
    strategy = StrategyConfig(
        id="s1",
        name="slow-scan",
        creator_id="owner",
        direction=StrategyDirection.BUY,
        notify_target="owner",
        conditions=[
            RuleCondition(
                metric="drawdown",
                operator=Operator.GT,
                parameters={"window": 1, "threshold": 0.1},
            )
        ],
    )
    store.strategies["s1"] = strategy

    scan_started = threading.Event()
    release_scan = threading.Event()
    calls = []

    class BlockingScanner:
        def run_manual_preview(self, strategy, target_date):
            calls.append((strategy.id, target_date))
            scan_started.set()
            release_scan.wait(timeout=10)

    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(feishu_interaction, "_PREVIEW_EXECUTOR", executor)

    started_at = time.monotonic()
    result = _handle_run_preview("owner", "s1", store, scanner=BlockingScanner())
    elapsed = time.monotonic() - started_at

    # The callback must ack well inside Feishu's 3-second deadline even while
    # the scan is still blocked, otherwise the client shows the error toast.
    assert elapsed < 2.0, f"callback blocked for {elapsed:.2f}s"
    _assert_callback_envelope(result, expect_card=True)
    assert result["toast"]["type"] == "success"

    assert scan_started.wait(timeout=2)
    release_scan.set()
    executor.shutdown(wait=True)
    assert calls and calls[0][0] == "s1"

def test_strategy_menu_trigger_condition(monkeypatch):
    import os
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("TUSHARE_CACHE_BUCKET", "test-bucket")
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

"""Tests for the strategy daily-scan API entry and its card dispatch."""

import json

from fastapi.testclient import TestClient

from china_a_share.api import (
    FEISHU_EVENTS_API_ROUTE,
    STRATEGY_SCAN_API_ROUTE,
    create_app,
)
from china_a_share.discovery.strategy_interaction import StrategyInteractionCoordinator
from china_a_share.discovery.strategy_store import MemoryStrategyStore
from china_a_share.feishu import (
    FeishuResearchBot,
    MemoryConversationStore,
)


class FakeSender:
    def __init__(self):
        self.replies = []
        self.cards = []

    def reply(self, message_id, text):
        self.replies.append((message_id, text))
        return f"reply-{len(self.replies)}"

    def update(self, message_id, text):
        raise AssertionError("Unexpected message update")

    def reply_card(self, message_id, card):
        self.cards.append((message_id, card))
        return f"card-{len(self.cards)}"


class FakeTaskCoordinator:
    def submit(self, request, *, task_id=None):
        raise AssertionError("Research tasks must not be submitted here")

    def get(self, task_id):
        return None


class FakeScanner:
    def __init__(self, failing=False):
        self.calls = []
        self.failing = failing

    def run_daily_scan(self, request_id):
        self.calls.append(request_id)
        if self.failing:
            raise RuntimeError("scan failed")


def build_bot(scanner=None):
    sender = FakeSender()
    store = MemoryStrategyStore()
    interaction = StrategyInteractionCoordinator(store, scanner)
    bot = FeishuResearchBot(
        FakeTaskCoordinator(),
        sender,
        MemoryConversationStore(),
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        strategy_interaction=interaction,
    )
    return bot, sender, store


def build_client(bot):
    return TestClient(create_app(feishu_research_bot=bot))


def env_setup(monkeypatch, token="scan-secret"):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("STRATEGY_SCAN_TOKEN", token)


def test_daily_scan_requires_authorization(monkeypatch):
    env_setup(monkeypatch)
    scanner = FakeScanner()
    bot, _, _ = build_bot(scanner)
    client = build_client(bot)

    response = client.post(STRATEGY_SCAN_API_ROUTE)
    assert response.status_code == 401
    assert scanner.calls == []


def test_daily_scan_rejects_wrong_static_token(monkeypatch):
    env_setup(monkeypatch)
    scanner = FakeScanner()
    bot, _, _ = build_bot(scanner)
    client = build_client(bot)

    response = client.post(
        STRATEGY_SCAN_API_ROUTE,
        headers={"Authorization": "Bearer not-the-secret"},
    )
    assert response.status_code == 401
    assert scanner.calls == []


def test_daily_scan_runs_scan_with_valid_token(monkeypatch):
    env_setup(monkeypatch)
    scanner = FakeScanner()
    bot, _, _ = build_bot(scanner)
    client = build_client(bot)

    response = client.post(
        STRATEGY_SCAN_API_ROUTE,
        headers={"Authorization": "Bearer scan-secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "completed"}
    assert len(scanner.calls) == 1


def test_daily_scan_propagates_scan_failure_as_server_error(monkeypatch):
    env_setup(monkeypatch)
    scanner = FakeScanner(failing=True)
    bot, _, _ = build_bot(scanner)
    client = build_client(bot)

    response = client.post(
        STRATEGY_SCAN_API_ROUTE,
        headers={"Authorization": "Bearer scan-secret"},
    )
    assert response.status_code == 500
    assert len(scanner.calls) == 1


def test_daily_scan_reports_missing_scanner(monkeypatch):
    env_setup(monkeypatch)
    bot, _, _ = build_bot(scanner=None)
    client = build_client(bot)

    response = client.post(
        STRATEGY_SCAN_API_ROUTE,
        headers={"Authorization": "Bearer scan-secret"},
    )
    assert response.status_code == 503


def strategy_card_payload(action_name, value):
    return {
        "header": {
            "token": "verification-token",
            "event_type": "card.action.trigger",
            "event_id": "evt-card-api-1",
            "tenant_key": "tenant",
        },
        "event": {
            "operator": {"open_id": "ou_api_user"},
            "context": {
                "open_chat_id": "oc_api_chat",
                "open_message_id": "msg_api_card",
            },
            "action": {"value": {"action": action_name, **value}},
        },
    }


def test_feishu_events_route_dispatches_strategy_card_actions():
    scanner = FakeScanner()
    bot, sender, store = build_bot(scanner)
    client = build_client(bot)

    response = client.post(
        FEISHU_EVENTS_API_ROUTE,
        json=strategy_card_payload("strategy_create_draft", {}),
    )
    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "success"
    # Background tasks run before the TestClient response resolves.
    assert len(sender.cards) == 1
    drafts = store.list_drafts("ou_api_user")
    assert len(drafts) == 1
    assert drafts[0].chat_id == "oc_api_chat"


def test_feishu_events_route_keeps_legacy_card_actions_working():
    bot, _, store = build_bot(FakeScanner())
    client = build_client(bot)

    response = client.post(
        FEISHU_EVENTS_API_ROUTE,
        json=strategy_card_payload("task_status", {}),
    )
    assert response.status_code == 200
    # Legacy actions are accepted with the standard toast without a strategy draft.
    assert response.json()["toast"]["type"] == "success"
    assert store.list_drafts("ou_api_user") == []

"""API entry and Feishu routing tests for the VIX group-alert capability."""

import json

from fastapi.testclient import TestClient

from china_a_share.api import VIX_SCAN_API_ROUTE, create_app
from china_a_share.feishu import FeishuResearchBot, MemoryConversationStore
from china_a_share.vix.rules import MemoryVixRuleStore, VixRule


class FakeSender:
    def __init__(self):
        self.replies = []

    def reply(self, message_id, text):
        self.replies.append((message_id, text))
        return f"reply-{len(self.replies)}"

    def update(self, message_id, text):
        raise AssertionError("Unexpected message update")

    def reply_card(self, message_id, card):
        raise AssertionError("Unexpected card reply")

    def send_chat_text(self, chat_id, text):
        raise AssertionError("Unexpected proactive chat send")


class FakeTaskCoordinator:
    def submit(self, request, *, task_id=None):
        raise AssertionError("Research tasks must not be submitted here")

    def get(self, task_id):
        return None


class ScanOnlyVixService:
    """Satisfies the scan endpoint only."""

    def __init__(self, summary=None, failing=False):
        self.summary = summary or {"status": "completed", "alerts_sent": 0}
        self.failing = failing
        self.calls = 0

    def run_hourly_scan(self):
        self.calls += 1
        if self.failing:
            raise RuntimeError("scan exploded")
        return dict(self.summary)


class CommandCapableVixService(ScanOnlyVixService):
    """Adds the command surface consumed by vix.commands (rules + lookback)."""

    def __init__(self):
        super().__init__()
        self.rules = MemoryVixRuleStore()
        self.lookback_result = None

    def lookback(self, date_str):
        return self.lookback_result


def build_bot(vix_service=None):
    sender = FakeSender()
    bot = FeishuResearchBot(
        FakeTaskCoordinator(),
        sender,
        MemoryConversationStore(),
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        vix_service=vix_service,
    )
    return bot, sender


def build_client(bot):
    return TestClient(create_app(feishu_research_bot=bot))


def env_setup(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.delenv("VIX_SCAN_TOKEN", raising=False)
    monkeypatch.delenv("PUBLIC_APP_URL", raising=False)


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def message_payload(prompt_text, event_id="evt-1"):
    return {
        "header": {
            "token": "verification-token",
            "event_type": "im.message.receive_v1",
            "event_id": event_id,
            "tenant_key": "tenant",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_type": "text",
                "chat_id": "oc_chat",
                "message_id": f"om-{event_id}",
                "content": json.dumps({"text": f"<at user_id=\"1\">@bot</at>{prompt_text}"}),
            },
        },
    }


# --- scan endpoint ----------------------------------------------------------


def test_vix_scan_requires_authorization(monkeypatch):
    env_setup(monkeypatch)
    client = build_client(build_bot(ScanOnlyVixService())[0])
    assert client.post(VIX_SCAN_API_ROUTE).status_code == 401


def test_vix_scan_rejects_wrong_static_token(monkeypatch):
    env_setup(monkeypatch)
    monkeypatch.setenv("VIX_SCAN_TOKEN", "vix-secret")
    client = build_client(build_bot(ScanOnlyVixService())[0])
    response = client.post(VIX_SCAN_API_ROUTE, headers=auth_headers("wrong-token"))
    assert response.status_code == 401


def test_vix_scan_accepts_configured_static_token(monkeypatch):
    env_setup(monkeypatch)
    monkeypatch.setenv("VIX_SCAN_TOKEN", "vix-secret")
    service = ScanOnlyVixService(
        {"status": "completed", "quote_date": "2026-10-02", "alerts_sent": 2}
    )
    client = build_client(build_bot(service)[0])
    response = client.post(VIX_SCAN_API_ROUTE, headers=auth_headers("vix-secret"))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["alerts_sent"] == 2
    assert service.calls == 1


def test_vix_scan_accepts_google_oidc_token_for_public_audience(monkeypatch):
    env_setup(monkeypatch)
    monkeypatch.setenv("PUBLIC_APP_URL", "https://example.run.app")

    def fake_verify(token, request, audience):
        assert audience in (
            "https://example.run.app",
            f"https://example.run.app{VIX_SCAN_API_ROUTE}",
        )
        return {"iss": "https://accounts.google.com", "email": "sched@example.iam"}

    monkeypatch.setattr(
        "china_a_share.api.google_id_token.verify_oauth2_token", fake_verify
    )
    service = ScanOnlyVixService()
    client = build_client(build_bot(service)[0])
    response = client.post(
        VIX_SCAN_API_ROUTE, headers=auth_headers("google-identity-token")
    )
    assert response.status_code == 200
    assert service.calls == 1


def test_vix_scan_rejects_oidc_token_with_wrong_issuer(monkeypatch):
    env_setup(monkeypatch)
    monkeypatch.setenv("PUBLIC_APP_URL", "https://example.run.app")
    monkeypatch.setattr(
        "china_a_share.api.google_id_token.verify_oauth2_token",
        lambda token, request, audience: {"iss": "https://evil.example.com"},
    )
    client = build_client(build_bot(ScanOnlyVixService())[0])
    response = client.post(
        VIX_SCAN_API_ROUTE, headers=auth_headers("forged-identity-token")
    )
    assert response.status_code == 401


def test_vix_scan_reports_missing_service(monkeypatch):
    env_setup(monkeypatch)
    monkeypatch.setenv("VIX_SCAN_TOKEN", "vix-secret")
    client = build_client(build_bot(None)[0])
    response = client.post(VIX_SCAN_API_ROUTE, headers=auth_headers("vix-secret"))
    assert response.status_code == 503
    assert "VIX alert service" in response.json()["detail"]


def test_vix_scan_propagates_scan_failure_as_server_error(monkeypatch):
    env_setup(monkeypatch)
    monkeypatch.setenv("VIX_SCAN_TOKEN", "vix-secret")
    client = build_client(build_bot(ScanOnlyVixService(failing=True))[0])
    response = client.post(VIX_SCAN_API_ROUTE, headers=auth_headers("vix-secret"))
    assert response.status_code == 500


# --- Feishu command routing -------------------------------------------------


def test_vix_rule_command_round_trip_through_bot_process():
    service = CommandCapableVixService()
    bot, sender = build_bot(service)
    event = bot.parse_event(message_payload("VIX提醒 上涨10%", "evt-rule"))
    assert event.chat_id == "oc_chat"
    bot.process(event)
    assert len(sender.replies) == 1
    assert "已添加规则" in sender.replies[0][1]
    stored = service.rules.load("oc_chat")
    assert len(stored) == 1
    assert stored[0].direction == "up"
    assert stored[0].threshold_pct == 10.0
    assert stored[0].created_by == "ou_user"


def test_vix_lookback_command_round_trip_through_bot_process():
    service = CommandCapableVixService()
    service.lookback_result = {
        "date": "2026-04-04",
        "close": 45.31,
        "prev_date": "2026-04-03",
        "prev_close": 30.02,
        "change_pct": 50.93,
        "source": "history",
    }
    bot, sender = build_bot(service)
    bot.process_event_for_test = None  # no special-casing; use process()
    event = bot.parse_event(message_payload("VIX回溯 20260404", "evt-look"))
    bot.process(event)
    reply = sender.replies[0][1]
    assert "收盘 45.31" in reply
    assert "上涨 50.93%" in reply
    assert "本群还没有" in reply  # no rules configured yet


def test_vix_commands_do_not_reach_research_submission():
    service = CommandCapableVixService()
    bot, sender = build_bot(service)
    event = bot.parse_event(message_payload("VIX规则", "evt-list"))
    bot.process(event)
    assert len(sender.replies) == 1
    assert "本群" in sender.replies[0][1]
    assert "研究" not in sender.replies[0][1]


def test_disabled_vix_service_answers_with_notice():
    bot, sender = build_bot(None)
    bot.process(bot.parse_event(message_payload("VIX规则", "evt-off")))
    assert "未启用" in sender.replies[0][1]


def test_rule_describe_renders_threshold():
    assert VixRule("x", "up", 10.0, "u", "t").describe() == "VIX 上涨 ≥10.00%"
    assert VixRule("y", "down", 5.5, "u", "t").describe() == "VIX 下跌 ≥5.50%"

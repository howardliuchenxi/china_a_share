import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from china_a_share import bootstrap
from china_a_share.api import create_app
from china_a_share.config import Settings
from china_a_share.feishu import (
    FeishuEventError,
    FeishuResearchBot,
    MemoryConversationStore,
)


class FakeSender:
    def __init__(self):
        self.replies = []

    def reply(self, message_id, text):
        self.replies.append((message_id, text))


class FakeAnalysisService:
    def __init__(self):
        self.requests = []

    def answer(self, request_id, prompt, conversation):
        self.requests.append((request_id, prompt, conversation))
        return f"Answer: {prompt}", f'{{"tool":"test","prompt":"{prompt}"}}'


def build_bot(*, allowed_open_ids=None):
    service = FakeAnalysisService()
    sender = FakeSender()
    store = MemoryConversationStore()
    bot = FeishuResearchBot(
        service,
        sender,
        store,
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        allowed_open_ids=allowed_open_ids,
    )
    return bot, service, sender, store


def test_bootstrap_allows_feishu_availability_range_without_open_id_allowlist(
    monkeypatch,
):
    provider = object()
    store = object()
    sender = object()
    research_service = object()
    monkeypatch.setattr(bootstrap, "_create_data_provider", lambda settings: provider)
    monkeypatch.setattr(
        bootstrap,
        "LocalResearchConversationService",
        lambda active_provider: research_service,
    )
    monkeypatch.setattr(
        bootstrap,
        "FeishuOpenApiClient",
        lambda app_id, app_secret: sender,
    )
    monkeypatch.setattr(
        bootstrap,
        "CloudStorageConversationStore",
        lambda bucket_name: store,
    )

    bot = bootstrap.create_feishu_research_bot(
        Settings(
            tushare_token="token",
            deepseek_api_key="deepseek",
            tushare_cache_bucket="bucket",
            feishu_app_id="app",
            feishu_app_secret="secret",
            feishu_verification_token="verification",
            feishu_encrypt_key="encryption",
        )
    )

    assert bot._research_service is research_service
    assert bot._sender is sender
    assert bot._store is store
    assert bot._allowed_open_ids == set()


def message_payload(event_id="event-1", text="<at user_id=\"bot\">Bot</at> 统计二连板"):
    return {
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "tenant_key": "tenant-1",
            "token": "verification-token",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "message_id": f"message-{event_id}",
                "chat_id": "chat-1",
                "thread_id": "thread-1",
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def test_signature_validation_accepts_exact_body_and_rejects_mutation():
    bot, _, _, _ = build_bot()
    body = b'{"safe":true}'
    signature = hashlib.sha256(b"123nonceencrypt-key" + body).hexdigest()

    bot.verify_signature(body, "123", "nonce", signature)

    with pytest.raises(FeishuEventError, match="signature is invalid"):
        bot.verify_signature(body + b" ", "123", "nonce", signature)


def test_message_event_removes_bot_mention_and_isolates_conversation():
    bot, _, _, _ = build_bot(allowed_open_ids={"user-1"})

    event = bot.parse_event(message_payload())

    assert event is not None
    assert event.prompt == "统计二连板"
    assert event.conversation_id == "tenant-1:chat-1:thread-1:user-1"


def test_message_event_rejects_unapproved_user():
    bot, _, _, _ = build_bot(allowed_open_ids={"different-user"})

    with pytest.raises(FeishuEventError, match="not allowed"):
        bot.parse_event(message_payload())


def test_processing_reuses_bounded_context_and_deduplicates_event():
    bot, service, sender, store = build_bot()
    first = bot.parse_event(message_payload("event-1", "统计二连板"))
    second = bot.parse_event(message_payload("event-2", "只看八月"))
    assert first is not None
    assert second is not None

    bot.process(first)
    bot.process(second)
    bot.process(second)

    assert len(service.requests) == 2
    assert service.requests[0][2] == []
    assert len(service.requests[1][2]) == 1
    assert service.requests[1][2][0].prompt == "统计二连板"
    assert len(sender.replies) == 2
    assert len(store.get(second.conversation_id)) == 2


def test_endpoint_verification_requires_matching_token():
    bot, _, _, _ = build_bot()

    assert bot.verify_challenge(
        {
            "type": "url_verification",
            "token": "verification-token",
            "challenge": "challenge-value",
        }
    ) == "challenge-value"

    with pytest.raises(FeishuEventError, match="token is invalid"):
        bot.verify_challenge(
            {
                "type": "url_verification",
                "token": "wrong-token",
                "challenge": "challenge-value",
            }
        )
class FakeEndpointBot:
    def __init__(self):
        self.processed = []

    def verify_signature(self, body, timestamp, nonce, signature):
        assert body
        assert (timestamp, nonce, signature) == ("123", "nonce", "signature")

    def verify_challenge(self, payload):
        assert payload["token"] == "verification-token"
        return payload["challenge"]

    def parse_event(self, payload):
        return payload["event"]

    def process(self, event):
        self.processed.append(event)


def test_feishu_endpoint_acknowledges_and_processes_authenticated_event():
    bot = FakeEndpointBot()
    client = TestClient(create_app(feishu_research_bot=bot))

    response = client.post(
        "/api/integrations/feishu/events",
        headers={
            "X-Lark-Request-Timestamp": "123",
            "X-Lark-Request-Nonce": "nonce",
            "X-Lark-Signature": "signature",
        },
        json={"event": {"event_id": "event-1"}},
    )

    assert response.status_code == 200
    assert response.json() == {"code": 0}
    assert bot.processed == [{"event_id": "event-1"}]


def test_feishu_endpoint_returns_verified_challenge():
    bot = FakeEndpointBot()
    client = TestClient(create_app(feishu_research_bot=bot))

    response = client.post(
        "/api/integrations/feishu/events",
        headers={
            "X-Lark-Request-Timestamp": "123",
            "X-Lark-Request-Nonce": "nonce",
            "X-Lark-Signature": "signature",
        },
        json={
            "type": "url_verification",
            "token": "verification-token",
            "challenge": "challenge-value",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-value"}

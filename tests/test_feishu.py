import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os

import pytest
from Crypto.Cipher import AES
from fastapi.testclient import TestClient

from china_a_share import bootstrap
from china_a_share.api import create_app
from china_a_share.config import Settings
from china_a_share.core.contracts import (
    MAX_ANALYSIS_PROMPT_LENGTH,
    AnalysisRequest,
    AnalysisResponse,
    AnalysisStatus,
    AnalysisTask,
    AnalysisTaskStatus,
    AnalysisTaskSubmission,
    DataQuery,
    QueryPlan,
    ServiceError,
)
from china_a_share.feishu import (
    CloudStorageConversationStore,
    FeishuConversationTurn,
    FeishuEventError,
    FeishuResearchBot,
    FeishuTaskRecord,
    MemoryConversationStore,
    format_analysis_task,
)
from china_a_share.feishu_feedback import (
    FeedbackTurn,
    FeishuFeedbackCoordinator,
)
from china_a_share.feishu_agent import FeishuAgentCoordinator
from china_a_share.tasks import MemoryAnalysisTaskStore
from google.api_core.exceptions import PreconditionFailed


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


class FailOnceSender(FakeSender):
    def __init__(self):
        super().__init__()
        self.failed = False

    def reply(self, message_id, text):
        if not self.failed:
            self.failed = True
            raise RuntimeError("temporary Feishu reply failure")
        super().reply(message_id, text)


class ReplayableMemoryConversationStore(MemoryConversationStore):
    def claim_event(self, event_id):
        return True


class FakeTaskCoordinator:
    def __init__(self):
        self.requests = []
        self.tasks = {}

    def submit(self, request, *, task_id=None):
        self.requests.append(request)
        task_id = task_id or f"{len(self.requests):032x}"
        if task_id in self.tasks:
            return AnalysisTaskSubmission(
                task_id=task_id,
                status=self.tasks[task_id].status,
                status_url=f"/api/analysis/tasks/{task_id}",
            )
        now = datetime.now(timezone.utc)
        self.tasks[task_id] = AnalysisTask(
            task_id=task_id,
            status=AnalysisTaskStatus.QUEUED,
            request=request,
            created_at=now,
            updated_at=now,
        )
        return AnalysisTaskSubmission(
            task_id=task_id,
            status=AnalysisTaskStatus.QUEUED,
            status_url=f"/api/analysis/tasks/{task_id}",
        )

    def get(self, task_id):
        task = self.tasks.get(task_id)
        return task.model_copy(deep=True) if task else None


class FakeEventBlob:
    def __init__(self):
        self.content = None
        self.generation = None
        self.updated = None

    def upload_from_string(self, content, **kwargs):
        generation_match = kwargs.get("if_generation_match")
        if generation_match == 0 and self.generation is not None:
            raise PreconditionFailed("event already exists")
        if generation_match not in (None, 0, self.generation):
            raise PreconditionFailed("event generation changed")
        self.content = content
        self.generation = (self.generation or 0) + 1
        self.updated = datetime.now(timezone.utc)

    def reload(self):
        return None

    def exists(self):
        return self.content is not None

    def download_as_text(self):
        return self.content


class FakeEventBucket:
    def __init__(self):
        self.blobs = {}

    def blob(self, name):
        return self.blobs.setdefault(name, FakeEventBlob())


def build_bot(*, allowed_open_ids=None):
    service = FakeTaskCoordinator()
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
    # Storage clients resolve lazily; a fake project keeps every constructor
    # offline instead of depending on the local gcloud environment.
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    coordinator = object()
    store = object()
    sender = object()
    monkeypatch.setattr(
        bootstrap,
        "create_analysis_task_coordinator",
        lambda settings: coordinator,
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
    llm_switcher = object()
    monkeypatch.setattr(
        bootstrap,
        "create_llm_preference_controller",
        lambda settings: llm_switcher,
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

    assert bot._task_coordinator is coordinator
    assert bot._sender is sender
    assert bot._store is store
    assert bot._allowed_open_ids == set()
    assert bot._llm_switcher is llm_switcher


def message_payload(
    event_id="event-1",
    text="<at user_id=\"bot\">Bot</at> 统计二连板",
    *,
    mentions=None,
    root_id=None,
    thread_id="thread-1",
):
    message = {
        "message_id": f"message-{event_id}",
        "chat_id": "chat-1",
        "thread_id": thread_id,
        "message_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "mentions": mentions or [],
    }
    if root_id is not None:
        message["root_id"] = root_id
    return {
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "tenant_key": "tenant-1",
            "token": "verification-token",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": message,
        },
    }


def card_action_payload(
    action,
    *,
    event_id="card-event-1",
    form_value=None,
    extra_value=None,
):
    value = {"action": action}
    if extra_value:
        value.update(extra_value)
    return {
        "header": {
            "event_id": event_id,
            "event_type": "card.action.trigger",
            "tenant_key": "tenant-1",
            "token": "verification-token",
        },
        "event": {
            "operator": {"open_id": "user-1"},
            "context": {
                "open_message_id": "card-message-1",
                "open_chat_id": "chat-1",
            },
            "action": {
                "tag": "button",
                "name": action,
                "value": value,
                "form_value": form_value or {},
            },
        },
    }


def encrypt_payload(payload, encrypt_key="encrypt-key"):
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    padding_size = AES.block_size - len(plaintext) % AES.block_size
    padded = plaintext + bytes([padding_size]) * padding_size
    iv = bytes(range(AES.block_size))
    key = hashlib.sha256(encrypt_key.encode()).digest()
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(padded)
    return json.dumps(
        {"encrypt": base64.b64encode(iv + ciphertext).decode("ascii")}
    ).encode("utf-8")


def test_signature_validation_accepts_exact_body_and_rejects_mutation():
    bot, _, _, _ = build_bot()
    body = b'{"safe":true}'
    signature = hashlib.sha256(b"123nonceencrypt-key" + body).hexdigest()

    bot.verify_signature(body, "123", "nonce", signature)

    with pytest.raises(FeishuEventError, match="signature is invalid"):
        bot.verify_signature(body + b" ", "123", "nonce", signature)


def test_signature_validation_allows_only_fully_missing_card_headers():
    bot, _, _, _ = build_bot()

    bot.verify_signature(b'{"schema":"2.0"}', "", "", "", allow_missing=True)

    with pytest.raises(FeishuEventError, match="headers are required"):
        bot.verify_signature(b'{"schema":"2.0"}', "", "", "")

    with pytest.raises(FeishuEventError, match="headers are required"):
        bot.verify_signature(
            b'{"schema":"2.0"}',
            "123",
            "",
            "",
            allow_missing=True,
        )


def test_callback_decoder_accepts_plaintext_and_encrypted_payloads():
    bot, _, _, _ = build_bot()
    payload = message_payload()

    assert bot.decode_payload(json.dumps(payload).encode("utf-8")) == payload
    assert bot.decode_payload(encrypt_payload(payload)) == payload


def test_callback_decoder_rejects_malformed_ciphertext():
    bot, _, _, _ = build_bot()
    body = json.dumps({"encrypt": base64.b64encode(b"too-short").decode()}).encode()

    with pytest.raises(FeishuEventError, match="encryption is invalid"):
        bot.decode_payload(body)


def test_message_event_removes_bot_mention_and_isolates_conversation():
    bot, _, _, _ = build_bot(allowed_open_ids={"user-1"})

    event = bot.parse_event(message_payload())

    assert event is not None
    assert event.prompt == "统计二连板"
    assert event.conversation_id == "tenant-1:chat-1:thread-1:user-1"


def test_quoted_reply_keeps_direct_message_conversation_identity():
    """Ordinary-chat replies (root_id) must not fork the conversation bucket."""
    bot, _, _, _ = build_bot(allowed_open_ids={"user-1"})

    direct = bot.parse_event(
        message_payload("event-direct", "研究对象为A股所有股票", thread_id=None)
    )
    quoted_reply = bot.parse_event(
        message_payload(
            "event-reply",
            "2",
            thread_id=None,
            root_id="bot-answer-message-1",
        )
    )
    direct_follow_up = bot.parse_event(
        message_payload("event-direct-2", "3", thread_id=None)
    )

    assert direct is not None
    assert quoted_reply is not None
    assert direct_follow_up is not None
    assert quoted_reply.conversation_id == direct.conversation_id
    assert direct_follow_up.conversation_id == direct.conversation_id


def test_topic_group_threads_still_partition_conversation_identity():
    """Topic-group thread_id remains a durable partition across replies."""
    bot, _, _, _ = build_bot(allowed_open_ids={"user-1"})

    first_topic = bot.parse_event(
        message_payload("event-topic-1", "话题一提问", thread_id="topic-1")
    )
    first_topic_reply = bot.parse_event(
        message_payload(
            "event-topic-1-reply",
            "追问",
            thread_id="topic-1",
            root_id="bot-answer-topic-1",
        )
    )
    second_topic = bot.parse_event(
        message_payload("event-topic-2", "话题二提问", thread_id="topic-2")
    )

    assert first_topic is not None
    assert first_topic_reply is not None
    assert second_topic is not None
    assert first_topic_reply.conversation_id == first_topic.conversation_id
    assert second_topic.conversation_id != first_topic.conversation_id


def test_message_event_removes_structured_mention_placeholder_before_commands():
    bot, _, _, _ = build_bot(allowed_open_ids={"user-1"})

    event = bot.parse_event(
        message_payload(
            "event-status",
            "@_user_1 查看进度",
            mentions=[{"key": "@_user_1", "name": "A股研究助手"}],
        )
    )

    assert event is not None
    assert event.prompt == "查看进度"


def test_message_event_rejects_unapproved_user():
    bot, _, _, _ = build_bot(allowed_open_ids={"different-user"})

    with pytest.raises(FeishuEventError, match="not allowed"):
        bot.parse_event(message_payload())


def test_empty_bot_mention_opens_quick_menu_without_submitting_task():
    bot, service, sender, _ = build_bot()
    event = bot.parse_event(
        message_payload("event-menu", '<at user_id="bot">Bot</at>')
    )
    assert event is not None

    bot.process(event)

    assert service.requests == []
    assert sender.replies == []
    assert len(sender.cards) == 1
    message_id, card = sender.cards[0]
    assert message_id == "message-event-menu"
    form = next(element for element in card["elements"] if element["tag"] == "form")
    assert {element["name"] for element in form["elements"]} == {
        "prompt",
        "submit_research",
    }
    actions = next(
        element for element in card["elements"] if element["tag"] == "action"
    )
    assert [button["value"]["action"] for button in actions["actions"]] == [
        "new_session",
        "list_sessions",
        "task_status",
    ]


def test_card_form_submission_becomes_research_prompt():
    bot, _, _, _ = build_bot(allowed_open_ids={"user-1"})

    event = bot.parse_card_action(
        card_action_payload(
            "submit_research",
            form_value={"prompt": "查询最近5个交易日涨幅最大的10只A股"},
        )
    )

    assert event is not None
    assert event.prompt == "查询最近5个交易日涨幅最大的10只A股"
    assert event.message_id == "card-message-1"
    assert event.conversation_id == "tenant-1:chat-1:root:user-1"


@pytest.mark.parametrize(
    "action, expected_prompt",
    [
        ("new_session", "新建会话"),
        ("list_sessions", "会话列表"),
        ("task_status", "查看进度"),
    ],
)
def test_card_shortcut_becomes_existing_command(action, expected_prompt):
    bot, _, _, _ = build_bot()

    event = bot.parse_card_action(card_action_payload(action))

    assert event is not None
    assert event.prompt == expected_prompt


def test_card_action_requires_matching_verification_token():
    bot, _, _, _ = build_bot()
    payload = card_action_payload("list_sessions")
    payload["header"]["token"] = "wrong-token"

    with pytest.raises(FeishuEventError, match="token is invalid"):
        bot.parse_card_action(payload)


def test_processing_submits_durable_task_and_deduplicates_event():
    bot, service, sender, store = build_bot()
    first = bot.parse_event(message_payload("event-1", "统计二连板"))
    assert first is not None

    bot.process(first)
    bot.process(first)

    task_id = bot._task_id_for_event("event-1")
    assert len(service.requests) == 1
    assert service.requests[0].prompt == "统计二连板"
    assert service.requests[0].conversation == []
    assert len(sender.replies) == 1
    assert "研究任务已受理" in sender.replies[0][1]
    assert task_id in sender.replies[0][1]
    assert store.get_latest_task(first.conversation_id).task_id == task_id


def test_retried_event_reuses_task_created_before_reply_failure():
    service = FakeTaskCoordinator()
    sender = FailOnceSender()
    store = ReplayableMemoryConversationStore()
    bot = FeishuResearchBot(
        service,
        sender,
        store,
        verification_token="verification-token",
        encrypt_key="encrypt-key",
    )
    event = bot.parse_event(message_payload("event-1", "查散户比例前十"))
    assert event is not None

    bot.process(event)
    bot.process(event)

    assert len(service.requests) == 1
    assert len(sender.replies) == 2
    assert "研究任务已受理" in sender.replies[1][1]


def test_new_prompt_waits_for_active_conversation_task():
    bot, service, sender, _ = build_bot()
    first = bot.parse_event(message_payload("event-1", "查散户比例前十"))
    follow_up = bot.parse_event(message_payload("event-2", "只看创业板"))
    assert first is not None
    assert follow_up is not None

    bot.process(first)
    bot.process(follow_up)

    assert len(service.requests) == 1
    assert "上一项研究任务" in sender.replies[1][1]
    assert "查看进度" in sender.replies[1][1]


def test_status_records_completed_context_for_follow_up_submission():
    bot, service, sender, _ = build_bot()
    first = bot.parse_event(message_payload("event-1", "统计2026年8月涨停股票"))
    assert first is not None
    bot.process(first)
    task_id = bot._task_id_for_event("event-1")
    completed = service.tasks[task_id]
    completed.status = AnalysisTaskStatus.SUCCEEDED
    completed.response = AnalysisResponse(
        request_id=task_id,
        planner="test-planner",
        data_provider="test-provider",
        status=AnalysisStatus.SUCCESS,
        plan=QueryPlan(
            interpretation="Analyze August 2026 limit-up stocks.",
            queries=[
                DataQuery(
                    query_id="limit-ups",
                    operation="limit_list_d",
                    purpose="Retrieve the requested limit-up stocks.",
                )
            ],
        ),
    )

    status_event = bot.parse_event(message_payload("event-2", "查看进度"))
    follow_up = bot.parse_event(message_payload("event-3", "只看前十名"))
    assert status_event is not None
    assert follow_up is not None
    bot.process(status_event)
    bot.process(follow_up)

    assert "状态：已完成" in sender.replies[1][1]
    assert len(service.requests[1].conversation) == 1
    assert service.requests[1].conversation[0].prompt == "统计2026年8月涨停股票"
    assert service.requests[1].conversation[0].interpretation == (
        "Analyze August 2026 limit-up stocks."
    )


def test_processing_accepts_prompt_beyond_record_cap_and_replays_context():
    long_prompt = "请研究以下名单的近期表现：" + "、".join(f"股票{i}" for i in range(600))
    assert 1_000 < len(long_prompt) < MAX_ANALYSIS_PROMPT_LENGTH
    bot, service, sender, store = build_bot()
    event = bot.parse_event(message_payload("event-1", long_prompt))
    assert event is not None

    bot.process(event)

    task_id = bot._task_id_for_event("event-1")
    assert "研究任务已受理" in sender.replies[0][1]
    assert service.requests[0].prompt == long_prompt
    assert store.get_latest_task(event.conversation_id).prompt == long_prompt

    completed = service.tasks[task_id]
    completed.status = AnalysisTaskStatus.SUCCEEDED
    completed.response = AnalysisResponse(
        request_id=task_id,
        planner="test-planner",
        data_provider="test-provider",
        status=AnalysisStatus.SUCCESS,
        plan=QueryPlan(
            interpretation="Analyze the requested watchlist.",
            queries=[
                DataQuery(
                    query_id="watchlist",
                    operation="daily",
                    purpose="Retrieve the requested watchlist prices.",
                )
            ],
        ),
    )
    status_event = bot.parse_event(message_payload("event-2", "查看进度"))
    follow_up = bot.parse_event(message_payload("event-3", "只看涨幅前二十"))
    assert status_event is not None
    assert follow_up is not None
    bot.process(status_event)
    bot.process(follow_up)

    assert "状态：已完成" in sender.replies[1][1]
    assert len(service.requests[1].conversation) == 1
    assert service.requests[1].conversation[0].prompt == long_prompt


def test_processing_rejects_over_limit_prompt_with_actionable_reply():
    over_limit = "研" * (MAX_ANALYSIS_PROMPT_LENGTH + 1)
    bot, service, sender, _ = build_bot()
    event = bot.parse_event(message_payload("event-1", over_limit))
    assert event is not None

    bot.process(event)

    assert service.requests == []
    assert "问题过长" in sender.replies[0][1]
    assert str(MAX_ANALYSIS_PROMPT_LENGTH) in sender.replies[0][1]

    later = bot.parse_event(message_payload("event-2", "统计二连板"))
    assert later is not None
    bot.process(later)
    assert "研究任务已受理" in sender.replies[1][1]


def test_retry_creates_new_task_only_after_failure():
    bot, service, sender, _ = build_bot()
    first = bot.parse_event(message_payload("event-1", "查散户比例前十"))
    assert first is not None
    bot.process(first)
    task_id = bot._task_id_for_event("event-1")

    running_retry = bot.parse_event(message_payload("event-2", "重试"))
    assert running_retry is not None
    bot.process(running_retry)
    assert "只有失败任务可以重试" in sender.replies[1][1]
    assert len(service.requests) == 1

    service.tasks[task_id].status = AnalysisTaskStatus.FAILED
    service.tasks[task_id].error = ServiceError(
        source="system", message="temporary provider timeout"
    )
    failed_retry = bot.parse_event(message_payload("event-3", f"重试 {task_id}"))
    assert failed_retry is not None
    bot.process(failed_retry)

    assert len(service.requests) == 2
    assert service.requests[1] == service.tasks[task_id].request
    assert f"新任务编号：{bot._task_id_for_event('event-3')}" in sender.replies[2][1]


def test_agent_bot_supports_named_sessions_and_parallel_submissions():
    task_store = MemoryAnalysisTaskStore()

    class RecordingDispatcher:
        def __init__(self):
            self.task_ids = []

        def dispatch(self, task_id):
            self.task_ids.append(task_id)

    dispatcher = RecordingDispatcher()
    agent_coordinator = FeishuAgentCoordinator(task_store, dispatcher)
    sender = FakeSender()
    conversation_store = MemoryConversationStore()
    bot = FeishuResearchBot(
        agent_coordinator,
        sender,
        conversation_store,
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        agent_coordinator=agent_coordinator,
    )

    create_event = bot.parse_event(message_payload("event-session", "新建会话 银行研究"))
    first_prompt = bot.parse_event(message_payload("event-agent-1", "查询银行股估值"))
    second_prompt = bot.parse_event(message_payload("event-agent-2", "再查股息率"))
    assert create_event is not None
    assert first_prompt is not None
    assert second_prompt is not None

    bot.process(create_event)
    bot.process(first_prompt)
    bot.process(second_prompt)

    assert "已新建并切换到会话：银行研究" in sender.replies[0][1]
    assert len(sender.replies) == 1
    assert len(dispatcher.task_ids) == 2
    submitted_tasks = [task_store.get(task_id) for task_id in dispatcher.task_ids]
    assert all(task.status == AnalysisTaskStatus.QUEUED for task in submitted_tasks)
    assert submitted_tasks[0].request.conversation_id == submitted_tasks[1].request.conversation_id
    assert submitted_tasks[0].request.conversation_name == "银行研究"
    assert submitted_tasks[1].request.conversation_name == "银行研究"
    assert ":session:" in submitted_tasks[0].request.conversation_id


def test_agent_follow_up_by_quoting_bot_answer_keeps_history():
    """Answering the bot's menu by replying to it must retain prior turns."""
    task_store = MemoryAnalysisTaskStore()

    class RecordingDispatcher:
        def __init__(self):
            self.task_ids = []

        def dispatch(self, task_id):
            self.task_ids.append(task_id)

    agent_coordinator = FeishuAgentCoordinator(task_store, RecordingDispatcher())
    bot = FeishuResearchBot(
        agent_coordinator,
        FakeSender(),
        MemoryConversationStore(),
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        agent_coordinator=agent_coordinator,
    )

    first = bot.parse_event(
        message_payload("event-agent-1", "研究对象为A股所有股票", thread_id=None)
    )
    assert first is not None
    bot.process(first)
    first_task_id = bot._task_id_for_event("event-agent-1")
    first_task = task_store.get(first_task_id)
    first_task.status = AnalysisTaskStatus.SUCCEEDED
    first_task.answer = "已记录研究范围为A股全部股票。回复序号选择分析类型：1 涨跌分布 2 财务指标统计。"
    task_store.put(first_task)

    follow_up = bot.parse_event(
        message_payload(
            "event-agent-2",
            "2",
            thread_id=None,
            root_id="bot-answer-message-event-agent-1",
        )
    )
    assert follow_up is not None
    bot.process(follow_up)

    follow_up_task = task_store.get(bot._task_id_for_event("event-agent-2"))
    assert follow_up_task is not None
    assert follow_up_task.request.prompt == "2"
    assert [turn.prompt for turn in follow_up_task.request.conversation] == [
        "研究对象为A股所有股票"
    ]
    assert "财务指标统计" in follow_up_task.request.conversation[0].answer


def test_agent_bot_creates_backend_session_and_submits_combined_prompt():
    task_store = MemoryAnalysisTaskStore()

    class RecordingDispatcher:
        def __init__(self):
            self.task_ids = []

        def dispatch(self, task_id):
            self.task_ids.append(task_id)

    dispatcher = RecordingDispatcher()
    coordinator = FeishuAgentCoordinator(task_store, dispatcher)
    sender = FakeSender()
    conversation_store = MemoryConversationStore()
    bot = FeishuResearchBot(
        coordinator,
        sender,
        conversation_store,
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        agent_coordinator=coordinator,
    )
    event = bot.parse_event(
        message_payload(
            "event-combined",
            "新建对话，查出最近30个交易日累计涨幅最大的个股",
        )
    )
    assert event is not None

    bot.process(event)

    assert sender.replies == []
    assert len(dispatcher.task_ids) == 1
    task = task_store.get(dispatcher.task_ids[0])
    assert task.request.prompt == "查出最近30个交易日累计涨幅最大的个股"
    state = conversation_store.get_session_state(event.conversation_id)
    assert state.active_session_id == bot._session_id_for_event("event-combined")
    assert state.sessions[-1].name == "查出最近30个交易日累计涨幅最大的个股"
    assert task.request.conversation_id.endswith(
        f":session:{state.active_session_id}"
    )


def test_agent_bot_uses_explicit_name_for_combined_session_prompt():
    task_store = MemoryAnalysisTaskStore()

    class RecordingDispatcher:
        def __init__(self):
            self.task_ids = []

        def dispatch(self, task_id):
            self.task_ids.append(task_id)

    dispatcher = RecordingDispatcher()
    coordinator = FeishuAgentCoordinator(task_store, dispatcher)
    conversation_store = MemoryConversationStore()
    bot = FeishuResearchBot(
        coordinator,
        FakeSender(),
        conversation_store,
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        agent_coordinator=coordinator,
    )
    event = bot.parse_event(
        message_payload(
            "event-named-combined",
            "新建会话 30日涨幅研究，并查询最近30个交易日涨幅最大的股票",
        )
    )
    assert event is not None

    bot.process(event)

    task = task_store.get(dispatcher.task_ids[0])
    state = conversation_store.get_session_state(event.conversation_id)
    assert state.sessions[-1].name == "30日涨幅研究"
    assert task.request.prompt == "查询最近30个交易日涨幅最大的股票"


def test_session_creation_is_idempotent_for_retried_event():
    bot, _, _, store = build_bot()
    event = bot.parse_event(message_payload("event-session", "新建对话 银行研究"))
    assert event is not None

    first_reply = bot._session_command_reply(event)
    second_reply = bot._session_command_reply(event)

    state = store.get_session_state(event.conversation_id)
    matching_sessions = [
        session
        for session in state.sessions
        if session.session_id == bot._session_id_for_event("event-session")
    ]
    assert first_reply == second_reply
    assert len(matching_sessions) == 1


def test_backend_loads_context_only_from_the_active_session():
    task_store = MemoryAnalysisTaskStore()

    class RecordingDispatcher:
        def __init__(self):
            self.task_ids = []

        def dispatch(self, task_id):
            self.task_ids.append(task_id)

    dispatcher = RecordingDispatcher()
    coordinator = FeishuAgentCoordinator(task_store, dispatcher)
    conversation_store = MemoryConversationStore()
    bot = FeishuResearchBot(
        coordinator,
        FakeSender(),
        conversation_store,
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        agent_coordinator=coordinator,
    )
    first_event = bot.parse_event(
        message_payload("event-first", "新建会话 银行研究，查询银行股估值")
    )
    second_event = bot.parse_event(
        message_payload("event-second", "新建会话 半导体研究，查询芯片股估值")
    )
    assert first_event is not None
    assert second_event is not None

    bot.process(first_event)
    first_task = task_store.get(dispatcher.task_ids[-1])
    first_task.status = AnalysisTaskStatus.SUCCEEDED
    first_task.answer = "银行研究结果"
    task_store.put(first_task)
    bot.process(second_event)
    second_task = task_store.get(dispatcher.task_ids[-1])
    assert second_task.request.conversation == []

    switch_event = bot.parse_event(
        message_payload("event-switch", "切换会话 银行研究")
    )
    follow_up = bot.parse_event(
        message_payload("event-follow-up", "继续比较股息率")
    )
    assert switch_event is not None
    assert follow_up is not None
    bot.process(switch_event)
    bot.process(follow_up)

    follow_up_task = task_store.get(dispatcher.task_ids[-1])
    assert len(follow_up_task.request.conversation) == 1
    assert follow_up_task.request.conversation[0].prompt == "查询银行股估值"
    assert follow_up_task.request.conversation[0].answer == "银行研究结果"


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ANALYSIS") != "1",
    reason="Set RUN_LIVE_ANALYSIS=1 to call the configured model and Tushare.",
)
def test_live_combined_session_command_answers_reported_thirty_day_ranking():
    task_store = MemoryAnalysisTaskStore()

    class RecordingDispatcher:
        def __init__(self):
            self.task_ids = []

        def dispatch(self, task_id):
            self.task_ids.append(task_id)

    class RecordingProgressSink:
        def __init__(self):
            self.messages = []

        def reply(self, message_id, text):
            self.messages.append((message_id, text))
            return "progress-message"

        def update(self, message_id, text):
            self.messages.append((message_id, text))

        def reply_file(self, message_id, path):
            self.messages.append((message_id, path.name))

    dispatcher = RecordingDispatcher()
    coordinator = FeishuAgentCoordinator(task_store, dispatcher)
    conversation_store = MemoryConversationStore()
    bot = FeishuResearchBot(
        coordinator,
        FakeSender(),
        conversation_store,
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        agent_coordinator=coordinator,
    )
    event = bot.parse_event(
        message_payload(
            "live-event-combined",
            "新建对话，查出最近30个交易日累计涨幅最大的个股",
        )
    )
    assert event is not None

    bot.process(event)
    completed = coordinator.run(
        dispatcher.task_ids[0],
        bootstrap.create_feishu_agent_runtime(Settings.from_env()),
        RecordingProgressSink(),
    )

    assert completed.status == AnalysisTaskStatus.SUCCEEDED
    assert completed.request.prompt == "查出最近30个交易日累计涨幅最大的个股"
    assert "额度已用尽" not in (completed.answer or "")
    assert any(character.isdigit() for character in (completed.answer or ""))


def test_agent_bot_routes_mentioned_status_command_without_new_submission():
    task_store = MemoryAnalysisTaskStore()

    class RecordingDispatcher:
        def __init__(self):
            self.task_ids = []

        def dispatch(self, task_id):
            self.task_ids.append(task_id)

    dispatcher = RecordingDispatcher()
    agent_coordinator = FeishuAgentCoordinator(task_store, dispatcher)
    sender = FakeSender()
    bot = FeishuResearchBot(
        agent_coordinator,
        sender,
        MemoryConversationStore(),
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        agent_coordinator=agent_coordinator,
    )
    prompt_event = bot.parse_event(message_payload("event-agent", "查询银行股估值"))
    status_event = bot.parse_event(
        message_payload(
            "event-status",
            "@_user_1 查看进度",
            mentions=[{"key": "@_user_1", "name": "A股研究助手"}],
        )
    )
    assert prompt_event is not None
    assert status_event is not None

    bot.process(prompt_event)
    bot.process(status_event)

    assert len(dispatcher.task_ids) == 1
    assert len(sender.replies) == 1
    assert "状态：排队中" in sender.replies[-1][1]

def test_task_progress_reply_displays_completed_and_total_items():
    now = datetime.now(timezone.utc)
    task = AnalysisTask(
        task_id="progress-task",
        status=AnalysisTaskStatus.RUNNING,
        request=AnalysisRequest(prompt="Rank stocks."),
        created_at=now,
        updated_at=now,
        completed_items=37,
        total_items=100,
    )

    assert format_analysis_task(task) == (
        "任务 progress-task\n状态：运行中\n进度：37/100"
    )


def test_cloud_store_reclaims_abandoned_event_but_not_completed_event():
    store = CloudStorageConversationStore.__new__(CloudStorageConversationStore)
    store._bucket = FakeEventBucket()

    assert store.claim_event("event-1") is True
    assert store.claim_event("event-1") is False

    blob = store._bucket.blob(store._event_object("event-1"))
    blob.updated = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert store.claim_event("event-1") is True

    store.complete_event("event-1")
    blob.updated = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert store.claim_event("event-1") is False


def test_cloud_store_persists_task_lookup_by_task_event_and_conversation():
    store = CloudStorageConversationStore.__new__(CloudStorageConversationStore)
    store._bucket = FakeEventBucket()
    record = FeishuTaskRecord(
        task_id="a" * 32,
        source_event_id="event-1",
        conversation_id="tenant:chat:thread:user",
        prompt="Rank the top ten stocks.",
    )

    store.put_task(record)

    assert store.get_task(record.task_id) == record
    assert store.get_event_task(record.source_event_id) == record
    assert store.get_latest_task(record.conversation_id) == record


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
        self.signature_allow_missing = []

    def verify_signature(
        self,
        body,
        timestamp,
        nonce,
        signature,
        *,
        allow_missing=False,
    ):
        assert body
        if timestamp or nonce or signature:
            assert (timestamp, nonce, signature) == ("123", "nonce", "signature")
        self.signature_allow_missing.append(allow_missing)

    def decode_payload(self, body):
        return json.loads(body)

    def verify_challenge(self, payload):
        assert payload["token"] == "verification-token"
        return payload["challenge"]

    def parse_event(self, payload):
        return payload["event"]

    def parse_card_action(self, payload):
        return {"action": payload["event"]["action"]["value"]["action"]}

    def parse_feedback_card_action(self, payload):
        return None

    def parse_strategy_card_action(self, payload):
        action_name = payload["event"]["action"]["value"]["action"]
        if action_name.startswith("strategy_"):
            return {"strategy_action": action_name}
        return None

    def process_strategy_card_action(self, action):
        self.processed.append(action)

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
    assert bot.signature_allow_missing == [False]


def test_feishu_endpoint_acknowledges_card_action_with_toast():
    bot = FakeEndpointBot()
    client = TestClient(create_app(feishu_research_bot=bot))

    response = client.post(
        "/api/integrations/feishu/events",
        headers={
            "X-Lark-Request-Timestamp": "123",
            "X-Lark-Request-Nonce": "nonce",
            "X-Lark-Signature": "signature",
        },
        json=card_action_payload("list_sessions"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "toast": {"type": "success", "content": "操作已提交"}
    }
    assert bot.processed == [{"action": "list_sessions"}]
    assert bot.signature_allow_missing == [True]


def test_feishu_endpoint_accepts_token_authenticated_card_action_without_signature():
    bot = FakeEndpointBot()
    client = TestClient(create_app(feishu_research_bot=bot))

    response = client.post(
        "/api/integrations/feishu/events",
        json=card_action_payload("list_sessions"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "toast": {"type": "success", "content": "操作已提交"}
    }
    assert bot.processed == [{"action": "list_sessions"}]
    assert bot.signature_allow_missing == [True]


def test_feishu_endpoint_returns_verified_challenge():
    bot, _, _, _ = build_bot()
    client = TestClient(create_app(feishu_research_bot=bot))
    body = encrypt_payload(
        {
            "type": "url_verification",
            "token": "verification-token",
            "challenge": "challenge-value",
        }
    )

    response = client.post(
        "/api/integrations/feishu/events",
        content=body,
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-value"}


def test_feishu_endpoint_requires_signature_for_regular_events():
    bot, _, _, _ = build_bot()
    client = TestClient(create_app(feishu_research_bot=bot))

    response = client.post(
        "/api/integrations/feishu/events",
        content=encrypt_payload(message_payload()),
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Feishu signature headers are required."}


class FakeFeedbackTranscriber:
    def __init__(self, report="整理后的排查报告", error=None):
        self.report = report
        self.error = error
        self.calls = []

    def transcribe(self, description, turns):
        self.calls.append((description, [dict(row) for row in turns]))
        if self.error is not None:
            raise self.error
        return self.report


class FakeFeedbackRecordStore:
    def __init__(self):
        self.records = []

    def put(self, feedback_id, record):
        self.records.append((feedback_id, dict(record)))


def build_feedback_bot(*, store_turns=None, transcriber=None, admin_open_id="ou-admin"):
    service = FakeTaskCoordinator()
    sender = FakeSender()
    store = MemoryConversationStore()
    transcriber = transcriber or FakeFeedbackTranscriber()
    record_store = FakeFeedbackRecordStore()
    interaction = FeishuFeedbackCoordinator(
        transcriber, record_store, admin_open_id=admin_open_id
    )
    bot = FeishuResearchBot(
        service,
        sender,
        store,
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        feedback_interaction=interaction,
    )
    if store_turns is not None:
        store.put("tenant-1:chat-1:root:user-1:session:default", store_turns)
    return bot, sender, store, transcriber, record_store


def test_quick_menu_card_offers_feedback_button():
    bot, _, sender, _ = build_bot()
    event = bot.parse_event(
        message_payload("event-menu", '<at user_id="bot">Bot</at>')
    )

    bot.process(event)

    _, card = sender.cards[0]
    feedback_buttons = [
        action
        for element in card["elements"]
        if element["tag"] == "action"
        for action in element["actions"]
        if action["value"].get("action") == "report_issue"
    ]
    assert len(feedback_buttons) == 1
    assert feedback_buttons[0]["text"]["content"] == "反馈问题"


def test_report_issue_button_opens_feedback_form_bound_to_active_session():
    bot, sender, _, _, _ = build_feedback_bot()

    click_event = bot.parse_card_action(card_action_payload("report_issue"))
    assert click_event is not None
    assert click_event.prompt == "反馈问题"

    bot.process(click_event)

    assert len(sender.cards) == 1
    _, card = sender.cards[0]
    form = next(element for element in card["elements"] if element["tag"] == "form")
    assert [element["name"] for element in form["elements"]] == [
        "turns",
        "description",
        "submit_feedback",
    ]
    # The v1 card form whitelist: a select_menu tag makes Feishu drop the
    # whole form silently, so the dropdown must stay a select_static.
    assert [element["tag"] for element in form["elements"]] == [
        "select_static",
        "input",
        "button",
    ]
    select = form["elements"][0]
    assert [(option["value"], option["text"]["content"]) for option in select["options"]] == [
        ("1", "近1轮"),
        ("2", "近2轮"),
        ("3", "近3轮"),
        ("5", "近5轮"),
    ]
    description_input = form["elements"][1]
    assert description_input["max_length"] == 500
    submit = form["elements"][2]
    assert submit["action_type"] == "form_submit"
    assert (
        submit["value"]["conversation_id"]
        == "tenant-1:chat-1:root:user-1:session:default"
    )
    assert "select_menu" not in json.dumps(card)


def test_feedback_form_submission_transcribes_and_replies_result_card():
    bot, sender, _, transcriber, record_store = build_feedback_bot(
        store_turns=[
            FeishuConversationTurn(prompt="第一问", answer="第一答"),
            FeishuConversationTurn(prompt="第二问", answer="第二答"),
        ]
    )

    action = bot.parse_feedback_card_action(
        card_action_payload(
            "submit_feedback",
            event_id="feedback-event-1",
            form_value={"description": "表格列名看不懂", "turns": "2"},
            extra_value={
                "conversation_id": "tenant-1:chat-1:root:user-1:session:default"
            },
        )
    )
    assert action is not None
    assert action.submission.turns == 2
    assert action.submission.description == "表格列名看不懂"

    bot.process_feedback_card_action(action)

    description, rows = transcriber.calls[0]
    assert description == "表格列名看不懂"
    assert [row["content"] for row in rows] == ["第一问", "第一答", "第二问", "第二答"]
    feedback_id, record = record_store.records[-1]
    assert record["status"] == "transcribed"
    assert record["turns_requested"] == 2
    assert record["turns_included"] == 2
    assert record["window_truncated"] is False
    assert record["transcript"] == rows
    _, card = sender.cards[-1]
    assert card["header"]["title"]["content"] == "反馈已收到"
    content = card["elements"][0]["text"]["content"]
    assert "已收到，感谢反馈" in content
    assert "整理后的排查报告" in content
    assert "<at id=ou-admin></at>" in content


def test_feedback_reports_shortfall_when_fewer_turns_available():
    bot, sender, _, transcriber, record_store = build_feedback_bot(
        store_turns=[FeishuConversationTurn(prompt="只有一问", answer="只有一答")]
    )

    action = bot.parse_feedback_card_action(
        card_action_payload(
            "submit_feedback",
            form_value={"description": "d", "turns": "3"},
            extra_value={
                "conversation_id": "tenant-1:chat-1:root:user-1:session:default"
            },
        )
    )
    bot.process_feedback_card_action(action)

    _, rows = transcriber.calls[0]
    assert [row["content"] for row in rows] == ["只有一问", "只有一答"]
    _, record = record_store.records[-1]
    assert record["turns_included"] == 1
    _, card = sender.cards[-1]
    content = card["elements"][0]["text"]["content"]
    assert "只有 1 轮" in content


def test_feedback_transcription_failure_keeps_raw_feedback_visible():
    bot, sender, _, _, record_store = build_feedback_bot(
        transcriber=FakeFeedbackTranscriber(error=RuntimeError("upstream down")),
        store_turns=[FeishuConversationTurn(prompt="q", answer="a")],
    )

    action = bot.parse_feedback_card_action(
        card_action_payload(
            "submit_feedback",
            form_value={"description": "描述原文", "turns": "1"},
            extra_value={
                "conversation_id": "tenant-1:chat-1:root:user-1:session:default"
            },
        )
    )
    bot.process_feedback_card_action(action)

    _, card = sender.cards[-1]
    assert card["header"]["title"]["content"] == "反馈已收到（转写失败）"
    content = card["elements"][0]["text"]["content"]
    assert "描述原文" in content
    assert "<at id=ou-admin></at>" in content
    _, record = record_store.records[-1]
    assert record["status"] == "transcription_failed"
    assert "upstream down" in record["error"]
    assert record["description"] == "描述原文"


def test_feedback_submission_is_deduplicated_per_event():
    bot, _, _, transcriber, record_store = build_feedback_bot()
    payload = card_action_payload(
        "submit_feedback",
        event_id="feedback-event-dup",
        form_value={"description": "d", "turns": "1"},
        extra_value={
            "conversation_id": "tenant-1:chat-1:root:user-1:session:default"
        },
    )
    action = bot.parse_feedback_card_action(payload)

    bot.process_feedback_card_action(action)
    bot.process_feedback_card_action(action)

    assert len(transcriber.calls) == 1
    # The coordinator writes one "received" record and one final record for
    # the SAME feedback id; a duplicate callback must not mint a new id.
    assert len({feedback_id for feedback_id, _ in record_store.records}) == 1


@pytest.mark.parametrize(
    "form_value,extra_value",
    [
        ({"description": "x" * 501, "turns": "2"}, {"conversation_id": "c1"}),
        ({"description": "   ", "turns": "2"}, {"conversation_id": "c1"}),
        ({"description": "ok", "turns": "4"}, {"conversation_id": "c1"}),
        ({"description": "ok", "turns": "2"}, {}),
    ],
)
def test_feedback_form_rejects_invalid_fields(form_value, extra_value):
    bot, _, _, _, _ = build_feedback_bot()

    payload = card_action_payload(
        "submit_feedback", form_value=form_value, extra_value=extra_value
    )

    with pytest.raises(FeishuEventError):
        bot.parse_feedback_card_action(payload)


def test_feedback_command_replies_disabled_text_without_interaction_module():
    bot, _, sender, _ = build_bot()

    event = bot.parse_event(
        message_payload("event-fb", '<at user_id="bot">Bot</at> 反馈问题')
    )
    bot.process(event)

    assert sender.cards == []
    assert sender.replies[-1][1] == "反馈功能未启用，请联系管理员。"


def test_feedback_parse_returns_none_without_interaction_module():
    bot, _, _, _ = build_bot()

    payload = card_action_payload(
        "submit_feedback",
        form_value={"description": "d", "turns": "2"},
        extra_value={"conversation_id": "c"},
    )

    assert bot.parse_feedback_card_action(payload) is None


def test_feedback_turn_window_accepts_bounded_pairs():
    pair = FeedbackTurn(prompt="q", answer="a")
    assert pair.prompt == "q"


def test_feedback_transcript_normalizes_legacy_interpretation_turns():
    bot, sender, _, transcriber, _ = build_feedback_bot(
        store_turns=[
            FeishuConversationTurn(prompt="旧一问", interpretation="旧的解读"),
            FeishuConversationTurn(prompt="新一问", answer="新一答"),
        ]
    )

    action = bot.parse_feedback_card_action(
        card_action_payload(
            "submit_feedback",
            form_value={"description": "d", "turns": "2"},
            extra_value={
                "conversation_id": "tenant-1:chat-1:root:user-1:session:default"
            },
        )
    )
    bot.process_feedback_card_action(action)

    _, rows = transcriber.calls[0]
    assert [row["content"] for row in rows] == ["旧一问", "旧的解读", "新一问", "新一答"]

"""Invariants for the Feishu chat model switch on the Codex-agent runtime."""

import json

import pytest

from china_a_share import bootstrap
from china_a_share.codex_agent import CodexFeishuAgentRuntime
from china_a_share.config import Settings
from china_a_share.feishu import (
    FeishuResearchBot,
    MemoryConversationStore,
    build_feishu_quick_menu_card,
)
from china_a_share.glm_agent import GlmFeishuAgentRuntime
from china_a_share.llm_preference import (
    ALLOWED_PROVIDERS,
    CloudStorageLlmPreferenceStore,
    LlmPreferenceController,
    MemoryLlmPreferenceStore,
    glm_agent_base_url,
    glm_agent_model,
    read_llm_preference,
    resolve_active_provider,
)
from china_a_share.feishu_agent import FeishuAgentRequest


class FakeBlob:
    def __init__(self):
        self.content = None

    def exists(self):
        return self.content is not None

    def download_as_text(self):
        return self.content

    def upload_from_string(self, content, **kwargs):
        self.content = content


class FakeBucket:
    def __init__(self):
        self.blobs = {}

    def blob(self, name):
        return self.blobs.setdefault(name, FakeBlob())


class FakeStorageClient:
    def __init__(self):
        self._buckets = {}

    def bucket(self, name):
        return self._buckets.setdefault(name, FakeBucket())


class FakeSender:
    def __init__(self):
        self.replies = []
        self.cards = []

    def reply(self, message_id, text):
        self.replies.append((message_id, text))
        return "reply-1"

    def update(self, message_id, text):
        raise AssertionError("Unexpected message update")

    def reply_card(self, message_id, card):
        self.cards.append((message_id, card))
        return "card-1"


class ReplayableMemoryConversationStore(MemoryConversationStore):
    def claim_event(self, event_id):
        return True


class FakeTaskCoordinator:
    def __init__(self):
        self.requests = []

    def submit(self, request, *, task_id=None):
        raise AssertionError("The model command must not submit research tasks.")

    def get(self, task_id):
        return None


class FakeToolbox:
    def __init__(self):
        self.calls = []

    @property
    def definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_market_data",
                    "description": "Search operations.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
            }
        ]

    def call(self, name, arguments, progress):
        self.calls.append((name, arguments))
        return {"results": ["daily"]}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse(self.responses.pop(0))


def glm_capable_settings(**overrides):
    return Settings(
        tushare_token="token",
        deepseek_api_key="deepseek-key",
        zai_api_key="zai-key",
        tushare_cache_bucket="bucket",
        **overrides,
    )


def test_preference_store_round_trip_and_validation():
    store = MemoryLlmPreferenceStore()
    assert store.get() is None
    store.set("glm")
    assert store.get() == "glm"
    with pytest.raises(ValueError, match="Unknown chat-research provider"):
        store.set("claude")
    assert store.get() == "glm"


def test_cloud_preference_store_round_trip_ignores_corruption():
    client = FakeStorageClient()
    store = CloudStorageLlmPreferenceStore("bucket", storage_client=client)
    store.set("glm")
    assert store.get() == "glm"

    client.bucket("bucket").blob(
        CloudStorageLlmPreferenceStore.OBJECT_NAME
    ).upload_from_string("not-json")
    assert store.get() is None


def test_resolve_prefers_glm_only_with_zhipu_key():
    settings = glm_capable_settings()

    assert resolve_active_provider(settings, "glm") == "glm"
    assert resolve_active_provider(settings, "deepseek") == "deepseek"
    assert resolve_active_provider(settings, None) == "deepseek"
    assert resolve_active_provider(
        Settings(tushare_token="token", deepseek_api_key="k"), "glm"
    ) == "deepseek"


def test_glm_endpoint_defaults_and_overrides():
    assert glm_agent_base_url(glm_capable_settings()).endswith(
        "/api/coding/paas/v4"
    )
    assert glm_agent_model(glm_capable_settings()) == "glm-5.3"
    assert (
        glm_agent_base_url(
            glm_capable_settings(glm_agent_base_url="https://example.invalid/v4")
        )
        == "https://example.invalid/v4"
    )
    assert (
        glm_agent_model(glm_capable_settings(glm_agent_model="glm-5.3-flash"))
        == "glm-5.3-flash"
    )


def test_controller_switch_persists_and_reports_status():
    store = MemoryLlmPreferenceStore()
    controller = LlmPreferenceController(glm_capable_settings(), store)

    assert controller.status_line() == "DeepSeek（默认，Codex 研究代理）"
    reply = controller.switch("GLM")
    assert "已切换研究模型" in reply
    assert store.get() == "glm"
    assert controller.current() == "glm"
    assert "GLM" in controller.status_line()
    assert "切换模型 glm" in controller.status_reply()


def test_controller_rejects_glm_without_zhipu_key():
    store = MemoryLlmPreferenceStore()
    controller = LlmPreferenceController(
        Settings(tushare_token="token", deepseek_api_key="deepseek-key"), store
    )

    reply = controller.switch("glm")

    assert "ZAI_API_KEY" in reply
    assert store.get() is None
    assert controller.current() == "deepseek"


def test_controller_rejects_unknown_target():
    controller = LlmPreferenceController(
        glm_capable_settings(), MemoryLlmPreferenceStore()
    )

    reply = controller.switch("gpt")

    assert "未知模型" in reply


def build_model_command_bot(settings):
    sender = FakeSender()
    bot = FeishuResearchBot(
        FakeTaskCoordinator(),
        sender,
        ReplayableMemoryConversationStore(),
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        llm_switcher=LlmPreferenceController(
            settings, MemoryLlmPreferenceStore()
        ),
    )
    return bot, sender


def message_payload(event_id, text):
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
                "mentions": [],
            },
        },
    }


def card_action_payload(action):
    return {
        "header": {
            "event_id": "card-event-model",
            "event_type": "card.action.trigger",
            "tenant_key": "tenant-1",
            "token": "verification-token",
        },
        "event": {
            "operator": {"open_id": "user-1"},
            "context": {
                "open_message_id": "card-message-model",
                "open_chat_id": "chat-1",
            },
            "action": {
                "tag": "button",
                "name": action,
                "value": {"action": action},
                "form_value": {},
            },
        },
    }


def test_bot_text_command_switches_model():
    bot, sender = build_model_command_bot(glm_capable_settings())
    event = bot.parse_event(message_payload("event-1", "切换模型 glm"))

    bot.process(event)

    assert sender.replies
    assert "已切换研究模型" in sender.replies[0][1]
    assert bot._llm_switcher.current() == "glm"


def test_bot_current_model_command_reports_default():
    bot, sender = build_model_command_bot(glm_capable_settings())
    event = bot.parse_event(message_payload("event-2", "当前模型"))

    bot.process(event)

    assert "DeepSeek" in sender.replies[0][1]


def test_bot_model_command_without_switcher_explains_configuration():
    sender = FakeSender()
    bot = FeishuResearchBot(
        FakeTaskCoordinator(),
        sender,
        ReplayableMemoryConversationStore(),
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        llm_switcher=None,
    )
    event = bot.parse_event(message_payload("event-3", "切换模型 glm"))

    bot.process(event)

    assert "不可用" in sender.replies[0][1]


def test_quick_menu_card_lists_model_buttons_and_status():
    card = build_feishu_quick_menu_card(model_status="DeepSeek（默认，Codex 研究代理）")

    actions = [
        element
        for element in card["elements"]
        if element.get("tag") == "action"
    ]
    model_buttons = [
        button["value"]["action"]
        for element in actions
        for button in element.get("actions", [])
        if str(button["value"].get("action", "")).startswith("switch_model")
    ]
    assert model_buttons == ["switch_model_glm", "switch_model_deepseek"]
    status_text = next(
        element["text"]["content"]
        for element in card["elements"]
        if element.get("tag") == "div"
    )
    assert "DeepSeek" in status_text


def test_quick_menu_card_omits_status_without_switcher():
    card = build_feishu_quick_menu_card()

    assert all(
        "**研究模型**" not in str(element) for element in card["elements"]
    )


@pytest.mark.parametrize(
    "action, expected_prompt",
    [
        ("switch_model_glm", "切换模型 glm"),
        ("switch_model_deepseek", "切换模型 deepseek"),
    ],
)
def test_card_model_button_becomes_model_command(action, expected_prompt):
    bot, _ = build_model_command_bot(glm_capable_settings())

    event = bot.parse_card_action(card_action_payload(action))

    assert event is not None
    assert event.prompt == expected_prompt


def test_bootstrap_selects_runtime_by_preference():
    settings = glm_capable_settings(
        llm_base_url="https://api.deepseek.com",
        llm_model="deepseek-flash",
        llm_api_key="deepseek-key",
        research_sandbox_url="https://sandbox.example.invalid",
    )

    glm_runtime = bootstrap.create_feishu_agent_runtime(
        settings, llm_preference="glm"
    )
    codex_runtime = bootstrap.create_feishu_agent_runtime(settings)

    assert isinstance(glm_runtime, GlmFeishuAgentRuntime)
    assert isinstance(codex_runtime, CodexFeishuAgentRuntime)


def test_bootstrap_glm_runtime_requires_sandbox_url():
    settings = glm_capable_settings(
        llm_base_url="https://api.deepseek.com",
        llm_model="deepseek-flash",
        llm_api_key="deepseek-key",
        research_sandbox_url="",
    )

    with pytest.raises(Exception, match="RESEARCH_SANDBOX_URL"):
        bootstrap.create_feishu_agent_runtime(settings, llm_preference="glm")


def test_glm_runtime_runs_bounded_tool_loop():
    toolbox = FakeToolbox()
    session = FakeSession(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_market_data",
                                        "arguments": '{"query": "daily"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "研究完成：收盘价 11.70。",
                        }
                    }
                ]
            },
        ]
    )
    runtime = GlmFeishuAgentRuntime(
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        model="glm-5.3",
        api_key="zai-key",
        toolbox_factory=lambda artifact_dir, conversation_id: toolbox,
        session=session,
    )

    outcome = runtime.run(
        FeishuAgentRequest(
            prompt="平安银行最新收盘价",
            conversation_id="tenant:chat:root:user",
            source_message_id="message-1",
        ),
        lambda stage, message: None,
    )

    assert outcome.answer == "研究完成：收盘价 11.70。"
    assert toolbox.calls == [("search_market_data", {"query": "daily"})]
    url, kwargs = session.calls[0]
    assert url[0].endswith("/chat/completions")
    assert kwargs["headers"]["Authorization"] == "Bearer zai-key"
    assert kwargs["json"]["model"] == "glm-5.3"
    assert kwargs["json"]["tools"] == toolbox.definitions
    tool_message = next(
        message
        for message in session.calls[1][1]["json"]["messages"]
        if message.get("role") == "tool"
    )
    assert json.loads(tool_message["content"]) == {"results": ["daily"]}


def test_read_llm_preference_returns_none_without_bucket():
    assert read_llm_preference(Settings(tushare_token="t")) is None


def test_allowed_providers_are_deepseek_and_glm():
    assert ALLOWED_PROVIDERS == ("deepseek", "glm")

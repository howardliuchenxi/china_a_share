"""Invariants for the chat-driven planning-provider preference switch."""

import pytest

from china_a_share import bootstrap
from china_a_share.config import Settings
from china_a_share.feishu import (
    FeishuMessageEvent,
    FeishuResearchBot,
    MemoryConversationStore,
)
from china_a_share.llm_preference import (
    ALLOWED_PROVIDERS,
    CloudStorageLlmPreferenceStore,
    LlmPreferenceController,
    MemoryLlmPreferenceStore,
    read_llm_preference,
    resolve_active_provider,
)
from china_a_share.planners.glm import GLM_CODING_API_URL, GLM_MODEL
from china_a_share.planners.deepseek import DEEPSEEK_API_URL, DEEPSEEK_MODEL


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

    def reply(self, message_id, text):
        self.replies.append((message_id, text))


class FakeTaskCoordinator:
    def submit(self, request, *, task_id=None):
        raise AssertionError("The model command must not submit research tasks.")

    def get(self, task_id):
        return None


def glm_capable_settings(**overrides):
    return Settings(
        tushare_token="token",
        deepseek_api_key="deepseek-key",
        zai_api_key="zai-key",
        **overrides,
    )


def memory_store_round_trip(store):
    assert store.get() is None
    store.set("glm")
    assert store.get() == "glm"
    store.set("deepseek")
    assert store.get() == "deepseek"


def test_memory_preference_store_round_trip():
    memory_store_round_trip(MemoryLlmPreferenceStore())


def test_memory_preference_store_rejects_unknown_provider():
    store = MemoryLlmPreferenceStore()
    with pytest.raises(ValueError, match="Unknown planning provider"):
        store.set("claude")
    assert store.get() is None


def test_cloud_preference_store_round_trip_through_json():
    client = FakeStorageClient()
    store = CloudStorageLlmPreferenceStore("bucket", storage_client=client)

    memory_store_round_trip(store)
    stored = client.bucket("bucket").blobs[
        CloudStorageLlmPreferenceStore.OBJECT_NAME
    ].download_as_text()
    assert '"provider": "deepseek"' in stored


def test_cloud_preference_store_ignores_corrupted_object():
    client = FakeStorageClient()
    blob = client.bucket("bucket").blob(CloudStorageLlmPreferenceStore.OBJECT_NAME)
    blob.upload_from_string("not-json")
    store = CloudStorageLlmPreferenceStore("bucket", storage_client=client)

    assert store.get() is None


def test_cloud_preference_store_ignores_unknown_provider_value():
    client = FakeStorageClient()
    blob = client.bucket("bucket").blob(CloudStorageLlmPreferenceStore.OBJECT_NAME)
    blob.upload_from_string('{"provider": "claude"}')
    store = CloudStorageLlmPreferenceStore("bucket", storage_client=client)

    assert store.get() is None


def test_resolve_prefers_persisted_choice_when_key_available():
    settings = glm_capable_settings()

    assert resolve_active_provider(settings, "glm") == "glm"
    assert resolve_active_provider(settings, "deepseek") == "deepseek"
    assert resolve_active_provider(settings, None) == "deepseek"


def test_resolve_falls_back_when_required_key_is_missing():
    glm_only = Settings(
        tushare_token="token",
        zai_api_key="zai-key",
        llm_provider="glm",
    )
    deepseek_only = Settings(
        tushare_token="token",
        deepseek_api_key="deepseek-key",
    )

    assert resolve_active_provider(glm_only, "deepseek") == "glm"
    assert resolve_active_provider(deepseek_only, "glm") == "deepseek"


def test_controller_switch_persists_and_confirms():
    store = MemoryLlmPreferenceStore()
    controller = LlmPreferenceController(glm_capable_settings(), store)

    reply = controller.switch("GLM")

    assert "已切换" in reply
    assert "GLM" in reply
    assert store.get() == "glm"
    assert controller.current() == "glm"


def test_controller_rejects_switch_without_required_key():
    store = MemoryLlmPreferenceStore()
    settings = Settings(tushare_token="token", deepseek_api_key="deepseek-key")
    controller = LlmPreferenceController(settings, store)

    reply = controller.switch("glm")

    assert "ZAI_API_KEY" in reply
    assert store.get() is None
    assert controller.current() == "deepseek"


def test_controller_rejects_unknown_target_with_usage():
    controller = LlmPreferenceController(
        glm_capable_settings(), MemoryLlmPreferenceStore()
    )

    reply = controller.switch("gpt")

    assert "未知模型" in reply
    assert "切换模型 glm" in reply


def build_model_command_bot(settings):
    sender = FakeSender()
    bot = FeishuResearchBot(
        FakeTaskCoordinator(),
        sender,
        MemoryConversationStore(),
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        allowed_open_ids={"user-1"},
        llm_switcher=LlmPreferenceController(settings, MemoryLlmPreferenceStore()),
    )
    return bot, sender


def model_event(prompt):
    return FeishuMessageEvent(
        event_id="event-model-1",
        message_id="message-1",
        conversation_id="tenant:chat:root:user-1",
        prompt=prompt,
        mentions_bot=True,
    )


def test_bot_switch_model_command_answers_and_persists():
    bot, sender = build_model_command_bot(glm_capable_settings())

    bot.process(model_event("切换模型 glm"))

    assert sender.replies, "the model command must reply"
    reply = sender.replies[0][1]
    assert "已切换" in reply
    assert bot._llm_switcher.current() == "glm"


def test_bot_current_model_command_reports_active_provider():
    bot, sender = build_model_command_bot(glm_capable_settings())

    bot.process(model_event("当前模型"))

    assert "当前模型" in sender.replies[0][1]
    assert "DeepSeek" in sender.replies[0][1]
    assert bot._llm_switcher.current() == "deepseek"


def test_bot_switch_model_without_switcher_explains_configuration():
    sender = FakeSender()
    bot = FeishuResearchBot(
        FakeTaskCoordinator(),
        sender,
        MemoryConversationStore(),
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        llm_switcher=None,
    )

    bot.process(model_event("切换模型 glm"))

    assert "不可用" in sender.replies[0][1]


def test_bot_model_command_does_not_swallow_research_prompts():
    bot, sender = build_model_command_bot(glm_capable_settings())
    coordinator = RecordingTaskCoordinator()
    bot._task_coordinator = coordinator

    bot.process(model_event("模型轮动策略最近表现怎么样"))

    assert coordinator.requests, (
        "a research prompt that merely starts with 模型 must still be submitted"
    )


class RecordingTaskCoordinator(FakeTaskCoordinator):
    def __init__(self):
        self.requests = []

    def submit(self, request, *, task_id=None):
        from china_a_share.core.contracts import (
            AnalysisTaskStatus,
            AnalysisTaskSubmission,
        )

        self.requests.append(request)
        return AnalysisTaskSubmission(
            task_id="0" * 32,
            status=AnalysisTaskStatus.QUEUED,
            status_url="/api/analysis/tasks/0",
        )


def test_agent_runtime_honors_persisted_preference(monkeypatch):
    monkeypatch.setattr(
        bootstrap,
        "_create_feishu_data_provider",
        lambda settings: object(),
    )
    settings = glm_capable_settings()

    glm_runtime = bootstrap.create_feishu_agent_runtime(
        settings, llm_preference="glm"
    )
    default_runtime = bootstrap.create_feishu_agent_runtime(settings)

    assert glm_runtime._api_url == GLM_CODING_API_URL
    assert glm_runtime._model == GLM_MODEL
    assert default_runtime._api_url == DEEPSEEK_API_URL
    assert default_runtime._model == DEEPSEEK_MODEL


def test_read_llm_preference_returns_none_without_bucket():
    settings = Settings(tushare_token="token", deepseek_api_key="deepseek-key")

    assert read_llm_preference(settings) is None


def test_allowed_providers_are_exactly_the_two_chat_providers():
    assert ALLOWED_PROVIDERS == ("deepseek", "glm")

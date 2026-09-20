"""Invariants for the DeepSeek/GLM planning-provider switch."""

import json

import pytest

from china_a_share.config import ConfigurationError, Settings
from china_a_share.bootstrap import _glm_chat_config
from china_a_share.core.contracts import (
    AnalysisRequest,
    DataQuery,
    DataOperation,
    QueryPlan,
    UiFeedbackChatRequest,
)
from china_a_share.feedback import DeepSeekUiFeedbackAssistant
from china_a_share.feishu_agent import (
    MAX_AGENT_ROUNDS,
    FeishuAgentRequest,
    FeishuAgentRuntime,
)
from china_a_share.planners.deepseek import (
    DEEPSEEK_API_URL,
    DEEPSEEK_FALLBACK_MODEL,
    DEEPSEEK_MAX_ATTEMPTS,
    DEEPSEEK_MODEL,
    DeepSeekQueryPlanner,
)
from china_a_share.planners.glm import (
    GLM_CODING_API_URL,
    GLM_FALLBACK_MODEL,
    GLM_MODEL,
    GlmQueryPlanner,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None):
        self.response = response
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class FakeAgentProvider:
    name = "test-provider"

    def search_operations(self, prompt):
        return []

    def supports(self, operation):
        return False

    def query(self, operation, params, fields, *, api_route, request_id, query_id):
        raise AssertionError("No provider query is expected.")


class StubSourceSearch:
    def search(self, request):
        return ""


def make_daily_plan():
    return QueryPlan(
        interpretation="Count daily market direction.",
        requirements=[
            {
                "requirement": "Count advancing and declining securities.",
                "status": "covered",
                "implementation": "Aggregate the daily change field locally.",
                "evidence": "The daily operation documents the change field.",
            }
        ],
        queries=[
            DataQuery(
                query_id="market_direction",
                operation="daily",
                params={"trade_date": "20260717"},
                fields=["ts_code", "change"],
                purpose="Retrieve full-market daily changes.",
                aggregations=[
                    {
                        "label": "Advanced",
                        "field": "change",
                        "operator": "gt",
                        "value": 0,
                    },
                    {
                        "label": "Declined",
                        "field": "change",
                        "operator": "lt",
                        "value": 0,
                    },
                ],
            )
        ],
    )


def make_settings(monkeypatch, tmp_path, **env):
    """Build Settings from an isolated environment and an absent env file."""
    for name in (
        "TUSHARE_TOKEN",
        "DEEPSEEK_API_KEY",
        "ZAI_API_KEY",
        "LLM_PROVIDER",
        "GLM_API_URL",
        "GLM_MODEL",
        "GLM_FALLBACK_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings.from_env(env_file=tmp_path / "absent.env")


def test_default_provider_still_requires_deepseek_key(monkeypatch, tmp_path):
    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"):
        make_settings(monkeypatch, tmp_path, TUSHARE_TOKEN="tushare-token")


def test_unknown_provider_is_rejected(monkeypatch, tmp_path):
    with pytest.raises(ConfigurationError, match="LLM_PROVIDER"):
        make_settings(
            monkeypatch,
            tmp_path,
            TUSHARE_TOKEN="tushare-token",
            DEEPSEEK_API_KEY="deepseek-key",
            LLM_PROVIDER="anthropic",
        )


def test_glm_provider_requires_zai_key(monkeypatch, tmp_path):
    with pytest.raises(ConfigurationError, match="ZAI_API_KEY"):
        make_settings(
            monkeypatch,
            tmp_path,
            TUSHARE_TOKEN="tushare-token",
            LLM_PROVIDER="glm",
        )


def test_glm_provider_allows_missing_deepseek_key(monkeypatch, tmp_path):
    settings = make_settings(
        monkeypatch,
        tmp_path,
        TUSHARE_TOKEN="tushare-token",
        LLM_PROVIDER="glm",
        ZAI_API_KEY="zai-key",
    )

    assert settings.llm_provider == "glm"
    assert settings.deepseek_api_key == ""
    assert settings.glm_api_url == ""
    assert settings.glm_model == ""


def test_glm_overrides_flow_into_settings(monkeypatch, tmp_path):
    settings = make_settings(
        monkeypatch,
        tmp_path,
        TUSHARE_TOKEN="tushare-token",
        LLM_PROVIDER="glm",
        ZAI_API_KEY="zai-key",
        GLM_API_URL="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        GLM_MODEL="glm-5.3",
        GLM_FALLBACK_MODEL="glm-5.3-flash",
    )

    assert settings.glm_api_url.endswith("/chat/completions")
    assert settings.glm_model == "glm-5.3"
    assert settings.glm_fallback_model == "glm-5.3-flash"


def test_glm_chat_config_prefers_explicit_overrides():
    overridden = _glm_chat_config(
        Settings(
            tushare_token="t",
            zai_api_key="zai-key",
            llm_provider="glm",
            glm_api_url="https://example.invalid/chat/completions",
            glm_model="custom-model",
            glm_fallback_model="custom-fallback",
        )
    )
    assert overridden == {
        "api_key": "zai-key",
        "api_url": "https://example.invalid/chat/completions",
        "model": "custom-model",
        "fallback_model": "custom-fallback",
    }

    default = _glm_chat_config(Settings(tushare_token="t", zai_api_key="zai-key"))
    assert default["api_url"] == GLM_CODING_API_URL
    assert default["model"] == GLM_MODEL
    assert default["fallback_model"] == GLM_FALLBACK_MODEL


def test_deepseek_planner_defaults_are_unchanged():
    session = FakeSession(
        FakeResponse(
            {"choices": [{"message": {"content": make_daily_plan().model_dump_json()}}]}
        )
    )

    planner = DeepSeekQueryPlanner("deepseek-key", session=session)
    plan = planner.plan(
        AnalysisRequest(prompt="Count stocks."),
        [DataOperation(name="daily", description="Daily prices.")],
    )

    assert planner.name == "deepseek"
    assert plan.queries[0].operation == "daily"
    url, kwargs = session.calls[0]
    assert url[0] == DEEPSEEK_API_URL
    assert kwargs["headers"]["Authorization"] == "Bearer deepseek-key"
    assert kwargs["json"]["model"] == DEEPSEEK_MODEL
    assert kwargs["json"]["thinking"] == {"type": "disabled"}


def test_glm_planner_targets_the_coding_plan_endpoint():
    session = FakeSession(
        FakeResponse(
            {"choices": [{"message": {"content": make_daily_plan().model_dump_json()}}]}
        )
    )

    planner = GlmQueryPlanner("zai-key", session=session)
    plan = planner.plan(
        AnalysisRequest(prompt="统计今日上涨家数。"),
        [DataOperation(name="daily", description="Daily prices.")],
    )

    assert planner.name == "glm"
    assert plan.queries[0].operation == "daily"
    url, kwargs = session.calls[0]
    assert url[0] == GLM_CODING_API_URL
    assert kwargs["headers"]["Authorization"] == "Bearer zai-key"
    assert kwargs["json"]["model"] == GLM_MODEL
    assert kwargs["json"]["thinking"] == {"type": "disabled"}
    assert kwargs["json"]["response_format"] == {"type": "json_object"}
    assert kwargs["json"]["temperature"] == 0


def test_planner_model_escalation_is_provider_scoped():
    deepseek = DeepSeekQueryPlanner("deepseek-key")
    assert deepseek._model_for_attempt(0) == DEEPSEEK_MODEL
    assert deepseek._model_for_attempt(DEEPSEEK_MAX_ATTEMPTS - 3) == DEEPSEEK_MODEL
    assert (
        deepseek._model_for_attempt(DEEPSEEK_MAX_ATTEMPTS - 2)
        == DEEPSEEK_FALLBACK_MODEL
    )

    glm = GlmQueryPlanner("zai-key")
    assert glm._model_for_attempt(0) == GLM_MODEL
    assert glm._model_for_attempt(DEEPSEEK_MAX_ATTEMPTS - 3) == GLM_MODEL
    assert glm._model_for_attempt(DEEPSEEK_MAX_ATTEMPTS - 2) == GLM_FALLBACK_MODEL


def test_agent_runtime_uses_the_glm_endpoint_and_models():
    session = FakeSession(
        FakeResponse(
            {"choices": [{"message": {"content": "查询完成。"}}]}
        )
    )

    runtime = FeishuAgentRuntime(
        "zai-key",
        FakeAgentProvider(),
        session=session,
        api_url=GLM_CODING_API_URL,
        model=GLM_MODEL,
        fallback_model=GLM_FALLBACK_MODEL,
        label="GLM",
    )
    outcome = runtime.run(
        FeishuAgentRequest(
            prompt="今日上涨家数",
            conversation_id="tenant:chat:root:user",
            source_message_id="message-1",
        ),
        lambda stage, message: None,
    )

    assert outcome.answer == "查询完成。"
    assert runtime._model_for_round(0) == GLM_MODEL
    assert runtime._model_for_round(MAX_AGENT_ROUNDS - 2) == GLM_FALLBACK_MODEL
    url, kwargs = session.calls[0]
    assert url[0] == GLM_CODING_API_URL
    assert kwargs["headers"]["Authorization"] == "Bearer zai-key"
    assert kwargs["json"]["model"] == GLM_MODEL
    assert kwargs["json"]["tool_choice"] == "auto"


def test_ui_feedback_assistant_uses_the_glm_endpoint_and_model():
    session = FakeSession(
        FakeResponse({"choices": [{"message": {"content": "explanation"}}]})
    )

    assistant = DeepSeekUiFeedbackAssistant(
        "zai-key",
        session=session,
        source_search=StubSourceSearch(),
        api_url=GLM_CODING_API_URL,
        model=GLM_MODEL,
    )
    reply = assistant.reply(
        UiFeedbackChatRequest(
            page_path="/",
            feedback_id="feedback-1",
            selected_text="selected region",
            conversation=[
                {"role": "user", "content": "Why does this region look stale?"}
            ],
        )
    )

    assert reply == "explanation"
    url, kwargs = session.calls[0]
    assert url[0] == GLM_CODING_API_URL
    assert kwargs["headers"]["Authorization"] == "Bearer zai-key"
    assert kwargs["json"]["model"] == GLM_MODEL

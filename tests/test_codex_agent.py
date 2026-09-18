from pathlib import Path
from types import SimpleNamespace

from china_a_share.codex_agent import CodexFeishuAgentRuntime
from china_a_share.feishu_agent import (
    FeishuAgentConversationTurn,
    FeishuAgentRequest,
)


class FakeCodexConfig:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.instances.append(self)


class FakeSandbox:
    workspace_write = "workspace-write"


class FakeApprovalMode:
    deny_all = "deny-all"


class FakeCodex:
    instances = []

    def __init__(self, config):
        self.config = config
        self.thread_kwargs = None
        self.prompt = None
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return None

    def thread_start(self, **kwargs):
        self.thread_kwargs = kwargs
        parent = self

        class FakeThread:
            def turn(self, prompt):
                parent.prompt = prompt

                class FakeTurn:
                    def stream(self):
                        artifact_dir = Path(
                            parent.config.kwargs["env"][
                                "CODEX_AGENT_ARTIFACT_DIR"
                            ]
                        )
                        (artifact_dir / "result.xlsx").write_bytes(b"xlsx")
                        yield SimpleNamespace(
                            method="item/started",
                            payload={
                                "item": {
                                    "type": "mcpToolCall",
                                    "tool": "query_market_data",
                                }
                            },
                        )
                        yield SimpleNamespace(
                            method="item/completed",
                            payload={
                                "item": {
                                    "type": "mcpToolCall",
                                    "tool": "query_market_data",
                                    "status": "completed",
                                    "durationMs": 12,
                                }
                            },
                        )
                        yield SimpleNamespace(
                            method="item/completed",
                            payload={
                                "item": {
                                    "type": "agentMessage",
                                    "phase": "final_answer",
                                    "text": "通用分析已完成。",
                                }
                            },
                        )
                        yield SimpleNamespace(
                            method="turn/completed",
                            payload={
                                "turn": {
                                    "status": "completed",
                                    "durationMs": 1234,
                                }
                            },
                        )

                    def interrupt(self):
                        raise AssertionError("Successful turns must not be interrupted.")

                return FakeTurn()

        return FakeThread()


def test_codex_runtime_preserves_context_uses_generic_mcp_and_persists_artifact():
    FakeCodex.instances.clear()
    FakeCodexConfig.instances.clear()
    runtime = CodexFeishuAgentRuntime(
        base_url="https://model.example/v1",
        model="deepseek-v4-pro",
        api_key="model-secret",
        tushare_token="data-secret",
        cache_bucket="cache-bucket",
        sandbox_url="https://sandbox.example",
        google_cloud_project="project-id",
        sdk_loader=lambda: (
            FakeCodex,
            FakeCodexConfig,
            FakeSandbox,
            FakeApprovalMode,
        ),
    )
    progress = []

    outcome = runtime.run(
        FeishuAgentRequest(
            prompt="Rank every row in the latest complete dataset.",
            conversation_id="tenant:chat:session:user",
            source_message_id="message-1",
            conversation=[
                FeishuAgentConversationTurn(
                    prompt="Load the dataset.",
                    answer="The dataset is ready.",
                )
            ],
        ),
        lambda stage, message: progress.append((stage, message)),
    )

    codex = FakeCodex.instances[0]
    config = FakeCodexConfig.instances[0].kwargs
    overrides = set(config["config_overrides"])
    assert outcome.answer == "通用分析已完成。"
    assert outcome.artifact_path is not None
    assert outcome.artifact_path.read_bytes() == b"xlsx"
    assert "Load the dataset." in codex.prompt
    assert "Rank every row in the latest complete dataset." in codex.prompt
    assert codex.thread_kwargs["ephemeral"] is True
    assert codex.thread_kwargs["sandbox"] == "workspace-write"
    assert config["env"]["LLM_API_KEY"] == "model-secret"
    assert config["env"]["TUSHARE_TOKEN"] == "data-secret"
    assert 'model_provider="deepseek"' in overrides
    assert 'model_providers.deepseek.wire_api="responses"' in overrides
    assert "shell_environment_policy.ignore_default_excludes=false" in overrides
    assert (
        'mcp_servers.market_data.default_tools_approval_mode="approve"'
        in overrides
    )
    mcp_env = next(
        value
        for value in overrides
        if value.startswith("mcp_servers.market_data.env_vars=")
    )
    assert "TUSHARE_TOKEN" in mcp_env
    assert "LLM_API_KEY" not in mcp_env
    assert progress == [
        ("researching", "Codex 正在调用通用工具并处理完整数据集…"),
        ("tool", "Codex 正在读取完整数据集…"),
    ]


def test_codex_runtime_turns_empty_final_response_into_recoverable_follow_up():
    class EmptyCodex(FakeCodex):
        def thread_start(self, **kwargs):
            self.thread_kwargs = kwargs

            class EmptyThread:
                def turn(self, _prompt):
                    class EmptyTurn:
                        def stream(self):
                            yield SimpleNamespace(
                                method="turn/completed",
                                payload={
                                    "turn": {
                                        "status": "completed",
                                        "durationMs": 1,
                                    }
                                },
                            )

                        def interrupt(self):
                            raise AssertionError(
                                "Successful turns must not be interrupted."
                            )

                    return EmptyTurn()

            return EmptyThread()

    runtime = CodexFeishuAgentRuntime(
        base_url="https://model.example/v1",
        model="deepseek-v4-pro",
        api_key="model-secret",
        tushare_token="data-secret",
        cache_bucket="cache-bucket",
        sandbox_url="https://sandbox.example",
        sdk_loader=lambda: (
            EmptyCodex,
            FakeCodexConfig,
            FakeSandbox,
            FakeApprovalMode,
        ),
    )

    outcome = runtime.run(
        FeishuAgentRequest(
            prompt="Analyze the dataset.",
            conversation_id="conversation",
            source_message_id="message",
        ),
        lambda _stage, _message: None,
    )

    assert "没有生成可验证的回答" in outcome.answer
    assert "1. 按原问题重试（推荐）" in outcome.answer
    assert "请回复序号" in outcome.answer


def test_codex_runtime_accepts_latest_agent_message_without_phase():
    class UnknownPhaseCodex(FakeCodex):
        def thread_start(self, **kwargs):
            self.thread_kwargs = kwargs

            class UnknownPhaseThread:
                def turn(self, _prompt):
                    class UnknownPhaseTurn:
                        def stream(self):
                            yield SimpleNamespace(
                                method="item/completed",
                                payload={
                                    "item": {
                                        "type": "agentMessage",
                                        "text": "DeepSeek 返回的有效最终回答。",
                                    }
                                },
                            )
                            yield SimpleNamespace(
                                method="turn/completed",
                                payload={
                                    "turn": {
                                        "status": "completed",
                                        "durationMs": 2,
                                    }
                                },
                            )

                        def interrupt(self):
                            raise AssertionError(
                                "Successful turns must not be interrupted."
                            )

                    return UnknownPhaseTurn()

            return UnknownPhaseThread()

    runtime = CodexFeishuAgentRuntime(
        base_url="https://model.example/v1",
        model="deepseek-v4-pro",
        api_key="model-secret",
        tushare_token="data-secret",
        cache_bucket="cache-bucket",
        sandbox_url="https://sandbox.example",
        sdk_loader=lambda: (
            UnknownPhaseCodex,
            FakeCodexConfig,
            FakeSandbox,
            FakeApprovalMode,
        ),
    )

    outcome = runtime.run(
        FeishuAgentRequest(
            prompt="Analyze the dataset.",
            conversation_id="conversation",
            source_message_id="message",
        ),
        lambda _stage, _message: None,
    )

    assert outcome.answer == "DeepSeek 返回的有效最终回答。"

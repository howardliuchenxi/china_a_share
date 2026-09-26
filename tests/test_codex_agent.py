from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from types import SimpleNamespace

import pytest

from china_a_share.codex_agent import (
    CODEX_INTERRUPT_GRACE_SECONDS,
    CODEX_TURN_TIMEOUT_SECONDS,
    CodexFeishuAgentRuntime,
    _build_research_visualization,
    _developer_instructions,
    _normalize_token_usage,
    _run_turn_with_progress,
    _safe_artifact_filename_stem,
)
from china_a_share.core.contracts import QueryResult, QueryStatus
from china_a_share.feishu_agent import (
    FeishuAgentConversationTurn,
    FeishuAgentRequest,
    build_research_workbook,
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


def test_developer_instructions_bind_staged_model_conditions_to_their_own_dates():
    """Reported regression: level-judgment queries re-tested earlier stages'
    entry conditions against the query date and silently executed a stage
    condition that contradicts its own trigger day, instead of anchoring
    backward and surfacing the contradiction."""
    instructions = _developer_instructions()

    assert "date-bound evaluation" in instructions
    assert "own occurrence date" in instructions
    assert "anchor backward" in instructions
    assert "most recent transition on or before that date" in instructions
    assert "each date's own as-of data" in instructions
    assert "same-day contradictions" in instructions
    assert "cannot hold on its own trigger day by construction" in instructions
    assert "numbered interpretation choices" in instructions
    assert "permanently empty" in instructions
    assert "day by day in the Python sandbox" in instructions
    # The discipline stays generic: no market-domain vocabulary creeps in.
    for domain_token in ("市盈率", "均线", "下穿", "MA5", "PE"):
        assert domain_token not in instructions


def test_codex_turn_timeout_is_six_hours_and_interrupts_the_turn(monkeypatch):
    observed_timeouts = []
    shutdown_calls = []

    class TimeoutFuture:
        def result(self, timeout):
            observed_timeouts.append(timeout)
            if timeout == CODEX_TURN_TIMEOUT_SECONDS:
                raise FutureTimeoutError
            return None

    class TimeoutExecutor:
        def __init__(self, **_kwargs):
            pass

        def submit(self, _function, *_args):
            return TimeoutFuture()

        def shutdown(self, **kwargs):
            shutdown_calls.append(kwargs)

    class InterruptibleTurn:
        interrupted = False

        def interrupt(self):
            self.interrupted = True

    monkeypatch.setattr(
        "china_a_share.codex_agent.ThreadPoolExecutor",
        TimeoutExecutor,
    )
    turn = InterruptibleTurn()

    with pytest.raises(RuntimeError, match="Codex turn exceeded 6 hours"):
        _run_turn_with_progress(turn, lambda _stage, _message: None)

    assert CODEX_TURN_TIMEOUT_SECONDS == 6 * 60 * 60
    assert observed_timeouts == [
        CODEX_TURN_TIMEOUT_SECONDS,
        CODEX_INTERRUPT_GRACE_SECONDS,
    ]
    assert turn.interrupted is True
    assert shutdown_calls == [{"wait": False, "cancel_futures": True}]


def test_artifact_filename_preserves_safe_session_name_and_replaces_separators():
    assert _safe_artifact_filename_stem(" 银行/低估值:筛选? ") == (
        "银行_低估值_筛选_"
    )


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
                            method="thread/tokenUsage/updated",
                            payload={
                                "threadId": "thread-1",
                                "turnId": "turn-1",
                                "tokenUsage": {
                                    "total": {
                                        "inputTokens": 1_200,
                                        "cachedInputTokens": 900,
                                        "outputTokens": 240,
                                        "reasoningOutputTokens": 80,
                                        "totalTokens": 1_440,
                                    },
                                    "last": {
                                        "inputTokens": 400,
                                        "cachedInputTokens": 300,
                                        "outputTokens": 80,
                                        "reasoningOutputTokens": 20,
                                        "totalTokens": 480,
                                    },
                                },
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


def test_codex_runtime_preserves_context_uses_generic_mcp_and_persists_artifact(
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("ANALYSIS_TASK_ID", "agent-task")
    caplog.set_level("INFO")
    FakeCodex.instances.clear()
    FakeCodexConfig.instances.clear()
    runtime = CodexFeishuAgentRuntime(
        base_url="https://model.example/v1",
        model="deepseek-v4-pro",
        api_key="model-secret",
        tushare_token="data-secret",
        massive_api_key="massive-secret",
        finnhub_api_key="finnhub-secret",
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
            conversation_name="银行低估值研究",
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
    assert outcome.artifact_path.name == "银行低估值研究.xlsx"
    assert outcome.artifact_path.read_bytes() == b"xlsx"
    assert "Load the dataset." in codex.prompt
    assert "Rank every row in the latest complete dataset." in codex.prompt
    assert '"conversation_id"' not in codex.prompt
    assert codex.thread_kwargs["ephemeral"] is True
    assert codex.thread_kwargs["sandbox"] == "workspace-write"
    assert config["env"]["LLM_API_KEY"] == "model-secret"
    assert config["env"]["TUSHARE_TOKEN"] == "data-secret"
    assert config["env"]["MASSIVE_API_KEY"] == "massive-secret"
    assert config["env"]["FINNHUB_API_KEY"] == "finnhub-secret"
    assert config["env"]["ANALYSIS_TASK_ID"] == "agent-task"
    assert config["env"]["CODEX_AGENT_CONVERSATION_ID"] == (
        "tenant:chat:session:user"
    )
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
    assert "MASSIVE_API_KEY" in mcp_env
    assert "FINNHUB_API_KEY" in mcp_env
    assert "ANALYSIS_TASK_ID" in mcp_env
    assert "CODEX_AGENT_CONVERSATION_ID" in mcp_env
    assert "LLM_API_KEY" not in mcp_env
    assert progress == [
        ("researching", "Codex 正在调用通用工具并处理完整数据集…"),
        ("tool", "Codex 正在读取完整数据集…"),
    ]
    usage_record = next(
        record.message
        for record in caplog.records
        if record.message.startswith("codex_feishu_turn_usage")
    )
    assert "workload=feishu_user" in usage_record
    assert "input_tokens=1200" in usage_record
    assert "cached_input_tokens=900" in usage_record
    assert "reasoning_output_tokens=80" in usage_record


def test_token_usage_rejects_partial_or_non_numeric_snapshots():
    assert _normalize_token_usage({"inputTokens": 10}) is None
    assert (
        _normalize_token_usage(
            {
                "inputTokens": 10,
                "cachedInputTokens": 4,
                "outputTokens": 3,
                "reasoningOutputTokens": 1,
                "totalTokens": 13,
            }
        )
        == {
            "input_tokens": 10,
            "cached_input_tokens": 4,
            "output_tokens": 3,
            "reasoning_output_tokens": 1,
            "total_tokens": 13,
        }
    )


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


def test_workbook_is_converted_to_bounded_interactive_dataset(tmp_path):
    workbook_path = build_research_workbook(
        QueryResult(
            query_id="ranking",
            provider="tushare",
            operation="daily_basic",
            status=QueryStatus.SUCCESS,
            columns=["name", "trade_date", "收盘日", "公告日期", "pe_ttm"],
            rows=[
                {
                    "name": "Example A",
                    "trade_date": "20260916",
                    "收盘日": 20221215,
                    "公告日期": "20240131",
                    "pe_ttm": 8.5,
                },
                {
                    "name": "Example B",
                    "trade_date": "20260916",
                    "收盘日": 20221216,
                    "公告日期": "20240201",
                    "pe_ttm": 12.0,
                },
            ],
            row_count=2,
        ),
        "Valuation ranking",
        "Rank the complete market snapshot.",
        output_dir=tmp_path,
        column_notes={
            "name": "证券简称，来自交易所证券主档。",
            "trade_date": "交易日，格式 YYYYMMDD。",
            "收盘日": "行情收盘日期，格式 YYYYMMDD。",
            "公告日期": "公告发布日期，格式 YYYYMMDD。",
            "pe_ttm": "滚动市盈率 = 收盘价 / 近12个月每股收益。",
        },
    )

    visualization = _build_research_visualization(workbook_path)

    assert visualization is not None
    assert visualization.title == "Valuation ranking"
    assert visualization.columns == [
        "name",
        "trade_date",
        "收盘日",
        "公告日期",
        "pe_ttm",
    ]
    assert visualization.numeric_columns == ["pe_ttm"]
    assert visualization.rows[0]["trade_date"] == "2026-09-16"
    assert visualization.rows[0]["收盘日"] == "2022-12-15"
    assert visualization.rows[0]["公告日期"] == "2024-01-31"
    assert visualization.rows[0]["pe_ttm"] == 8.5
    assert visualization.source_row_count == 2
    assert visualization.suggested_x == "name"
    assert visualization.suggested_y == "pe_ttm"
    assert visualization.methodology == "Rank the complete market snapshot."
    assert visualization.column_notes == {
        "name": "证券简称，来自交易所证券主档。",
        "trade_date": "交易日，格式 YYYYMMDD。",
        "收盘日": "行情收盘日期，格式 YYYYMMDD。",
        "公告日期": "公告发布日期，格式 YYYYMMDD。",
        "pe_ttm": "滚动市盈率 = 收盘价 / 近12个月每股收益。",
    }


def test_legacy_workbook_without_notes_still_extracts_methodology(tmp_path):
    workbook_path = build_research_workbook(
        QueryResult(
            query_id="legacy",
            provider="tushare",
            operation="daily",
            status=QueryStatus.SUCCESS,
            columns=["ts_code", "close"],
            rows=[{"ts_code": "600000.SH", "close": 12.5}],
            row_count=1,
        ),
        "Legacy study",
        "前复权口径说明。",
        output_dir=tmp_path,
    )

    visualization = _build_research_visualization(workbook_path)

    assert visualization is not None
    assert visualization.column_notes == {}
    assert visualization.methodology == "前复权口径说明。"

from pathlib import Path
import os

import pandas as pd
from openpyxl import load_workbook
import pytest

from china_a_share.bootstrap import create_feishu_agent_runtime
from china_a_share.config import Settings
from china_a_share.core.contracts import AnalysisTaskStatus, QueryResult, QueryStatus
from china_a_share.feishu_agent import (
    FeishuAgentCoordinator,
    FeishuAgentOutcome,
    FeishuAgentRequest,
    FeishuAgentRuntime,
    FeishuAgentTask,
    MAX_AGENT_ROUNDS,
    ResearchToolbox,
    build_research_workbook,
)
from china_a_share.feishu import FeishuOpenApiClient
from china_a_share.model_client import OpenAICompatibleChatModel
from china_a_share.tasks import MemoryAnalysisTaskStore


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def put(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FakeOperation:
    name = "daily"
    description = "Daily A-share prices with trade_date and close fields."


class FakeProvider:
    name = "test-provider"

    def search_operations(self, prompt):
        return [FakeOperation()]

    def supports(self, operation):
        return operation == "daily"

    def query(
        self,
        operation,
        params,
        fields,
        *,
        api_route,
        request_id,
        query_id,
    ):
        assert operation == "daily"
        assert params == {"trade_date": "20260916"}
        assert fields == ["ts_code", "close"]
        return pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "close": 10.25},
                {"ts_code": "600000.SH", "close": 11.5},
            ]
        )


class RecentReturnProvider:
    name = "test-provider"

    def search_operations(self, prompt):
        return [FakeOperation()]

    def supports(self, operation):
        return operation in {"daily", "stock_basic", "trade_cal"}

    def query(
        self,
        operation,
        params,
        fields,
        *,
        api_route,
        request_id,
        query_id,
    ):
        if operation == "stock_basic":
            assert params == {"list_status": "L"}
            return pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "name": "Ping An Bank", "industry": "Bank"},
                    {"ts_code": "600000.SH", "name": "SPDB", "industry": "Bank"},
                ]
            )
        if operation == "trade_cal":
            assert params["exchange"] == "SSE"
            assert params["is_open"] == "1"
            return pd.DataFrame(
                [
                    {"cal_date": trade_date, "is_open": 1}
                    for trade_date in ("20260914", "20260915", "20260916")
                ]
            )
        assert operation == "daily"
        changes = {
            "20260914": (1.0, 3.0),
            "20260915": (2.0, -1.0),
            "20260916": (3.0, 1.0),
        }
        trade_dates = (
            [params["trade_date"]]
            if "trade_date" in params
            else list(changes)
        )
        rows = []
        for trade_date in trade_dates:
            first_change, second_change = changes[trade_date]
            rows.extend(
                [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": trade_date,
                        "close": 10.0,
                        "pct_chg": first_change,
                    },
                    {
                        "ts_code": "600000.SH",
                        "trade_date": trade_date,
                        "close": 12.0,
                        "pct_chg": second_change,
                    },
                ]
            )
        return pd.DataFrame(rows)


class RecordingPythonSandbox:
    def __init__(self):
        self.calls = []

    def run(self, code, datasets):
        self.calls.append((code, datasets))
        daily = datasets["dataset_1"].copy()
        daily["pct_chg"] = pd.to_numeric(daily["pct_chg"])
        selected_dates = sorted(daily["trade_date"].unique())[-3:]
        selected = daily.loc[daily["trade_date"].isin(selected_dates)]
        result = (
            selected.groupby("ts_code")["pct_chg"]
            .apply(lambda values: ((1 + values / 100).prod() - 1) * 100)
            .rename("period_return_pct")
            .reset_index()
            .sort_values("period_return_pct", ascending=False)
            .head(1)
        )
        return result


class RecordingDispatcher:
    def __init__(self):
        self.task_ids = []

    def dispatch(self, task_id):
        self.task_ids.append(task_id)


class RecordingSink:
    def __init__(self):
        self.messages = []
        self.updates = []
        self.files = []

    def reply(self, message_id, text):
        self.messages.append((message_id, text))
        return "progress-message-1"

    def update(self, message_id, text):
        self.updates.append((message_id, text))

    def reply_file(self, message_id, path):
        self.files.append((message_id, path))


def test_agent_runtime_uses_tools_and_exports_complete_excel():
    session = SequenceSession(
        [
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-query",
                                        "type": "function",
                                        "function": {
                                            "name": "query_market_data",
                                            "arguments": (
                                                '{"operation":"daily","params":'
                                                '{"trade_date":"20260916"},'
                                                '"fields":["ts_code","close"]}'
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-export",
                                        "type": "function",
                                        "function": {
                                            "name": "export_excel",
                                            "arguments": (
                                                '{"dataset_id":"dataset_1",'
                                                '"title":"Daily prices",'
                                                '"methodology":"Tushare daily query."}'
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "查询完成，Excel 已生成。",
                            }
                        }
                    ]
                }
            ),
        ]
    )
    progress = []

    model = OpenAICompatibleChatModel(
        "https://model.example/v1",
        "research-model",
        "test-key",
        session=session,
    )
    outcome = FeishuAgentRuntime(
        model,
        FakeProvider(),
    ).run(
        FeishuAgentRequest(
            prompt="导出最近交易日收盘价 Excel",
            conversation_id="tenant:chat:root:user",
            source_message_id="message-1",
        ),
        lambda stage, message: progress.append((stage, message)),
    )

    assert outcome.answer == "查询完成，Excel 已生成。"
    assert outcome.artifact_path is not None
    workbook = load_workbook(outcome.artifact_path, data_only=False)
    assert workbook.sheetnames == ["Results", "Methodology"]
    assert workbook["Results"]["A6"].value == "000001.SZ"
    assert workbook["Results"]["B6"].value == 10.25
    assert workbook["Methodology"]["B4"].value == "test-provider"
    assert [call[1]["json"]["model"] for call in session.calls] == [
        "research-model",
        "research-model",
        "research-model",
    ]
    assert all(
        call[0] == "https://model.example/v1/chat/completions"
        for call in session.calls
    )
    assert {stage for stage, _message in progress} == {"querying", "exporting"}


def test_agent_runtime_synthesizes_answer_after_tool_budget_is_exhausted():
    class LoopingModel:
        model = "looping-model"

        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            if not tools:
                return {
                    "role": "assistant",
                    "content": "已根据现有数据完成回答。",
                }
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call-{self.calls}",
                        "type": "function",
                        "function": {
                            "name": "query_market_data",
                            "arguments": (
                                '{"operation":"daily","params":'
                                '{"trade_date":"20260916"},'
                                '"fields":["ts_code","close"]}'
                            ),
                        },
                    }
                ],
            }

    model = LoopingModel()

    outcome = FeishuAgentRuntime(model, FakeProvider()).run(
        FeishuAgentRequest(
            prompt="查询行情。",
            conversation_id="conversation",
            source_message_id="message",
        ),
        lambda _stage, _message: None,
    )

    assert outcome.answer == "已根据现有数据完成回答。"
    assert model.calls == MAX_AGENT_ROUNDS + 1


def test_generic_query_and_python_sandbox_replace_prompt_specific_ranking_tool():
    progress = []
    sandbox = RecordingPythonSandbox()
    toolbox = ResearchToolbox(
        RecentReturnProvider(),
        "request-1",
        python_sandbox=sandbox,
    )

    daily = toolbox.call(
        "query_market_data",
        {
            "operation": "daily",
            "params": {"start_date": "20260901", "end_date": "20260916"},
            "fields": ["ts_code", "trade_date", "close", "pct_chg"],
        },
        lambda stage, message: progress.append((stage, message)),
    )
    payload = toolbox.call(
        "run_python_analysis",
        {
            "dataset_ids": [daily["dataset_id"]],
            "code": (
                'daily = datasets["dataset_1"].copy()\n'
                'result = daily.groupby("ts_code")["pct_chg"].sum().reset_index()'
            ),
        },
        lambda stage, message: progress.append((stage, message)),
    )

    assert payload["row_count"] == 1
    row = payload["preview"][0]
    assert row["ts_code"] == "000001.SZ"
    assert row["period_return_pct"] == pytest.approx(6.1106)
    assert sandbox.calls[0][0].startswith('daily = datasets["dataset_1"]')
    assert set(sandbox.calls[0][1]) == {"dataset_1"}
    assert "rank_recent_market_return" not in {
        definition["function"]["name"] for definition in toolbox.definitions
    }
    assert {stage for stage, _message in progress} == {"querying", "calculating"}


def test_research_workbook_preserves_numeric_values_and_source_context():
    result = QueryResult(
        query_id="ranked",
        provider="tushare",
        operation="daily_basic",
        status=QueryStatus.SUCCESS,
        columns=["ts_code", "pe", "dividend_yield"],
        rows=[{"ts_code": "600519.SH", "pe": 24.5, "dividend_yield": 0.021}],
        row_count=1,
        completeness="complete",
    )

    path = build_research_workbook(result, "Valuation results", "Ranked by PE.")

    workbook = load_workbook(path, data_only=False)
    results = workbook["Results"]
    methodology = workbook["Methodology"]
    assert results["B6"].value == 24.5
    assert isinstance(results["B6"].value, float)
    assert methodology["B4"].value == "tushare"
    assert methodology["B5"].value == "daily_basic"
    assert methodology["B7"].value == "Ranked by PE."
    assert results["C6"].number_format == "0.00%"
    assert all(
        cell.data_type != "e"
        for sheet in workbook.worksheets
        for row in sheet.iter_rows()
        for cell in row
    )


def test_agent_coordinator_reports_progress_answer_and_file(tmp_path):
    store = MemoryAnalysisTaskStore()
    dispatcher = RecordingDispatcher()
    coordinator = FeishuAgentCoordinator(store, dispatcher)
    request = FeishuAgentRequest(
        prompt="Research banks and export Excel.",
        conversation_id="tenant:chat:root:user",
        source_message_id="message-1",
    )
    task = coordinator.submit(request, task_id="agent-task")
    artifact_path = tmp_path / "result.xlsx"
    artifact_path.write_bytes(b"xlsx")

    class FakeRuntime:
        def run(self, received_request, progress):
            assert received_request == request
            progress("querying", "正在查询市场数据…")
            progress("querying", "正在查询市场数据…")
            return FeishuAgentOutcome(
                answer="研究完成。",
                artifact_path=artifact_path,
            )

    sink = RecordingSink()
    completed = coordinator.run(task.task_id, FakeRuntime(), sink)

    assert dispatcher.task_ids == ["agent-task"]
    assert completed.status == AnalysisTaskStatus.SUCCEEDED
    assert completed.answer == "研究完成。"
    assert sink.messages == [
        ("message-1", "正在理解问题并选择研究工具…"),
    ]
    assert sink.updates == [
        ("progress-message-1", "正在查询市场数据…"),
        ("progress-message-1", "研究完成。"),
    ]
    assert sink.files == [("message-1", artifact_path)]
    assert isinstance(store.get("agent-task"), FeishuAgentTask)


def test_agent_coordinator_reserves_final_message_update():
    store = MemoryAnalysisTaskStore()
    coordinator = FeishuAgentCoordinator(store, RecordingDispatcher())
    task = coordinator.submit(
        FeishuAgentRequest(
            prompt="Research a broad market question.",
            conversation_id="tenant:chat:root:user",
            source_message_id="message-1",
        ),
        task_id="agent-task",
    )

    class VerboseRuntime:
        def run(self, _request, progress):
            for index in range(30):
                progress("querying", f"Progress {index}")
            return FeishuAgentOutcome(answer="Final answer.")

    sink = RecordingSink()
    completed = coordinator.run(task.task_id, VerboseRuntime(), sink)

    assert completed.status == AnalysisTaskStatus.SUCCEEDED
    assert len(sink.messages) == 1
    assert len(sink.updates) == 20
    assert sink.updates[-1] == ("progress-message-1", "Final answer.")


def test_feishu_client_returns_visible_reply_message_id():
    session = SequenceSession(
        [
            FakeResponse({"code": 0, "tenant_access_token": "tenant-token"}),
            FakeResponse({"code": 0, "data": {"message_id": "reply-message-1"}}),
        ]
    )

    reply_message_id = FeishuOpenApiClient(
        "app-id", "app-secret", session=session
    ).reply(
        "message-1",
        "Research accepted.",
    )

    assert reply_message_id == "reply-message-1"
    assert session.calls[1][0].endswith("/im/v1/messages/message-1/reply")
    assert "params" not in session.calls[1][1]
    assert session.calls[1][1]["json"]["msg_type"] == "text"
    assert "reply_in_thread" not in session.calls[1][1]["json"]


def test_feishu_client_updates_existing_text_reply():
    session = SequenceSession(
        [
            FakeResponse({"code": 0, "tenant_access_token": "tenant-token"}),
            FakeResponse({"code": 0}),
        ]
    )

    FeishuOpenApiClient("app-id", "app-secret", session=session).update(
        "reply-message-1",
        "Research completed.",
    )

    assert session.calls[1][0].endswith("/im/v1/messages/reply-message-1")
    assert session.calls[1][1]["json"]["msg_type"] == "text"
    assert session.calls[1][1]["json"]["content"] == (
        '{"text": "Research completed."}'
    )


def test_feishu_client_replies_with_interactive_card():
    session = SequenceSession(
        [
            FakeResponse({"code": 0, "tenant_access_token": "tenant-token"}),
            FakeResponse({"code": 0, "data": {"message_id": "card-message-1"}}),
        ]
    )
    card = {"elements": [{"tag": "div"}]}

    message_id = FeishuOpenApiClient(
        "app-id", "app-secret", session=session
    ).reply_card("message-1", card)

    assert message_id == "card-message-1"
    assert session.calls[1][0].endswith("/im/v1/messages/message-1/reply")
    assert session.calls[1][1]["json"] == {
        "msg_type": "interactive",
        "content": '{"elements": [{"tag": "div"}]}',
    }


def test_feishu_client_uploads_and_replies_with_excel_file(tmp_path):
    path = tmp_path / "result.xlsx"
    path.write_bytes(b"workbook")
    session = SequenceSession(
        [
            FakeResponse({"code": 0, "tenant_access_token": "tenant-token"}),
            FakeResponse({"code": 0, "data": {"file_key": "file-key"}}),
            FakeResponse({"code": 0}),
        ]
    )

    FeishuOpenApiClient("app-id", "app-secret", session=session).reply_file(
        "message-1",
        path,
    )

    assert session.calls[1][0].endswith("/im/v1/files")
    assert session.calls[1][1]["data"] == {
        "file_type": "xls",
        "file_name": "result.xlsx",
    }
    assert session.calls[2][0].endswith("/im/v1/messages/message-1/reply")
    assert "params" not in session.calls[2][1]
    assert session.calls[2][1]["json"]["msg_type"] == "file"
    assert "reply_in_thread" not in session.calls[2][1]["json"]


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ANALYSIS") != "1",
    reason="Set RUN_LIVE_ANALYSIS=1 to call the configured model and Tushare.",
)
def test_live_feishu_agent_answers_one_historical_price_question():
    runtime = create_feishu_agent_runtime(Settings.from_env())

    outcome = runtime.run(
        FeishuAgentRequest(
            prompt=(
                "请查询贵州茅台（600519.SH）在2026年9月15日的收盘价，"
                "只回答代码、日期和收盘价。"
            ),
            conversation_id="live:feishu:agent:session",
            source_message_id="live-message",
        ),
        lambda _stage, _message: None,
    )

    assert "600519.SH" in outcome.answer
    assert "2026" in outcome.answer


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ANALYSIS") != "1",
    reason="Set RUN_LIVE_ANALYSIS=1 to call the configured model and Tushare.",
)
def test_live_feishu_agent_answers_reported_five_day_return_ranking():
    runtime = create_feishu_agent_runtime(Settings.from_env())

    outcome = runtime.run(
        FeishuAgentRequest(
            prompt="查出最近5个交易日累计涨幅最大的个股",
            conversation_id="live:feishu:agent:reported-five-day-return",
            source_message_id="live-reported-recent-return",
        ),
        lambda _stage, _message: None,
    )

    assert "二连板" not in outcome.answer
    assert "第三日" not in outcome.answer
    assert "复权收益" not in outcome.answer
    assert "并非复权" in outcome.answer or "复权" not in outcome.answer
    assert any(character.isdigit() for character in outcome.answer)

from pathlib import Path
import os

import pandas as pd
from openpyxl import load_workbook
import pytest

from china_a_share.bootstrap import create_feishu_agent_runtime
from china_a_share.config import Settings
from china_a_share.core.contracts import AnalysisTaskStatus, QueryResult, QueryStatus
from china_a_share.feishu_agent import (
    DEEPSEEK_AGENT_FALLBACK_MODEL,
    DEEPSEEK_AGENT_MODEL,
    MAX_AGENT_ROUNDS,
    FeishuAgentCoordinator,
    FeishuAgentOutcome,
    FeishuAgentRequest,
    FeishuAgentRuntime,
    FeishuAgentTask,
    _agent_model_for_round,
    build_research_workbook,
)
from china_a_share.feishu import FeishuOpenApiClient
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


class FakeOperation:
    name = "daily"
    description = "Daily A-share prices with trade_date and close fields."


class FakeProvider:
    name = "test-provider"

    def search_operations(self, prompt):
        return [FakeOperation()]

    def supports(self, operation):
        return operation == "daily"

    def validate_query(self, operation, params, fields):
        assert operation == "daily"
        assert params == {"trade_date": "20260916"}
        assert fields == ["ts_code", "close"]

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


class RecordingDispatcher:
    def __init__(self):
        self.task_ids = []

    def dispatch(self, task_id):
        self.task_ids.append(task_id)


class RecordingSink:
    def __init__(self):
        self.messages = []
        self.files = []

    def reply(self, message_id, text):
        self.messages.append((message_id, text))

    def reply_file(self, message_id, path):
        self.files.append((message_id, path))


def test_agent_model_escalates_only_final_recovery_rounds():
    assert _agent_model_for_round(0) == DEEPSEEK_AGENT_MODEL
    assert _agent_model_for_round(MAX_AGENT_ROUNDS - 3) == DEEPSEEK_AGENT_MODEL
    assert (
        _agent_model_for_round(MAX_AGENT_ROUNDS - 2)
        == DEEPSEEK_AGENT_FALLBACK_MODEL
    )
    assert (
        _agent_model_for_round(MAX_AGENT_ROUNDS - 1)
        == DEEPSEEK_AGENT_FALLBACK_MODEL
    )


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

    outcome = FeishuAgentRuntime(
        "test-key",
        FakeProvider(),
        session=session,
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
        DEEPSEEK_AGENT_MODEL,
        DEEPSEEK_AGENT_MODEL,
        DEEPSEEK_AGENT_MODEL,
    ]
    assert {stage for stage, _message in progress} == {"querying", "exporting"}


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
        ("message-1", "正在查询市场数据…"),
        ("message-1", "研究完成。"),
    ]
    assert sink.files == [("message-1", artifact_path)]
    assert isinstance(store.get("agent-task"), FeishuAgentTask)


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
    assert session.calls[2][1]["json"]["msg_type"] == "file"


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ANALYSIS") != "1",
    reason="Set RUN_LIVE_ANALYSIS=1 to call DeepSeek and Tushare.",
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


def test_agent_system_prompt_requires_uniform_number_precision():
    from china_a_share.feishu_agent import _agent_system_prompt

    prompt = _agent_system_prompt()

    assert "two decimal" in prompt
    assert "integers" in prompt

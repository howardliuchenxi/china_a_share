from pathlib import Path
import os

import pandas as pd
from openpyxl import load_workbook
import pytest

from china_a_share.bootstrap import create_feishu_agent_runtime
from china_a_share.config import Settings
from china_a_share.core.contracts import AnalysisTaskStatus, QueryResult, QueryStatus
from china_a_share.feishu_agent import (
    DEEPSEEK_AGENT_MODEL,
    FeishuAgentCoordinator,
    FeishuAgentOutcome,
    FeishuAgentRequest,
    FeishuAgentRuntime,
    FeishuAgentTask,
    ResearchToolbox,
    _requires_recent_market_return_tool,
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
        trade_date = params["trade_date"]
        first_change, second_change = {
            "20260914": (1.0, 3.0),
            "20260915": (2.0, -1.0),
            "20260916": (3.0, 1.0),
        }[trade_date]
        return pd.DataFrame(
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


@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("查出最近5个交易日累计涨幅最大的个股", True),
        ("近10个交易日跌幅最低的A股排行", True),
        ("最近5个交易日A股涨幅排名", False),
        ("查询贵州茅台最近5个交易日收盘价", False),
        ("查出今年累计涨幅最大的个股", False),
    ],
)
def test_recent_market_return_tool_selection_covers_ranking_intent(prompt, expected):
    assert _requires_recent_market_return_tool(prompt) is expected


def test_agent_runtime_executes_bounded_tool_for_recent_market_ranking():
    session = SequenceSession([])

    outcome = FeishuAgentRuntime(
        "test-key",
        RecentReturnProvider(),
        session=session,
    ).run(
        FeishuAgentRequest(
            prompt="查出最近3个交易日累计涨幅最大的个股",
            conversation_id="test:recent-return",
            source_message_id="message-1",
        ),
        lambda _stage, _message: None,
    )

    assert "000001.SZ" in outcome.answer
    assert "6.11%" in outcome.answer
    assert "并非复权价格收益" in outcome.answer
    assert session.calls == []


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_recent_market_return_tool_compounds_complete_trading_sessions(direction):
    progress = []

    payload = ResearchToolbox(RecentReturnProvider(), "request-1").call(
        "rank_recent_market_return",
        {
            "trading_sessions": 3,
            "direction": direction,
            "limit": 1,
            "end_date": "20260916",
        },
        lambda stage, message: progress.append((stage, message)),
    )

    assert payload["row_count"] == 1
    row = payload["preview"][0]
    expected_code = "000001.SZ" if direction == "desc" else "600000.SH"
    assert row["ts_code"] == expected_code
    assert row["start_trade_date"] == "20260914"
    assert row["end_trade_date"] == "20260916"
    assert row["trading_session_count"] == 3
    assert row["name"]
    assert payload["calculation"] == "compound_daily_pct_chg"
    assert "not an adjusted-price return" in payload["methodology"]
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
        ("message-1", "正在查询市场数据…"),
        ("message-1", "研究完成。"),
    ]
    assert sink.files == [("message-1", artifact_path)]
    assert isinstance(store.get("agent-task"), FeishuAgentTask)


def test_feishu_client_replies_inside_source_message_thread():
    session = SequenceSession(
        [
            FakeResponse({"code": 0, "tenant_access_token": "tenant-token"}),
            FakeResponse({"code": 0}),
        ]
    )

    FeishuOpenApiClient("app-id", "app-secret", session=session).reply(
        "message-1",
        "Research accepted.",
    )

    assert session.calls[1][0].endswith("/im/v1/messages/message-1/reply")
    assert "params" not in session.calls[1][1]
    assert session.calls[1][1]["json"]["msg_type"] == "text"
    assert session.calls[1][1]["json"]["reply_in_thread"] is True


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
    assert session.calls[2][1]["json"]["reply_in_thread"] is True


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


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ANALYSIS") != "1",
    reason="Set RUN_LIVE_ANALYSIS=1 to call DeepSeek and Tushare.",
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

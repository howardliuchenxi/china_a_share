from pathlib import Path
import os
import re

import pandas as pd
from openpyxl import load_workbook
import pytest

from china_a_share.bootstrap import create_feishu_agent_runtime
from china_a_share.capabilities import get_operation_capability
from china_a_share.config import Settings
from china_a_share.core.contracts import AnalysisTaskStatus, QueryResult, QueryStatus
from china_a_share.feishu_agent import (
    FeishuAgentCoordinator,
    FeishuAgentConversationTurn,
    FeishuAgentOutcome,
    FeishuAgentRequest,
    FeishuAgentTask,
    FeishuResearchVisualization,
    ResearchToolbox,
    build_research_workbook,
    research_visualization_token_hash,
)
from china_a_share.feishu import FeishuOpenApiClient
from china_a_share.registry import TushareOperationCatalog
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


class UnauditedFakeOperation:
    name = "bak_basic"
    description = "Unaudited backup operation."


class FakeProvider:
    name = "test-provider"

    def search_operations(self, _prompt):
        return [FakeOperation()]

    def supports(self, operation):
        return operation == "daily"

    def describe_query_shapes(self, operation):
        capability = get_operation_capability(operation)
        if capability is None:
            return ()
        return tuple(
            {
                "shape_id": shape.shape_id,
                "required_params": list(shape.required_params),
            }
            for shape in capability.query_shapes
        )

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


class RecentReturnProvider:
    name = "test-provider"

    def search_operations(self, prompt):
        return [FakeOperation()]

    def supports(self, operation):
        return operation in {"daily", "stock_basic", "trade_cal"}

    def validate_query(self, operation, params, fields):
        assert self.supports(operation)

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


def test_request_clarification_returns_one_bounded_prompt():
    toolbox = ResearchToolbox(FakeProvider(), "request-1")

    payload = toolbox.call(
        "request_clarification",
        {
            "question": "请确认排名口径：",
            "options": ["口径一（推荐）", "口径二", "自定义口径"],
        },
        lambda _stage, _message: None,
    )

    assert payload["clarification"] == (
        "请确认排名口径：\n"
        "1. 口径一（推荐）\n"
        "2. 口径二\n"
        "3. 自定义口径\n"
        "请回复序号，或直接补充你的完整口径。"
    )
    assert "final answer" in payload["instruction"]


def test_search_market_data_returns_all_audited_operations_with_query_shapes():
    class DiscoveryProvider(FakeProvider):
        def search_operations(self, prompt):
            assert prompt == "valuation"
            return [UnauditedFakeOperation(), FakeOperation()]

    payload = ResearchToolbox(DiscoveryProvider(), "request-1").call(
        "search_market_data",
        {"query": "valuation"},
        lambda _stage, _message: None,
    )

    assert [operation["name"] for operation in payload["operations"]] == ["daily"]
    assert payload["operations"][0]["query_shapes"] == [
        {
            "shape_id": "security",
            "required_params": ["ts_code"],
        },
        {
            "shape_id": "market_snapshot",
            "required_params": ["trade_date"],
        },
        {
            "shape_id": "bounded_range",
            "required_params": ["start_date", "end_date"],
        },
    ]


def test_search_market_data_exposes_production_catalog_operations():
    class CatalogProvider(FakeProvider):
        def search_operations(self, prompt):
            assert prompt == "valuation ranking"
            return TushareOperationCatalog().search(prompt)

    payload = ResearchToolbox(CatalogProvider(), "request-1").call(
        "search_market_data",
        {"query": "valuation ranking"},
        lambda _stage, _message: None,
    )

    operation_names = [operation["name"] for operation in payload["operations"]]
    assert "daily_basic" in operation_names
    assert "bak_basic" not in operation_names
    assert len(operation_names) > 12


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


def test_model_preview_keeps_top_ten_rows_and_retains_the_complete_dataset():
    rows = [
        {"ts_code": f"{index:06d}.SZ", "close": float(index)}
        for index in range(12)
    ]
    result = QueryResult(
        query_id="ranking",
        provider="tushare",
        operation="daily",
        status=QueryStatus.SUCCESS,
        columns=["ts_code", "close"],
        rows=rows,
        row_count=len(rows),
    )

    toolbox = ResearchToolbox(FakeProvider(), "request-1", session_dataset=result)
    payload = toolbox.call(
        "inspect_session_dataset",
        {},
        lambda _stage, _message: None,
    )

    assert len(payload["preview"]) == 10
    assert payload["preview_truncated"] is True
    assert payload["dataset_scope"] == "complete_retained_result"


def test_toolbox_restores_session_dataset_and_archives_follow_up_result(tmp_path):
    session_result = QueryResult(
        query_id="previous_final",
        provider="tushare",
        operation="daily_basic",
        status=QueryStatus.SUCCESS,
        columns=["ts_code", "total_mv"],
        rows=[
            {"ts_code": "000001.SZ", "total_mv": 120.0},
            {"ts_code": "600000.SH", "total_mv": 90.0},
        ],
        row_count=2,
        completeness="complete",
    )
    store = MemoryAnalysisTaskStore()
    toolbox = ResearchToolbox(
        FakeProvider(),
        "request-1",
        artifact_dir=tmp_path,
        dataset_archive=store,
        task_id="agent-task",
        session_dataset=session_result,
    )

    inspected = toolbox.call(
        "inspect_session_dataset",
        {},
        lambda _stage, _message: None,
    )
    ranked = toolbox.call(
        "rank_dataset",
        {
            "dataset_id": "session_dataset",
            "sort_by": "total_mv",
            "direction": "asc",
            "limit": 1,
            "fields": ["ts_code", "total_mv"],
        },
        lambda _stage, _message: None,
    )
    toolbox.call(
        "export_excel",
        {
            "dataset_id": ranked["dataset_id"],
            "title": "Filtered result",
            "methodology": "Continued from the complete session dataset.",
            "column_notes": [
                {"column": "ts_code", "note": "A股证券代码，含交易所后缀。"},
                {"column": "total_mv", "note": "当日总市值，单位为万元。"},
            ],
        },
        lambda _stage, _message: None,
    )

    assert inspected["dataset_id"] == "session_dataset"
    assert inspected["row_count"] == 2
    assert ranked["preview"] == [{"ts_code": "600000.SH", "total_mv": 90.0}]
    assert store.promote_session_workspace("chat:session", "agent-task") is True
    restored = store.get_session_workspace("chat:session")
    assert restored is not None
    assert restored.rows == ranked["preview"]


def test_transform_tool_exposes_complete_pipeline_contract():
    toolbox = ResearchToolbox(RecentReturnProvider(), "request-1")

    transform = next(
        definition["function"]
        for definition in toolbox.definitions
        if definition["function"]["name"] == "transform_dataset"
    )
    parameters = transform["parameters"]
    pipeline = parameters["properties"]["pipeline"]

    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["pipeline"]
    assert pipeline["properties"]["steps"]["items"] == {
        "$ref": "#/$defs/ResultPipelineStep"
    }
    assert "ResultPipelineStep" in parameters["$defs"]
    assert "operation" in parameters["$defs"]["ResultPipelineStep"]["properties"]


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

    path = build_research_workbook(
        result,
        "Valuation results",
        "Ranked by PE.",
        column_notes={
            "ts_code": "A股证券代码，含交易所后缀。",
            "pe": "市盈率 = 收盘价 / 每股收益。",
            "dividend_yield": "股息率 = 近12个月每股分红 / 收盘价。",
        },
    )

    workbook = load_workbook(path, data_only=False)
    results = workbook["Results"]
    methodology = workbook["Methodology"]
    notes = workbook["列说明"]
    assert results["B6"].value == 24.5
    assert isinstance(results["B6"].value, float)
    assert methodology["B4"].value == "tushare"
    assert methodology["B5"].value == "daily_basic"
    assert methodology["B7"].value == "Ranked by PE."
    assert results["C6"].number_format == "0.00%"
    assert notes["A5"].value == "ts_code"
    assert notes["B5"].value == "A股证券代码，含交易所后缀。"
    assert notes["A6"].value == "pe"
    assert notes["B7"].value == "股息率 = 近12个月每股分红 / 收盘价。"
    assert results["A5"].comment is not None
    assert "证券代码" in results["A5"].comment.text
    assert all(
        cell.data_type != "e"
        for sheet in workbook.worksheets
        for row in sheet.iter_rows()
        for cell in row
    )


def test_research_workbook_links_security_codes_to_quote_pages():
    result = QueryResult(
        query_id="events",
        provider="tushare",
        operation="event_study",
        status=QueryStatus.SUCCESS,
        columns=["signal_date", "ts_code", "hk_code"],
        rows=[
            {"signal_date": 20260401, "ts_code": "688220.SH", "hk_code": "00700.HK"},
            {"signal_date": 20260402, "ts_code": "000002.SZ", "hk_code": "2.HK"},
        ],
        row_count=2,
        completeness="complete",
    )

    path = build_research_workbook(result, "Signals", "Event study basis.")

    results = load_workbook(path)["Results"]
    assert results["A6"].hyperlink is None
    assert results["B6"].hyperlink.target == "https://stockpage.10jqka.com.cn/688220/"
    assert results["B7"].hyperlink.target == "https://stockpage.10jqka.com.cn/000002/"
    assert results["C6"].hyperlink.target == "https://stockpage.10jqka.com.cn/HK0700/"
    assert results["C7"].hyperlink.target == "https://stockpage.10jqka.com.cn/HK0002/"


def test_export_tool_rejects_incomplete_column_notes(tmp_path):
    toolbox = ResearchToolbox(FakeProvider(), "request-1", artifact_dir=tmp_path)
    queried = toolbox.call(
        "query_market_data",
        {
            "operation": "daily",
            "params": {"trade_date": "20260916"},
            "fields": ["ts_code", "close"],
        },
        lambda _stage, _message: None,
    )

    missing = pytest.raises(
        ValueError,
        match=r"column_notes must exactly cover.*missing notes for: close",
    )
    with missing:
        toolbox.call(
            "export_excel",
            {
                "dataset_id": queried["dataset_id"],
                "title": "Incomplete",
                "methodology": "Basis.",
                "column_notes": [
                    {"column": "ts_code", "note": "A股证券代码，含交易所后缀。"}
                ],
            },
            lambda _stage, _message: None,
        )
    with pytest.raises(ValueError, match="notes for unknown columns: name"):
        toolbox.call(
            "export_excel",
            {
                "dataset_id": queried["dataset_id"],
                "title": "Unexpected",
                "methodology": "Basis.",
                "column_notes": [
                    {"column": "ts_code", "note": "A股证券代码，含交易所后缀。"},
                    {"column": "close", "note": "未复权收盘价，单位为元。"},
                    {"column": "name", "note": "不在导出数据集中的列。"},
                ],
            },
            lambda _stage, _message: None,
        )
    with pytest.raises(ValueError, match=r"8-400 characters"):
        toolbox.call(
            "export_excel",
            {
                "dataset_id": queried["dataset_id"],
                "title": "Too short",
                "methodology": "Basis.",
                "column_notes": [
                    {"column": "ts_code", "note": "太短"},
                    {"column": "close", "note": "未复权收盘价，单位为元。"},
                ],
            },
            lambda _stage, _message: None,
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
    assert store.get_artifact("agent-task", "result.xlsx") == b"xlsx"
    assert isinstance(store.get("agent-task"), FeishuAgentTask)


def test_agent_coordinator_preserves_success_when_file_delivery_fails(tmp_path):
    store = MemoryAnalysisTaskStore()
    coordinator = FeishuAgentCoordinator(store, RecordingDispatcher())
    request = FeishuAgentRequest(
        prompt="Research banks and export Excel.",
        conversation_id="tenant:chat:root:user",
        source_message_id="message-1",
    )
    task = coordinator.submit(request, task_id="agent-task")
    artifact_path = tmp_path / "result.xlsx"
    artifact_path.write_bytes(b"xlsx")

    class FakeRuntime:
        def run(self, _request, _progress):
            return FeishuAgentOutcome(
                answer="研究完成。",
                artifact_path=artifact_path,
            )

    class FailingFileSink(RecordingSink):
        def reply_file(self, _message_id, _path):
            raise RuntimeError("Feishu file upload failed.")

    sink = FailingFileSink()
    completed = coordinator.run(task.task_id, FakeRuntime(), sink)

    assert completed.status == AnalysisTaskStatus.SUCCEEDED
    assert completed.answer == "研究完成。"
    assert completed.progress_message == "研究完成，但附件发送失败。"
    assert completed.error is None
    assert sink.updates[-1] == (
        "progress-message-1",
        "研究完成。\n\n研究已完成，但附件发送失败。请稍后回复“重试”重新生成附件。",
    )


def test_agent_coordinator_publishes_tokenized_visualization_link(tmp_path):
    store = MemoryAnalysisTaskStore()
    coordinator = FeishuAgentCoordinator(
        store,
        RecordingDispatcher(),
        public_app_url="https://research.example/",
    )
    task = coordinator.submit(
        FeishuAgentRequest(
            prompt="Create an interactive valuation chart.",
            conversation_id="tenant:chat:root:user",
            source_message_id="message-1",
        ),
        task_id="agent-task",
    )
    artifact_path = tmp_path / "result.xlsx"
    artifact_path.write_bytes(b"workbook")
    visualization = FeishuResearchVisualization(
        title="Valuation ranking",
        columns=["name", "pe_ttm"],
        numeric_columns=["pe_ttm"],
        rows=[{"name": "Example", "pe_ttm": 12.5}],
        source_row_count=1,
        truncated=False,
        suggested_x="name",
        suggested_y="pe_ttm",
    )

    class FakeRuntime:
        def run(self, _request, _progress):
            return FeishuAgentOutcome(
                answer="研究完成。",
                artifact_path=artifact_path,
                visualization=visualization,
            )

    sink = RecordingSink()
    completed = coordinator.run(task.task_id, FakeRuntime(), sink)

    terminal_message = sink.updates[-1][1]
    assert "研究结果页面（30天内有效，点击后直接查看）" in terminal_message
    assert "交互图表" not in terminal_message
    link = terminal_message.rsplit("\n", 1)[-1]
    token = link.split("token=", 1)[1]
    assert link.startswith("https://research.example/research/agent-task?")
    assert completed.visualization == visualization
    assert completed.visualization_token_hash == research_visualization_token_hash(
        token
    )
    assert token not in completed.model_dump_json()
    assert completed.visualization_expires_at is not None
    assert store.get_artifact("agent-task", "result.xlsx") == b"workbook"


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


def test_feishu_client_reports_file_upload_permission_error(tmp_path):
    path = tmp_path / "result.xlsx"
    path.write_bytes(b"workbook")
    session = SequenceSession(
        [
            FakeResponse({"code": 0, "tenant_access_token": "tenant-token"}),
            FakeResponse(
                {
                    "code": 99991672,
                    "msg": (
                        "Access denied. Required scope: "
                        "im:resource:upload."
                    ),
                },
                status_code=400,
            ),
        ]
    )

    with pytest.raises(RuntimeError, match="99991672.*im:resource:upload"):
        FeishuOpenApiClient(
            "app-id",
            "app-secret",
            session=session,
        ).reply_file("message-1", path)


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


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ANALYSIS") != "1",
    reason="Set RUN_LIVE_ANALYSIS=1 to call the configured model and Tushare.",
)
def test_live_feishu_agent_answers_reported_thirty_day_return_ranking():
    runtime = create_feishu_agent_runtime(Settings.from_env())

    outcome = runtime.run(
        FeishuAgentRequest(
            prompt="查出最近30个交易日累计涨幅最大的个股",
            conversation_id="live:feishu:agent:reported-thirty-day-return",
            source_message_id="live-reported-thirty-day-return",
        ),
        lambda _stage, _message: None,
    )

    assert "无法可靠查出" not in outcome.answer
    assert "工具调用额度" not in outcome.answer
    assert "只返回前 20 行" not in outcome.answer
    assert re.search(r"\b\d{6}\.(?:SH|SZ|BJ)\b", outcome.answer)
    assert "%" in outcome.answer


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ANALYSIS") != "1",
    reason="Set RUN_LIVE_ANALYSIS=1 to call the configured model and Tushare.",
)
def test_live_feishu_agent_resolves_bare_menu_number_from_history():
    """A bare menu reply must resolve through the retained prior turn."""
    runtime = create_feishu_agent_runtime(Settings.from_env())

    outcome = runtime.run(
        FeishuAgentRequest(
            prompt="2",
            conversation_id="live:feishu:agent:reported-menu-selection",
            source_message_id="live-reported-menu-selection",
            conversation=[
                FeishuAgentConversationTurn(
                    prompt="研究对象为A股所有股票",
                    answer=(
                        "已记录研究范围为A股全部股票。请问接下来您希望对这批股票"
                        "进行哪类分析？回复序号选择：1 涨跌表现统计 2 财务指标统计"
                        " 3 估值水平统计。"
                    ),
                )
            ],
        ),
        lambda _stage, _message: None,
    )

    assert "没有历史上下文" not in outcome.answer
    assert "无法确定" not in outcome.answer
    assert "财务" in outcome.answer


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ANALYSIS") != "1",
    reason="Set RUN_LIVE_ANALYSIS=1 to call the configured model and Tushare.",
)
def test_live_feishu_agent_clarifies_reported_ambiguous_pe_ranking():
    runtime = create_feishu_agent_runtime(Settings.from_env())

    outcome = runtime.run(
        FeishuAgentRequest(
            prompt="你能查到今天市盈率前10的股票吗",
            conversation_id="live:feishu:agent:ambiguous-pe-ranking",
            source_message_id="live-ambiguous-pe-ranking",
        ),
        lambda _stage, _message: None,
    )

    assert "市盈率" in outcome.answer
    assert "1." in outcome.answer
    assert "推荐" in outcome.answer
    assert "请回复序号" in outcome.answer


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ANALYSIS") != "1",
    reason="Set RUN_LIVE_ANALYSIS=1 to call the configured model and Tushare.",
)
def test_live_feishu_agent_answers_reported_precise_pe_ranking():
    runtime = create_feishu_agent_runtime(Settings.from_env())

    outcome = runtime.run(
        FeishuAgentRequest(
            prompt=(
                "查询最近一个已完成交易日，全A股中 PE_TTM 最低且大于0的10只股票，"
                "排除ST、退市整理和市盈率为空的股票，列出代码、名称、PE_TTM和总市值。"
            ),
            conversation_id="live:feishu:agent:precise-pe-ranking",
            source_message_id="live-precise-pe-ranking",
        ),
        lambda _stage, _message: None,
    )

    assert "DSML" not in outcome.answer
    assert "<｜｜" not in outcome.answer
    answered_with_data = "PE" in outcome.answer and any(
        character.isdigit() for character in outcome.answer
    )
    requested_clarification = (
        "推荐" in outcome.answer and "请回复序号" in outcome.answer
    )
    assert answered_with_data or requested_clarification


REPORTED_LEVEL_MODEL_STANDARD_V23 = r"""## 模型标准总本 v2.3（截至 2026-09-26 锁定）

**时效**：数据基准＝最新已披露交易日 **2026-09-24 收盘**（as_of 2026-09-26，数据最新记录日 2026-09-24，距今 2 日；0925 中秋休市、0926/27 周末 → 下一交易日 2026-09-28）。

**版本沿革**：v1.7（取消锁定·每日全流程）→ v1.8（L7 加 VR）→ v1.9（闸门严格执行＋进入当日计入＋VR 含边界）→ v2.0（加 L7-④ 与全链最小停留）→ v2.1（删 L7-④）→ v2.2（最小停留限 L4/L6）→ **v2.3（最小停留改挂 L5/L6，仅此两条）**。

---

### 一、总则（L0）

| 编号 | 条款 |
|---|---|
| L0-1 | **对象**：A 股全市场；剔 68／9 开头（科创板／B 股）、剔北交所（.BJ）。 |
| L0-2 | **价量口径**：基础价格序列＝`daily.close`（未复权，元）× 当日 `adj_factor`（ts_code+trade_date 一对一 join）＝**前复权 qf**；基期不归一（同股比值等价于以该股 adj_factor 为基期）；停牌不插值。 |
| L0-3 | **均线（交易日窗口）**：MA5／20／60＝截至 t 日最近 5／20／60 个**有行情交易日（含端点）**qf 简单均值，min_periods＝窗口长度，一律未舍入。 |
| L0-4 | **有行情交易日**：该股当日有 `daily` 行情记录之日；停牌日不计入任何窗口（MA、10 日连续链、20 日保留／累积期、前向 20 日收益窗口），不插值、不占天数。 |
| L0-5 | **层级嵌套**：L9⊆L8⊆L7⊆L6⊆L5⊆L4⊆L3⊆L2⊆L1，不得跳层。 |
| L0-6 | **每层条件只对该层有效**：进入条件只对当日上一层集合测试，不外溢、不跨层复用；进入下层后上层条件不再作为维持条件重测。 |
| L0-7 | **不做任何锁定·每日全流程执行**：每日自 L1 起对全市场依序重算至 L9，不跨日携带在册状态；前一交易日结果不影响当日。 |
| L0-8 | **20 个有行情交易日保留／累积窗口**：由当日往前回溯历史行情逐层重放得出；唯一固定的是 **t₀＝进入该层当日＝第 1 个有行情交易日**；窗口＝［t₀, t₀+19］共 20 个有行情交易日（含两端），逐日 +1／日、**不要求连续、中断不清零**，第 20 日收盘后到期，自 t₀+20 起移出该层及全部下游层。 |
| L0-9 | **执行段同序同日**：L7／L7r／L8／L9 同属一时序段，四层条件须在同一交易日 t（进入目标层当日）内算完，不引用 t+1 及以后信息。 |
| L0-10 | **执行段保留期＝1 个有行情交易日**：t 日在册，t+1 交易日起自动清空；不跨日保留、不次日复核、不累积、不顺延。 |
| L0-11 | **N1 60 线原则（唯一跨层全局规则）**：chg60＝MA60(t)／MA60(t−1)−1 ≥ **−0.1%**（等价 r60 ≥ 0.999；前复权、未舍入、相邻有行情交易日）；作用于 L5→L9 全链，**先判 60 线，再判本层条件**；因 60 线被剔除者不适用任何 20 日保留豁免。 |
| L0-12 | **事件去重**：凡涉及前向 20 个有行情交易日收益统计，先逐信号算前向窗口结束日，再用 `research_flag_overlapping_signals(ts_code, signal_date, window_end)` 做 **keep-first 重叠链折叠**；同一证券窗口相交即同一事件；须同时报**原始信号数**与**去重事件数**。单日横截面读数不适用去重，原始信号数＝去重事件数。 |
| L0-13 | **逐层最小停留 2 个有行情交易日（v2.3：仅 L5 观察层、L6 注意层）**：dwell(t) ≥ 2，即 t ≥ t₀+1（t₀＝进入该层当日＝第 1 日），第 2 个有行情交易日（t₀+1）起方可进下一层；**该两层不得当日进、当日转出，不得同日跳层**。计数由当日往前回溯重放该层在册状态逐日累计，**中途跌出即清零，重入 t₀ 重置从 1 重算**；窗口为有行情交易日，停牌不计入、不插值、不占天数。**L4 跟踪层不受本约束**（L3→L4、L4→L5 均可当日完成）。 |

> 计数说明（按文本直读）：**保留／累积窗口（L0-8）与最小停留（L0-13）是两套独立计数** —— 前者中断不清零，后者跌出清零。如有异议回一句即改。

---

### 二、逐层标准 L1–L9

| 层 | 在册来源 | 该层条件（只看本层） | 时效 |
|---|---|---|---|
| **L1 集合M** | 全市场 | ①剔 68／9 开头 ②剔 .BJ ③`daily_basic.close`（未复权，元）≤160 ④0<`pe_ttm`≤cap 段 ⑤非 ST（`stock_basic.name` 不含 ST／\*ST／退）⑥`list_date`≤基准日−365 **自然日**（含边界） | 单日截面，每股唯一一行 |
| **L2 基础层** | 当日 L1 | 同花顺三级行业（**884xxx**）：n＝该行业当日集合M只数，k=(3n+9)//10；`daily.amount`（千元）降序前 k ∪ `daily_basic.turnover_rate`（%）降序前 k，并列按 ts_code 升序取满 k；两榜**并集** | 每日重算 |
| **L3 激活层** | 当日 L2 | 前复权 cond：qf>MA20 且 MA5>MA20 且 MA20>MA60；闸门严格执行，须**真实处于** L2 行业前 k 名额内 | 每日重算 |
| **L4 跟踪层** | 当日 L3 | 最近 **10 个有行情交易日（含端点）**每日 cond 均成立（连续口径，cnt10＝10） | 每日重算（当日回溯重放） |
| **L5 观察层** | 当日 L4 | 在册期间发生 MA5 下穿 MA20（MA5(t)<MA20(t) 且 MA5(t−1)≥MA20(t−1)），**下穿当日须处于跟踪层**；t₀＝下穿日 | 保留 20 个有行情交易日（L0-8）；**须 dwell5 ≥2 方进 L6（L0-13）** |
| **L6 注意层** | 当日 L5 | qf(t) ≥ MA5(t) 且 MA5(t) ≥ MA5(t−1) | t₀＝首次进入注意层之日，累积 20 个有行情交易日到期剔除，同周期不回补；**须 dwell6 ≥2 方进 L7（L0-13）** |
| **L7 目标层** | 当日 L6 | 先过 60 线 → dwell6≥2 →（① qf>MA5>MA20 且 MA5、MA20 双上行；或 ② qf>MA20>MA5 且双上行）→ VR≥1.5 | 1 个有行情交易日（当日，L0-9／L0-10） |
| **L7r 参照层** | 当日 L7 | 原样通过、不加筛选，仅挂双超额与 RS | 同 L7 |
| **L8 风险警示层** | 当日 L7 | 先过 60 线，再扫**近 20 个有行情交易日（t−19…t，含端点）**利空代理 **≥1** 命中 | 同 L7 |
| **L9 预期收益层** | 当日 L8 ∩ L7r | 过 60 线＋每三级行业 ≤2 只、每只等额组合 | 同 L7；预期收益＝前向 20 个有行情交易日（t…t+19，含端点）等权收益 |

**L7 完整判定顺序（v2.3 汇总）**
1. **N1 60 线**：r60＝MA60(t)／MA60(t−1) ≥ 0.999（前复权 qf、未舍入、相邻有行情交易日）。
2. **dwell6 ≥ 2**：t ≥ t₀L6+1（t₀L6＝首次进入注意层当日）；因 L5 亦受 L0-13 约束，实到 L7 的路径上 L5 也须 ≥2 日。
3. **形态**：① qf(t)>MA5(t)>MA20(t) 且 MA5、MA20 双上行；或 ② qf(t)>MA20(t)>MA5(t) 且 MA5、MA20 双上行。
4. **VR(t) ≥ 1.5（含边界）**：`daily.amount`(t) ÷ mean{`daily.amount`(t−5…t−1)}；分子＝t 日千元未复权原始成交额（**不使用 `adj_factor`**），分母＝前 5 个有行情交易日（含两端、**不含当日**）简单平均；前 5 日不足 → 直接不通过，不以更短窗口替代。
（L7-④「跟踪层停留满 5 个有行情交易日」已于 v2.1 **整条作废**，本版不恢复。）

---

### 三、L1 四段市值 × PE_TTM 上限

分界左闭右开：段2=[100,300)、段3=[300,1000)、段4=[1000,＋∞)，单位亿元。

| 市值段 | PE_TTM 上限 |
|---|---|
| < 100 亿 | 0 < PE_TTM ≤ 100 |
| 100–300 亿 | 0 < PE_TTM ≤ 100 |
| 300–1000 亿 | 0 < PE_TTM ≤ 110 |
| ≥ 1000 亿 | 0 < PE_TTM ≤ 200 |

**PE_TTM 空值、≤0 一律不通过。**

---

### 四、行业体系与资金流层级

- **基础层行业归属与排名**＝同花顺三级行业 **884xxx**（一股多板块按「成分数最多、并列取板块代码最小」唯一归属）。
- **板块资金流**＝同花顺二级行业 **881xxx**（内容板块另有概念板块层级）；**三级与二级不可相加**，任何资金流数字须标注所用层级，并按**相邻交易日**、文档化的**净流字段**比较。

---

### 五、派生指标口径（逐项：输入／序列／窗口／去重）

| 指标 | 输入字段 | 价格序列与复权基础 | 窗口语义 | 备注 |
|---|---|---|---|---|
| **mv_yi** | `daily_basic.total_mv`（万元）÷10000 | 无价格 | 单日快照 | 分段 1<100亿；2=[100,300)；3=[300,1000)；4≥1000亿 |
| **cap** | 由 mv_yi 分段 | 无价格 | 单日快照 | <300亿→100 倍；[300,1000)→110 倍；≥1000亿→200 倍 |
| **pe_ttm** | `daily_basic.pe_ttm` | 无价格 | 单日快照 | 空值不填补，直接不通过 |
| **close_未复权** | `daily_basic.close`（元） | 未复权 | 单日快照 | 仅用于 L1 ≤160 元门槛与展示；**不用于均线** |
| **qf 前复权价** | `daily.close`（元）× 当日 `adj_factor` | 前复权，基期不归一 | 逐有行情交易日，停牌不插值 | 均线唯一基础序列 |
| **MA5／20／60** | qf | 前复权 | 截至 t 日最近 5／20／60 个有行情交易日（含端点）简单均值，min_periods＝窗口长度，未舍入 | 交易日窗口，非自然日 |
| **cond** | qf、MA5、MA20、MA60 | 前复权 | 单日判定 | qf>MA20 且 MA5>MA20 且 MA20>MA60 |
| **cnt10** | cond | 前复权 | 最近 10 个有行情交易日（含端点） | 连续口径定长 10 日，未舍入 |
| **r60／chg60** | MA60 | 前复权 | 相邻有行情交易日 t／t−1 | r60＝MA60(t)／MA60(t−1)，阈值 ≥0.999 |
| **dwell5／dwell6** | 各层逐日回溯重放的在册标记 | 前复权（承接各层条件） | 有行情交易日，进入当日＝第 1 日，含端点 | 中断跌出清零、重入从 1 重算（L0-13） |
| **L5 下穿** | MA5、MA20 | 前复权 | 相邻有行情交易日组合判定 | MA5(t)<MA20(t) 且 MA5(t−1)≥MA20(t−1)，且下穿日在跟踪层在册 |
| **L2 行业内名次** | `daily.amount`（千元）、`daily_basic.turnover_rate`（%） | 成交额与换手率均**不复权** | 单日截面，按同花顺三级行业 884xxx 分组 | 两榜降序前 k，并列按 ts_code 升序，取并集；k=(3n+9)//10 |
| **VR（L7-③）** | `daily.amount`(t)、`daily.amount`(t−5…t−1) | 未复权原始成交额，**不使用 adj_factor** | 交易日窗口，前 5 日含两端、不含当日 | 比值无量纲；≥1.5 含边界；不足 5 日不通过 |
| **前向 20 日收益** | qf | 前复权 | ［t, t+19］共 20 个有行情交易日（含两端） | 个股等权收益＝qf(t+19)／qf(t)−1，不含分红再投；t＝信号日 |
| **双超额** | 个股收益、基准收益 | 前复权；基准为等权收益 | 截至 t 日区间 | 基准①＝信号日当日集合M全成分等权；基准②＝信号日所属同花顺三级行业当日集合M成分等权 |
| **L1 上市判定** | `stock_basic.list_date` | 无价格 | **自然日**，含边界 | 模型内唯一自然日窗口：list_date ≤ 基准日−365 |
| **事件去重** | ts_code、signal_date、window_end | 承接前向 20 日收益 | 前向窗口相交即同一事件 | keep-first 折叠；须并报原始信号数与去重事件数 |

---

### 六、已作废／被取代的条款

- 原**月度锁定**（L1／L2 月内不新增不替换）→ 由 L0-7 取代。
- 原**跨日携带在册状态**（含 L4⊆基础层旧来源、L5／L6 逐日在册推进）→ 作废；L4 在册来源已修订为**激活层 L3**。
- 原**L7-④ 跟踪层停留满 5 个有行情交易日**（含配套「L4 停留连续在册、跌出清零」）→ v2.1 **整条作废**。
- 原**L0-13 全链 L3→L7 均须 ≥2 日**（v2.0）→ v2.2 收窄为 L4／L6 → **v2.3 改挂 L5／L6，dwell4 门槛不再存在**。
- 由此，此前按当日横截面或按在册推进算出的读数（0924 激活 379／跟踪 71、0804 的 18／3、0604 的 77／7、16 个重估点只数表、302132／002274／002246 层级判定等）**均非 v2.3 标准读数**，须逐日全流程重跑后方可引用。

---

### 七、两处开口（未留明文，须补录才可上线统计）

1. **L1 四段市值×PE_TTM 阈值**：现文本为按历史读数反推校准的 v1.0（辨识度验证点：剔北交所后候选池恰为 4,295 只），非原始分段文本。敏感性（0924 截面）：段3 上限 100→2,446、120→2,455；段2 上限 110→2,470；段4 上限 150→2,451、300→2,453；仅要求 PE>0 不设上限→3,101。
2. **L8 八类利空代理清单**：已具名 7 类 —— 业绩预告（下修／预减／首亏／续亏／略减）、业绩快报不及预期、股东减持、限售解禁、大宗交易、跌停（涨停不计利空）、研报评级或目标价下调；**第 8 类未在记录中留名**。
3. （附带）**L7r 的 RS 定义**与 **L9 每三级行业 >2 只时的取舍规则**亦未留明文，仅「双超额（相对集合M等权、相对所属三级行业等权）」已明确。

---

标准总本按上述文本锁定（其余条款不动）。此后引用任何层级数字，均须以「每日全流程执行、由当日往前回溯重放 20 日窗口」的口径重跑获得，并在同一答复内标注数据基准日与是否为原始信号数／去重事件数。

下一步回一句即可：回「**出标准**」→ 导出 v2.3 标准本 Excel（标准页＋口径页＋逐列中文说明，不含测试数据）；回「**跑**」→ 按 2026-09-01 → 2026-09-24 逐日全流程推进，输出各层在册名单、逐日进出与执行段事件（含原始信号数／去重事件数）。"""


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ANALYSIS") != "1",
    reason="Set RUN_LIVE_ANALYSIS=1 to call the configured model and Tushare.",
)
def test_live_feishu_agent_judges_reported_historical_level_by_backward_anchor():
    """Reported regression (2026-09-26): asked which model level 603823 held
    on the historical date 2026-08-10, the agent re-tested the stage-1 entry
    screen against the query date to veto the whole historical chain and
    silently executed a stage condition that contradicts its own trigger
    day."""
    runtime = create_feishu_agent_runtime(Settings.from_env())

    outcome = runtime.run(
        FeishuAgentRequest(
            prompt="603823，在8.10这一天 属于哪一层",
            conversation_id="live:feishu:agent:reported-level-backward-anchor",
            source_message_id="live-reported-level-backward-anchor",
            conversation=[
                FeishuAgentConversationTurn(
                    prompt="现在，请给出模型标准",
                    answer=REPORTED_LEVEL_MODEL_STANDARD_V23,
                )
            ],
        ),
        lambda _stage, _message: None,
    )

    answer = outcome.answer
    # The reported failure shape vetoed the historical chain purely because
    # the query date failed the stage-1 entry screen.
    assert not re.search(r"唯一阻断点|即被剔除", answer)
    asked_clarification = "请回复序号" in answer and "推荐" in answer
    backward_anchor = any(
        token in answer for token in ("倒推", "锚定", "最近一次")
    )
    assert asked_clarification or backward_anchor
    if not asked_clarification:
        # Executing the model must surface the same-day contradiction instead
        # of silently running an impossible stage condition.
        assert any(
            token in answer
            for token in ("矛盾", "不可能", "必不满足", "前一交易日", "前一日")
        )


def test_research_workbook_renders_compact_calendar_dates_not_grouped_numbers():
    from china_a_share.feishu_agent import compact_calendar_date_text

    assert compact_calendar_date_text("最近大涨日", 20260917) == "2026-09-17"
    assert compact_calendar_date_text("trade_date", "20260917 ") == "2026-09-17"
    assert compact_calendar_date_text("上市时间", 20260917) == "2026-09-17"
    assert compact_calendar_date_text("上市日期", 20261345) == 20261345
    assert compact_calendar_date_text("市值_元", 20260917) == 20260917
    assert compact_calendar_date_text("最近大涨日", 708) == 708

    result = QueryResult(
        query_id="ranked",
        provider="tushare",
        operation="daily",
        status=QueryStatus.SUCCESS,
        columns=["代码", "名称", "最近大涨日", "市值_元"],
        rows=[
            {
                "代码": "002487.SZ",
                "名称": "大金重工",
                "最近大涨日": 20260917,
                "市值_元": 20260917,
            }
        ],
        row_count=1,
        completeness="complete",
    )

    path = build_research_workbook(
        result,
        "大涨日触发统计",
        "未复权日线口径。",
        column_notes={
            "代码": "A股证券代码，含交易所后缀。",
            "名称": "证券简称。",
            "最近大涨日": "窗口内最近一次收盘涨幅达到5%的交易日。",
            "市值_元": "收盘总市值，单位为元。",
        },
    )

    results = load_workbook(path, data_only=False)["Results"]
    assert results["C6"].value == "2026-09-17"
    assert results["D6"].value == 20260917

"""Broker-research access, dataset recency metadata, and withdrawn replies.

These invariants generalize the production incident behind the CATL research
question: an entitled broker-report operation existed but was unaudited, stale
event-triggered guidance posed as the newest fact, and withdrawn source
messages crashed the worker instead of cancelling the task.
"""

import pytest

import pandas as pd

from china_a_share.capabilities import get_operation_capability, resolve_query_shape
from china_a_share.core.contracts import (
    AnalysisTaskStatus,
    QueryResult,
    QueryStatus,
)
from china_a_share.feishu import (
    FEISHU_MESSAGE_WITHDRAWN_CODE,
    FeishuOpenApiClient,
    FeishuSourceMessageWithdrawnError,
)
from china_a_share.feishu_agent import (
    FeishuAgentCoordinator,
    FeishuAgentOutcome,
    FeishuAgentRequest,
    _result_payload,
)
from china_a_share.glm_agent import GLM_RECOVERY_INSTRUCTIONS
from china_a_share.providers.eastmoney import (
    EASTMONEY_OPERATION_GUIDANCE,
    EastmoneyDataProvider,
)
from china_a_share.registry import data_recency_note, TushareOperationCatalog
from china_a_share.tasks import MemoryAnalysisTaskStore


def guidance_for(operation: str) -> str:
    catalog = TushareOperationCatalog()
    tushare_guidance = next(
        (
            item.description
            for item in catalog.search("研报")
            if item.name == operation
        ),
        "",
    )
    return tushare_guidance or EASTMONEY_OPERATION_GUIDANCE.get(operation, "")


def test_broker_reports_has_audited_query_shapes():
    capability = get_operation_capability("broker_reports")

    assert capability is not None
    assert capability.allowed_params == (
        "ts_code",
        "start_date",
        "end_date",
        "limit",
        "offset",
    )
    security = resolve_query_shape("broker_reports", {"ts_code": "300750.SZ"})
    bounded = resolve_query_shape(
        "broker_reports",
        {"ts_code": "300750.SZ", "start_date": "20260701", "end_date": "20260920"},
    )
    assert security.shape_id == "security"
    assert bounded.shape_id == "bounded_security_range"
    assert security.execution_strategy == "provider_query"


def test_broker_reports_rejects_unaudited_parameters():
    with pytest.raises(ValueError, match="requires start_date and end_date together"):
        resolve_query_shape(
            "broker_reports",
            {"ts_code": "300750.SZ", "start_date": "20260701"},
        )
    with pytest.raises(ValueError, match="audited query shape"):
        resolve_query_shape("broker_reports", {})


def test_broker_reports_guidance_documents_units_and_call_discipline():
    guidance = guidance_for("broker_reports")

    assert "Unit note" in guidance
    assert "CNY per share (元)" in guidance
    assert "target-price bounds" in guidance
    assert "ONE" in guidance
    assert "券商研报" in guidance
    assert "filter report_date windows" in guidance


def test_event_triggered_guidance_documents_recency_boundary():
    forecast = guidance_for("forecast")
    express = guidance_for("express")

    assert "event-triggered" in forecast
    assert "fina_indicator" in forecast
    assert "never as the company's current state" in forecast
    assert "event-triggered" in express
    assert "fina_indicator" in express


def test_data_recency_note_reports_stale_latest_record():
    note = data_recency_note(
        ["ts_code", "ann_date", "type"],
        [
            {"ts_code": "300750.SZ", "ann_date": "20250121", "type": "略增"},
            {"ts_code": "600519.SH", "ann_date": "20240730", "type": "预增"},
        ],
        "20260921",
    )

    assert note == {
        "date_field": "ann_date",
        "latest_record": "2025-01-21",
        "as_of": "2026-09-21",
        "age_days": 608,
    }


def test_data_recency_note_prefers_disclosure_dates_over_reporting_periods():
    note = data_recency_note(
        ["ts_code", "end_date", "ann_date"],
        [{"ts_code": "000001.SZ", "end_date": "20260630", "ann_date": "20260829"}],
        "2026-09-21",
    )

    assert note["date_field"] == "ann_date"
    assert note["latest_record"] == "2026-08-29"
    assert note["age_days"] == 23


def test_data_recency_note_skips_rows_without_identifiable_dates():
    assert data_recency_note(["ann_date"], [{"ann_date": None}], "20260921") is None
    assert (
        data_recency_note(["ts_code", "industry"], [{"ts_code": "000001.SZ"}], "20260921")
        is None
    )


def _query_result(operation: str, columns: list, rows: list) -> QueryResult:
    return QueryResult(
        query_id="recency-check",
        provider="tushare",
        operation=operation,
        status=QueryStatus.SUCCESS,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        completeness="complete",
        completeness_evidence=["test"],
    )


def test_dataset_payload_embeds_data_recency_for_stale_guidance():
    payload = _result_payload(
        _query_result(
            "forecast",
            ["ts_code", "ann_date", "type"],
            [{"ts_code": "300750.SZ", "ann_date": "20250121", "type": "略增"}],
        ),
        as_of="20260921",
    )

    assert payload["data_recency"]["age_days"] == 608
    assert payload["data_recency"]["latest_record"] == "2025-01-21"


def test_dataset_payload_embeds_data_recency_for_fresh_market_data():
    payload = _result_payload(
        _query_result(
            "daily",
            ["ts_code", "trade_date", "close"],
            [{"ts_code": "300750.SZ", "trade_date": "20260918", "close": 301.95}],
        ),
        as_of="20260921",
    )

    assert payload["data_recency"]["age_days"] == 3
    assert payload["data_recency"]["date_field"] == "trade_date"


def test_dataset_payload_omits_data_recency_without_date_columns():
    payload = _result_payload(
        _query_result(
            "stock_basic",
            ["ts_code", "industry"],
            [{"ts_code": "000001.SZ", "industry": "银行"}],
        ),
        as_of="20260921",
    )

    assert "data_recency" not in payload


def test_runtime_prompts_carry_generic_recency_discipline_only():
    from china_a_share.codex_agent import _developer_instructions

    codex_instructions = _developer_instructions()

    for prompt in (codex_instructions, GLM_RECOVERY_INSTRUCTIONS):
        assert "Recency discipline" in prompt
        assert "data_recency" in prompt
        # Catalog-level facts stay in the interface layer, never in prompts.
        assert "report_rc" not in prompt
        assert "研报" not in prompt


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


def test_feishu_withdrawn_code_maps_to_dedicated_error():
    with pytest.raises(FeishuSourceMessageWithdrawnError):
        FeishuOpenApiClient._raise_for_feishu_error(
            FakeResponse(
                {
                    "code": FEISHU_MESSAGE_WITHDRAWN_CODE,
                    "msg": "The message was withdrawn.",
                },
                status_code=400,
            ),
            "message reply",
        )


class RecordingDispatcher:
    def __init__(self):
        self.task_ids = []

    def dispatch(self, task_id):
        self.task_ids.append(task_id)


class WithdrawnReplySink:
    def __init__(self):
        self.reply_calls = 0

    def reply(self, _message_id, _text):
        self.reply_calls += 1
        raise FeishuSourceMessageWithdrawnError(
            "Feishu message reply rejected because the source message was withdrawn."
        )

    def update(self, _message_id, _text):
        raise AssertionError("update must not be attempted after withdrawal")

    def reply_file(self, _message_id, _path):
        raise AssertionError("file reply must not be attempted after withdrawal")


def test_withdrawn_source_message_cancels_task_without_crashing():
    store = MemoryAnalysisTaskStore()
    coordinator = FeishuAgentCoordinator(store, RecordingDispatcher())
    request = FeishuAgentRequest(
        prompt="你能查到宁德时代最近的研报观点吗？",
        conversation_id="tenant:chat:root:user",
        source_message_id="message-1",
    )
    task = coordinator.submit(request, task_id="agent-task")

    class NeverInvokedRuntime:
        def run(self, _request, _progress):
            raise AssertionError("runtime must not run once the source is withdrawn")

    sink = WithdrawnReplySink()
    completed = coordinator.run(task.task_id, NeverInvokedRuntime(), sink)

    assert completed.status == AnalysisTaskStatus.FAILED
    assert completed.stage == "cancelled"
    assert completed.progress_message == "提问消息已撤回，研究已取消。"
    assert sink.reply_calls == 1


class WithdrawnMidRunSink:
    def __init__(self):
        self.updates = 0

    def reply(self, _message_id, _text):
        return "progress-message-1"

    def update(self, _message_id, _text):
        self.updates += 1
        raise FeishuSourceMessageWithdrawnError(
            "Feishu message update rejected because the source message was withdrawn."
        )

    def reply_file(self, _message_id, _path):
        raise FeishuSourceMessageWithdrawnError(
            "Feishu file reply rejected because the source message was withdrawn."
        )


def test_withdrawn_mid_run_message_preserves_completed_research():
    store = MemoryAnalysisTaskStore()
    coordinator = FeishuAgentCoordinator(store, RecordingDispatcher())
    request = FeishuAgentRequest(
        prompt="统计最近交易日的上涨家数。",
        conversation_id="tenant:chat:root:user",
        source_message_id="message-1",
    )
    task = coordinator.submit(request, task_id="agent-task")

    class CompletingRuntime:
        def run(self, _request, progress):
            progress("querying", "正在查询市场数据…")
            return FeishuAgentOutcome(answer="研究完成。")

    sink = WithdrawnMidRunSink()
    completed = coordinator.run(task.task_id, CompletingRuntime(), sink)

    assert completed.status == AnalysisTaskStatus.SUCCEEDED
    assert completed.answer == "研究完成。"
    assert completed.error is None
    assert completed.progress_message == "研究完成。"
    # Delivery stopped after the first withdrawn update instead of failing the task.
    assert sink.updates == 1


class FakeEastmoneyResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class FakeEastmoneySession:
    def __init__(self, payload):
        self.headers = {}
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return FakeEastmoneyResponse(self.payload)


def _eastmoney_provider(payload):
    from china_a_share.cache import (
        DEFAULT_L1_MAX_BYTES,
        DEFAULT_L1_MAX_ENTRIES,
        LayeredDataResponseCache,
        MemoryDataCacheStore,
        NoopDataCacheStore,
    )
    from china_a_share.providers.eastmoney import EastmoneyCacheExpirationPolicy

    session = FakeEastmoneySession(payload)
    provider = EastmoneyDataProvider(
        LayeredDataResponseCache(
            memory_store=MemoryDataCacheStore(
                max_entries=DEFAULT_L1_MAX_ENTRIES,
                max_bytes=DEFAULT_L1_MAX_BYTES,
            ),
            persistent_store=NoopDataCacheStore(),
            expiration_policy=EastmoneyCacheExpirationPolicy(),
        ),
        session=session,
    )
    return provider, session


def test_eastmoney_provider_normalizes_broker_report_rows():
    provider, _session = _eastmoney_provider(
        {
            "hits": 1,
            "data": [
                {
                    "stockCode": "300750",
                    "stockName": "宁德时代",
                    "publishDate": "2026-07-31 00:00:00.000",
                    "title": "业绩维持快速增长，回购彰显发展信心",
                    "orgSName": "国信证券",
                    "emRatingName": "增持",
                    "indvAimPriceT": "",
                    "indvAimPriceL": "",
                    "predictThisYearEps": "20.8300",
                    "predictNextYearEps": "25.96",
                    "predictNextTwoYearEps": None,
                    "researcher": "王蔚祺,李全",
                }
            ],
        }
    )

    frame = provider.query(
        "broker_reports",
        {"ts_code": "300750.SZ"},
        [],
        api_route="/test",
        request_id="request-1",
        query_id="query-1",
    )

    row = frame.iloc[0]
    assert row["ts_code"] == "300750.SZ"
    assert row["report_date"] == "20260731"
    assert row["org_name"] == "国信证券"
    assert row["rating"] == "增持"
    assert row["this_year_eps"] == 20.83
    assert row["next_year_eps"] == 25.96
    # Empty upstream strings become nulls instead of fake zero values.
    assert pd.isna(row["max_price"])
    assert pd.isna(row["min_price"])
    assert pd.isna(row["year_after_next_eps"])


def test_eastmoney_provider_bounds_request_window_and_reuses_cache():
    provider, session = _eastmoney_provider({"hits": 0, "data": []})

    for _ in range(2):
        provider.query(
            "broker_reports",
            {
                "ts_code": "300750.SZ",
                "start_date": "20260701",
                "end_date": "20260920",
            },
            [],
            api_route="/test",
            request_id="request-1",
            query_id="query-1",
        )

    assert len(session.calls) == 1
    url, params = session.calls[0]
    assert url.endswith("/report/list")
    assert params["code"] == "300750"
    assert params["beginTime"] == "2026-07-01"
    assert params["endTime"] == "2026-09-20"
    assert params["qType"] == 0


def test_eastmoney_provider_rejects_operations_it_does_not_own():
    provider, _session = _eastmoney_provider({"hits": 0, "data": []})

    with pytest.raises(ValueError, match="Unsupported Eastmoney operation"):
        provider.query(
            "daily",
            {"ts_code": "300750.SZ"},
            [],
            api_route="/test",
            request_id="request-1",
            query_id="query-1",
        )

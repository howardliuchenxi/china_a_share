"""Codex-style tool-using research runtime for the Feishu channel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import tempfile
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
import requests

from china_a_share.capabilities import resolve_query_shape
from china_a_share.core.contracts import (
    AnalysisTaskStatus,
    QueryResult,
    QueryStatus,
    ResultPipeline,
    ServiceError,
)
from china_a_share.result_pipeline import ResultPipelineExecutor


DEEPSEEK_AGENT_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_AGENT_MODEL = "deepseek-v4-pro"
DEEPSEEK_AGENT_TIMEOUT_SECONDS = 180
MAX_AGENT_ROUNDS = 12
MAX_AGENT_PREVIEW_ROWS = 20
MAX_AGENT_CONTEXT_TURNS = 12
MAX_RECENT_RETURN_SESSIONS = 60
RECENT_RETURN_CALENDAR_BUFFER_DAYS = 14
logger = logging.getLogger(__name__)


class FeishuAgentConversationTurn(BaseModel):
    """One completed user and assistant exchange retained by a Feishu session."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=4_000)
    answer: str = Field(min_length=1, max_length=12_000)


class FeishuAgentRequest(BaseModel):
    """Immutable input required to execute one Feishu agent turn."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=4_000)
    conversation_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    conversation: List[FeishuAgentConversationTurn] = Field(
        default_factory=list,
        max_length=MAX_AGENT_CONTEXT_TURNS,
    )


class FeishuAgentTask(BaseModel):
    """Durable lifecycle and output for one independent Feishu agent turn."""

    model_config = ConfigDict(extra="forbid")

    task_type: Literal["feishu_agent"] = "feishu_agent"
    task_id: str = Field(min_length=1)
    status: AnalysisTaskStatus
    request: FeishuAgentRequest
    created_at: datetime
    updated_at: datetime
    stage: str = Field(default="queued", min_length=1)
    progress_message: str = Field(default="等待研究任务启动。", min_length=1)
    answer: Optional[str] = None
    artifact_name: Optional[str] = None
    error: Optional[ServiceError] = None


class AgentTaskStore(Protocol):
    """Persist complete Feishu agent task records."""

    def get(self, task_id: str) -> Optional[Any]:
        """Return one task when it exists."""

    def put(self, task: FeishuAgentTask) -> None:
        """Create or replace one task."""


class AgentTaskDispatcher(Protocol):
    """Dispatch one persisted Feishu agent task to a worker."""

    def dispatch(self, task_id: str) -> None:
        """Start asynchronous execution for one task."""


class AgentProgressSink(Protocol):
    """Deliver stage changes and terminal artifacts to Feishu."""

    def reply(self, message_id: str, text: str) -> None:
        """Reply with one progress or answer message."""

    def reply_file(self, message_id: str, path: Path) -> None:
        """Upload and reply with one generated file."""


class MarketDataProvider(Protocol):
    """Expose the read-only provider surface available to the agent tools."""

    @property
    def name(self) -> str:
        """Return the provider identifier."""

    def search_operations(self, prompt: str) -> Any:
        """Return matching read-only provider operations."""

    def supports(self, operation: str) -> bool:
        """Return whether one operation is allowlisted."""

    def query(
        self,
        operation: str,
        params: Dict[str, Any],
        fields: List[str],
        *,
        api_route: str,
        request_id: str,
        query_id: str,
    ) -> pd.DataFrame:
        """Execute one audited read-only provider query."""


class FeishuAgentCoordinator:
    """Submit independent Feishu turns without conversation-level serialization."""

    def __init__(self, store: AgentTaskStore, dispatcher: AgentTaskDispatcher) -> None:
        self._store = store
        self._dispatcher = dispatcher

    def submit(
        self,
        request: FeishuAgentRequest,
        *,
        task_id: Optional[str] = None,
    ) -> FeishuAgentTask:
        """Persist and dispatch an idempotent agent task."""
        task_id = task_id or uuid4().hex
        existing = self._store.get(task_id)
        if existing is not None:
            if not isinstance(existing, FeishuAgentTask) or existing.request != request:
                raise ValueError(f"Agent task identifier is already in use: {task_id}")
            return existing
        now = datetime.now(timezone.utc)
        task = FeishuAgentTask(
            task_id=task_id,
            status=AnalysisTaskStatus.QUEUED,
            request=request,
            created_at=now,
            updated_at=now,
        )
        self._store.put(task)
        try:
            self._dispatcher.dispatch(task_id)
        except Exception as exc:
            logger.exception("feishu_agent_dispatch_failed task_id=%s", task_id)
            task.status = AnalysisTaskStatus.FAILED
            task.stage = "failed"
            task.progress_message = "研究任务未能启动。"
            task.error = ServiceError(source="system", message=str(exc))
            task.updated_at = datetime.now(timezone.utc)
            self._store.put(task)
            raise
        return task

    def get(self, task_id: str) -> Optional[FeishuAgentTask]:
        """Return one Feishu agent task when it exists."""
        task = self._store.get(task_id)
        return task if isinstance(task, FeishuAgentTask) else None

    def run(
        self,
        task_id: str,
        runtime: "FeishuAgentRuntime",
        progress_sink: AgentProgressSink,
    ) -> FeishuAgentTask:
        """Execute one queued task and proactively report material stages."""
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"Feishu agent task does not exist: {task_id}")
        if task.status in {AnalysisTaskStatus.RUNNING, AnalysisTaskStatus.SUCCEEDED}:
            return task

        def report(stage: str, message: str) -> None:
            task.stage = stage
            task.progress_message = message
            task.updated_at = datetime.now(timezone.utc)
            self._store.put(task)
            progress_sink.reply(task.request.source_message_id, message)

        task.status = AnalysisTaskStatus.RUNNING
        task.error = None
        report("planning", "正在理解问题并选择研究工具…")
        try:
            outcome = runtime.run(task.request, report)
            task.answer = outcome.answer
            task.artifact_name = (
                outcome.artifact_path.name if outcome.artifact_path is not None else None
            )
            task.status = AnalysisTaskStatus.SUCCEEDED
            task.stage = "completed"
            task.progress_message = "研究完成。"
            task.updated_at = datetime.now(timezone.utc)
            self._store.put(task)
            progress_sink.reply(task.request.source_message_id, outcome.answer)
            if outcome.artifact_path is not None:
                progress_sink.reply_file(
                    task.request.source_message_id,
                    outcome.artifact_path,
                )
        except Exception as exc:
            logger.exception("feishu_agent_execution_failed task_id=%s", task_id)
            task.status = AnalysisTaskStatus.FAILED
            task.stage = "failed"
            task.progress_message = "研究任务失败。"
            task.error = ServiceError(source="system", message=str(exc))
            task.updated_at = datetime.now(timezone.utc)
            self._store.put(task)
            progress_sink.reply(
                task.request.source_message_id,
                f"研究任务失败：{exc}",
            )
        return task


class FeishuAgentOutcome(BaseModel):
    """Terminal conversational answer and optional generated workbook."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    answer: str = Field(min_length=1)
    artifact_path: Optional[Path] = None


class ResearchToolbox:
    """Own audited datasets and deterministic transformations for one agent turn."""

    def __init__(self, provider: MarketDataProvider, request_id: str) -> None:
        self._provider = provider
        self._request_id = request_id
        self._datasets: Dict[str, QueryResult] = {}
        self.artifact_path: Optional[Path] = None

    @property
    def definitions(self) -> List[Dict[str, Any]]:
        """Return the bounded function tools advertised to DeepSeek."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_market_data",
                    "description": "Find relevant allowlisted Tushare operations before querying.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "query_market_data",
                    "description": "Execute one read-only Tushare operation and retain the complete dataset.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "operation": {"type": "string"},
                            "params": {"type": "object"},
                            "fields": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["operation", "params", "fields"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "rank_recent_market_return",
                    "description": (
                        "Rank all listed A-shares by compounded percentage return over "
                        "the latest N completed market trading sessions. Use this tool "
                        "for recent multi-session gain, loss, or return rankings; it "
                        "compounds the provider's official daily pct_chg values and "
                        "does not use an adjusted-price series or block-trade records."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "trading_sessions": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": MAX_RECENT_RETURN_SESSIONS,
                            },
                            "direction": {
                                "type": "string",
                                "enum": ["asc", "desc"],
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 1000,
                            },
                            "end_date": {
                                "type": "string",
                                "description": (
                                    "Optional inclusive YYYYMMDD upper bound. Omit for "
                                    "the latest completed market data."
                                ),
                            },
                        },
                        "required": ["trading_sessions", "direction", "limit"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "rank_dataset",
                    "description": "Sort a retained dataset, keep a bounded top or bottom set, and optionally select output fields.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "dataset_id": {"type": "string"},
                            "sort_by": {"type": "string"},
                            "direction": {"type": "string", "enum": ["asc", "desc"]},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                            "fields": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["dataset_id", "sort_by", "direction", "limit", "fields"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "join_datasets",
                    "description": "Join two retained datasets on explicit key fields using validated many-to-one or one-to-one semantics.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "left_dataset_id": {"type": "string"},
                            "right_dataset_id": {"type": "string"},
                            "join_on": {"type": "array", "items": {"type": "string"}},
                            "right_fields": {"type": "array", "items": {"type": "string"}},
                            "cardinality": {"type": "string", "enum": ["one_to_one", "many_to_one"]},
                        },
                        "required": ["left_dataset_id", "right_dataset_id", "join_on", "right_fields", "cardinality"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "transform_dataset",
                    "description": (
                        "Apply an allowlisted ResultPipeline to retained datasets. "
                        "Use source_query_id and output_query_id plus 1-16 validated steps."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"pipeline": {"type": "object"}},
                        "required": ["pipeline"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "export_excel",
                    "description": "Create a polished two-tab Excel workbook from a retained dataset.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "dataset_id": {"type": "string"},
                            "title": {"type": "string"},
                            "methodology": {"type": "string"},
                        },
                        "required": ["dataset_id", "title", "methodology"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        progress: Callable[[str, str], None],
    ) -> Dict[str, Any]:
        """Execute one named tool or fail fast on an unknown capability."""
        if name == "search_market_data":
            operations = self._provider.search_operations(str(arguments["query"]))
            return {
                "operations": [
                    {"name": operation.name, "description": operation.description}
                    for operation in list(operations)[:12]
                ]
            }
        if name == "query_market_data":
            operation = str(arguments["operation"])
            if not self._provider.supports(operation):
                raise ValueError(f"Unsupported market-data operation: {operation}")
            params = dict(arguments["params"])
            if resolve_query_shape(operation, params) is None:
                raise ValueError(
                    f"Operation lacks an audited Feishu agent query shape: {operation}"
                )
            progress("querying", f"正在查询市场数据：{operation}…")
            dataset_id = f"dataset_{len(self._datasets) + 1}"
            fields = [str(field) for field in arguments["fields"]]
            frame = self._provider.query(
                operation,
                params,
                fields,
                api_route="/feishu/agent/tools/query",
                request_id=self._request_id,
                query_id=dataset_id,
            )
            result = _frame_to_result(dataset_id, self._provider.name, operation, frame)
            self._datasets[dataset_id] = result
            return _result_payload(result)
        if name == "rank_recent_market_return":
            return self._rank_recent_market_return(arguments, progress)
        if name == "rank_dataset":
            progress("calculating", "正在执行排序与排名…")
            dataset_id = str(arguments["dataset_id"])
            source = self._datasets.get(dataset_id)
            if source is None:
                raise ValueError(f"Unknown ranking dataset: {dataset_id}")
            sort_by = str(arguments["sort_by"])
            fields = [str(field) for field in arguments["fields"]]
            missing = set([sort_by] + fields).difference(source.columns)
            if missing:
                raise ValueError(
                    "Ranking fields are missing: " + ", ".join(sorted(missing))
                )
            frame = pd.DataFrame(source.rows)
            ranked = frame.sort_values(
                sort_by,
                ascending=str(arguments["direction"]) == "asc",
                kind="mergesort",
                na_position="last",
            ).head(int(arguments["limit"]))
            if fields:
                ranked = ranked.loc[:, fields]
            output_id = f"dataset_{len(self._datasets) + 1}"
            result = _frame_to_result(
                output_id,
                source.provider,
                f"{source.operation}_ranked",
                ranked,
            )
            self._datasets[output_id] = result
            return _result_payload(result)
        if name == "join_datasets":
            progress("calculating", "正在合并市场与财务数据…")
            left = self._datasets.get(str(arguments["left_dataset_id"]))
            right = self._datasets.get(str(arguments["right_dataset_id"]))
            if left is None or right is None:
                raise ValueError("Join references an unknown dataset.")
            join_on = [str(field) for field in arguments["join_on"]]
            right_fields = [str(field) for field in arguments["right_fields"]]
            missing_left = set(join_on).difference(left.columns)
            missing_right = set(join_on + right_fields).difference(right.columns)
            if missing_left or missing_right:
                raise ValueError("Join fields are missing from the retained datasets.")
            cardinality = str(arguments["cardinality"])
            validate = "1:1" if cardinality == "one_to_one" else "m:1"
            merged = pd.DataFrame(left.rows).merge(
                pd.DataFrame(right.rows).loc[:, join_on + right_fields],
                how="inner",
                on=join_on,
                validate=validate,
            )
            output_id = f"dataset_{len(self._datasets) + 1}"
            result = _frame_to_result(
                output_id,
                left.provider,
                f"{left.operation}_joined",
                merged,
            )
            self._datasets[output_id] = result
            return _result_payload(result)
        if name == "transform_dataset":
            progress("calculating", "正在执行确定性筛选与计算…")
            pipeline = ResultPipeline.model_validate(arguments["pipeline"])
            source = self._datasets.get(pipeline.source_query_id)
            if source is None:
                raise ValueError(
                    f"Unknown source dataset: {pipeline.source_query_id}"
                )
            result = ResultPipelineExecutor().execute(
                pipeline,
                source,
                self._datasets,
            )
            self._datasets[result.query_id] = result
            return _result_payload(result)
        if name == "export_excel":
            dataset_id = str(arguments["dataset_id"])
            result = self._datasets.get(dataset_id)
            if result is None:
                raise ValueError(f"Unknown Excel dataset: {dataset_id}")
            progress("exporting", "正在生成 Excel 研究结果…")
            self.artifact_path = build_research_workbook(
                result,
                str(arguments["title"]),
                str(arguments["methodology"]),
            )
            return {"file_name": self.artifact_path.name, "row_count": result.row_count}
        raise ValueError(f"Unknown research tool: {name}")

    def _rank_recent_market_return(
        self,
        arguments: Mapping[str, Any],
        progress: Callable[[str, str], None],
    ) -> Dict[str, Any]:
        """Rank complete full-market returns over recent trading sessions."""
        trading_sessions = int(arguments["trading_sessions"])
        if not 1 <= trading_sessions <= MAX_RECENT_RETURN_SESSIONS:
            raise ValueError(
                "trading_sessions must be between 1 and "
                f"{MAX_RECENT_RETURN_SESSIONS}."
            )
        direction = str(arguments["direction"])
        if direction not in {"asc", "desc"}:
            raise ValueError("direction must be asc or desc.")
        limit = int(arguments["limit"])
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000.")

        raw_end_date = str(arguments.get("end_date") or "").strip()
        if raw_end_date:
            try:
                end_date = datetime.strptime(raw_end_date, "%Y%m%d").date()
            except ValueError as exc:
                raise ValueError("end_date must use YYYYMMDD format.") from exc
        else:
            end_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        lookback_days = trading_sessions * 3 + RECENT_RETURN_CALENDAR_BUFFER_DAYS
        start_date = end_date - timedelta(days=lookback_days)

        progress("querying", "正在查询最近交易日的全市场日行情…")
        daily_frame = self._provider.query(
            "daily",
            {
                "start_date": start_date.strftime("%Y%m%d"),
                "end_date": end_date.strftime("%Y%m%d"),
            },
            ["ts_code", "trade_date", "close", "pct_chg"],
            api_route="/feishu/agent/tools/recent-market-return",
            request_id=self._request_id,
            query_id="recent_market_daily",
        )
        required = {"ts_code", "trade_date", "close", "pct_chg"}
        missing = required.difference(daily_frame.columns)
        if missing:
            raise ValueError(
                "Daily market data is missing required fields: "
                + ", ".join(sorted(missing))
            )

        normalized = daily_frame.copy()
        normalized["trade_date"] = normalized["trade_date"].astype(str)
        normalized["pct_chg"] = pd.to_numeric(
            normalized["pct_chg"], errors="coerce"
        )
        normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")
        normalized = normalized.dropna(subset=["ts_code", "pct_chg", "close"])
        available_dates = sorted(normalized["trade_date"].unique())
        if len(available_dates) < trading_sessions:
            raise ValueError(
                f"Only {len(available_dates)} completed trading sessions were "
                f"available; {trading_sessions} are required."
            )
        selected_dates = available_dates[-trading_sessions:]
        selected = normalized.loc[
            normalized["trade_date"].isin(selected_dates)
        ].copy()
        session_counts = selected.groupby("ts_code")["trade_date"].nunique()
        complete_codes = session_counts.loc[
            session_counts == trading_sessions
        ].index
        selected = selected.loc[selected["ts_code"].isin(complete_codes)]
        if selected.empty:
            raise ValueError("No security has complete data for the selected sessions.")

        progress("calculating", "正在复合计算区间涨跌幅并执行全市场排名…")
        returns = (
            selected.groupby("ts_code", sort=False)["pct_chg"]
            .apply(lambda values: ((1.0 + values / 100.0).prod() - 1.0) * 100.0)
            .rename("period_return_pct")
            .reset_index()
        )
        latest_close = (
            selected.sort_values("trade_date")
            .groupby("ts_code", as_index=False)
            .tail(1)
            .loc[:, ["ts_code", "close"]]
        )
        ranked = returns.merge(latest_close, on="ts_code", validate="one_to_one")

        progress("querying", "正在补充股票名称与行业信息…")
        reference = self._provider.query(
            "stock_basic",
            {"list_status": "L"},
            ["ts_code", "name", "industry"],
            api_route="/feishu/agent/tools/recent-market-return",
            request_id=self._request_id,
            query_id="recent_market_reference",
        )
        if "ts_code" not in reference.columns:
            raise ValueError("Stock reference data is missing ts_code.")
        reference_fields = [
            field for field in ("ts_code", "name", "industry")
            if field in reference.columns
        ]
        ranked = ranked.merge(
            reference.loc[:, reference_fields],
            how="left",
            on="ts_code",
            validate="one_to_one",
        )
        ranked.insert(1, "start_trade_date", selected_dates[0])
        ranked.insert(2, "end_trade_date", selected_dates[-1])
        ranked.insert(3, "trading_session_count", trading_sessions)
        ranked["period_return_pct"] = ranked["period_return_pct"].round(4)
        ranked = ranked.sort_values(
            ["period_return_pct", "ts_code"],
            ascending=[direction == "asc", True],
            kind="mergesort",
        ).head(limit)
        preferred_fields = [
            "ts_code",
            "name",
            "industry",
            "start_trade_date",
            "end_trade_date",
            "trading_session_count",
            "close",
            "period_return_pct",
        ]
        ranked = ranked.loc[
            :, [field for field in preferred_fields if field in ranked.columns]
        ]
        dataset_id = f"dataset_{len(self._datasets) + 1}"
        result = _frame_to_result(
            dataset_id,
            self._provider.name,
            "recent_market_period_return",
            ranked.reset_index(drop=True),
        )
        self._datasets[dataset_id] = result
        payload = _result_payload(result)
        payload["calculation"] = "compound_daily_pct_chg"
        payload["methodology"] = (
            "Compounded the official daily pct_chg values for every security with "
            f"complete observations on all {trading_sessions} selected market "
            "sessions. This is not an adjusted-price return series."
        )
        return payload


class FeishuAgentRuntime:
    """Run a bounded DeepSeek tool loop over audited market-data capabilities."""

    def __init__(
        self,
        api_key: str,
        provider: MarketDataProvider,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._api_key = api_key
        self._provider = provider
        self._session = session or requests.Session()

    def run(
        self,
        request: FeishuAgentRequest,
        progress: Callable[[str, str], None],
    ) -> FeishuAgentOutcome:
        """Return a final answer after at most the configured tool-call rounds."""
        toolbox = ResearchToolbox(self._provider, uuid4().hex)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _agent_system_prompt()},
        ]
        for turn in request.conversation[-MAX_AGENT_CONTEXT_TURNS:]:
            messages.append({"role": "user", "content": turn.prompt})
            messages.append({"role": "assistant", "content": turn.answer})
        messages.append({"role": "user", "content": request.prompt})

        for _round in range(MAX_AGENT_ROUNDS):
            response = self._session.post(
                DEEPSEEK_AGENT_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": DEEPSEEK_AGENT_MODEL,
                    "messages": messages,
                    "tools": toolbox.definitions,
                    "tool_choice": "auto",
                    "temperature": 0,
                    "max_tokens": 8_000,
                },
                timeout=DEEPSEEK_AGENT_TIMEOUT_SECONDS,
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"DeepSeek agent returned HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
            payload = response.json()
            message = payload["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                answer = str(message.get("content") or "").strip()
                if not answer:
                    raise RuntimeError("DeepSeek agent returned an empty answer.")
                return FeishuAgentOutcome(
                    answer=answer,
                    artifact_path=toolbox.artifact_path,
                )
            messages.append(message)
            for tool_call in tool_calls:
                function = tool_call.get("function") or {}
                name = str(function.get("name") or "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                    result = toolbox.call(name, arguments, progress)
                except Exception as exc:
                    result = {"error": str(exc)}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        raise RuntimeError("DeepSeek agent exceeded the bounded tool-call limit.")


def build_research_workbook(
    result: QueryResult,
    title: str,
    methodology: str,
) -> Path:
    """Create one readable two-tab workbook from a complete result dataset."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise RuntimeError("Excel export requires the openpyxl dependency.") from exc

    workbook = Workbook()
    results_sheet = workbook.active
    results_sheet.title = "Results"
    methodology_sheet = workbook.create_sheet("Methodology")
    results_sheet.sheet_view.showGridLines = False
    methodology_sheet.sheet_view.showGridLines = False

    results_sheet["A2"] = title
    results_sheet["A2"].font = Font(name="Arial", size=14, bold=True)
    results_sheet["A3"] = f"Rows: {result.row_count}"
    results_sheet["A3"].font = Font(name="Arial", size=10, italic=True, color="666666")
    header_row = 5
    for column_index, column in enumerate(result.columns, start=1):
        cell = results_sheet.cell(header_row, column_index, column)
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row_index, row in enumerate(result.rows, start=header_row + 1):
        for column_index, column in enumerate(result.columns, start=1):
            cell = results_sheet.cell(row_index, column_index, row.get(column))
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(vertical="center")
    results_sheet.freeze_panes = "A6"
    results_sheet.auto_filter.ref = (
        f"A{header_row}:{get_column_letter(max(len(result.columns), 1))}"
        f"{header_row + max(result.row_count, 1)}"
    )
    for column_index, column in enumerate(result.columns, start=1):
        sample_values = [str(row.get(column) or "") for row in result.rows[:200]]
        width = min(max([len(column)] + [len(value) for value in sample_values]) + 2, 36)
        results_sheet.column_dimensions[get_column_letter(column_index)].width = width
        normalized_column = column.casefold()
        if "ratio" in normalized_column or "yield" in normalized_column:
            for row_index in range(header_row + 1, header_row + result.row_count + 1):
                results_sheet.cell(row_index, column_index).number_format = "0.00%"
    results_sheet.sheet_properties.pageSetUpPr.fitToPage = True
    results_sheet.page_setup.orientation = "landscape"
    results_sheet.page_setup.fitToWidth = 1
    results_sheet.page_setup.fitToHeight = 0
    results_sheet.print_area = (
        f"A1:{get_column_letter(max(len(result.columns), 1))}"
        f"{header_row + max(result.row_count, 1)}"
    )

    methodology_sheet["A2"] = "Methodology"
    methodology_sheet["A2"].font = Font(name="Arial", size=14, bold=True)
    methodology_sheet["A4"] = "Data provider"
    methodology_sheet["B4"] = result.provider
    methodology_sheet["A5"] = "Operation"
    methodology_sheet["B5"] = result.operation
    methodology_sheet["A6"] = "Dataset"
    methodology_sheet["B6"] = result.query_id
    methodology_sheet["A7"] = "Method"
    methodology_sheet["B7"] = methodology
    methodology_sheet["A8"] = "Generated at"
    methodology_sheet["B8"] = datetime.now(timezone.utc).replace(tzinfo=None)
    methodology_sheet["B8"].number_format = "yyyy-mm-dd hh:mm"
    methodology_sheet.column_dimensions["A"].width = 18
    methodology_sheet.column_dimensions["B"].width = 60
    methodology_sheet["B7"].alignment = Alignment(wrap_text=True, vertical="top")
    for row in methodology_sheet.iter_rows(min_row=4, max_row=8, min_col=1, max_col=2):
        for cell in row:
            cell.font = Font(name="Arial", size=10)
    for row_index in range(4, 9):
        methodology_sheet.cell(row_index, 1).font = Font(name="Arial", size=10, bold=True)
    methodology_sheet.sheet_properties.pageSetUpPr.fitToPage = True
    methodology_sheet.page_setup.fitToWidth = 1
    methodology_sheet.page_setup.fitToHeight = 1
    methodology_sheet.print_area = "A1:B9"

    output_dir = Path(tempfile.mkdtemp(prefix="feishu-agent-"))
    output_path = output_dir / "a_share_research.xlsx"
    workbook.save(output_path)
    return output_path


def _frame_to_result(
    query_id: str,
    provider: str,
    operation: str,
    frame: pd.DataFrame,
) -> QueryResult:
    """Convert one provider frame into the shared deterministic result contract."""
    normalized = frame.astype(object).where(pd.notna(frame), None)
    rows = normalized.to_dict(orient="records")
    return QueryResult(
        query_id=query_id,
        provider=provider,
        operation=operation,
        status=QueryStatus.SUCCESS,
        columns=list(frame.columns),
        rows=rows,
        row_count=len(rows),
        completeness="complete",
        completeness_evidence=["feishu_agent_complete_provider_query"],
    )


def _result_payload(result: QueryResult) -> Dict[str, Any]:
    """Return bounded model-visible evidence while retaining the complete dataset."""
    return {
        "dataset_id": result.query_id,
        "row_count": result.row_count,
        "columns": result.columns,
        "summary": result.summary,
        "preview": result.rows[:MAX_AGENT_PREVIEW_ROWS],
        "preview_truncated": result.row_count > MAX_AGENT_PREVIEW_ROWS,
    }


def _agent_system_prompt() -> str:
    """Return stable tool-use and evidence rules for the Feishu research agent."""
    return (
        "You are an A-share research agent. Answer in concise Chinese. Use tools for "
        "every market-data claim and never invent prices, rankings, dates, companies, "
        "or financial metrics. Search the operation catalog before using the generic "
        "query_market_data tool. Dedicated analytical tools do not require catalog "
        "search. For a market-wide ranking over the latest N trading sessions, always "
        "use rank_recent_market_return; never use block_trade, which contains large "
        "off-exchange transaction records rather than ordinary stock returns. Report "
        "that tool's result as compounded daily pct_chg and never call it an adjusted "
        "or adjusted-price return. "
        "Use only returned dataset identifiers and deterministic transformations. "
        "When the user requests Excel, or a result contains more than ten rows, call "
        "export_excel after producing the final retained dataset. Explain proxy metrics "
        "and missing data explicitly. Never provide personalized buy or sell advice. "
        "Prefer rank_dataset and join_datasets for ordinary comparisons. "
        "ResultPipeline supports advanced allowlisted operations such as select_fields, filter, "
        "filter_range, sort, limit, aggregate, summarize, distinct, latest_by_group, "
        "derive, join_fields, inner_join, and union_all. If a tool returns an error, "
        "correct the arguments or explain the limitation instead of guessing."
    )

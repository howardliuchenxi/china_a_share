"""Codex SDK runtime for durable Feishu assistant turns."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Callable, Optional, Tuple

from china_a_share.feishu_agent import (
    FeishuAgentOutcome,
    FeishuAgentRequest,
    MAX_AGENT_CONTEXT_TURNS,
)


logger = logging.getLogger(__name__)
CODEX_MODEL_PROVIDER = "deepseek"
CODEX_TURN_TIMEOUT_SECONDS = 900
CODEX_INTERRUPT_GRACE_SECONDS = 30
SUPPORTED_ARTIFACT_SUFFIXES = {".csv", ".docx", ".pdf", ".xlsx"}


class CodexFeishuAgentRuntime:
    """Run one Feishu turn with the full Codex agent harness."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        tushare_token: str,
        cache_bucket: str,
        sandbox_url: str,
        google_cloud_project: str = "",
        sdk_loader: Optional[Callable[[], Tuple[Any, Any, Any, Any]]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._model = model
        self._api_key = api_key
        self._tushare_token = tushare_token
        self._cache_bucket = cache_bucket
        self._sandbox_url = sandbox_url
        self._google_cloud_project = google_cloud_project
        self._sdk_loader = sdk_loader or _load_codex_sdk

    def run(
        self,
        request: FeishuAgentRequest,
        progress: Callable[[str, str], None],
    ) -> FeishuAgentOutcome:
        """Execute the request without imposing an application-level turn limit."""
        Codex, CodexConfig, Sandbox, ApprovalMode = self._sdk_loader()
        with tempfile.TemporaryDirectory(prefix="feishu-codex-") as workspace_value:
            workspace = Path(workspace_value)
            artifact_dir = workspace / "artifacts"
            artifact_dir.mkdir()
            config = CodexConfig(
                cwd=str(workspace),
                env=self._codex_environment(artifact_dir),
                config_overrides=self._codex_overrides(),
            )
            progress(
                "researching",
                "Codex 正在调用通用工具并处理完整数据集…",
            )
            with Codex(config) as codex:
                thread = codex.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    cwd=str(workspace),
                    developer_instructions=_developer_instructions(),
                    ephemeral=True,
                    model=self._model,
                    model_provider=CODEX_MODEL_PROVIDER,
                    sandbox=Sandbox.workspace_write,
                )
                turn = thread.turn(_request_prompt(request))
                final_response, duration_ms = _run_turn_with_progress(turn, progress)

            artifact_path = _persist_artifact(artifact_dir)
            answer = str(final_response or "").strip()
            if not answer:
                answer = _empty_response_follow_up(artifact_path)
                logger.warning(
                    "codex_feishu_turn_empty_response conversation_id=%s model=%s "
                    "duration_ms=%s artifact=%s",
                    request.conversation_id,
                    self._model,
                    duration_ms,
                    artifact_path.name if artifact_path is not None else "none",
                )
            logger.info(
                "codex_feishu_turn_completed conversation_id=%s model=%s "
                "duration_ms=%s artifact=%s",
                request.conversation_id,
                self._model,
                duration_ms,
                artifact_path.name if artifact_path is not None else "none",
            )
            return FeishuAgentOutcome(
                answer=answer,
                artifact_path=artifact_path,
            )

    def _codex_environment(self, artifact_dir: Path) -> dict[str, str]:
        env = {
            "LLM_API_KEY": self._api_key,
            "TUSHARE_TOKEN": self._tushare_token,
            "TUSHARE_CACHE_BUCKET": self._cache_bucket,
            "RESEARCH_SANDBOX_URL": self._sandbox_url,
            "CODEX_AGENT_ARTIFACT_DIR": str(artifact_dir),
        }
        if self._google_cloud_project:
            env["GOOGLE_CLOUD_PROJECT"] = self._google_cloud_project
        return env

    def _codex_overrides(self) -> tuple[str, ...]:
        model_catalog = Path(__file__).with_name("deepseek_models.json")
        mcp_env_vars = [
            "TUSHARE_TOKEN",
            "TUSHARE_CACHE_BUCKET",
            "RESEARCH_SANDBOX_URL",
            "CODEX_AGENT_ARTIFACT_DIR",
        ]
        if self._google_cloud_project:
            mcp_env_vars.append("GOOGLE_CLOUD_PROJECT")
        values = {
            "model": self._model,
            "model_provider": CODEX_MODEL_PROVIDER,
            "model_reasoning_effort": "high",
            "model_catalog_json": str(model_catalog),
            "forced_login_method": "api",
            "check_for_update_on_startup": False,
            "web_search": "disabled",
            "history.persistence": "none",
            "features.apps": False,
            "features.multi_agent": False,
            "features.remote_plugin": False,
            "features.hooks": False,
            "features.goals": False,
            "shell_environment_policy.inherit": "core",
            "shell_environment_policy.ignore_default_excludes": False,
            f"model_providers.{CODEX_MODEL_PROVIDER}.name": "DeepSeek",
            f"model_providers.{CODEX_MODEL_PROVIDER}.base_url": self._base_url,
            f"model_providers.{CODEX_MODEL_PROVIDER}.env_key": "LLM_API_KEY",
            f"model_providers.{CODEX_MODEL_PROVIDER}.wire_api": "responses",
            "mcp_servers.market_data.command": sys.executable,
            "mcp_servers.market_data.args": ["-m", "china_a_share.codex_mcp"],
            "mcp_servers.market_data.env_vars": mcp_env_vars,
            "mcp_servers.market_data.startup_timeout_sec": 30,
            "mcp_servers.market_data.tool_timeout_sec": 600,
            "mcp_servers.market_data.required": True,
            "mcp_servers.market_data.default_tools_approval_mode": "approve",
        }
        return tuple(
            f"{key}={json.dumps(value, ensure_ascii=True)}"
            for key, value in values.items()
        )


def _load_codex_sdk() -> Tuple[Any, Any, Any, Any]:
    try:
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
    except ImportError as exc:
        raise RuntimeError(
            "The Feishu agent requires the openai-codex runtime dependency."
        ) from exc
    return Codex, CodexConfig, Sandbox, ApprovalMode


def _run_turn_with_progress(
    turn: Any,
    progress: Callable[[str, str], None],
) -> tuple[Optional[str], Optional[int]]:
    """Collect one turn while surfacing material MCP activity and bounding runtime."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codex-turn")
    future = executor.submit(_collect_turn, turn, progress)
    try:
        return future.result(timeout=CODEX_TURN_TIMEOUT_SECONDS)
    except FutureTimeoutError as exc:
        logger.error(
            "codex_feishu_turn_timeout timeout_seconds=%s",
            CODEX_TURN_TIMEOUT_SECONDS,
        )
        turn.interrupt()
        try:
            future.result(timeout=CODEX_INTERRUPT_GRACE_SECONDS)
        except Exception:
            logger.exception("codex_feishu_turn_interrupt_failed")
        raise RuntimeError(
            f"Codex turn exceeded {CODEX_TURN_TIMEOUT_SECONDS // 60} minutes."
        ) from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _collect_turn(
    turn: Any,
    progress: Callable[[str, str], None],
) -> tuple[Optional[str], Optional[int]]:
    final_response: Optional[str] = None
    last_unknown_phase_response: Optional[str] = None
    duration_ms: Optional[int] = None
    completed = False
    for event in turn.stream():
        payload = _event_payload(event.payload)
        if event.method == "item/started":
            item = payload.get("item") or {}
            if item.get("type") == "mcpToolCall":
                tool = str(item.get("tool") or "unknown")
                progress("tool", _tool_progress_message(tool))
                logger.info("codex_mcp_tool_started tool=%s", tool)
            continue
        if event.method == "item/completed":
            item = payload.get("item") or {}
            item_type = item.get("type")
            if item_type == "mcpToolCall":
                logger.info(
                    "codex_mcp_tool_completed tool=%s status=%s duration_ms=%s",
                    item.get("tool"),
                    item.get("status"),
                    item.get("durationMs"),
                )
            elif item_type == "agentMessage":
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                if item.get("phase") == "final_answer":
                    final_response = text
                elif item.get("phase") is None:
                    # The official SDK accepts the latest agent message whose phase
                    # is absent when a model provider does not emit final-answer
                    # metadata. DeepSeek uses this valid compatibility path.
                    last_unknown_phase_response = text
            continue
        if event.method == "turn/completed":
            completed = True
            completed_turn = payload.get("turn") or {}
            duration_ms = completed_turn.get("durationMs")
            status = str(completed_turn.get("status") or "")
            if status != "completed":
                error = completed_turn.get("error") or {}
                message = error.get("message") or f"Codex turn ended with {status}."
                raise RuntimeError(str(message))
    if not completed:
        raise RuntimeError("Codex turn ended without a completion event.")
    return final_response or last_unknown_phase_response, duration_ms


def _empty_response_follow_up(artifact_path: Optional[Path]) -> str:
    """Return a recoverable user response when a completed turn has no text."""
    if artifact_path is not None:
        return "研究结果文件已生成，请查看附件。"
    return (
        "这次没有生成可验证的回答。请选择下一步：\n"
        "1. 按原问题重试（推荐）\n"
        "2. 补充查询范围、时间和指标口径后再试\n"
        "请回复序号，或直接补充你的完整口径。"
    )


def _event_payload(payload: Any) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        value = payload.model_dump(
            by_alias=True,
            exclude_none=True,
            mode="json",
        )
        if isinstance(value, dict):
            return value
    if isinstance(payload, dict):
        return payload
    return {}


def _tool_progress_message(tool: str) -> str:
    messages = {
        "request_clarification": "Codex 正在整理需要你确认的选项…",
        "search_market_data": "Codex 正在查找可用数据接口…",
        "query_market_data": "Codex 正在读取完整数据集…",
        "rank_dataset": "Codex 正在排序完整数据集…",
        "join_datasets": "Codex 正在关联完整数据集…",
        "transform_dataset": "Codex 正在转换完整数据集…",
        "run_python_analysis": "Codex 正在沙箱中计算完整数据集…",
        "export_excel": "Codex 正在生成 Excel 文件…",
    }
    return messages.get(tool, "Codex 正在执行通用工具…")


def _developer_instructions() -> str:
    return (
        "You are the remote assistant behind a Feishu conversation. Answer the "
        "current user in concise Chinese. Use the available MCP data tools whenever "
        "the answer depends on external or current structured data. Treat every tool "
        "preview as display-only: retained-dataset tools and the Python sandbox work "
        "with the complete dataset identified by dataset_id. Prefer generic query, "
        "join, transform, rank, and sandbox composition over assumptions or manual "
        "reconstruction. If a material product or analytical choice is ambiguous, "
        "call request_clarification once with two to four numbered choices, mark the "
        "safest default as recommended, and return its clarification verbatim. When "
        "conversation history shows the user answering that clarification, resolve "
        "the answer from context instead of asking again. For calculations that need "
        "several tabular operations, "
        "prefer one Python sandbox call over a long sequence of transformations. If "
        "a tool rejects invalid arguments, inspect its schema and correct the call "
        "once; do not repeat equivalent failing calls. Never invent missing values. "
        "If a result has more than ten rows or the user asks for a file, create an "
        "Excel artifact with the export tool. Do not create keyword routes, "
        "domain-specific shortcuts, or special cases for individual questions."
    )


def _request_prompt(request: FeishuAgentRequest) -> str:
    conversation = [
        {"user": turn.prompt, "assistant": turn.answer}
        for turn in request.conversation[-MAX_AGENT_CONTEXT_TURNS:]
    ]
    payload = {
        "conversation_id": request.conversation_id,
        "previous_conversation": conversation,
        "current_user_request": request.prompt,
    }
    return (
        "Continue the following backend-managed conversation. Previous exchanges are "
        "context only; answer current_user_request directly.\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _persist_artifact(artifact_dir: Path) -> Optional[Path]:
    candidates = [
        path
        for path in artifact_dir.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.casefold() in SUPPORTED_ARTIFACT_SUFFIXES
        and path.stat().st_size > 0
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise RuntimeError("Codex produced more than one terminal artifact.")
    source = candidates[0]
    output_dir = Path(tempfile.mkdtemp(prefix="feishu-agent-output-"))
    output_path = output_dir / source.name
    shutil.copy2(source, output_path)
    return output_path

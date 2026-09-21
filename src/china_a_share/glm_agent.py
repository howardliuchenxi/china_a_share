"""OpenAI chat-completions tool-loop runtime for GLM Feishu turns.

The Codex harness speaks the OpenAI Responses protocol, which Zhipu GLM does
not serve, so GLM chat research runs through this bounded function-calling
loop instead. It reuses the same research toolbox, remote Python sandbox,
developer instructions, artifact persistence, and visualization assembly as
the Codex runtime, keeping capability parity wherever the wire protocol is
not the bottleneck. Planning and error-recovery depth approach the Codex
harness through enabled thinking, a high tool-round budget, and an explicit
retry strategy instead of protocol-level orchestration.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Dict, Optional
from zoneinfo import ZoneInfo

import requests

from china_a_share.codex_agent import (
    _build_research_visualization,
    _developer_instructions,
    _persist_artifact,
    _request_prompt,
)
from china_a_share.feishu_agent import (
    FeishuAgentOutcome,
    FeishuAgentRequest,
    ResearchToolbox,
)
from china_a_share.observability import log_event


GLM_RUNTIME_NAME = "glm"
GLM_CHAT_PATH = "/chat/completions"
GLM_RUNTIME_TIMEOUT_SECONDS = 600
GLM_RUNTIME_TIMEOUT_RETRIES = 1
GLM_RUNTIME_MAX_ROUNDS = 120
GLM_RUNTIME_MAX_OUTPUT_TOKENS = 16_000
# Tools that never advance a study (catalog search or user clarification).
# Consecutive rounds spent only on them mean the model is spinning, so the
# loop aborts early instead of burning the full round budget.
GLM_PASSIVE_TOOLS = frozenset({"search_market_data", "request_clarification"})
GLM_RUNTIME_STAGNATION_LIMIT = 8
GLM_RECOVERY_INSTRUCTIONS = (
    "Research loop discipline:\n"
    "- Plan before acting: decompose the question into the data you need, "
    "then fetch exactly that.\n"
    "- When a tool call fails or returns an error payload, do not give up "
    "and do not answer from memory. Analyze the error, adjust parameters, "
    "narrow the query, or split it into smaller queries, then retry.\n"
    "- Unit discipline: read the field_units notes returned with each "
    "dataset before reporting monetary or volume figures; state the raw "
    "unit and the applied conversion, and sanity-check the magnitude "
    "against typical A-share levels before reporting.\n"
    "- Recency discipline: when a dataset carries data_recency metadata or "
    "disclosure-date columns, prefer the most recently disclosed records, "
    "cite each figure's disclosure date, and never present an older record "
    "as the current state; when substituting an alternative for an "
    "unavailable metric, pick the freshest disclosed source and state the "
    "substitution with its as-of date.\n"
    "- When the request is materially ambiguous (for example an unclear "
    "relative date range such as 上周 on a weekend), ask a clarifying "
    "question with concrete options and one recommended default instead "
    "of silently guessing one interpretation.\n"
    "- When a requested metric is unavailable, use the nearest documented "
    "alternative and state the substitution explicitly in the final answer.\n"
    "- Verify surprising numbers by cross-checking one independent query "
    "before reporting them.\n"
    "- Keep going until every part of the user's question is answered with "
    "retrieved data; only stop when the answer is complete."
)


GLM_QUOTA_ERROR_MARKERS = ("使用上限", "额度已", "quota", "insufficient")


def _friendly_glm_error(error: Any) -> str:
    """Translate provider errors into group-visible guidance."""
    message = str(error.get("message") or error)
    if any(marker in message for marker in GLM_QUOTA_ERROR_MARKERS):
        return (
            f"GLM 套餐额度已用完，本次研究中断（智谱返回：{message}）。"
            "额度随 5 小时窗口自动恢复；如需立即继续，"
            "请在快捷菜单切换回 DeepSeek 后重试。"
        )
    return f"GLM agent request failed: {message}"


def _current_date_line() -> str:
    """Anchor relative dates to the real Shanghai trading calendar clock."""
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    return (
        f"Current date: {today} (Asia/Shanghai). Resolve every relative "
        "date such as 最近/今天/上周 from this date, and prefer the latest "
        "completed trading day for end-of-day data."
    )

logger = logging.getLogger(__name__)


class GlmFeishuAgentRuntime:
    """Run one Feishu turn with GLM through an OpenAI-compatible tool loop."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        toolbox_factory: Callable[[Path, str], ResearchToolbox],
        session: Optional[requests.Session] = None,
    ) -> None:
        self._api_url = base_url.strip().rstrip("/") + GLM_CHAT_PATH
        self._model = model
        self._api_key = api_key
        self._toolbox_factory = toolbox_factory
        self._session = session or requests.Session()

    def _post_with_retry(self, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Post one chat request, retrying transient read timeouts once."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        last_exc: Optional[Exception] = None
        for attempt in range(GLM_RUNTIME_TIMEOUT_RETRIES + 1):
            try:
                return self._session.post(
                    self._api_url,
                    headers=headers,
                    json=request_payload,
                    timeout=GLM_RUNTIME_TIMEOUT_SECONDS,
                ).json()
            except requests.Timeout as exc:
                last_exc = exc
                log_event(
                    logger,
                    logging.WARNING,
                    "glm_agent_request_timeout",
                    attempt=attempt + 1,
                    retries=GLM_RUNTIME_TIMEOUT_RETRIES,
                )
        raise last_exc

    def run(
        self,
        request: FeishuAgentRequest,
        progress: Callable[[str, str], None],
    ) -> FeishuAgentOutcome:
        """Return one terminal answer after bounded tool-call rounds."""
        with tempfile.TemporaryDirectory(prefix="feishu-glm-") as workspace_value:
            artifact_dir = Path(workspace_value) / "artifacts"
            artifact_dir.mkdir()
            toolbox = self._toolbox_factory(artifact_dir, request.conversation_id)
            messages: List[Dict[str, Any]] = [
                {
                    "role": "system",
                    "content": (
                        f"{_current_date_line()}\n\n"
                        f"{_developer_instructions()}\n\n"
                        f"{GLM_RECOVERY_INSTRUCTIONS}"
                    ),
                },
                {"role": "user", "content": _request_prompt(request)},
            ]
            answer = ""
            stagnation_rounds = 0
            for round_index in range(GLM_RUNTIME_MAX_ROUNDS):
                payload = self._post_with_retry(
                    {
                        "model": self._model,
                        "messages": messages,
                        "tools": toolbox.definitions,
                        "tool_choice": "auto",
                        "thinking": {"type": "enabled"},
                        "temperature": 0,
                        "max_tokens": GLM_RUNTIME_MAX_OUTPUT_TOKENS,
                        "stream": False,
                    }
                )
                error = payload.get("error")
                if error:
                    raise RuntimeError(_friendly_glm_error(error))
                message = ((payload.get("choices") or [{}])[0].get("message")) or {}
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    answer = str(message.get("content") or "").strip()
                    break
                tool_names = [
                    str((tool_call.get("function") or {}).get("name") or "")
                    for tool_call in tool_calls
                ]
                log_event(
                    logger,
                    logging.INFO,
                    "glm_agent_round",
                    conversation_id=request.conversation_id,
                    model=self._model,
                    round=round_index + 1,
                    tools=tool_names,
                )
                if all(name in GLM_PASSIVE_TOOLS for name in tool_names):
                    stagnation_rounds += 1
                else:
                    stagnation_rounds = 0
                if stagnation_rounds >= GLM_RUNTIME_STAGNATION_LIMIT:
                    raise RuntimeError(
                        "研究循环连续多轮停留在检索/澄清阶段没有实质进展，已提前"
                        "终止。请把问题描述得更具体（给出代码、日期区间或指标"
                        "定义），或切换回 DeepSeek 后重试。"
                    )
                # Reasoning fields are provider-specific; strip them so the
                # accumulated history stays plain OpenAI-compatible.
                history_message = {
                    key: value
                    for key, value in message.items()
                    if key != "reasoning_content"
                }
                messages.append(history_message)
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
            if not answer:
                raise RuntimeError(
                    "GLM agent exceeded the bounded tool-call limit without an answer."
                )
            artifact_path = _persist_artifact(artifact_dir, request.conversation_name)
            return FeishuAgentOutcome(
                answer=answer,
                artifact_path=artifact_path,
                visualization=_build_research_visualization(artifact_path),
            )

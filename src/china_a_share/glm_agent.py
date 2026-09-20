"""OpenAI chat-completions tool-loop runtime for GLM Feishu turns.

The Codex harness speaks the OpenAI Responses protocol, which Zhipu GLM does
not serve, so GLM chat research runs through this bounded function-calling
loop instead. It reuses the same research toolbox, remote Python sandbox,
developer instructions, artifact persistence, and visualization assembly as
the Codex runtime, keeping capability parity wherever the wire protocol is
not the bottleneck.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Callable, List, Dict, Optional

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


GLM_RUNTIME_NAME = "glm"
GLM_CHAT_PATH = "/chat/completions"
GLM_RUNTIME_TIMEOUT_SECONDS = 180
GLM_RUNTIME_MAX_ROUNDS = 24
GLM_RUNTIME_MAX_OUTPUT_TOKENS = 12_000

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
                {"role": "system", "content": _developer_instructions()},
                {"role": "user", "content": _request_prompt(request)},
            ]
            answer = ""
            for round_index in range(GLM_RUNTIME_MAX_ROUNDS):
                payload = self._session.post(
                    self._api_url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "messages": messages,
                        "tools": toolbox.definitions,
                        "tool_choice": "auto",
                        "temperature": 0,
                        "max_tokens": GLM_RUNTIME_MAX_OUTPUT_TOKENS,
                        "stream": False,
                    },
                    timeout=GLM_RUNTIME_TIMEOUT_SECONDS,
                ).json()
                error = payload.get("error")
                if error:
                    raise RuntimeError(
                        f"GLM agent request failed: {error.get('message') or error}"
                    )
                message = ((payload.get("choices") or [{}])[0].get("message")) or {}
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    answer = str(message.get("content") or "").strip()
                    break
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

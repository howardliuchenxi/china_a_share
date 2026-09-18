"""Expose the retained-dataset toolbox through a minimal stdio MCP server."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict, Mapping, Optional
from uuid import uuid4

from china_a_share.config import ConfigurationError, Settings
from china_a_share.feishu_agent import ResearchToolbox
from china_a_share.research_sandbox import RemotePythonSandbox


MCP_PROTOCOL_VERSION = "2025-06-18"
logger = logging.getLogger(__name__)


class ResearchMcpServer:
    """Translate MCP JSON-RPC calls into one stateful research toolbox."""

    def __init__(self, toolbox: ResearchToolbox) -> None:
        self._toolbox = toolbox

    def handle(self, request: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Return one JSON-RPC response or no response for notifications."""
        request_id = request.get("id")
        method = str(request.get("method") or "")
        params = request.get("params") or {}
        if not isinstance(params, Mapping):
            return self._error(request_id, -32602, "Request params must be an object.")

        if method == "initialize":
            requested_version = str(
                params.get("protocolVersion") or MCP_PROTOCOL_VERSION
            )
            return self._result(
                request_id,
                {
                    "protocolVersion": requested_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "china-a-share-research-tools",
                        "version": "1.0.0",
                    },
                },
            )
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(
                request_id,
                {
                    "tools": [
                        {
                            "name": definition["function"]["name"],
                            "description": definition["function"]["description"],
                            "inputSchema": definition["function"]["parameters"],
                            "annotations": {
                                "destructiveHint": False,
                                "idempotentHint": True,
                                "readOnlyHint": (
                                    definition["function"]["name"]
                                    != "export_excel"
                                ),
                            },
                        }
                        for definition in self._toolbox.definitions
                    ]
                },
            )
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return self._error(request_id, -32601, f"Unsupported MCP method: {method}")

    def _call_tool(
        self,
        request_id: Any,
        params: Mapping[str, Any],
    ) -> Dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not name or not isinstance(arguments, Mapping):
            return self._error(
                request_id,
                -32602,
                "Tool name and object arguments are required.",
            )
        try:
            result = self._toolbox.call(
                name,
                arguments,
                lambda _stage, _message: None,
            )
        except Exception as exc:
            logger.exception("codex_mcp_tool_failed tool=%s", name)
            message = str(exc)
            return self._result(
                request_id,
                {
                    "content": [{"type": "text", "text": message}],
                    "isError": True,
                },
            )
        return self._result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False),
                    }
                ],
                "structuredContent": result,
                "isError": False,
            },
        )

    @staticmethod
    def _result(request_id: Any, result: Mapping[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}

    @staticmethod
    def _error(
        request_id: Any,
        code: int,
        message: str,
    ) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def serve(server: ResearchMcpServer) -> None:
    """Serve newline-delimited MCP requests until standard input closes."""
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise ValueError("MCP request must be a JSON object.")
            response = server.handle(request)
        except Exception as exc:
            logger.exception("codex_mcp_request_failed")
            response = ResearchMcpServer._error(None, -32700, str(exc))
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def create_server_from_env() -> ResearchMcpServer:
    """Build the credentialed provider boundary for one Codex thread."""
    token = _required_env("TUSHARE_TOKEN")
    bucket = _required_env("TUSHARE_CACHE_BUCKET")
    sandbox_url = _required_env("RESEARCH_SANDBOX_URL")
    settings = Settings(
        tushare_token=token,
        tushare_cache_bucket=bucket,
        google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT", "").strip(),
        research_sandbox_url=sandbox_url,
        massive_api_key=os.getenv("MASSIVE_API_KEY", "").strip(),
        finnhub_api_key=os.getenv("FINNHUB_API_KEY", "").strip(),
    )
    # Import lazily so the worker can assemble its Codex runtime without a
    # module-import cycle through bootstrap.
    from china_a_share.bootstrap import _create_feishu_data_provider

    artifact_dir_value = os.getenv("CODEX_AGENT_ARTIFACT_DIR", "").strip()
    artifact_dir = Path(artifact_dir_value) if artifact_dir_value else None
    toolbox = ResearchToolbox(
        _create_feishu_data_provider(settings),
        uuid4().hex,
        python_sandbox=RemotePythonSandbox(sandbox_url),
        artifact_dir=artifact_dir,
    )
    return ResearchMcpServer(toolbox)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required by the Codex MCP server.")
    return value


def main() -> None:
    """Run the stdio MCP server without writing protocol noise to stdout."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    serve(create_server_from_env())


if __name__ == "__main__":
    main()

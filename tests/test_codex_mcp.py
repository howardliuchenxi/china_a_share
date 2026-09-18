from china_a_share.codex_mcp import ResearchMcpServer


class FakeToolbox:
    def __init__(self):
        self.calls = []

    @property
    def definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "query_data",
                    "description": "Read one complete dataset.",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    def call(self, name, arguments, progress):
        self.calls.append((name, dict(arguments)))
        progress("querying", "ignored")
        if name == "fail":
            raise ValueError("invalid dataset")
        return {"dataset_id": "dataset_1", "row_count": 42}


def test_mcp_server_initializes_and_lists_tool_contracts():
    server = ResearchMcpServer(FakeToolbox())

    initialized = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    listed = server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )

    assert initialized["result"]["protocolVersion"] == "2025-06-18"
    assert initialized["result"]["capabilities"] == {
        "tools": {"listChanged": False}
    }
    assert listed["result"]["tools"] == [
        {
            "name": "query_data",
            "description": "Read one complete dataset.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            "annotations": {
                "destructiveHint": False,
                "idempotentHint": True,
                "readOnlyHint": True,
            },
        }
    ]


def test_mcp_server_calls_tools_and_returns_structured_complete_results():
    toolbox = FakeToolbox()
    server = ResearchMcpServer(toolbox)

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "query_data",
                "arguments": {"name": "complete"},
            },
        }
    )

    assert toolbox.calls == [("query_data", {"name": "complete"})]
    assert response["result"]["structuredContent"] == {
        "dataset_id": "dataset_1",
        "row_count": 42,
    }
    assert response["result"]["isError"] is False
    assert '"row_count": 42' in response["result"]["content"][0]["text"]


def test_mcp_server_contains_tool_failures_without_breaking_transport():
    server = ResearchMcpServer(FakeToolbox())

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "fail", "arguments": {}},
        }
    )

    assert response["result"]["isError"] is True
    assert response["result"]["content"] == [
        {"type": "text", "text": "invalid dataset"}
    ]


def test_mcp_notifications_do_not_emit_responses():
    server = ResearchMcpServer(FakeToolbox())

    assert (
        server.handle(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )
        is None
    )

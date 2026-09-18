from china_a_share.model_client import OpenAICompatibleChatModel


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, message=None):
        self.message = message or {"role": "assistant", "content": "ok"}

    def json(self):
        return {"choices": [{"message": self.message}]}


class RecordingSession:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or FakeResponse()

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_openai_compatible_model_uses_only_generic_transport_configuration():
    session = RecordingSession()
    model = OpenAICompatibleChatModel(
        "https://gateway.example/v1/",
        "research-model",
        "api-key",
        api_secret="api-secret",
        session=session,
    )

    message = model.complete([{"role": "user", "content": "hello"}], [])

    assert message == {"role": "assistant", "content": "ok"}
    assert model.model == "research-model"
    url, request = session.calls[0]
    assert url == "https://gateway.example/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer api-key"
    assert request["headers"]["X-API-Secret"] == "api-secret"
    assert request["json"]["model"] == "research-model"
    assert "tools" not in request["json"]


def test_openai_compatible_model_exposes_standard_tool_contract():
    session = RecordingSession()
    model = OpenAICompatibleChatModel(
        "https://gateway.example",
        "other-model",
        "api-key",
        session=session,
    )
    tools = [{"type": "function", "function": {"name": "lookup"}}]

    model.complete([{"role": "user", "content": "hello"}], tools)

    request = session.calls[0][1]
    assert request["json"]["tools"] == tools
    assert request["json"]["tool_choice"] == "auto"
    assert "X-API-Secret" not in request["headers"]


def test_openai_compatible_model_normalizes_dsml_tool_calls():
    session = RecordingSession(
        FakeResponse(
            {
                "role": "assistant",
                "content": (
                    '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="query_market_data">'
                    '<｜｜DSML｜｜parameter name="operation" string="true">daily_basic'
                    '</｜｜DSML｜｜parameter><｜｜DSML｜｜parameter name="params">'
                    '{"trade_date":"20260917"}</｜｜DSML｜｜parameter>'
                    '<｜｜DSML｜｜parameter name="fields">["ts_code","pe_ttm"]'
                    '</｜｜DSML｜｜parameter></｜｜DSML｜｜invoke>'
                    '</｜｜DSML｜｜tool_calls>'
                ),
            }
        )
    )
    model = OpenAICompatibleChatModel(
        "https://gateway.example",
        "research-model",
        "api-key",
        session=session,
    )

    message = model.complete(
        [{"role": "user", "content": "rank valuations"}],
        [{"type": "function", "function": {"name": "query_market_data"}}],
    )

    assert message["content"] is None
    assert len(message["tool_calls"]) == 1
    tool_call = message["tool_calls"][0]
    assert tool_call["id"].startswith("dsml-")
    assert tool_call["function"] == {
        "name": "query_market_data",
        "arguments": (
            '{"operation": "daily_basic", "params": '
            '{"trade_date": "20260917"}, "fields": ["ts_code", "pe_ttm"]}'
        ),
    }


def test_openai_compatible_model_rejects_malformed_dsml_tool_calls():
    session = RecordingSession(
        FakeResponse(
            {
                "role": "assistant",
                "content": "<｜｜DSML｜｜tool_calls>malformed</｜｜DSML｜｜tool_calls>",
            }
        )
    )
    model = OpenAICompatibleChatModel(
        "https://gateway.example",
        "research-model",
        "api-key",
        session=session,
    )

    try:
        model.complete(
            [{"role": "user", "content": "rank valuations"}],
            [{"type": "function", "function": {"name": "query_market_data"}}],
        )
    except RuntimeError as exc:
        assert str(exc) == "Model API returned malformed DSML tool calls."
    else:
        raise AssertionError("Malformed DSML tool calls must fail closed.")

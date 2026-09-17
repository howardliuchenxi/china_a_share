from china_a_share.model_client import OpenAICompatibleChatModel


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


class RecordingSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


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

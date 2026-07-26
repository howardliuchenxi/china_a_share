from china_a_share.core.contracts import (
    UiFeedbackChatRequest,
    UiFeedbackRequest,
    UiFeedbackStatus,
)
from china_a_share.feedback import DeepSeekUiFeedbackAssistant, UiFeedbackService


class FakeVerifier:
    def __init__(self):
        self.tokens = []

    def verify(self, token):
        self.tokens.append(token)
        return "admin@example.com"


class FakeStore:
    def __init__(self):
        self.records = []

    def put(self, feedback_id, record):
        self.records.append((feedback_id, record.copy()))


class FakeDispatcher:
    actions_url = "https://github.com/example/repository/actions"

    def __init__(self, error=None):
        self.error = error
        self.payloads = []

    def dispatch(self, payload):
        self.payloads.append(payload)
        if self.error:
            raise self.error


class FakeAssistant:
    def __init__(self, reply="Use a clearer empty-state explanation."):
        self.reply_text = reply
        self.requests = []

    def reply(self, request):
        self.requests.append(request)
        return self.reply_text


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "choices": [
                {"message": {"content": "Explain why the result is empty."}}
            ]
        }


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse()


class FakeSourceSearch:
    def __init__(self):
        self.requests = []

    def search(self, request):
        self.requests.append(request)
        return "SOURCE src/example.py\nLINES 10-11\n10: def explain():\n11:     return True"


def feedback_request():
    return UiFeedbackRequest(
        page_path="/analysis",
        feedback_id="results-panel",
        selected_text="A visible result",
        suggestion="Use a clearer unit.",
        conversation=[
            {"role": "user", "content": "Why is this confusing?"},
            {"role": "assistant", "content": "The empty state lacks a next step."},
        ],
        rect={"x": 10, "y": 20, "width": 100, "height": 30},
        viewport={
            "width": 1440,
            "height": 900,
            "scroll_x": 0,
            "scroll_y": 300,
        },
    )


def create_service(dispatcher, assistant=None):
    store = FakeStore()
    active_assistant = assistant or FakeAssistant()
    return UiFeedbackService(
        FakeVerifier(),
        store,
        dispatcher,
        active_assistant,
        google_client_id="client-id",
        git_branch="main",
        git_sha="a" * 40,
    ), store, active_assistant


def test_ui_feedback_persists_and_dispatches_bounded_context():
    dispatcher = FakeDispatcher()
    service, _, _ = create_service(dispatcher)

    submission = service.submit("google-token", feedback_request())

    assert submission.status == UiFeedbackStatus.SUBMITTED
    assert submission.actions_url == dispatcher.actions_url
    assert dispatcher.payloads[0]["component_id"] == "results-panel"
    assert dispatcher.payloads[0]["git_branch"] == "main"
    assert dispatcher.payloads[0]["git_sha"] == "a" * 40
    assert dispatcher.payloads[0]["conversation"][0]["role"] == "user"


def test_ui_feedback_records_dispatch_failure_before_reraising():
    dispatcher = FakeDispatcher(RuntimeError("dispatch failed"))
    service, store, _ = create_service(dispatcher)

    try:
        service.submit("google-token", feedback_request())
    except RuntimeError as exc:
        assert str(exc) == "dispatch failed"
    else:
        raise AssertionError("Expected dispatch failure.")

    assert store.records[-1][1]["status"] == "dispatch_failed"


def test_ui_feedback_chat_authenticates_and_returns_assistant_message():
    assistant = FakeAssistant()
    service, _, _ = create_service(FakeDispatcher(), assistant)
    request = UiFeedbackChatRequest(
        page_path="/analysis",
        feedback_id="results-panel",
        selected_text="No data found",
        conversation=[
            {"role": "user", "content": "How should this explain the next step?"}
        ],
    )

    response = service.chat("google-token", request)

    assert response.message.role == "assistant"
    assert response.message.content == assistant.reply_text
    assert assistant.requests == [request]


def test_deepseek_ui_feedback_assistant_sends_bounded_ui_context():
    session = FakeSession()
    source_search = FakeSourceSearch()
    assistant = DeepSeekUiFeedbackAssistant(
        "secret-key",
        session,
        source_search,
        git_sha="a" * 40,
    )
    request = UiFeedbackChatRequest(
        page_path="/analysis",
        feedback_id="results-panel",
        selected_text="No data found",
        conversation=[
            {"role": "user", "content": "What should the empty state say?"}
        ],
    )

    reply = assistant.reply(request)

    assert reply == "Explain why the result is empty."
    sent = session.calls[0][1]
    assert sent["headers"]["Authorization"] == "Bearer secret-key"
    assert '"component_id": "results-panel"' in sent["json"]["messages"][0]["content"]
    assert "SOURCE src/example.py" in sent["json"]["messages"][0]["content"]
    assert f"DEPLOYED_GIT_SHA: {'a' * 40}" in sent["json"]["messages"][0]["content"]
    assert sent["json"]["messages"][-1] == {
        "role": "user",
        "content": "What should the empty state say?",
    }
    assert source_search.requests == [request]

from china_a_share.core.contracts import (
    UiFeedbackRequest,
    UiFeedbackStatus,
)
from china_a_share.feedback import UiFeedbackService


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


def feedback_request():
    return UiFeedbackRequest(
        page_path="/analysis",
        feedback_id="results-panel",
        selected_text="A visible result",
        suggestion="Use a clearer unit.",
        rect={"x": 10, "y": 20, "width": 100, "height": 30},
        viewport={
            "width": 1440,
            "height": 900,
            "scroll_x": 0,
            "scroll_y": 300,
        },
    )


def create_service(dispatcher):
    store = FakeStore()
    return UiFeedbackService(
        FakeVerifier(),
        store,
        dispatcher,
        google_client_id="client-id",
        git_branch="main",
        git_sha="a" * 40,
    ), store


def test_ui_feedback_persists_and_dispatches_bounded_context():
    dispatcher = FakeDispatcher()
    service, _ = create_service(dispatcher)

    submission = service.submit("google-token", feedback_request())

    assert submission.status == UiFeedbackStatus.SUBMITTED
    assert submission.actions_url == dispatcher.actions_url
    assert dispatcher.payloads[0]["component_id"] == "results-panel"
    assert dispatcher.payloads[0]["git_branch"] == "main"
    assert dispatcher.payloads[0]["git_sha"] == "a" * 40


def test_ui_feedback_records_dispatch_failure_before_reraising():
    dispatcher = FakeDispatcher(RuntimeError("dispatch failed"))
    service, store = create_service(dispatcher)

    try:
        service.submit("google-token", feedback_request())
    except RuntimeError as exc:
        assert str(exc) == "dispatch failed"
    else:
        raise AssertionError("Expected dispatch failure.")

    assert store.records[-1][1]["status"] == "dispatch_failed"

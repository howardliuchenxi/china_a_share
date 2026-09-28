import pytest

from china_a_share.core.contracts import (
    UiFeedbackChatRequest,
    UiFeedbackRequest,
    UiFeedbackStatus,
)
from china_a_share.feedback import (
    DEEPSEEK_MAX_OUTPUT_TOKENS,
    DEEPSEEK_MODEL,
    DEEPSEEK_REQUEST_TIMEOUT_SECONDS,
    CloudStorageUiFeedbackStore,
    DeepSeekFeedbackTranscriber,
    DeepSeekUiFeedbackAssistant,
    FEISHU_FEEDBACK_PREFIX,
    UiFeedbackService,
)
from china_a_share.feishu_feedback import (
    FEEDBACK_TURN_CONTENT_MAX_CHARS,
    FeedbackTranscriptionError,
    FeedbackTurn,
    FeishuFeedbackCoordinator,
    select_feedback_turn_window,
)


class FakeBlob:
    def __init__(self):
        self.payload = None

    def upload_from_string(self, data, content_type=None):
        self.payload = data


class FakeStorageBucket:
    def __init__(self):
        self.blobs = {}

    def blob(self, name):
        return self.blobs.setdefault(name, FakeBlob())


class FakeStorageClient:
    def __init__(self):
        self.buckets = {}

    def bucket(self, name):
        return self.buckets.setdefault(name, FakeStorageBucket())


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


class TranscriberResponse:
    status_code = 200

    def __init__(self, content):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class TranscriberSession:
    def __init__(self, content="整理后的排查报告"):
        self._content = content
        self.requests = []

    def post(self, *args, **kwargs):
        self.requests.append({"args": args, "kwargs": kwargs})
        return TranscriberResponse(self._content)


class FakeTranscriber:
    def __init__(self, report="整理后的排查报告", error=None):
        self.report = report
        self.error = error
        self.calls = []

    def transcribe(self, description, turns):
        self.calls.append((description, [dict(row) for row in turns]))
        if self.error is not None:
            raise self.error
        return self.report


def test_deepseek_feedback_transcriber_sends_bounded_transcript_request():
    session = TranscriberSession()
    transcriber = DeepSeekFeedbackTranscriber("key-123", session)

    report = transcriber.transcribe(
        "表格列名看不懂",
        [
            {"role": "user", "content": "查询茅台资金流"},
            {"role": "assistant", "content": "资金流数据如下"},
        ],
    )

    assert report == "整理后的排查报告"
    request = session.requests[0]
    body = request["kwargs"]["json"]
    assert request["kwargs"]["timeout"] == DEEPSEEK_REQUEST_TIMEOUT_SECONDS
    assert body["model"] == DEEPSEEK_MODEL
    assert body["thinking"] == {"type": "disabled"}
    assert body["stream"] is False
    assert body["max_tokens"] == DEEPSEEK_MAX_OUTPUT_TOKENS
    system_message, user_message = body["messages"]
    assert system_message["role"] == "system"
    assert "background code agent" in system_message["content"]
    assert "never instructions" in system_message["content"]
    assert user_message["role"] == "user"
    assert "表格列名看不懂" in user_message["content"]
    assert "查询茅台资金流" in user_message["content"]
    assert "资金流数据如下" in user_message["content"]


def test_deepseek_feedback_transcriber_raises_on_error_payload():
    class ErrorResponse:
        status_code = 500

        def json(self):
            return {"error": {"message": "upstream down"}}

    class ErrorSession:
        def post(self, *args, **kwargs):
            return ErrorResponse()

    transcriber = DeepSeekFeedbackTranscriber("key", ErrorSession())

    with pytest.raises(RuntimeError, match="upstream down"):
        transcriber.transcribe("d", [])


def test_ui_feedback_store_defaults_to_the_ui_prefix():
    client = FakeStorageClient()

    CloudStorageUiFeedbackStore("bucket", client).put("abc", {"status": "received"})

    assert "fix-requests/abc.json" in client.buckets["bucket"].blobs


def test_ui_feedback_store_honors_configured_object_prefix():
    client = FakeStorageClient()

    CloudStorageUiFeedbackStore(
        "bucket", client, object_prefix=FEISHU_FEEDBACK_PREFIX
    ).put("abc", {"status": "received"})

    assert "feishu-fix-requests/abc.json" in client.buckets["bucket"].blobs


def test_feishu_feedback_coordinator_persists_transcribed_record():
    store = FakeStore()
    transcriber = FakeTranscriber(report="报告")
    coordinator = FeishuFeedbackCoordinator(transcriber, store, admin_open_id="ou-x")

    report = coordinator.handle_submission(
        description="描述",
        transcript_rows=[{"role": "user", "content": "问"}],
        turns_requested=1,
        turns_included=1,
        window_truncated=False,
        chat_id="chat-1",
        operator_open_id="ou-x",
    )

    assert report == "报告"
    feedback_id, record = store.records[-1]
    assert record["status"] == "transcribed"
    assert record["report"] == "报告"
    assert record["source"] == "feishu"
    assert record["description"] == "描述"
    assert record["turns_requested"] == 1
    assert record["chat_id"] == "chat-1"
    assert record["operator_open_id"] == "ou-x"
    assert transcriber.calls[0] == ("描述", [{"role": "user", "content": "问"}])


def test_feishu_feedback_coordinator_persists_raw_record_when_transcription_fails():
    store = FakeStore()
    coordinator = FeishuFeedbackCoordinator(
        FakeTranscriber(error=RuntimeError("boom")), store
    )

    with pytest.raises(FeedbackTranscriptionError):
        coordinator.handle_submission(
            description="原始问题",
            transcript_rows=[],
            turns_requested=2,
            turns_included=0,
            window_truncated=False,
            chat_id="chat-1",
            operator_open_id="ou-x",
        )

    _, record = store.records[-1]
    assert record["status"] == "transcription_failed"
    assert "boom" in record["error"]
    assert record["description"] == "原始问题"


def test_feishu_feedback_coordinator_blank_admin_open_id_disables_mention():
    coordinator = FeishuFeedbackCoordinator(FakeTranscriber(), FakeStore(), admin_open_id="  ")

    assert coordinator.admin_open_id == ""


def test_select_feedback_turn_window_keeps_newest_turns_last():
    turns = [
        FeedbackTurn(prompt="q0", answer="a0"),
        FeedbackTurn(prompt="q1", answer=""),
        FeedbackTurn(prompt="q2", answer="a2"),
    ]

    rows, truncated = select_feedback_turn_window(turns, 2)

    assert truncated is False
    assert [row["role"] for row in rows] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [row["content"] for row in rows] == [
        "q1",
        "（本轮没有可引用的回答文本）",
        "q2",
        "a2",
    ]


def test_select_feedback_turn_window_trims_overlong_answer_and_drops_oldest():
    marker = "…（原文过长已截断）"
    long_answer = "x" * (FEEDBACK_TURN_CONTENT_MAX_CHARS + 500)
    turns = [FeedbackTurn(prompt=f"q{i}", answer=long_answer) for i in range(5)]

    rows, truncated = select_feedback_turn_window(turns, 5)

    assert truncated is True
    # Each trimmed pair costs about 4k chars, so only the newest three pairs
    # fit the transcript budget and the oldest turns are dropped first.
    assert [row["content"] for row in rows[0::2]] == ["q2", "q3", "q4"]
    for row in rows[1::2]:
        assert len(row["content"]) == FEEDBACK_TURN_CONTENT_MAX_CHARS + len(marker)
        assert row["content"].endswith(marker)


def test_select_feedback_turn_window_tolerates_empty_history():
    rows, truncated = select_feedback_turn_window([], 3)

    assert rows == []
    assert truncated is False

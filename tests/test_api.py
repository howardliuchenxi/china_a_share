import logging
from datetime import date

from fastapi.testclient import TestClient

import china_a_share.api as api_module
from china_a_share.api import create_app
from china_a_share.client import TushareApiError
from china_a_share.core.contracts import (
    AnalysisResponse,
    AnalysisStatus,
    StockListItem,
    StockListResponse,
    UiFeedbackConfig,
    UiFeedbackChatResponse,
    UiFeedbackConversationMessage,
    UiFeedbackStatus,
    UiFeedbackSubmission,
)
from china_a_share.tasks import AnalysisTaskCoordinator, MemoryAnalysisTaskStore


ORIGINAL_COMPLEX_PROMPT = (
    "\u8fc7\u53bb\u4e00\u4e2a\u6708\uff0c\u533b\u7597\u884c\u4e1a\uff0c"
    "\u6309\u7167\u6563\u6237\u6bd4\u4f8b\u5206\u4e24\u534a\uff0c"
    "\u54ea\u4e00\u534a\u516c\u53f8\u4e0a\u6da8\u7684\u591a\uff1f"
)


class FakeDispatcher:
    def __init__(self):
        self.task_ids = []

    def dispatch(self, task_id):
        self.task_ids.append(task_id)


class FakeAnalysisService:
    def __init__(self):
        self.calls = []
        
        class FakePlanner:
            name = "test-planner"
            
        self.planner = FakePlanner()
        self._vision_analyzer = None

    def analyze(self, request_id, request, *, api_route):
        self.calls.append((request_id, request, api_route))
        return AnalysisResponse(
            request_id=request_id,
            planner="test-planner",
            data_provider="test-provider",
            status=AnalysisStatus.SUCCESS,
        )


class FailingAnalysisService(FakeAnalysisService):
    def analyze(self, request_id, request, *, api_route):
        raise RuntimeError("unexpected transform failure")


class FakeStockCatalogService:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def list_stocks(self, request_id, **kwargs):
        self.calls.append((request_id, kwargs))
        if self.error is not None:
            raise self.error
        return self.response.model_copy(update={"request_id": request_id})


class FakeUiFeedbackService:
    def __init__(self):
        self.calls = []

    def config(self):
        return UiFeedbackConfig(
            enabled=True,
            google_client_id="public-client-id",
            git_branch="main",
            git_sha="a" * 40,
        )

    def submit(self, token, request):
        self.calls.append((token, request))
        return UiFeedbackSubmission(
            feedback_id="feedback-1",
            status=UiFeedbackStatus.SUBMITTED,
            actions_url="https://github.com/example/repository/actions",
        )

    def chat(self, token, request):
        self.calls.append((token, request))
        return UiFeedbackChatResponse(
            message=UiFeedbackConversationMessage(
                role="assistant",
                content="Explain the empty state and offer a next step.",
            )
        )


def stock_response():
    return StockListResponse(
        request_id="placeholder",
        page=1,
        page_size=20,
        total=1,
        total_pages=1,
        available_industries=["Banking"],
        items=[
            StockListItem(
                code="000001.SZ",
                symbol="000001",
                name="Ping An Bank",
                area="Shenzhen",
                industry="Banking",
                board="Main Board",
                exchange="SZSE",
                listed_on=date(1991, 4, 3),
            )
        ],
    )


def test_health_endpoint_reports_backend_availability(caplog):
    client = TestClient(create_app(FakeAnalysisService()))

    with caplog.at_level(logging.INFO):
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "planner": "test-planner",
        "vision_provider": "none"
    }
    event = next(
        record.structured_fields
        for record in caplog.records
        if getattr(record, "structured_fields", {}).get("event")
        == "http_request_completed"
    )
    assert event["api_route"] == "/api/health"
    assert event["method"] == "GET"
    assert event["status_class"] == "2xx"
    assert event["request_id"]


def test_ui_feedback_config_exposes_only_public_values():
    client = TestClient(
        create_app(
            FakeAnalysisService(),
            ui_feedback_service=FakeUiFeedbackService(),
        )
    )

    response = client.get("/api/ui-feedback/config")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "google_client_id": "public-client-id",
        "git_branch": "main",
        "git_sha": "a" * 40,
    }


def test_ui_feedback_requires_bearer_authentication():
    client = TestClient(
        create_app(
            FakeAnalysisService(),
            ui_feedback_service=FakeUiFeedbackService(),
        )
    )

    response = client.post(
        "/api/ui-feedback",
        json={
            "page_path": "/analysis",
            "feedback_id": "results-panel",
            "selected_text": "Selected result",
            "suggestion": "",
            "rect": {"x": 1, "y": 2, "width": 3, "height": 4},
            "viewport": {
                "width": 1280,
                "height": 800,
                "scroll_x": 0,
                "scroll_y": 100,
            },
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Administrator authentication is required."


def test_ui_feedback_dispatches_authenticated_request():
    service = FakeUiFeedbackService()
    client = TestClient(
        create_app(FakeAnalysisService(), ui_feedback_service=service)
    )

    response = client.post(
        "/api/ui-feedback",
        headers={"Authorization": "Bearer google-token"},
        json={
            "page_path": "/analysis",
            "feedback_id": "results-panel",
            "selected_text": "Selected result",
            "suggestion": "Clarify this result.",
            "rect": {"x": 1, "y": 2, "width": 3, "height": 4},
            "viewport": {
                "width": 1280,
                "height": 800,
                "scroll_x": 0,
                "scroll_y": 100,
            },
        },
    )

    assert response.status_code == 202
    assert response.json()["feedback_id"] == "feedback-1"
    assert service.calls[0][0] == "google-token"
    assert service.calls[0][1].feedback_id == "results-panel"


def test_ui_feedback_chat_returns_authenticated_assistant_reply():
    service = FakeUiFeedbackService()
    client = TestClient(
        create_app(FakeAnalysisService(), ui_feedback_service=service)
    )

    response = client.post(
        "/api/ui-feedback/chat",
        headers={"Authorization": "Bearer google-token"},
        json={
            "page_path": "/analysis",
            "feedback_id": "results-panel",
            "selected_text": "No data found",
            "conversation": [
                {"role": "user", "content": "How can this be more useful?"}
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["message"]["role"] == "assistant"
    assert "next step" in response.json()["message"]["content"]
    assert service.calls[0][0] == "google-token"


def test_analysis_endpoint_runs_the_injected_service(caplog):
    service = FakeAnalysisService()
    client = TestClient(create_app(service))

    with caplog.at_level(logging.INFO):
        response = client.post("/api/analysis", json={"prompt": "Show bank prices."})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["request_id"]
    assert service.calls[0][0] == payload["request_id"]
    assert service.calls[0][2] == "/api/analysis"
    event = next(
        record.structured_fields
        for record in caplog.records
        if getattr(record, "structured_fields", {}).get("event")
        == "http_request_completed"
    )
    assert event["api_route"] == "/api/analysis"
    assert event["method"] == "POST"


def test_analysis_endpoint_returns_structured_error_for_unexpected_failure(caplog):
    client = TestClient(create_app(FailingAnalysisService()))

    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/api/analysis",
            json={"prompt": "Analyze arbitrary stock data."},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["request_id"]
    assert payload["error"]["code"] == "ANALYSIS_EXECUTION_FAILED"
    event = next(
        record
        for record in caplog.records
        if getattr(record, "structured_fields", {}).get("event")
        == "analysis_request_failed"
    )
    assert event.exc_info
    assert event.structured_fields["request_id"] == payload["request_id"]


def test_complex_analysis_returns_pollable_async_task():
    coordinator = AnalysisTaskCoordinator(
        MemoryAnalysisTaskStore(),
        FakeDispatcher(),
    )
    client = TestClient(
        create_app(
            FakeAnalysisService(),
            task_coordinator=coordinator,
        )
    )

    submission_response = client.post(
        "/api/analysis",
        json={"prompt": ORIGINAL_COMPLEX_PROMPT},
    )

    assert submission_response.status_code == 202
    submission = submission_response.json()
    assert submission["status"] == "queued"
    task_response = client.get(submission["status_url"])
    assert task_response.status_code == 200
    assert task_response.json()["task_id"] == submission["task_id"]
    assert task_response.json()["status"] == "queued"
    assert "request" not in task_response.json()


def test_stock_endpoint_returns_paginated_catalog():
    stock_service = FakeStockCatalogService(response=stock_response())
    client = TestClient(
        create_app(FakeAnalysisService(), stock_catalog_service=stock_service)
    )

    response = client.get(
        "/api/stocks",
        params={
            "page": 1,
            "page_size": 20,
            "search": "bank",
            "exchange": "SZSE",
            "industry": "Banking",
        },
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["code"] == "000001.SZ"
    assert response.json()["items"][0]["listed_on"] == "1991-04-03"
    assert stock_service.calls[0][1] == {
        "page": 1,
        "page_size": 20,
        "search": "bank",
        "exchange": "SZSE",
        "industry": "Banking",
        "api_route": "/api/stocks",
    }


def test_stock_endpoint_rejects_invalid_query_parameters():
    client = TestClient(
        create_app(
            FakeAnalysisService(),
            stock_catalog_service=FakeStockCatalogService(response=stock_response()),
        )
    )

    response = client.get(
        "/api/stocks",
        params={"page": 0, "page_size": 101, "exchange": "INVALID"},
    )

    assert response.status_code == 422
    assert len(response.json()["detail"]) == 3


def test_stock_endpoint_maps_out_of_range_page_to_422():
    stock_service = FakeStockCatalogService(error=IndexError("page exceeds result"))
    client = TestClient(
        create_app(FakeAnalysisService(), stock_catalog_service=stock_service)
    )

    response = client.get("/api/stocks", params={"page": 2})

    assert response.status_code == 422
    assert response.json()["error"]["source"] == "system"
    assert response.json()["error"]["message"] == "page exceeds result"


def test_stock_endpoint_maps_provider_failure_to_502():
    stock_service = FakeStockCatalogService(
        error=TushareApiError(
            message="Permission denied.",
            code=40203,
            http_status=200,
            raw_response={"code": 40203, "msg": "Permission denied."},
        )
    )
    client = TestClient(
        create_app(FakeAnalysisService(), stock_catalog_service=stock_service)
    )

    response = client.get("/api/stocks")

    assert response.status_code == 502
    assert response.json()["error"] == {
        "source": "tushare",
        "code": 40203,
        "message": "Permission denied.",
        "http_status": 200,
        "raw_response": {"code": 40203, "msg": "Permission denied."},
    }


def test_frontend_routes_serve_the_unified_page(tmp_path, monkeypatch):
    index_file = tmp_path / "index.html"
    index_file.write_text("<html><body>A-Share Lab</body></html>", encoding="utf-8")
    monkeypatch.setattr(api_module, "FRONTEND_DIST", tmp_path)
    client = TestClient(create_app(FakeAnalysisService()))

    root_response = client.get("/", follow_redirects=False)
    analysis_response = client.get("/analysis")
    basic_response = client.get("/basic", follow_redirects=False)

    assert root_response.status_code == 307
    assert root_response.headers["location"] == "/analysis"
    assert analysis_response.status_code == 200
    assert analysis_response.text == "<html><body>A-Share Lab</body></html>"
    assert basic_response.status_code == 307
    assert basic_response.headers["location"] == "/analysis"

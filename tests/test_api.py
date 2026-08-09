import logging
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

import china_a_share.api as api_module
from china_a_share.api import create_app
from china_a_share.client import TushareApiError
from china_a_share.core.contracts import (
    AnalysisRequest,
    AnalysisTask,
    AnalysisTaskStatus,
    AnalysisTaskSubmission,
    AnalysisResponse,
    AnalysisStatus,
    ServiceError,
    StockListItem,
    StockListResponse,
    UiFeedbackConfig,
    UiFeedbackChatResponse,
    UiFeedbackConversationMessage,
    UiFeedbackStatus,
    UiFeedbackSubmission,
    DiscoveryTask,
    DiscoveryTaskProgress,
)
from china_a_share.e2e_cases import (
    LiveCaseChangeSubmission,
    LiveCaseListResponse,
)
ORIGINAL_COMPLEX_PROMPT = (
    "\u8fc7\u53bb\u4e00\u4e2a\u6708\uff0c\u533b\u7597\u884c\u4e1a\uff0c"
    "\u6309\u7167\u6563\u6237\u6bd4\u4f8b\u5206\u4e24\u534a\uff0c"
    "\u54ea\u4e00\u534a\u516c\u53f8\u4e0a\u6da8\u7684\u591a\uff1f"
)
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


class BackgroundRequiredAnalysisService(FakeAnalysisService):
    def analyze(self, request_id, request, *, api_route):
        self.calls.append((request_id, request, api_route))
        return AnalysisResponse(
            request_id=request_id,
            planner="test-planner",
            data_provider="test-provider",
            status=AnalysisStatus.ERROR,
            error=ServiceError(
                source="system",
                code="BACKGROUND_TASK_REQUIRED",
                message="Background execution is required.",
            ),
        )


class FakeAnalysisTaskCoordinator:
    def __init__(self, error=None):
        self.error = error
        self.requests = []

    def submit(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return AnalysisTaskSubmission(
            task_id="analysis-api-task",
            status=AnalysisTaskStatus.QUEUED,
            status_url="/api/analysis/tasks/analysis-api-task",
        )


class FakeDiscoveryCoordinator:
    def __init__(self):
        self.requests = []
        self.task = None

    def submit_discovery(self, request):
        self.requests.append(request)
        now = datetime.now(timezone.utc)
        self.task = DiscoveryTask(
            task_id="discovery-api-task",
            status=AnalysisTaskStatus.QUEUED,
            request=request,
            created_at=now,
            updated_at=now,
            progress=DiscoveryTaskProgress(current_stage="queued"),
        )
        return AnalysisTaskSubmission(
            task_id=self.task.task_id,
            status=self.task.status,
            status_url=f"/api/discovery/tasks/{self.task.task_id}",
        )

    def get(self, task_id):
        return self.task if self.task and self.task.task_id == task_id else None


class FailingDiscoveryCoordinator:
    def submit_discovery(self, request):
        raise RuntimeError("task queue unavailable")

    def get(self, task_id):
        raise RuntimeError("task store unavailable")


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


class FakeLiveCaseService:
    """Capture authenticated live-case API calls without external services."""

    def __init__(self):
        self.calls = []

    def list_cases(self, token):
        """Return one empty deployed catalog."""
        self.calls.append(("list", token))
        return LiveCaseListResponse(
            git_sha="a" * 40,
            cases=[],
            pending_deletions=[],
        )

    def submit(self, token, request):
        """Return one accepted deterministic mutation."""
        self.calls.append(("submit", token, request))
        return LiveCaseChangeSubmission(
            change_id="change-1",
            status="pending",
            actions_url="https://github.com/example/repository/actions",
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


def test_capabilities_endpoint_exposes_the_runtime_manifest():
    service = FakeAnalysisService()
    service.capability_manifest = {
        "schema_version": 1,
        "fingerprint": "sha256:abc",
        "capabilities": [{"id": "limit_up_streak", "version": 1}],
    }
    client = TestClient(create_app(service))

    response = client.get("/api/capabilities")

    assert response.status_code == 200
    assert response.json() == service.capability_manifest


def test_live_case_list_requires_admin_and_returns_merged_catalog():
    live_case_service = FakeLiveCaseService()
    client = TestClient(
        create_app(
            FakeAnalysisService(),
            live_case_service=live_case_service,
        )
    )

    unauthorized = client.get("/api/e2e-cases")
    response = client.get(
        "/api/e2e-cases",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json()["git_sha"] == "a" * 40
    assert live_case_service.calls == [("list", "admin-token")]


def test_live_case_change_dispatches_structured_mutation():
    live_case_service = FakeLiveCaseService()
    client = TestClient(
        create_app(
            FakeAnalysisService(),
            live_case_service=live_case_service,
        )
    )

    response = client.post(
        "/api/e2e-cases/changes",
        headers={"Authorization": "Bearer admin-token"},
        json={
            "operation": "delete",
            "case_id": "case-1",
            "base_git_sha": "a" * 40,
        },
    )

    assert response.status_code == 202
    assert response.json()["change_id"] == "change-1"
    _, token, request = live_case_service.calls[0]
    assert token == "admin-token"
    assert request.operation == "delete"
    assert request.case_id == "case-1"


def test_discovery_endpoints_validate_submit_and_return_progress():
    coordinator = FakeDiscoveryCoordinator()
    client = TestClient(
        create_app(FakeAnalysisService(), task_coordinator=coordinator)
    )

    submission = client.post(
        "/api/discovery/tasks",
        json={
            "target_pool": "A_SHARE",
            "train_start": "20240101",
            "train_end": "20251231",
            "val_start": "20260101",
            "val_end": "20260630",
            "factors": ["pe_ttm", "turnover_rate"],
            "prompt": "Find robust event patterns",
            "max_generations": 1,
            "forward_days": 20,
            "minimum_samples": 30,
            "minimum_trading_days": 20,
            "minimum_securities": 10,
            "minimum_outcome_coverage_pct": 95,
            "max_conditions": 2,
        },
    )

    assert submission.status_code == 202
    assert submission.json()["task_id"] == "discovery-api-task"
    assert coordinator.requests[0].forward_days == 20
    assert coordinator.requests[0].minimum_trading_days == 20
    assert coordinator.requests[0].minimum_securities == 10
    assert coordinator.requests[0].minimum_outcome_coverage_pct == 95

    status_response = client.get("/api/discovery/tasks/discovery-api-task")
    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["progress"]["current_stage"] == "queued"
    assert status_payload["research_config"] == {
        "target_pool": "A_SHARE",
        "train_start": "20240101",
        "train_end": "20251231",
        "val_start": "20260101",
        "val_end": "20260630",
        "factors": ["pe_ttm", "turnover_rate"],
        "forward_days": 20,
        "target_return_pct": 0.0,
        "minimum_samples": 30,
        "minimum_trading_days": 20,
        "minimum_securities": 10,
        "minimum_outcome_coverage_pct": 95.0,
        "max_conditions": 2,
    }
    assert "prompt" not in status_payload["research_config"]


def test_discovery_endpoint_rejects_overlapping_windows():
    client = TestClient(
        create_app(
            FakeAnalysisService(),
            task_coordinator=FakeDiscoveryCoordinator(),
        )
    )

    response = client.post(
        "/api/discovery/tasks",
        json={
            "target_pool": "A_SHARE",
            "train_start": "20250101",
            "train_end": "20251231",
            "val_start": "20251201",
            "val_end": "20260630",
            "factors": ["pe_ttm"],
        },
    )

    assert response.status_code == 422


def test_discovery_endpoint_rejects_unsupported_generation_count():
    client = TestClient(
        create_app(
            FakeAnalysisService(),
            task_coordinator=FakeDiscoveryCoordinator(),
        )
    )

    response = client.post(
        "/api/discovery/tasks",
        json={
            "target_pool": "A_SHARE",
            "train_start": "20250101",
            "train_end": "20251231",
            "val_start": "20260101",
            "val_end": "20260630",
            "factors": ["pe_ttm"],
            "max_generations": 2,
        },
    )

    assert response.status_code == 422


def test_discovery_endpoint_rejects_duplicate_factors():
    client = TestClient(
        create_app(
            FakeAnalysisService(),
            task_coordinator=FakeDiscoveryCoordinator(),
        )
    )

    response = client.post(
        "/api/discovery/tasks",
        json={
            "target_pool": "A_SHARE",
            "train_start": "20250101",
            "train_end": "20251231",
            "val_start": "20260101",
            "val_end": "20260630",
            "factors": ["pe_ttm", "pe_ttm"],
        },
    )

    assert response.status_code == 422


def test_discovery_status_hides_missing_and_non_discovery_tasks():
    coordinator = FakeDiscoveryCoordinator()
    client = TestClient(
        create_app(FakeAnalysisService(), task_coordinator=coordinator)
    )

    missing = client.get("/api/discovery/tasks/missing-task")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Discovery task was not found."

    now = datetime.now(timezone.utc)
    coordinator.task = AnalysisTask(
        task_id="analysis-task",
        status=AnalysisTaskStatus.QUEUED,
        request=AnalysisRequest(prompt="Show recent A-share prices."),
        created_at=now,
        updated_at=now,
    )
    wrong_type = client.get("/api/discovery/tasks/analysis-task")
    assert wrong_type.status_code == 404
    assert wrong_type.json()["detail"] == "Discovery task was not found."


def test_discovery_endpoints_map_coordinator_failures_to_service_unavailable():
    client = TestClient(
        create_app(
            FakeAnalysisService(),
            task_coordinator=FailingDiscoveryCoordinator(),
        )
    )
    payload = {
        "target_pool": "A_SHARE",
        "train_start": "20250101",
        "train_end": "20251231",
        "val_start": "20260101",
        "val_end": "20260630",
        "factors": ["pe_ttm"],
    }

    submission = client.post("/api/discovery/tasks", json=payload)
    assert submission.status_code == 503
    assert submission.json()["detail"] == (
        "Discovery task submission is temporarily unavailable."
    )

    task_status = client.get("/api/discovery/tasks/discovery-task")
    assert task_status.status_code == 503
    assert task_status.json()["detail"] == (
        "Discovery task status is temporarily unavailable."
    )


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


def test_analysis_endpoint_queues_supported_background_work(caplog):
    service = BackgroundRequiredAnalysisService()
    coordinator = FakeAnalysisTaskCoordinator()
    client = TestClient(create_app(service, task_coordinator=coordinator))

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/api/analysis",
            json={"prompt": "A股2026年汽车行业，市盈率和分红数据"},
        )

    assert response.status_code == 202
    assert response.json() == {
        "task_id": "analysis-api-task",
        "status": "queued",
        "status_url": "/api/analysis/tasks/analysis-api-task",
    }
    assert coordinator.requests[0].prompt == "A股2026年汽车行业，市盈率和分红数据"
    event = next(
        record.structured_fields
        for record in caplog.records
        if getattr(record, "structured_fields", {}).get("event")
        == "analysis_background_task_submitted"
    )
    assert event["request_id"] == service.calls[0][0]
    assert event["task_id"] == "analysis-api-task"


def test_analysis_endpoint_returns_503_when_background_queue_fails():
    coordinator = FakeAnalysisTaskCoordinator(error=RuntimeError("queue unavailable"))
    client = TestClient(
        create_app(
            BackgroundRequiredAnalysisService(),
            task_coordinator=coordinator,
        )
    )

    response = client.post(
        "/api/analysis",
        json={"prompt": "Run one supported fan-out analysis."},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Background analysis task submission is temporarily unavailable."
    )


def test_analysis_prompt_does_not_select_a_special_delivery_mode():
    client = TestClient(create_app(FakeAnalysisService()))

    response = client.post(
        "/api/analysis",
        json={"prompt": ORIGINAL_COMPLEX_PROMPT},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"


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

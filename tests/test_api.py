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
)


class FakeAnalysisService:
    def __init__(self):
        self.calls = []

    def analyze(self, request_id, request, *, api_route):
        self.calls.append((request_id, request, api_route))
        return AnalysisResponse(
            request_id=request_id,
            planner="test-planner",
            data_provider="test-provider",
            status=AnalysisStatus.SUCCESS,
        )


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
    assert response.json() == {"status": "ok"}
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

from fastapi.testclient import TestClient

from china_a_share.api import create_app
from china_a_share.contracts import AnalysisResponse, AnalysisStatus


class FakeAnalysisService:
    def analyze(self, request_id, request):
        return AnalysisResponse(
            request_id=request_id,
            status=AnalysisStatus.SUCCESS,
        )


client = TestClient(create_app(FakeAnalysisService()))


def test_health_endpoint_reports_backend_availability():
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_analysis_endpoint_runs_the_injected_service():
    response = client.post("/api/analysis", json={"prompt": "Show bank prices."})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["request_id"]

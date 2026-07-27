import base64
import json

import pandas as pd
import pytest
import requests

from china_a_share.application.workflow import (
    ASharePlanValidator,
    AnalysisService,
    DataQueryExecutor,
    SCREENSHOT_EVIDENCE_END,
    SCREENSHOT_EVIDENCE_START,
)
from china_a_share.core.contracts import (
    AnalysisImage,
    AnalysisRequest,
    DataOperation,
    DataQuery,
    QueryPlan,
)
from china_a_share.core.errors import VisionError
from china_a_share.vision.glm import (
    GLM_CHAT_COMPLETIONS_URL,
    GLM_VISION_MODEL,
    GLMVisionAnalyzer,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None, exception=None):
        self.response = response
        self.exception = exception
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exception:
            raise self.exception
        return self.response


class CapturingPlanner:
    def __init__(self):
        self.request = None

    @property
    def name(self):
        return "test-planner"

    def plan(self, request, candidate_operations):
        self.request = request
        return QueryPlan(
            interpretation=request.prompt,
            requirements=[
                {
                    "requirement": "Retrieve visible security data.",
                    "status": "covered",
                    "implementation": "Use the daily operation.",
                    "evidence": "The candidate catalog documents daily prices.",
                }
            ],
            queries=[
                DataQuery(
                    query_id="q1",
                    operation=candidate_operations[0].name,
                    params={"trade_date": "20260717"},
                    purpose="Retrieve visible security data.",
                )
            ],
        )


class FakeProvider:
    @property
    def name(self):
        return "test-provider"

    def search_operations(self, prompt):
        return [DataOperation(name="daily", description="Daily prices.")]

    def supports(self, operation):
        return operation == "daily"

    def query(
        self,
        operation,
        params,
        fields,
        *,
        api_route,
        request_id,
        query_id,
    ):
        return pd.DataFrame([{"ts_code": "000001.SZ", "close": 10.0}])


class FakeVisionAnalyzer:
    def __init__(self, description):
        self.description = description
        self.calls = []

    @property
    def name(self):
        return "glm"

    def analyze(self, prompt, image):
        self.calls.append((prompt, image))
        return self.description


def screenshot():
    return AnalysisImage(
        media_type="image/png",
        base64_data=base64.b64encode(b"screenshot").decode("ascii"),
    )


def test_glm_vision_analyzer_sends_bounded_multimodal_request():
    session = FakeSession(
        FakeResponse(
            {
                "id": "request-1",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "The screenshot shows 000001.SZ on 2026-07-17.",
                        }
                    }
                ],
            }
        )
    )

    result = GLMVisionAnalyzer("zai-key", session=session).analyze(
        "Which security is shown?",
        screenshot(),
    )

    assert result == "The screenshot shows 000001.SZ on 2026-07-17."
    call_args, call_kwargs = session.calls[0]
    assert call_args == (GLM_CHAT_COMPLETIONS_URL,)
    assert call_kwargs["headers"]["Authorization"] == "Bearer zai-key"
    assert call_kwargs["json"]["model"] == GLM_VISION_MODEL
    assert call_kwargs["json"]["thinking"] == {"type": "disabled"}
    assert call_kwargs["json"]["stream"] is False
    messages = call_kwargs["json"]["messages"]
    assert messages[0]["role"] == "system"
    content = messages[1]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1] == {"type": "text", "text": "Which security is shown?"}


def test_glm_vision_analyzer_preserves_upstream_error():
    payload = {
        "error": {
            "message": "Invalid API key.",
            "code": "invalid_api_key",
        }
    }
    analyzer = GLMVisionAnalyzer(
        "bad-key",
        session=FakeSession(FakeResponse(payload, status_code=401)),
    )

    with pytest.raises(VisionError, match="Invalid API key") as captured:
        analyzer.analyze("Read this screenshot.", screenshot())

    assert captured.value.source == "glm"
    assert captured.value.code == "invalid_api_key"
    assert captured.value.http_status == 401
    assert captured.value.raw_response == payload


def test_glm_vision_analyzer_converts_network_failure():
    analyzer = GLMVisionAnalyzer(
        "zai-key",
        session=FakeSession(exception=requests.ConnectionError("network unavailable")),
    )

    with pytest.raises(VisionError, match="network unavailable"):
        analyzer.analyze("Read this screenshot.", screenshot())


def test_workflow_marks_vision_output_as_untrusted_evidence():
    planner = CapturingPlanner()
    provider = FakeProvider()
    vision = FakeVisionAnalyzer("Visible label: Ping An Bank 000001.SZ.")
    service = AnalysisService(
        planner,
        provider,
        ASharePlanValidator(provider),
        DataQueryExecutor(provider),
        vision_analyzer=vision,
    )

    response = service.analyze(
        "request-vision",
        AnalysisRequest(prompt="Identify this security.", image=screenshot()),
        api_route="/api/analysis",
    )

    assert response.status == "success"
    assert len(vision.calls) == 1
    assert planner.request is not None
    assert planner.request.image is None
    assert SCREENSHOT_EVIDENCE_START in planner.request.prompt
    assert "Visible label: Ping An Bank 000001.SZ." in planner.request.prompt
    assert SCREENSHOT_EVIDENCE_END in planner.request.prompt


def test_workflow_returns_glm_error_when_vision_is_not_configured():
    planner = CapturingPlanner()
    provider = FakeProvider()
    service = AnalysisService(
        planner,
        provider,
        ASharePlanValidator(provider),
        DataQueryExecutor(provider),
    )

    response = service.analyze(
        "request-vision",
        AnalysisRequest(prompt="Identify this security.", image=screenshot()),
        api_route="/api/analysis",
    )

    assert response.status == "error"
    assert response.error is not None
    assert response.error.source == "glm"
    assert "ZAI_API_KEY" in response.error.message
    assert planner.request is None

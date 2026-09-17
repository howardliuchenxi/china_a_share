from io import StringIO

import pandas as pd
import pytest

from china_a_share.research_sandbox import (
    RemotePythonSandbox,
    RestrictedDataFrameRunner,
    SandboxDataset,
    SandboxRequest,
    SandboxValidationError,
)


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, frame):
        self._frame = frame

    def json(self):
        return {"frame": self._frame.to_json(orient="split")}


class RecordingSession:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.frame)


def sandbox_request(code):
    frame = pd.DataFrame(
        [
            {"ts_code": "000001.SZ", "pct_chg": 1.0},
            {"ts_code": "000001.SZ", "pct_chg": 2.0},
            {"ts_code": "600000.SH", "pct_chg": -1.0},
        ]
    )
    return SandboxRequest(
        code=code,
        datasets=[
            SandboxDataset(
                dataset_id="prices",
                frame=frame.to_json(orient="split"),
            )
        ],
    )


def test_restricted_runner_executes_dataframe_calculation_in_child_process():
    response = RestrictedDataFrameRunner().run(
        sandbox_request(
            'prices = datasets["prices"].copy()\n'
            'result = prices.groupby("ts_code")["pct_chg"].sum().reset_index()\n'
            'result = result.sort_values("pct_chg", ascending=False)'
        )
    )

    result = pd.read_json(StringIO(response.frame), orient="split")
    assert result.to_dict(orient="records") == [
        {"ts_code": "000001.SZ", "pct_chg": 3},
        {"ts_code": "600000.SH", "pct_chg": -1},
    ]


def test_remote_sandbox_uses_private_service_identity_token():
    session = RecordingSession(pd.DataFrame([{"value": 3}]))
    audiences = []
    sandbox = RemotePythonSandbox(
        "https://sandbox.example/",
        session=session,
        identity_token_provider=lambda audience: audiences.append(audience) or "token",
    )

    result = sandbox.run(
        'result = datasets["prices"].copy()',
        {"prices": pd.DataFrame([{"value": 3}])},
    )

    assert result.to_dict(orient="records") == [{"value": 3}]
    assert audiences == ["https://sandbox.example"]
    url, request = session.calls[0]
    assert url == "https://sandbox.example/v1/execute"
    assert request["headers"]["Authorization"] == "Bearer token"


@pytest.mark.parametrize(
    "code, expected",
    [
        ('import os\nresult = datasets["prices"]', "Import"),
        ('result = open("/etc/passwd")', "open"),
        ('result = datasets["prices"].__class__', "__class__"),
    ],
)
def test_restricted_runner_rejects_system_access(code, expected):
    with pytest.raises(SandboxValidationError, match=expected):
        RestrictedDataFrameRunner().run(sandbox_request(code))

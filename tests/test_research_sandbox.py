from io import StringIO

import pandas as pd
import pytest

from china_a_share.research_sandbox import (
    RemotePythonSandbox,
    RestrictedDataFrameRunner,
    SandboxDataset,
    SandboxRequest,
    SandboxValidationError,
    research_flag_overlapping_signals,
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


def signal_frame():
    return pd.DataFrame(
        {
            "ts_code": [
                "000001.SZ",
                "000001.SZ",
                "000001.SZ",
                "600000.SH",
                "600000.SH",
            ],
            "signal_date": [
                "2026-01-05",
                "2026-02-01",
                "2026-05-10",
                "2026-01-05",
                "2026-06-01",
            ],
            "window_end": [
                "2026-04-10",
                "2026-05-05",
                "2026-08-10",
                "2026-02-10",
                "2026-09-01",
            ],
        }
    )


def test_overlapping_same_security_signals_collapse_to_one_event():
    frame = signal_frame()

    mask = research_flag_overlapping_signals(
        frame,
        ts_code="ts_code",
        signal_date="signal_date",
        window_end="window_end",
    )

    # The January and February signals of 000001.SZ overlap and merge into one
    # event; the May signal starts after the merged window closes and is its
    # own event. The 600000.SH signals never overlap and both stay.
    assert mask.tolist() == [True, False, True, True, True]


def test_overlap_boundary_and_missing_window_end_semantics():
    boundary = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "signal_date": ["2026-01-05", "2026-04-10"],
            "window_end": ["2026-04-10", "2026-07-10"],
        }
    )
    # A signal landing exactly on the previous window end still overlaps.
    assert research_flag_overlapping_signals(
        boundary, "ts_code", "signal_date", "window_end"
    ).tolist() == [True, False]

    unknown_end = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ", "000001.SZ"],
            "signal_date": ["2026-01-05", "2026-01-06", "2026-02-01"],
            "window_end": [None, "2026-04-06", None],
        }
    )
    # Unknown window ends never block later signals; the next day's signal is
    # kept as its own event and then blocks through its own window end.
    assert research_flag_overlapping_signals(
        unknown_end, "ts_code", "signal_date", "window_end"
    ).tolist() == [True, True, False]


def test_overlap_mask_stays_aligned_with_unsorted_input():
    frame = signal_frame().iloc[[3, 1, 0, 4, 2]].reset_index(drop=True)

    mask = research_flag_overlapping_signals(
        frame,
        ts_code="ts_code",
        signal_date="signal_date",
        window_end="window_end",
    )

    assert mask.tolist() == [True, False, True, True, True]


def test_restricted_runner_exposes_overlap_helper_to_model_code():
    frame = signal_frame()
    request = SandboxRequest(
        code=(
            "signals = datasets[\"signals\"].copy()\n"
            "signals[\"is_first\"] = research_flag_overlapping_signals(\n"
            "    signals,\n"
            "    ts_code=\"ts_code\",\n"
            "    signal_date=\"signal_date\",\n"
            "    window_end=\"window_end\",\n"
            ")\n"
            "result = signals"
        ),
        datasets=[
            SandboxDataset(
                dataset_id="signals",
                frame=frame.to_json(orient="split"),
            )
        ],
    )

    response = RestrictedDataFrameRunner().run(request)
    result = pd.read_json(StringIO(response.frame), orient="split")

    assert result["is_first"].tolist() == [True, False, True, True, True]


def test_overlap_helper_requires_signal_columns():
    frame = pd.DataFrame({"code": ["000001.SZ"], "date": ["2026-01-05"]})
    with pytest.raises(ValueError, match="requires column 'window_end'"):
        research_flag_overlapping_signals(frame, "code", "date", "window_end")

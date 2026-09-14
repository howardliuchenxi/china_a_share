import pandas as pd

from china_a_share.feishu import FeishuConversationTurn
from china_a_share.research_chat import (
    LimitUpStudyRequest,
    compile_limit_up_study,
    format_limit_up_study,
    run_limit_up_study,
)


class FakeMarketDataProvider:
    def query(self, operation, params, fields, **kwargs):
        if operation == "trade_cal":
            return pd.DataFrame(
                {
                    "cal_date": [
                        "20260730",
                        "20260731",
                        "20260803",
                        "20260804",
                        "20260805",
                        "20260806",
                    ]
                }
            )
        if operation == "limit_list_d":
            return pd.DataFrame(
                {
                    "trade_date": ["20260803", "20260804"],
                    "ts_code": ["000001.SZ", "000001.SZ"],
                    "name": ["Example", "Example"],
                }
            )
        if operation == "daily":
            return pd.DataFrame(
                {
                    "ts_code": ["000001.SZ"],
                    "trade_date": [params["trade_date"]],
                    "high": [12.0],
                    "low": [10.0],
                    "close": [12.0 if params["trade_date"] == "20260805" else 11.0],
                    "pre_close": [11.0 if params["trade_date"] == "20260805" else 10.0],
                    "pct_chg": [9.09 if params["trade_date"] == "20260805" else 10.0],
                }
            )
        raise AssertionError(f"Unexpected operation: {operation}")


def test_compiler_builds_explicit_month_range_without_upper_workflow():
    study = compile_limit_up_study(
        "统计2026年1-8月连续2天涨停后第三天上涨概率，剔除一字板",
        [],
    )

    assert study == LimitUpStudyRequest(
        year=2026,
        start_month=1,
        end_month=8,
        exclude_one_price=True,
    )


def test_compiler_refines_prior_validated_tool_arguments():
    prior = LimitUpStudyRequest(
        year=2026,
        start_month=1,
        end_month=8,
        exclude_one_price=True,
    )
    conversation = [
        FeishuConversationTurn(
            prompt="统计2026年1-8月二连板",
            interpretation=prior.serialize(),
        )
    ]

    study = compile_limit_up_study("改成只看8月，并且包含一字板", conversation)

    assert study == LimitUpStudyRequest(
        year=2026,
        start_month=8,
        end_month=8,
        exclude_one_price=False,
    )


def test_compiler_rejects_unrelated_question_without_guessing():
    assert compile_limit_up_study("哪家公司基本面最好？", []) is None


def test_limit_up_study_uses_trade_calendar_without_shadowing_module():
    rows = run_limit_up_study(
        FakeMarketDataProvider(),
        LimitUpStudyRequest(2026, 8, 8),
        request_id="request-1",
    )

    assert rows == [
        {
            "month": 8,
            "events": 1,
            "up": 1,
            "probability": 1.0,
            "mean_return": 9.09,
            "median_return": 9.09,
        }
    ]


def test_formatter_keeps_sample_size_and_request_trace():
    text = format_limit_up_study(
        [
            {
                "month": 8,
                "events": 155,
                "up": 100,
                "probability": 100 / 155,
                "mean_return": 3.0,
                "median_return": 3.3,
            }
        ],
        LimitUpStudyRequest(2026, 8, 8),
        "request-1",
    )

    assert "8月｜155｜100｜64.52%｜+3.00%｜+3.30%" in text
    assert "请求编号：request-1" in text

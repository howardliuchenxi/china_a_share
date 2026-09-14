from china_a_share.feishu import FeishuConversationTurn
from china_a_share.research_chat import (
    LimitUpStudyRequest,
    compile_limit_up_study,
    format_limit_up_study,
)


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

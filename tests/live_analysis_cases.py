"""Unified paid live-analysis cases for real DeepSeek and Tushare validation."""

from typing import Any, Dict, List

from golden_questions import GOLDEN_QUESTION_FAMILIES


LIVE_SUPPORTED_FAMILIES = (
    "market_breadth",
    "limit_up_list",
    "limit_up_trend",
    "two_limit_up_probability",
    "limit_up_forward_horizon",
    "market_period_return",
    "valuation_period_return",
    "valuation_screen",
    "liquidity_ranking",
    "block_trade",
    "holder_count",
    "security_moneyflow",
    "margin_financing",
    "financial_statements",
    "dividend",
)
LIVE_UNSUPPORTED_CASE_COUNTS = {
    "verified_retail_ownership": 1,
    "future_price_prediction": 2,
    "investor_demographics": 1,
    "market_wide_dividend_total": 1,
}
GOLDEN_FAMILY_BY_NAME = {
    family["family"]: family for family in GOLDEN_QUESTION_FAMILIES
}


LIVE_ANALYSIS_CASES: List[Dict[str, Any]] = [
    {
        "family": family_name,
        "tier": GOLDEN_FAMILY_BY_NAME[family_name]["tier"],
        "operations": GOLDEN_FAMILY_BY_NAME[family_name]["operations"],
        "quality_invariants": GOLDEN_FAMILY_BY_NAME[family_name].get(
            "quality_invariants", []
        ),
        "prompt": prompt,
    }
    for family_name in LIVE_SUPPORTED_FAMILIES
    for prompt in GOLDEN_FAMILY_BY_NAME[family_name]["prompts"]
] + [
    {
        "family": family_name,
        "tier": GOLDEN_FAMILY_BY_NAME[family_name]["tier"],
        "operations": GOLDEN_FAMILY_BY_NAME[family_name]["operations"],
        "quality_invariants": GOLDEN_FAMILY_BY_NAME[family_name].get(
            "quality_invariants", []
        ),
        "prompt": prompt,
    }
    for family_name, count in LIVE_UNSUPPORTED_CASE_COUNTS.items()
    for prompt in GOLDEN_FAMILY_BY_NAME[family_name]["prompts"][:count]
] + [
    # Historical snapshots test explicit dates, direction, ties, and metric units.
    *[
        {"family": "historical_market_snapshot", "tier": "supported", "operations": ["daily"], "prompt": prompt}
        for prompt in (
            "Rank the 15 largest A-share gainers on 2026-06-18.",
            "List the ten largest A-share decliners on 2026-05-29.",
            "How many A-shares closed unchanged on 2026-04-30?",
            "Which A-share had the highest trading amount on 2026-03-16?",
            "Show the bottom 20 A-shares by percentage change on 2026-02-27.",
        )
    ],
    # Security windows stress name/code resolution and daily/weekly/monthly grain.
    *[
        {"family": "security_price_window", "tier": "supported", "operations": ["daily", "weekly", "monthly"], "prompt": prompt}
        for prompt in (
            "Show 600519.SH daily closes from 2026-01-05 through 2026-01-30.",
            "Ping An Bank weekly prices for the first half of 2026.",
            "China Ping An monthly price history during 2025.",
            "Compare Kweichow Moutai's highest and lowest close in June 2026.",
            "\u67e5\u770b000001.SZ\u8fd120\u4e2a\u4ea4\u6613\u65e5\u7684\u6536\u76d8\u4ef7\u8d70\u52bf",
        )
    ],
    # Composite valuation stresses local filters, missing values, and sort order.
    *[
        {"family": "composite_valuation", "tier": "supported", "operations": ["daily_basic"], "prompt": prompt}
        for prompt in (
            "Find A-shares with PE below 15 and PB below 2 on the latest trading day.",
            "Top 20 A-shares by dividend yield, excluding missing or zero yields.",
            "\u7b5b\u9009PE\u4e3a\u6b63\u4e14\u5c0f\u4e8e20\u3001\u6362\u624b\u7387\u5927\u4e8e3%\u7684A\u80a1",
            "\u627e\u51fa\u603b\u5e02\u503c\u8d85\u8fc71000\u4ebf\u4e14PB\u6700\u4f4e\u7684\u524d10\u53ea",
            "Rank the latest 30 stocks by lowest positive PE TTM, not static PE.",
        )
    ],
    *[
        {"family": "suspension_history", "tier": "supported", "operations": ["suspend_d"], "prompt": prompt}
        for prompt in (
            "List all A-shares suspended on 2026-06-12.",
            "Which stocks resumed trading on 2026-05-08?",
            "Show 600519.SH suspension and resumption records since 2020.",
            "\u8fc7\u53bb\u4e00\u4e2a\u6708\u505c\u724c\u5929\u6570\u6700\u591a\u7684\u80a1\u7968",
            "\u67e5\u8be22026\u5e744\u6708\u6240\u6709\u505c\u590d\u724c\u8bb0\u5f55",
        )
    ],
    *[
        {
            "family": "share_unlock_boundaries",
            "tier": "unsupported" if any(
                term in prompt
                for term in ("\u5360\u603b\u80a1\u672c\u6bd4\u4f8b", "distinct")
            ) else "supported",
            "operations": ["share_float"],
            "prompt": prompt,
        }
        for prompt in (
            "A-share unlocks scheduled between 2026-09-01 and 2026-09-30.",
            "Top 20 September 2026 unlocks by unlocked share count.",
            "\u5217\u51fa\u89e3\u7981\u80a1\u6570\u5360\u603b\u80a1\u672c\u6bd4\u4f8b\u6700\u9ad8\u768410\u53ea\u80a1\u7968",
            "\u67e5\u770b600519.SH\u4e0b\u4e00\u6b21\u9650\u552e\u80a1\u4e0a\u5e02\u65e5\u671f\u548c\u6570\u91cf",
            "How many distinct companies have unlocks in Q4 2026?",
        )
    ],
    *[
        {"family": "repurchase_detail", "tier": "supported", "operations": ["repurchase"], "prompt": prompt}
        for prompt in (
            "Latest repurchase progress for 600519.SH.",
            "Rank 2026 A-share repurchase plans by announced upper amount.",
            "\u8fc7\u53bb90\u5929\u5df2\u5b8c\u6210\u56de\u8d2d\u7684\u516c\u53f8\u6709\u54ea\u4e9b",
            "\u67e5\u770b\u5e73\u5b89\u94f6\u884c\u56de\u8d2d\u8d77\u6b62\u65e5\u671f\u3001\u4ef7\u683c\u4e0a\u9650\u548c\u5df2\u56de\u8d2d\u6570\u91cf",
            "Count distinct A-shares that announced repurchases in June 2026.",
        )
    ],
    *[
        {
            "family": "holder_transactions",
            "tier": "supported" if any(code in prompt for code in ("600519.SH", "China Ping An")) else "unsupported",
            "operations": ["stk_holdertrade"],
            "prompt": prompt,
        }
        for prompt in (
            "Major shareholder purchases of 600519.SH since 2025.",
            "Largest disclosed shareholder reduction amount in June 2026.",
            "\u8fd1\u4e00\u4e2a\u6708\u9ad8\u7ba1\u589e\u6301\u7684A\u80a1\u5217\u8868",
            "\u7edf\u8ba12026\u5e74\u80a1\u4e1c\u589e\u6301\u548c\u51cf\u6301\u6b21\u6570",
            "Show China Ping An shareholder trades with holder names and change ratios.",
        )
    ],
    *[
        {
            "family": "earnings_guidance",
            "tier": "unsupported" if any(
                term in prompt
                for term in ("largest profit increase", "\u9884\u4e8f\u516c\u53f8\u6570\u91cf")
            ) else "supported",
            "operations": ["forecast", "express"],
            "prompt": prompt,
        }
        for prompt in (
            "Kweichow Moutai's latest earnings forecast range.",
            "A-shares forecasting the largest profit increase for 2026 H1.",
            "\u67e5\u770b\u5e73\u5b89\u94f6\u884c2025\u5e74\u4e1a\u7ee9\u5feb\u62a5",
            "\u7edf\u8ba12026\u5e74\u4e0a\u534a\u5e74\u9884\u4e8f\u516c\u53f8\u6570\u91cf",
            "Compare forecast lower and upper net-profit bounds for 600519.SH.",
        )
    ],
    *[
        {"family": "business_segments", "tier": "supported", "operations": ["fina_mainbz"], "prompt": prompt}
        for prompt in (
            "Kweichow Moutai 2025 revenue by product segment.",
            "China Ping An's latest revenue split by business line.",
            "\u67e5\u770b600519.SH\u8fd1\u4e09\u5e74\u4e3b\u8425\u4e1a\u52a1\u6bdb\u5229\u7387\u53d8\u5316",
            "\u54ea\u4e2a\u4ea7\u54c1\u5360\u8d35\u5dde\u8305\u53f0\u8425\u4e1a\u6536\u5165\u6bd4\u4f8b\u6700\u9ad8",
            "List Ping An Bank's domestic and overseas segment revenue for 2025.",
        )
    ],
    *[
        {"family": "unverifiable_microstructure", "tier": "unsupported", "operations": [], "prompt": prompt}
        for prompt in (
            "Give every canceled order for 600519.SH at 10:31:07 yesterday.",
            "Identify the real beneficial owner behind every anonymous trade.",
            "\u7cbe\u786e\u5217\u51fa\u6240\u6709A\u80a1\u8d26\u6237\u7684\u5b9e\u65f6\u6301\u4ed3\u548c\u8eab\u4efd\u8bc1\u53f7",
            "Guarantee which order in tomorrow's opening auction will execute first.",
            "\u8fd8\u539f\u67d0\u80a1\u7968\u8fc7\u53bb\u4e00\u5e74\u6bcf\u7b14\u59d4\u6258\u7684\u5b8c\u6574\u8ba2\u5355\u7c3f",
        )
    ],
]

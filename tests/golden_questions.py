"""Curated question families for deterministic planning regression coverage."""

from typing import Any, Dict, List


GOLDEN_QUESTION_FAMILIES: List[Dict[str, Any]] = [
    {
        "family": "market_breadth",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["daily"],
        "prompts": [
            "\u4eca\u5929A\u80a1\u4e0a\u6da8\u3001\u4e0b\u8dcc\u548c\u5e73\u76d8\u5404\u6709\u591a\u5c11\u53ea\uff1f",
            "\u7edf\u8ba1\u6700\u8fd1\u4ea4\u6613\u65e5\u5927A\u6da8\u8dcc\u5bb6\u6570",
            "\u6628\u5929\u5168\u5e02\u573a\u7ea2\u76d8\u548c\u7eff\u76d8\u80a1\u7968\u6570\u91cf",
        ],
    },
    {
        "family": "limit_up_list",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["limit_list_d"],
        "prompts": [
            "\u6628\u5929\u6da8\u505c\u7684\u80a1\u7968\u6709\u54ea\u4e9b\uff1f",
            "\u5217\u51fa\u6700\u8fd1\u4ea4\u6613\u65e5\u6240\u6709\u6da8\u505c\u80a1",
            "\u5927A\u4eca\u5929\u6da8\u505c\u4e86\u591a\u5c11\u53ea\uff0c\u5206\u522b\u662f\u8c01\uff1f",
        ],
    },
    {
        "family": "limit_up_trend",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["limit_list_d", "daily"],
        "prompts": [
            "\u8fc7\u53bb\u4e00\u4e2a\u6708\u6bcf\u5929\u7684\u6da8\u505c\u6570\u91cf",
            "\u4eca\u5e74\u54ea\u4e00\u5929\u6da8\u505c\u80a1\u6700\u591a\uff1f",
            "\u8fd1\u4e09\u5341\u4e2a\u4ea4\u6613\u65e5\u6da8\u505c\u8d8b\u52bf",
        ],
    },
    {
        "family": "two_limit_up_probability",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["limit_list_d", "daily"],
        "quality_invariants": [
            "native_limit_up_source",
            "consecutive_session_count",
            "valid_sample_count",
        ],
        "prompts": [
            "\u8fde\u7eed\u4e24\u5929\u6da8\u505c\u540e\u7b2c\u4e09\u5929\u4e0a\u6da8\u7684\u6982\u7387",
            "\u8fc7\u53bb\u4e00\u4e2a\u6708\u4e8c\u8fde\u677f\u7b2c\u4e09\u65e5\u6536\u6da8\u6bd4\u4f8b",
            "\u4e24\u4e2a\u4ea4\u6613\u65e5\u8fde\u677f\u540e\u4e0b\u4e00\u5929\u8fd8\u6da8\u7684\u9891\u7387",
        ],
    },
    {
        "family": "limit_up_forward_horizon",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["limit_list_d", "daily"],
        "quality_invariants": [
            "native_limit_up_source",
            "consecutive_session_count",
            "future_horizon",
            "valid_sample_count",
        ],
        "prompts": [
            "A股20260101～20260601连续涨停三天的情况下，接下来一个月的上涨情况数据分析",
            "统计20250101至20251231三连板事件未来三个月的收益表现",
            "分析过去一年连续四个交易日涨停后未来两周的上涨概率和收益分布",
        ],
    },
    {
        "family": "market_period_return",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["daily"],
        "quality_invariants": [
            "period_return_direction",
            "sort_before_limit",
        ],
        "prompts": [
            "A\u80a16\u6708\u6da8\u5e45\u6700\u5927\u7684\u516c\u53f8\u662f",
            "\u5927A\u57286\u6708\u4e0a\u6da8\u6700\u591a\u7684\u80a1\u7968\u524d\u5341",
            "2026\u5e74A\u80a1\u8dcc\u5e45\u6700\u5927\u7684\u524d10\u53ea",
        ],
    },
    {
        "family": "st_period_return",
        "tier": "approximation",
        "delivery": "sync",
        "operations": ["stock_basic", "daily"],
        "prompts": [
            "st公司最近3个月的涨跌幅",
            "带ST的股票过去一年的收益率如何",
            "ST板块最近半个月的区间涨幅",
        ],
    },
    {
        "family": "valuation_period_return",
        "tier": "approximation",
        "delivery": "sync",
        "operations": ["daily_basic", "daily"],
        "prompts": [
            "市盈率p90线上的选10家公司，看看最近半年的涨跌幅",
            "高PE的前20只股票最近一个月涨了多少",
            "市净率最低的50家公司今年以来的收益率",
        ],
    },
    {
        "family": "retail_proxy_ranking",
        "tier": "approximation",
        "delivery": "async",
        "operations": ["stock_basic", "top10_floatholders"],
        "prompts": [
            "\u627e\u5230\u6563\u6237\u6bd4\u4f8btop10\u7684\u80a1\u7968",
            "\u5168\u5e02\u573a\u6563\u6237\u6bd4\u4f8b\u524d\u5341",
            "\u5927A\u6563\u6237\u6bd4\u4f8b\u6700\u9ad8\u768410\u53ea\u80a1\u7968",
        ],
    },
    {
        "family": "valuation_screen",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["daily_basic"],
        "prompts": [
            "\u7b5b\u9009\u5e02\u76c8\u7387\u4f4e\u4e8e10\u7684A\u80a1",
            "\u627e\u4f4ePE\u3001\u4f4ePB\u3001\u9ad8\u80a1\u606f\u7387\u7684\u5341\u53ea\u80a1\u7968",
            "\u6700\u8fd1\u4ea4\u6613\u65e5\u80a1\u606f\u7387\u6700\u9ad8\u7684\u524d10\u53ea",
        ],
    },
    {
        "family": "liquidity_ranking",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["daily", "daily_basic"],
        "prompts": [
            "\u4eca\u5929\u6210\u4ea4\u989d\u6392\u540d\u524d20\u7684A\u80a1",
            "\u6700\u8fd1\u4ea4\u6613\u65e5\u6362\u624b\u7387\u6700\u9ad8\u768420\u53ea\u80a1\u7968",
            "\u5217\u51fa\u4eca\u65e5\u6210\u4ea4\u91cf\u548c\u6362\u624b\u6700\u6d3b\u8dc3\u7684\u80a1\u7968",
        ],
    },
    {
        "family": "block_trade",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["block_trade"],
        "prompts": [
            "\u672c\u6708\u5927\u5b97\u4ea4\u6613\u6210\u4ea4\u91d1\u989d\u6700\u591a\u768420\u53ea\u80a1\u7968",
            "\u67e5\u8be2\u6628\u5929\u6240\u6709\u5927\u5b97\u4ea4\u6613",
            "\u5e73\u5b89\u94f6\u884c\u8fc7\u53bb\u4e09\u4e2a\u6708\u7684\u5927\u5b97\u4ea4\u6613",
        ],
    },
    {
        "family": "holder_count",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["stk_holdernumber"],
        "prompts": [
            "\u8d35\u5dde\u8305\u53f0\u6700\u65b0\u80a1\u4e1c\u6237\u6570",
            "\u5e73\u5b89\u94f6\u884c\u8fc7\u53bb\u4e24\u5e74\u80a1\u4e1c\u4eba\u6570\u53d8\u5316",
            "\u67e5\u770b600519.SH\u5404\u62a5\u544a\u671f\u80a1\u4e1c\u6237\u6570",
        ],
    },
    {
        "family": "security_moneyflow",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["moneyflow"],
        "prompts": [
            "\u67e5\u8be2\u5e73\u5b89\u94f6\u884c\u6628\u5929\u4e3b\u529b\u8d44\u91d1\u51c0\u6d41\u5165",
            "\u6700\u8fd1\u4ea4\u6613\u65e5A\u80a1\u5927\u5355\u4e70\u5165\u91d1\u989d\u6392\u540d",
            "\u8d35\u5dde\u8305\u53f0\u8fd1\u4e00\u6708\u5927\u5355\u5c0f\u5355\u8d44\u91d1\u6d41\u5411",
        ],
    },
    {
        "family": "margin_financing",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["margin_detail", "margin_secs"],
        "prompts": [
            "\u4eca\u65e5\u878d\u8d44\u4f59\u989d\u6700\u9ad8\u7684\u80a1\u7968",
            "\u67e5\u770b\u4e2d\u56fd\u5e73\u5b89\u6700\u8fd1\u4e00\u6708\u878d\u8d44\u878d\u5238\u53d8\u5316",
            "\u6700\u65b0\u4e0a\u4ea4\u6240\u878d\u8d44\u878d\u5238\u6807\u7684\u5217\u8868",
        ],
    },
    {
        "family": "financial_statements",
        "tier": "supported",
        "delivery": "sync",
        "operations": [
            "income",
            "balancesheet",
            "cashflow",
            "fina_indicator",
            "express",
        ],
        "prompts": [
            "\u8d35\u5dde\u8305\u53f02025\u5e74\u8425\u4e1a\u6536\u5165\u548c\u51c0\u5229\u6da6",
            "\u5e73\u5b89\u94f6\u884c\u6700\u65b0\u8d44\u4ea7\u8d1f\u503a\u8868",
            "\u6bd4\u8f83\u4e2d\u56fd\u5e73\u5b89\u8fd1\u4e09\u5e74ROE\u548c\u7ecf\u8425\u73b0\u91d1\u6d41",
        ],
    },
    {
        "family": "dividend",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["dividend"],
        "prompts": [
            "\u8d35\u5dde\u8305\u53f0\u6700\u65b0\u5206\u7ea2\u65b9\u6848",
            "600519.SH 2025\u5e74\u73b0\u91d1\u5206\u7ea2\u65b9\u6848",
            "\u5e73\u5b89\u94f6\u884c\u5386\u5e74\u6bcf\u80a1\u6d3e\u606f",
        ],
    },
    {
        "family": "share_unlock",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["share_float"],
        "prompts": [
            "\u4e0b\u4e2a\u6708A\u80a1\u9650\u552e\u80a1\u89e3\u7981\u65e5\u7a0b",
            "\u672c\u5468\u89e3\u7981\u6bd4\u4f8b\u6700\u9ad8\u7684\u80a1\u7968",
            "\u67e5\u8be2\u67d0\u516c\u53f8\u672a\u6765\u4e09\u4e2a\u6708\u89e3\u7981\u80a1\u4efd",
        ],
    },
    {
        "family": "repurchase",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["repurchase"],
        "prompts": [
            "\u6700\u8fd1\u4e00\u4e2a\u6708\u5ba3\u5e03\u56de\u8d2d\u7684A\u80a1",
            "\u56de\u8d2d\u91d1\u989d\u4e0a\u9650\u6700\u9ad8\u7684\u516c\u53f8",
            "\u67e5\u770b\u67d0\u80a1\u7968\u6700\u65b0\u56de\u8d2d\u8fdb\u5ea6",
        ],
    },
    {
        "family": "industry_moneyflow",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["moneyflow_ind_ths"],
        "prompts": [
            "\u4eca\u5929\u51c0\u6d41\u5165\u6700\u591a\u7684\u884c\u4e1a",
            "\u6700\u8fd1\u4ea4\u6613\u65e5\u540c\u82b1\u987a\u884c\u4e1a\u8d44\u91d1\u6d41\u5411",
            "\u54ea\u4e2a\u884c\u4e1a\u4e3b\u529b\u8d44\u91d1\u51c0\u6d41\u51fa\u6700\u591a\uff1f",
        ],
    },
    {
        "family": "northbound_flow",
        "tier": "supported",
        "delivery": "sync",
        "operations": ["moneyflow_hsgt"],
        "prompts": [
            "\u4eca\u5929\u5317\u5411\u8d44\u91d1\u51c0\u6d41\u5165\u591a\u5c11\uff1f",
            "\u8fd1\u4e00\u6708\u6caa\u80a1\u901a\u548c\u6df1\u80a1\u901a\u8d44\u91d1\u8d8b\u52bf",
            "\u4eca\u5e74\u5317\u5411\u8d44\u91d1\u6d41\u5165\u6700\u591a\u7684\u4e00\u5929",
        ],
    },
    {
        "family": "verified_retail_ownership",
        "tier": "unsupported",
        "delivery": "sync",
        "operations": [],
        "prompts": [
            "\u7cbe\u786e\u7edf\u8ba1A\u80a1\u771f\u5b9e\u4e2a\u4eba\u6295\u8d44\u8005\u6301\u80a1\u6bd4\u4f8b\uff0c\u4e0d\u63a5\u53d7\u4ee3\u7406\u6307\u6807",
            "\u7ed9\u51fa\u6bcf\u53ea\u80a1\u7968\u6563\u6237\u8d26\u6237\u7684\u5b9e\u9645\u6301\u80a1\u767e\u5206\u6bd4",
            "\u533a\u5206\u673a\u6784\u548c\u81ea\u7136\u4eba\u7684\u7cbe\u786e\u6d41\u901a\u80a1\u6301\u4ed3",
        ],
    },
    {
        "family": "future_price_prediction",
        "tier": "unsupported",
        "delivery": "sync",
        "operations": [],
        "prompts": [
            "\u9884\u6d4b\u660e\u5929\u4e00\u5b9a\u6da8\u505c\u7684\u80a1\u7968",
            "\u7ed9\u51fa\u4e0b\u5468\u6536\u76ca\u7387\u6700\u9ad8\u7684\u516c\u53f8",
            "\u4fdd\u8bc1\u9009\u51fa\u672a\u6765\u4e00\u4e2a\u6708\u4e0a\u6da8\u7684A\u80a1",
        ],
    },
    {
        "family": "market_wide_dividend_total",
        "tier": "unsupported",
        "delivery": "sync",
        "operations": [],
        "prompts": [
            "2025\u5e74\u73b0\u91d1\u5206\u7ea2\u603b\u989d\u6700\u9ad8\u7684A\u80a1\uff0c\u4e0d\u63a5\u53d7\u6bcf\u80a1\u5206\u7ea2\u66ff\u4ee3",
            "\u7cbe\u786e\u6392\u540d\u5168A\u80a1\u5e74\u5ea6\u73b0\u91d1\u5206\u7ea2\u603b\u91d1\u989d\uff0c\u4e0d\u5141\u8bb8\u4f7f\u7528\u6bcf\u80a1\u6d3e\u606f",
            "\u7ed9\u51fa\u5168\u5e02\u573a\u516c\u53f8\u5b9e\u9645\u652f\u4ed8\u73b0\u91d1\u5206\u7ea2\u603b\u989d\u699c\u5355",
        ],
    },
    {
        "family": "investor_demographics",
        "tier": "unsupported",
        "delivery": "sync",
        "operations": [],
        "prompts": [
            "\u67e5\u8be2\u67d0\u80a1\u7968\u6301\u6709\u8005\u7684\u5e73\u5747\u5e74\u9f84",
            "\u7edf\u8ba1\u7537\u6027\u548c\u5973\u6027\u6563\u6237\u6301\u80a1\u6bd4\u4f8b",
            "\u6309\u57ce\u5e02\u5217\u51fa\u6295\u8d44\u8005\u8d26\u6237\u6570",
        ],
    },
]

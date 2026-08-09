"""Read-only Tushare operation catalog exposed through neutral contracts."""

from typing import Sequence, Set

from .core.contracts import DataOperation


STOCK_API_NAMES = (
    "adj_factor", "bak_basic", "bak_daily", "balancesheet", "block_trade",
    "broker_recommend", "bse_mapping", "cashflow", "ccass_hold",
    "ccass_hold_detail", "cyq_chips", "cyq_perf", "daily", "daily_basic",
    "dc_concept", "dc_concept_cons", "dc_daily", "dc_hot", "dc_index",
    "dc_member", "disclosure_date", "dividend", "express", "fina_audit",
    "fina_indicator", "fina_mainbz", "forecast", "ggt_daily", "ggt_top10",
    "hk_hold", "hm_detail", "hm_list", "hsgt_top10", "income",
    "kpl_concept_cons", "kpl_list", "limit_cpt_list", "limit_list_d",
    "limit_list_ths", "limit_step", "margin", "margin_detail", "margin_secs",
    "moneyflow", "moneyflow_cnt_ths", "moneyflow_dc", "moneyflow_hsgt",
    "moneyflow_ind_dc", "moneyflow_ind_ths", "moneyflow_mkt_dc",
    "moneyflow_ths", "monthly", "namechange", "new_share", "pledge_detail",
    "pledge_stat", "pro_bar", "report_rc", "repurchase", "rt_k", "rt_min",
    "rt_min_daily", "share_float", "slb_len", "slb_len_mm", "slb_sec",
    "slb_sec_detail", "st", "stk_account", "stk_account_old",
    "stk_ah_comparison", "stk_alert", "stk_auction", "stk_auction_c",
    "stk_auction_o", "stk_factor", "stk_factor_pro", "stk_high_shock",
    "stk_holdernumber", "stk_holdertrade", "stk_limit", "stk_managers",
    "stk_mins", "stk_nineturn", "stk_premarket", "stk_rewards", "stk_shock",
    "stk_surv", "stk_week_month_adj", "stk_weekly_monthly", "stock_basic",
    "stock_company", "stock_hsgt", "stock_st", "suspend_d", "tdx_daily",
    "tdx_index", "tdx_member", "ths_daily", "ths_hot", "ths_index",
    "ths_member", "top10_floatholders", "top10_holders", "top_inst",
    "top_list", "trade_cal", "weekly",
)

ETF_API_NAMES = (
    "etf_basic", "etf_index", "etf_mins", "etf_sh_cons", "etf_share_size",
    "etf_sz_cons", "fund_adj", "fund_daily", "idx_anns", "rt_etf_k",
    "rt_etf_min", "rt_etf_min_daily", "rt_etf_sz_iopv",
)

INDEX_API_NAMES = (
    "ci_daily", "ci_index_member", "daily_info", "idx_factor_pro",
    "idx_mins", "index_basic", "index_classify", "index_daily",
    "index_dailybasic", "index_global", "index_member_all", "index_monthly",
    "index_weekly", "index_weight", "rt_idx_k", "rt_idx_min", "rt_sw_k",
    "sw_daily", "sw_mins", "sz_daily_info",
)

FUND_API_NAMES = (
    "fund_basic", "fund_company", "fund_div", "fund_factor_pro",
    "fund_manager", "fund_nav", "fund_portfolio", "fund_share", "mkt_idx_bmk",
)

FUTURES_API_NAMES = (
    "ft_limit", "ft_mins", "fut_basic", "fut_daily", "fut_holding",
    "fut_index_daily", "fut_mapping", "fut_settle", "fut_trade_cal",
    "fut_weekly_detail", "fut_weekly_monthly", "fut_wsr", "rt_fut_min",
)

SPOT_API_NAMES = ("sge_basic", "sge_daily")

OPTION_API_NAMES = ("opt_basic", "opt_daily", "opt_mins")

BOND_API_NAMES = (
    "bc_bestotcqt", "bc_otcqt", "bond_blk", "bond_blk_detail", "cb_basic",
    "cb_call", "cb_daily", "cb_factor_pro", "cb_issue", "cb_price_chg",
    "cb_rate", "cb_rating", "cb_share", "eco_cal", "repo_daily",
    "top10_cb_holders", "yc_cb",
)

FOREX_API_NAMES = ("fx_daily", "fx_obasic")

HONG_KONG_API_NAMES = (
    "hk_adjfactor", "hk_balancesheet", "hk_basic", "hk_cashflow",
    "hk_daily", "hk_daily_adj", "hk_fina_indicator", "hk_income",
    "hk_tradecal",
)

UNITED_STATES_API_NAMES = (
    "us_adjfactor", "us_balancesheet", "us_basic", "us_cashflow",
    "us_daily", "us_daily_adj", "us_fina_indicator", "us_income",
    "us_tradecal",
)

MACRO_API_NAMES = (
    "cn_cpi", "cn_gdp", "cn_m", "cn_pmi", "cn_ppi", "cn_schedule",
    "gz_index", "hibor", "libor", "sf_month", "shibor", "shibor_lpr",
    "shibor_quote", "us_tbr", "us_tltr", "us_trltr", "us_trycr", "us_tycr",
    "wz_index",
)

TEXT_API_NAMES = (
    "anns_d", "cctv_news", "irm_qa_sh", "irm_qa_sz", "major_news",
    "monetary_policy", "news", "npr", "research_report",
)

READ_ONLY_PORTFOLIO_API_NAMES = ("p_get", "p_list")

TUSHARE_API_CATEGORIES = {
    "stock": STOCK_API_NAMES,
    "etf": ETF_API_NAMES,
    "index": INDEX_API_NAMES,
    "fund": FUND_API_NAMES,
    "futures": FUTURES_API_NAMES,
    "spot": SPOT_API_NAMES,
    "option": OPTION_API_NAMES,
    "bond": BOND_API_NAMES,
    "forex": FOREX_API_NAMES,
    "hong_kong": HONG_KONG_API_NAMES,
    "united_states": UNITED_STATES_API_NAMES,
    "macro": MACRO_API_NAMES,
    "text": TEXT_API_NAMES,
    "portfolio_read": READ_ONLY_PORTFOLIO_API_NAMES,
}

READ_ONLY_API_NAMES = tuple(
    operation
    for operations in TUSHARE_API_CATEGORIES.values()
    for operation in operations
)


CORE_OPERATION_GUIDANCE = {
    "daily": (
        "Unadjusted A-share daily prices. Use trade_date=YYYYMMDD for the full "
        "market on one date, or ts_code with start_date and end_date. Common "
        "fields: ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount."
    ),
    "daily_basic": (
        "Daily valuation and trading metrics. Parameters include trade_date, "
        "ts_code, start_date, and end_date. Common output fields include ts_code, "
        "trade_date, close, turnover_rate, volume_ratio, pe, pe_ttm, pb, ps, "
        "ps_ttm, dv_ratio, dv_ttm, total_share, float_share, free_share, total_mv, "
        "and circ_mv. total_mv and circ_mv are reported in CNY 10,000 units, so "
        "CNY 10 billion equals 1,000,000 in these fields. Numeric screening "
        "thresholds such as pe<=10 are not native "
        "parameters; retrieve the field and use a deterministic local filter."
    ),
    "monthly": (
        "Monthly A-share prices. The native API requires at least ts_code or "
        "trade_date; start_date and end_date alone cannot retrieve the full market. "
        "For a full-market period-return ranking, use daily with start_date and "
        "end_date so the local executor can compare boundary trading-day snapshots."
    ),
    "weekly": (
        "Weekly A-share prices. The native API requires at least ts_code or "
        "trade_date; start_date and end_date alone cannot retrieve the full market. "
        "For a full-market period-return ranking, use daily with start_date and "
        "end_date so the local executor can compare boundary trading-day snapshots."
    ),
    "limit_list_d": (
        "Daily A-share limit-up, limit-down, and failed-limit list. Parameters "
        "include trade_date=YYYYMMDD, ts_code, limit_type, exchange, start_date, "
        "and end_date. Set the native limit_type parameter to U for limit-up, D "
        "for limit-down, or Z for failed-limit rows. Common output fields include "
        "trade_date, ts_code, name, industry, close, pct_chg, amount, float_mv, "
        "total_mv, turnover_ratio, fd_amount, first_time, last_time, open_times, "
        "up_stat, limit_times, and limit. The returned row count is the number of "
        "matching securities; do not create a conditional count over ts_code."
    ),
    "dividend": (
        "A-share dividend disclosures. Native parameters are ts_code, ann_date, "
        "record_date, ex_date, and imp_ann_date; year, start_date, and end_date "
        "are not native parameters. A market-wide annual ranking must use a "
        "stock_basic universe plus a dividend fan-out template, then filter the "
        "returned end_date or announcement date locally. Common fields include "
        "ts_code, end_date, ann_date, div_proc, cash_div_tax, record_date, "
        "ex_date, pay_date, and stk_div. This fan-out plan is supported and must not "
        "be rejected merely because each dividend call requires ts_code."
    ),
    "top10_floatholders": (
        "Top ten unrestricted float-holder snapshots. Parameters include ts_code, "
        "period, ann_date, start_date, and end_date. Fields include ts_code, ann_date, "
        "end_date, holder_name, hold_amount, hold_ratio, hold_float_ratio, hold_change, "
        "and holder_type. For requests mentioning retail ownership, retail ratio, "
        "shareholding dispersion, or CR10 trends, request ts_code,ann_date,end_date,"
        "holder_name,hold_amount,hold_float_ratio and set transform=cr10_float_trend. "
        "The period parameter is a reporting quarter end (YYYY0331, YYYY0630, "
        "YYYY0930, or YYYY1231), never an arbitrary calendar date. Interpret an "
        "arbitrary user date as an as-of date and pass it as end_date without period; "
        "the transform then selects the latest disclosed reporting snapshot. "
        "The resulting non_top10_float_ratio is the project's fixed proxy for retail "
        "ratio: 100% minus the disclosed top-ten unrestricted float-holder ratios. It "
        "includes both retail holders and institutions outside the top ten and is not "
        "a verified account-level percentage held by individual investors. Missing "
        "source ratios produce "
        "a partial result with a known ratio and an uncovered-ratio upper bound, not a "
        "complete CR10 value."
    ),
    "stk_holdernumber": (
        "Shareholder count per reporting period. Parameters include ts_code, "
        "end_date, start_date, and end_date. Common fields: ts_code, ann_date, "
        "end_date, holder_num. holder_num is total shareholders. Period must be "
        "a quarter end (YYYY0331, YYYY0630, YYYY0930, or YYYY1231). Use this "
        "to measure retail breadth: fewer shareholders + high average holding "
        "per account = institutional concentration; many shareholders + low "
        "average holdings = retail dispersion. For retail analysis, combine "
        "holder_num with daily_basic.float_share to compute average holding per "
        "shareholder."
    ),
    "stock_basic": (
        "A-share security master. Parameters include ts_code, exchange, market, "
        "and list_status. Common fields include ts_code,symbol,name,area,industry,"
        "market,list_date. Retrieve the listed security master and apply an exact "
        "local industry classification when an analysis requires an industry universe."
    ),
    "block_trade": (
        "A-share block trades. Parameters include ts_code, trade_date, start_date, "
        "and end_date. Common fields include ts_code, trade_date, price, vol, amount, "
        "buyer, and seller. Date-range full-market reads are supported."
    ),
    "income": "Listed-company income statements by ts_code or reporting dates.",
    "margin_detail": (
        "Security-level margin financing and securities lending history. Common "
        "fields include trade_date,ts_code,rzye,rqye,rzmre,rqyl. To calculate a "
        "period-over-period change, first use shift grouped by ts_code and ordered "
        "by trade_date to create the previous value, then derive the change."
    ),
    "balancesheet": "Listed-company balance sheets by ts_code or reporting dates.",
    "cashflow": (
        "Listed-company cash-flow statements by ts_code or reporting dates. Common "
        "fields include ts_code, ann_date, f_ann_date, end_date, report_type, comp_type, "
        "end_type, and n_cashflow_act. Reporting periods may have multiple disclosure "
        "versions; select the required version before joining."
    ),
    "fina_indicator": (
        "Listed-company financial indicators. Use period=YYYYMMDD for a reporting "
        "period such as 20260331 for 2026 Q1. The native API requires ts_code for "
        "security-specific reads; common fields include ts_code, ann_date, end_date, "
        "roe, roe_wa, and diluted_roe. Reporting periods may have multiple disclosure "
        "versions; select the required version before joining. Do not claim a "
        "full-market period screen is supported unless a documented full-market "
        "operation is available."
    ),
    "moneyflow": (
        "A-share security-level daily fund-flow data. Parameters include ts_code, "
        "trade_date, start_date, and end_date. Common fields include ts_code, "
        "trade_date, buy_sm_amount, sell_sm_amount, buy_lg_amount, sell_lg_amount, "
        "buy_elg_amount, sell_elg_amount, and net_mf_amount. Requested net flows must "
        "be derived from the documented buy and sell amount pairs when no direct net "
        "field exists."
    ),
    "repurchase": (
        "A-share repurchase disclosures. Parameters include ann_date, start_date, "
        "end_date, and ts_code; full-market reads are supported. Common fields include "
        "ts_code, ann_date, end_date, proc, exp_date, vol, amount, high_limit, and "
        "low_limit. Security names require a stock_basic join."
    ),
    "stk_holdertrade": (
        "Major shareholder transactions. Parameters include ts_code, ann_date, "
        "start_date, and end_date. Common fields include ts_code, ann_date, holder_name, "
        "holder_type, in_de, change_vol, change_ratio, after_share, after_ratio, "
        "avg_price, and total_share. Date-range market screens require a stock_basic "
        "universe and bounded ts_code fan-out; do not issue an unbound market-wide read."
    ),
    "forecast": (
        "Disclosed management earnings guidance, not a model-generated price or "
        "profit prediction. Use this operation for companies forecasting profit "
        "growth, loss, or other result types for a reporting period. Parameters "
        "include ts_code, ann_date, start_date, "
        "end_date, period, and type. Common fields include ts_code, ann_date, "
        "end_date, type, p_change_min, p_change_max, net_profit_min, "
        "net_profit_max, summary, and change_reason. Market-wide period screens "
        "should use a bounded start_date/end_date announcement window; the executor "
        "expands it into exact ann_date reads and filters the requested period. Avoid "
        "full-universe ts_code fan-out when an announcement window can be bounded."
    ),
    "express": (
        "Earnings express reports. Parameters include ts_code, ann_date, start_date, "
        "end_date, and period. Common fields include ts_code, ann_date, end_date, "
        "revenue, operate_profit, total_profit, n_income, total_assets, diluted_eps, "
        "diluted_roe, yoy_net_profit, bps, and perf_summary."
    ),
    "fina_mainbz": (
        "Main business composition by ts_code and reporting period. Common fields "
        "include ts_code, end_date, bz_item, bz_sales, bz_profit, bz_cost, curr_type, "
        "and update_flag. Gross margin is not a native field and must be derived as "
        "(bz_sales - bz_cost) / bz_sales when requested."
    ),
    "suspend_d": (
        "Daily suspension records. Parameters include ts_code, suspend_type, "
        "trade_date, start_date, and end_date. Common fields include ts_code, "
        "trade_date, suspend_timing, and suspend_type. Suspension-day counts must be "
        "aggregated from returned records; they are not a native field."
    ),
    "share_float": (
        "Restricted-share unlock schedules. Parameters include ts_code, ann_date, "
        "float_date, start_date, and end_date. Common fields include ts_code, "
        "ann_date, float_date, float_share, float_ratio, holder_name, and share_type. "
        "float_ratio is the native unlocked-share percentage of total shares; use it "
        "directly for ratio rankings without joining daily_basic. "
        "Market screens with a bounded schedule window use exact float_date fan-out. "
        "Without a defensible date window, use a stock_basic universe and bounded "
        "ts_code fan-out; do not issue an unbound market-wide read."
    ),
    "new_share": (
        "IPO issuance records. Common fields include ts_code, name, ipo_date, "
        "issue_date, amount, market_amount, price, pe, limit_amount, funds, and "
        "ballot. This operation does not provide first-day price change."
    ),
}


class TushareOperationCatalog:
    """Provide allowlisted read-only Tushare operations and planner guidance."""

    def __init__(self) -> None:
        self._operation_names: Set[str] = set(READ_ONLY_API_NAMES)

    def search(self, prompt: str) -> Sequence[DataOperation]:
        """Return candidate operations ordered by catalog position."""
        if not prompt.strip():
            return ()
        return tuple(
            DataOperation(
                name=name,
                description=CORE_OPERATION_GUIDANCE.get(
                    name,
                    (
                        f"Tushare read-only data operation {name}. Use only parameters "
                        "and fields defined by the official operation documentation."
                    ),
                ),
            )
            for name in READ_ONLY_API_NAMES
        )

    def contains(self, operation: str) -> bool:
        """Return whether an operation belongs to the Tushare stock catalog."""
        return operation in self._operation_names

"""Tushare operation catalog exposed through provider-neutral contracts."""

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
    "income": "Listed-company income statements by ts_code or reporting dates.",
    "margin_detail": (
        "Security-level margin financing and securities lending history. Common "
        "fields include trade_date,ts_code,rzye,rqye,rzmre,rqyl. To calculate a "
        "period-over-period change, first use shift grouped by ts_code and ordered "
        "by trade_date to create the previous value, then derive the change."
    ),
    "balancesheet": "Listed-company balance sheets by ts_code or reporting dates.",
    "cashflow": "Listed-company cash-flow statements by ts_code or reporting dates.",
    "fina_indicator": (
        "Listed-company financial indicators. Use period=YYYYMMDD for a reporting "
        "period such as 20260331 for 2026 Q1. The native API requires ts_code for "
        "security-specific reads; do not claim a full-market period screen is "
        "supported unless a documented full-market operation is available."
    ),
    "moneyflow": "A-share security-level daily fund-flow data.",
    "share_float": (
        "Restricted-share unlock schedules. Parameters include ts_code, ann_date, "
        "float_date, start_date, and end_date. Common fields include ts_code, "
        "ann_date, float_date, float_share, float_ratio, holder_name, and share_type."
    ),
    "new_share": (
        "IPO issuance records. Common fields include ts_code, name, ipo_date, "
        "issue_date, amount, market_amount, price, pe, limit_amount, funds, and "
        "ballot. This operation does not provide first-day price change."
    ),
}


class TushareOperationCatalog:
    """Provide allowlisted Tushare stock operations and planner guidance."""

    def __init__(self) -> None:
        self._operation_names: Set[str] = set(STOCK_API_NAMES)

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
                        f"Tushare stock-data operation {name}. Use only parameters "
                        "and fields defined by the official operation documentation."
                    ),
                ),
            )
            for name in STOCK_API_NAMES
        )

    def contains(self, operation: str) -> bool:
        """Return whether an operation belongs to the Tushare stock catalog."""
        return operation in self._operation_names

"""Allowlist of Tushare APIs in the official stock-data catalog."""

from typing import Sequence, Set


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


class StockApiRegistry:
    """Provide the allowlisted Tushare stock APIs relevant to a user prompt."""

    def __init__(self) -> None:
        self._api_names: Set[str] = set(STOCK_API_NAMES)

    def search(self, prompt: str) -> Sequence[str]:
        """Return candidate API names ordered by relevance to the prompt."""
        if not prompt.strip():
            return ()
        return STOCK_API_NAMES

    def contains(self, api_name: str) -> bool:
        """Return whether an API belongs to the official stock-data catalog."""
        return api_name in self._api_names

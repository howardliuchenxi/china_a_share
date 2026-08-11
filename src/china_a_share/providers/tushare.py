"""Tushare market-data provider and publication-time cache policy."""

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from china_a_share.capabilities import resolve_query_shape
from china_a_share.client import TushareTransport
from china_a_share.core.contracts import DataOperation
from china_a_share.core.ports import DataResponseCache
from china_a_share.market_time import DAILY_PUBLICATION_COMPLETION_TIME
from china_a_share.registry import READ_ONLY_API_NAMES, TushareOperationCatalog


TUSHARE_PROVIDER_NAME = "tushare"
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
SHORT_CACHE_TTL = timedelta(minutes=5)
REFERENCE_CACHE_TTL = timedelta(hours=24)
TRADE_CALENDAR_CACHE_TTL = timedelta(days=30)
HISTORICAL_CACHE_TTL = timedelta(days=90)
DISCLOSURE_CACHE_TTL = timedelta(days=90)
MAX_PAGINATION_PAGES = 20
PAGINATED_OPERATION_LIMITS = {
    "adj_factor": 6_000,
    "daily": 6_000,
    "daily_basic": 6_000,
    "block_trade": 1_000,
    "share_float": 6_000,
    "stock_st": 1_000,
}

NO_PERSISTENCE_OPERATIONS = {
    "dc_hot",
    "rt_k",
    "rt_min",
    "rt_min_daily",
    "ths_hot",
    "p_get",
    "p_list",
    "rt_etf_k",
    "rt_etf_min",
    "rt_etf_min_daily",
    "rt_etf_sz_iopv",
    "rt_fut_min",
    "rt_idx_k",
    "rt_idx_min",
    "rt_sw_k",
}
INTRADAY_OPERATIONS = {
    "stk_auction",
    "stk_auction_c",
    "stk_auction_o",
    "stk_mins",
    "stk_premarket",
}
REFERENCE_OPERATIONS = {
    "bak_basic",
    "bse_mapping",
    "dc_concept",
    "dc_concept_cons",
    "dc_index",
    "dc_member",
    "hm_list",
    "kpl_concept_cons",
    "kpl_list",
    "margin_secs",
    "stock_basic",
    "stock_company",
    "tdx_index",
    "tdx_member",
    "ths_index",
    "ths_member",
}
DISCLOSURE_OPERATIONS = {
    "balancesheet",
    "broker_recommend",
    "cashflow",
    "ccass_hold",
    "ccass_hold_detail",
    "disclosure_date",
    "dividend",
    "express",
    "fina_audit",
    "forecast",
    "fina_indicator",
    "fina_mainbz",
    "income",
    "namechange",
    "new_share",
    "pledge_detail",
    "pledge_stat",
    "repurchase",
    "share_float",
    "stk_holdernumber",
    "stk_holdertrade",
    "stk_managers",
    "stk_rewards",
    "top10_floatholders",
    "top10_holders",
}
DAILY_OPERATIONS = {
    "adj_factor",
    "bak_daily",
    "block_trade",
    "cyq_chips",
    "cyq_perf",
    "daily",
    "daily_basic",
    "dc_daily",
    "ggt_daily",
    "ggt_top10",
    "hk_hold",
    "hm_detail",
    "hsgt_top10",
    "limit_cpt_list",
    "limit_list_d",
    "limit_list_ths",
    "limit_step",
    "margin",
    "margin_detail",
    "moneyflow",
    "moneyflow_cnt_ths",
    "moneyflow_dc",
    "moneyflow_hsgt",
    "moneyflow_ind_dc",
    "moneyflow_ind_ths",
    "moneyflow_mkt_dc",
    "moneyflow_ths",
    "monthly",
    "pro_bar",
    "report_rc",
    "slb_len",
    "slb_len_mm",
    "slb_sec",
    "slb_sec_detail",
    "st",
    "stk_account",
    "stk_account_old",
    "stk_ah_comparison",
    "stk_alert",
    "stk_factor",
    "stk_factor_pro",
    "stk_high_shock",
    "stk_limit",
    "stk_nineturn",
    "stk_shock",
    "stk_surv",
    "stk_week_month_adj",
    "stk_weekly_monthly",
    "stock_hsgt",
    "stock_st",
    "suspend_d",
    "tdx_daily",
    "ths_daily",
    "top_inst",
    "top_list",
    "weekly",
}
GENERIC_READ_ONLY_OPERATIONS = (
    set(READ_ONLY_API_NAMES)
    - NO_PERSISTENCE_OPERATIONS
    - INTRADAY_OPERATIONS
    - REFERENCE_OPERATIONS
    - DISCLOSURE_OPERATIONS
    - DAILY_OPERATIONS
    - {"trade_cal"}
)
DAILY_OPERATIONS.update(GENERIC_READ_ONLY_OPERATIONS)
PROFILED_OPERATIONS = (
    NO_PERSISTENCE_OPERATIONS
    | INTRADAY_OPERATIONS
    | REFERENCE_OPERATIONS
    | DISCLOSURE_OPERATIONS
    | DAILY_OPERATIONS
    | {"trade_cal"}
)
if PROFILED_OPERATIONS != set(READ_ONLY_API_NAMES):
    missing = sorted(set(READ_ONLY_API_NAMES).difference(PROFILED_OPERATIONS))
    extra = sorted(PROFILED_OPERATIONS.difference(READ_ONLY_API_NAMES))
    raise RuntimeError(
        f"Tushare cache profiles must cover the operation catalog; "
        f"missing={missing}, extra={extra}"
    )
PUBLICATION_TIMES = {
    "daily": DAILY_PUBLICATION_COMPLETION_TIME,
    "daily_basic": DAILY_PUBLICATION_COMPLETION_TIME,
    "adj_factor": DAILY_PUBLICATION_COMPLETION_TIME,
    "weekly": DAILY_PUBLICATION_COMPLETION_TIME,
    "monthly": DAILY_PUBLICATION_COMPLETION_TIME,
    "stk_limit": time(9, 10),
    "moneyflow": time(19, 10),
    "stk_holdertrade": time(19, 10),
    "top_list": time(20, 10),
    "top_inst": time(20, 10),
    "block_trade": time(21, 10),
    "pledge_detail": time(21, 10),
    "pledge_stat": time(21, 10),
    "stk_mins": time(21, 10),
}


class TushareDataProvider:
    """Expose the Tushare stock catalog through the market-data provider port."""

    def __init__(
        self,
        token: str,
        response_cache: DataResponseCache,
        session: Optional[Any] = None,
        pro_api: Optional[Any] = None,
    ) -> None:
        """Store credentials, cache access, and injectable Tushare transports."""
        self._response_cache = response_cache
        self._catalog = TushareOperationCatalog()
        self._transport = TushareTransport(
            token=token,
            session=session,
            pro_api=pro_api,
        )

    @property
    def name(self) -> str:
        """Return the stable provider identifier used in results and cache keys."""
        return TUSHARE_PROVIDER_NAME

    @property
    def operation_names(self) -> Sequence[str]:
        """Return every Tushare operation connected through the generic transport."""
        return READ_ONLY_API_NAMES

    def search_operations(self, prompt: str) -> Sequence[DataOperation]:
        """Return read-only Tushare operations available to the query planner."""
        return self._catalog.search(prompt)

    def supports(self, operation: str) -> bool:
        """Return whether the Tushare stock catalog contains the operation."""
        return self._catalog.contains(operation)

    def describe_result_completeness(
        self,
        operation: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Describe completeness guaranteed by one successful audited request."""
        shape = resolve_query_shape(operation, params)
        if shape is None:
            return {
                "completeness": "unknown",
                "completeness_evidence": [],
            }
        if shape.execution_strategy != "provider_query":
            return {
                "completeness": "unknown",
                "completeness_evidence": [
                    f"query_shape={shape.shape_id}",
                    f"required_strategy={shape.execution_strategy}",
                ],
            }
        return {
            "completeness": "complete",
            "completeness_evidence": [
                f"query_shape={shape.shape_id}",
                f"execution_strategy={shape.execution_strategy}",
                f"completeness_policy={shape.completeness_policy}",
            ],
        }

    def query(
        self,
        operation: str,
        params: Dict[str, Any],
        fields: Sequence[str],
        *,
        api_route: str,
        request_id: str,
        query_id: str,
    ) -> pd.DataFrame:
        """Execute one cached Tushare read and return a normalized table."""
        if not self.supports(operation):
            raise ValueError(f"Unsupported Tushare operation: {operation}")
        return self._response_cache.get_or_fetch(
            self.name,
            operation,
            params,
            fields,
            lambda: self._fetch_complete(operation, params, fields),
            api_route=api_route,
            request_id=request_id,
            query_id=query_id,
        )

    def _fetch_complete(
        self,
        operation: str,
        params: Dict[str, Any],
        fields: Sequence[str],
    ) -> pd.DataFrame:
        """Fetch every provider page for operations with a documented row cap."""
        first_page = self._transport.query(operation, params, fields)
        page_limit = PAGINATED_OPERATION_LIMITS.get(operation)
        if page_limit is None or len(first_page) < page_limit:
            return first_page

        pages = [first_page]
        previous_page = first_page
        for page_index in range(1, MAX_PAGINATION_PAGES):
            page_params = dict(params)
            page_params["limit"] = page_limit
            page_params["offset"] = page_index * page_limit
            page = self._transport.query(operation, page_params, fields)
            if not page.empty and page.equals(previous_page):
                raise ValueError(
                    f"{operation} pagination repeated a page at offset "
                    f"{page_params['offset']}."
                )
            pages.append(page)
            if len(page) < page_limit:
                return pd.concat(pages, ignore_index=True)
            previous_page = page
        raise ValueError(
            f"{operation} exceeded the safe pagination limit of "
            f"{MAX_PAGINATION_PAGES * page_limit} rows."
        )


class TushareCacheExpirationPolicy:
    """Resolve cache expiration from Tushare publication windows."""

    def resolve(
        self,
        operation: str,
        params: Dict[str, Any],
        fetched_at: datetime,
    ) -> Optional[datetime]:
        """Return persistent-cache expiration for one explicitly profiled operation."""
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        if operation not in PROFILED_OPERATIONS:
            raise ValueError(f"Tushare operation has no cache profile: {operation}")
        beijing_now = fetched_at.astimezone(BEIJING_TIMEZONE)

        if operation in NO_PERSISTENCE_OPERATIONS:
            return None
        if operation == "trade_cal":
            return beijing_now + TRADE_CALENDAR_CACHE_TTL
        if operation in REFERENCE_OPERATIONS:
            return beijing_now + REFERENCE_CACHE_TTL
        requested_date = self._requested_end_date(params)
        if operation in INTRADAY_OPERATIONS:
            if requested_date is None or requested_date >= beijing_now.date():
                return None
            return beijing_now + HISTORICAL_CACHE_TTL
        if (
            operation == "pro_bar"
            and str(params.get("freq", "")).lower().endswith("min")
            and (requested_date is None or requested_date >= beijing_now.date())
        ):
            return None
        if operation in DISCLOSURE_OPERATIONS:
            if self._has_fixed_disclosure_window(params):
                return beijing_now + DISCLOSURE_CACHE_TTL
            return beijing_now + REFERENCE_CACHE_TTL

        publication_time = self._publication_time(operation, params)
        if publication_time is None:
            publication_time = time(21, 10)

        if requested_date is not None and requested_date < beijing_now.date():
            return beijing_now + HISTORICAL_CACHE_TTL

        publication_date = requested_date or beijing_now.date()
        completion = datetime.combine(
            publication_date,
            publication_time,
            tzinfo=BEIJING_TIMEZONE,
        )
        if requested_date is not None and beijing_now >= completion:
            return beijing_now + HISTORICAL_CACHE_TTL
        if requested_date is None and beijing_now >= completion:
            return completion + timedelta(days=1)

        # Partial or empty responses must expire quickly during publication windows.
        return min(beijing_now + SHORT_CACHE_TTL, completion)

    @staticmethod
    def _has_fixed_disclosure_window(params: Dict[str, Any]) -> bool:
        """Return whether a disclosure query is pinned to a fixed source cutoff."""
        return any(
            isinstance(params.get(name), str) and bool(params[name].strip())
            for name in (
                "period",
                "ann_date",
                "start_date",
                "end_date",
                "float_date",
            )
        )

    @staticmethod
    def _publication_time(
        operation: str,
        params: Dict[str, Any],
    ) -> Optional[time]:
        """Return the conservative completion time for a known Tushare operation."""
        if operation == "pro_bar" and str(params.get("freq", "")).lower().endswith(
            "min"
        ):
            return time(21, 10)
        return PUBLICATION_TIMES.get(operation)

    @staticmethod
    def _requested_end_date(params: Dict[str, Any]) -> Optional[date]:
        """Extract an explicit fixed business date from common Tushare parameters."""
        for name in ("trade_date", "end_date", "cal_date"):
            value = params.get(name)
            if not isinstance(value, str) or not value.strip():
                continue
            normalized = value.strip()
            try:
                if len(normalized) >= 10 and normalized[4] == "-":
                    return date.fromisoformat(normalized[:10])
                if len(normalized) >= 8 and normalized[:8].isdigit():
                    return datetime.strptime(normalized[:8], "%Y%m%d").date()
            except ValueError:
                # Invalid parameters remain the plan validator's responsibility.
                return None
        return None

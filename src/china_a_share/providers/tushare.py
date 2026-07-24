"""Tushare market-data provider and publication-time cache policy."""

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from china_a_share.client import TushareTransport
from china_a_share.core.contracts import DataOperation
from china_a_share.core.ports import DataResponseCache
from china_a_share.registry import TushareOperationCatalog


TUSHARE_PROVIDER_NAME = "tushare"
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_CACHE_TTL = timedelta(minutes=15)
SHORT_CACHE_TTL = timedelta(minutes=5)
REALTIME_CACHE_TTL = timedelta(seconds=15)
REFERENCE_CACHE_TTL = timedelta(hours=24)
TRADE_CALENDAR_CACHE_TTL = timedelta(days=30)
HISTORICAL_CACHE_TTL = timedelta(days=30)
FINANCIAL_CACHE_TTL = timedelta(hours=1)
MAX_PAGINATION_PAGES = 20
PAGINATED_OPERATION_LIMITS = {
    "daily": 6_000,
    "daily_basic": 6_000,
    "block_trade": 1_000,
    "share_float": 6_000,
    "stock_st": 1_000,
}

REALTIME_OPERATIONS = {"rt_k", "rt_min", "rt_min_daily"}
REFERENCE_OPERATIONS = {
    "stock_basic",
    "stock_company",
    "bse_mapping",
    "namechange",
    "ths_index",
    "ths_member",
    "dc_concept",
    "dc_concept_cons",
    "dc_index",
    "dc_member",
}
FINANCIAL_OPERATIONS = {
    "income",
    "balancesheet",
    "cashflow",
    "fina_indicator",
    "fina_audit",
    "fina_mainbz",
    "forecast",
    "express",
    "dividend",
    "disclosure_date",
}
PUBLICATION_TIMES = {
    "daily": time(17, 10),
    "daily_basic": time(17, 10),
    "adj_factor": time(17, 10),
    "weekly": time(17, 10),
    "monthly": time(17, 10),
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

    def search_operations(self, prompt: str) -> Sequence[DataOperation]:
        """Return Tushare stock operations relevant to the user prompt."""
        return self._catalog.search(prompt)

    def supports(self, operation: str) -> bool:
        """Return whether the Tushare stock catalog contains the operation."""
        return self._catalog.contains(operation)

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
    ) -> datetime:
        """Return the expiration instant for one successful Tushare response."""
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        beijing_now = fetched_at.astimezone(BEIJING_TIMEZONE)

        if operation in REALTIME_OPERATIONS:
            return beijing_now + REALTIME_CACHE_TTL
        if operation == "trade_cal":
            return beijing_now + TRADE_CALENDAR_CACHE_TTL
        if operation in REFERENCE_OPERATIONS:
            return beijing_now + REFERENCE_CACHE_TTL
        if operation in FINANCIAL_OPERATIONS:
            return beijing_now + FINANCIAL_CACHE_TTL

        publication_time = self._publication_time(operation, params)
        if publication_time is None:
            return beijing_now + DEFAULT_CACHE_TTL

        requested_date = self._requested_end_date(params)
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
            completion += timedelta(days=1)

        # Partial or empty responses must expire quickly during publication windows.
        return min(beijing_now + SHORT_CACHE_TTL, completion)

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

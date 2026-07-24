"""Deterministic access to the currently listed A-share security catalog."""

from datetime import datetime
import logging
from math import ceil
from typing import Any, Dict, Optional

import pandas as pd

from china_a_share.core.contracts import StockListItem, StockListResponse
from china_a_share.core.ports import MarketDataProvider
from china_a_share.observability import log_event


STOCK_BASIC_OPERATION = "stock_basic"
STOCK_BASIC_QUERY_ID = "stock-catalog"
STOCK_BASIC_DATE_FORMAT = "%Y%m%d"
STOCK_BASIC_FIELDS = (
    "ts_code",
    "symbol",
    "name",
    "area",
    "industry",
    "market",
    "exchange",
    "list_date",
)


logger = logging.getLogger(__name__)


class StockCatalogService:
    """Provide deterministic paginated access to listed A-share securities."""

    def __init__(self, provider: MarketDataProvider) -> None:
        """Store the provider used for the cached stock master-data read."""
        self._provider = provider

    def list_stocks(
        self,
        request_id: str,
        *,
        page: int,
        page_size: int,
        search: str,
        exchange: str,
        industry: str,
        api_route: str,
    ) -> StockListResponse:
        """Return one filtered and deterministically ordered stock page."""
        log_event(
            logger,
            logging.INFO,
            "stock_catalog_query_started",
            api_route=api_route,
            request_id=request_id,
            page=page,
            page_size=page_size,
        )
        frame = self._provider.query(
            STOCK_BASIC_OPERATION,
            {"list_status": "L"},
            STOCK_BASIC_FIELDS,
            api_route=api_route,
            request_id=request_id,
            query_id=STOCK_BASIC_QUERY_ID,
        )
        stocks = sorted(
            (self._normalize_stock(row) for row in frame.to_dict(orient="records")),
            key=lambda stock: stock.code,
        )
        available_industries = sorted(
            {stock.industry for stock in stocks if stock.industry is not None}
        )
        normalized_search = search.strip().casefold()
        normalized_exchange = exchange.strip().upper()
        normalized_industry = industry.strip()
        filtered_stocks = [
            stock
            for stock in stocks
            if self._matches_filters(
                stock,
                search=normalized_search,
                exchange=normalized_exchange,
                industry=normalized_industry,
            )
        ]
        total = len(filtered_stocks)
        total_pages = max(1, ceil(total / page_size))
        if page > total_pages:
            raise IndexError(
                f"page {page} exceeds the last available page {total_pages}"
            )
        start = (page - 1) * page_size
        items = filtered_stocks[start:start + page_size]
        log_event(
            logger,
            logging.INFO,
            "stock_catalog_query_completed",
            api_route=api_route,
            request_id=request_id,
            page=page,
            page_size=page_size,
            result_count=len(items),
            total=total,
        )
        return StockListResponse(
            request_id=request_id,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            available_industries=available_industries,
            items=items,
        )

    @staticmethod
    def _matches_filters(
        stock: StockListItem,
        *,
        search: str,
        exchange: str,
        industry: str,
    ) -> bool:
        """Return whether one normalized security matches all active filters."""
        searchable_values = (stock.code, stock.name, stock.industry or "")
        return (
            (not search or any(search in value.casefold() for value in searchable_values))
            and (not exchange or stock.exchange == exchange)
            and (not industry or stock.industry == industry)
        )

    @classmethod
    def _normalize_stock(cls, row: Dict[str, Any]) -> StockListItem:
        """Validate and normalize one provider row into the public stock contract."""
        list_date = cls._required_text(row.get("list_date"), "list_date")
        return StockListItem(
            code=cls._required_text(row.get("ts_code"), "ts_code"),
            symbol=cls._required_text(row.get("symbol"), "symbol"),
            name=cls._required_text(row.get("name"), "name"),
            area=cls._optional_text(row.get("area")),
            industry=cls._optional_text(row.get("industry")),
            board=cls._optional_text(row.get("market")),
            exchange=cls._required_text(row.get("exchange"), "exchange"),
            listed_on=datetime.strptime(
                list_date,
                STOCK_BASIC_DATE_FORMAT,
            ).date(),
        )

    @staticmethod
    def _required_text(value: Any, field_name: str) -> str:
        """Return required provider text or fail with the invalid field name."""
        normalized = StockCatalogService._optional_text(value)
        if normalized is None:
            raise ValueError(f"stock_basic returned an empty {field_name} field")
        return normalized

    @staticmethod
    def _optional_text(value: Any) -> Optional[str]:
        """Convert nullable provider scalar values to stripped optional text."""
        if value is None or pd.isna(value):
            return None
        normalized = str(value).strip()
        return normalized or None

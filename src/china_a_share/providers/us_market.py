"""U.S. equity research provider backed by Massive and Finnhub."""

from __future__ import annotations

from collections import deque
from datetime import date, datetime, time, timedelta, timezone
import logging
import re
from threading import Lock
from time import monotonic
from typing import Any, Callable, Deque, Dict, Mapping, Optional, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from china_a_share.core.contracts import DataOperation
from china_a_share.core.errors import DataProviderError
from china_a_share.core.ports import DataResponseCache


US_MARKET_PROVIDER_NAME = "us_market"
MASSIVE_PROVIDER_NAME = "massive"
FINNHUB_PROVIDER_NAME = "finnhub"
MASSIVE_API_BASE_URL = "https://api.massive.com"
FINNHUB_API_BASE_URL = "https://finnhub.io/api/v1"
UPSTREAM_TIMEOUT_SECONDS = 30
MASSIVE_FREE_REQUEST_LIMIT = 5
MASSIVE_FREE_RATE_WINDOW_SECONDS = 60
SHORT_CACHE_TTL = timedelta(minutes=15)
RESEARCH_CACHE_TTL = timedelta(hours=4)
REFERENCE_CACHE_TTL = timedelta(days=1)
HISTORICAL_DAILY_CACHE_TTL = timedelta(days=30)
NEW_YORK_TIMEZONE = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


US_OPERATION_GUIDANCE: Dict[str, str] = {
    "us_stock_daily": (
        "单只美股/US stock 的历史日线 OHLCV。参数：symbol、start_date、end_date，"
        "日期格式 YYYY-MM-DD 或 YYYYMMDD。输出包含 symbol、date、open、high、low、"
        "close、volume、vwap、transactions、data_source。Massive 为主数据源；其额度"
        "受限或临时不可用时自动降级至 Finnhub。"
    ),
    "us_market_daily": (
        "某一个交易日的全市场美股日线快照，适合全市场涨跌幅计算和排名。参数：date。"
        "输出包含 symbol、date、open、high、low、close、volume、vwap、transactions、"
        "data_source。该批量能力仅由 Massive 提供，不使用逐股票查询伪装完整结果。"
    ),
    "us_company_profile": (
        "Finnhub 美股公司资料。参数：symbol。常见字段包括 name、ticker、exchange、"
        "industry、country、market_cap、shares_outstanding、ipo_date、website。"
    ),
    "us_financial_metrics": (
        "Finnhub 美股基本财务和估值指标。参数：symbol，可选 metric，默认 all。"
        "输出为 metric、value、data_source 的长表，便于筛选和比较。"
    ),
    "us_analyst_recommendations": (
        "Finnhub 分析师推荐趋势。参数：symbol。输出 period、strong_buy、buy、hold、"
        "sell、strong_sell、data_source。"
    ),
    "us_price_target": (
        "Finnhub 分析师目标价汇总。参数：symbol。输出 target_high、target_low、"
        "target_mean、target_median、last_updated、data_source。"
    ),
    "us_company_news": (
        "Finnhub 公司新闻。参数：symbol、start_date、end_date。输出 datetime、headline、"
        "summary、source、url、category、data_source。"
    ),
    "us_earnings_calendar": (
        "Finnhub 财报日历。参数：start_date、end_date，可选 symbol。输出 date、symbol、"
        "quarter、year、eps_estimate、revenue_estimate、data_source。"
    ),
}

US_OPERATION_FIELDS: Dict[str, Sequence[str]] = {
    "us_stock_daily": (
        "symbol", "date", "open", "high", "low", "close", "volume", "vwap",
        "transactions", "data_source",
    ),
    "us_market_daily": (
        "symbol", "date", "open", "high", "low", "close", "volume", "vwap",
        "transactions", "data_source",
    ),
    "us_company_profile": (
        "symbol", "name", "ticker", "exchange", "industry", "country", "currency",
        "market_cap", "shares_outstanding", "ipo_date", "website", "logo",
        "data_source",
    ),
    "us_financial_metrics": ("symbol", "metric", "value", "data_source"),
    "us_analyst_recommendations": (
        "symbol", "period", "strong_buy", "buy", "hold", "sell", "strong_sell",
        "data_source",
    ),
    "us_price_target": (
        "symbol", "target_high", "target_low", "target_mean", "target_median",
        "last_updated", "data_source",
    ),
    "us_company_news": (
        "symbol", "datetime", "headline", "summary", "source", "url", "category",
        "data_source",
    ),
    "us_earnings_calendar": (
        "date", "symbol", "quarter", "year", "eps_estimate", "eps_actual",
        "revenue_estimate", "revenue_actual", "hour", "data_source",
    ),
}

US_OPERATION_REQUIRED_PARAMS: Dict[str, Sequence[str]] = {
    "us_stock_daily": ("symbol", "start_date", "end_date"),
    "us_market_daily": ("date",),
    "us_company_profile": ("symbol",),
    "us_financial_metrics": ("symbol",),
    "us_analyst_recommendations": ("symbol",),
    "us_price_target": ("symbol",),
    "us_company_news": ("symbol", "start_date", "end_date"),
    "us_earnings_calendar": ("start_date", "end_date"),
}


class USMarketDataProvider:
    """Route U.S. equity capabilities to equivalent upstream data products."""

    def __init__(
        self,
        massive_api_key: str,
        finnhub_api_key: str,
        response_cache: DataResponseCache,
        *,
        massive_session: Optional[requests.Session] = None,
        finnhub_session: Optional[requests.Session] = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        """Store optional credentials, shared cache, transports, and quota state."""
        if not massive_api_key and not finnhub_api_key:
            raise ValueError("At least one U.S. market-data API key is required.")
        self._massive_api_key = massive_api_key
        self._finnhub_api_key = finnhub_api_key
        self._response_cache = response_cache
        self._massive_session = massive_session or requests.Session()
        self._finnhub_session = finnhub_session or requests.Session()
        self._massive_limiter = _NonBlockingSlidingWindowLimiter(
            MASSIVE_FREE_REQUEST_LIMIT,
            MASSIVE_FREE_RATE_WINDOW_SECONDS,
            monotonic_clock,
        )

    @property
    def name(self) -> str:
        """Return the stable provider identity used by the composite catalog."""
        return US_MARKET_PROVIDER_NAME

    def search_operations(self, prompt: str) -> Sequence[DataOperation]:
        """Return every configured operation with complete planner guidance."""
        if not prompt.strip():
            return ()
        return tuple(
            DataOperation(name=name, description=description)
            for name, description in US_OPERATION_GUIDANCE.items()
            if self._operation_is_configured(name)
        )

    def supports(self, operation: str) -> bool:
        """Return whether the operation exists and has a configured upstream."""
        return operation in US_OPERATION_GUIDANCE and self._operation_is_configured(
            operation
        )

    def describe_query_shapes(self, operation: str) -> Sequence[Dict[str, Any]]:
        """Return the canonical validated parameter shape for one operation."""
        if not self.supports(operation):
            return ()
        return (
            {
                "shape_id": f"{operation}_canonical",
                "required_params": list(US_OPERATION_REQUIRED_PARAMS[operation]),
            },
        )

    def validate_query(
        self,
        operation: str,
        params: Dict[str, Any],
        fields: Sequence[str],
    ) -> None:
        """Validate one canonical U.S. market-data request before network access."""
        if not self.supports(operation):
            raise ValueError(f"Unsupported U.S. market-data operation: {operation}")
        allowed_fields = set(US_OPERATION_FIELDS[operation])
        unknown_fields = set(fields).difference(allowed_fields)
        if unknown_fields:
            raise ValueError(
                f"Unsupported fields for {operation}: "
                + ", ".join(sorted(unknown_fields))
            )

        allowed_params = {
            "us_stock_daily": {"symbol", "start_date", "end_date"},
            "us_market_daily": {"date"},
            "us_company_profile": {"symbol"},
            "us_financial_metrics": {"symbol", "metric"},
            "us_analyst_recommendations": {"symbol"},
            "us_price_target": {"symbol"},
            "us_company_news": {"symbol", "start_date", "end_date"},
            "us_earnings_calendar": {"symbol", "start_date", "end_date"},
        }[operation]
        unknown_params = set(params).difference(allowed_params)
        if unknown_params:
            raise ValueError(
                f"Unsupported parameters for {operation}: "
                + ", ".join(sorted(unknown_params))
            )
        if operation in {
            "us_stock_daily",
            "us_company_profile",
            "us_financial_metrics",
            "us_analyst_recommendations",
            "us_price_target",
            "us_company_news",
        }:
            self._normalize_symbol(params.get("symbol"))
        if operation == "us_market_daily":
            self._parse_date(params.get("date"), "date")
        if operation in {"us_stock_daily", "us_company_news", "us_earnings_calendar"}:
            start = self._parse_date(params.get("start_date"), "start_date")
            end = self._parse_date(params.get("end_date"), "end_date")
            if start > end:
                raise ValueError("start_date must not be after end_date")

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
        """Return one cached canonical dataset from the assigned upstream."""
        self.validate_query(operation, params, fields)
        normalized_params = self._normalize_params(operation, params)
        selected_fields = list(fields or US_OPERATION_FIELDS[operation])
        frame = self._response_cache.get_or_fetch(
            self.name,
            operation,
            normalized_params,
            selected_fields,
            lambda: self._fetch(operation, normalized_params),
            api_route=api_route,
            request_id=request_id,
            query_id=query_id,
        )
        sources = frame.get("data_source")
        source_name: Optional[str] = None
        if sources is not None and not sources.empty:
            unique_sources = sources.dropna().astype(str).unique()
            if len(unique_sources) == 1:
                source_name = unique_sources[0]
        if selected_fields:
            frame = frame.reindex(columns=selected_fields)
        if source_name is not None:
            frame.attrs["provider"] = source_name
        return frame

    def _fetch(self, operation: str, params: Dict[str, Any]) -> pd.DataFrame:
        """Dispatch one validated operation without prompt-dependent branches."""
        if operation == "us_stock_daily":
            return self._fetch_stock_daily(params)
        if operation == "us_market_daily":
            return self._fetch_massive_market_daily(params)
        handlers = {
            "us_company_profile": self._fetch_finnhub_company_profile,
            "us_financial_metrics": self._fetch_finnhub_financial_metrics,
            "us_analyst_recommendations": self._fetch_finnhub_recommendations,
            "us_price_target": self._fetch_finnhub_price_target,
            "us_company_news": self._fetch_finnhub_company_news,
            "us_earnings_calendar": self._fetch_finnhub_earnings_calendar,
        }
        return handlers[operation](params)

    def _fetch_stock_daily(self, params: Dict[str, Any]) -> pd.DataFrame:
        """Use Massive first and Finnhub only for equivalent single-symbol bars."""
        massive_error: Optional[DataProviderError] = None
        if self._massive_api_key:
            try:
                return self._fetch_massive_stock_daily(params)
            except DataProviderError as exc:
                if not self._eligible_for_fallback(exc):
                    raise
                massive_error = exc
                logger.warning(
                    "us_daily_provider_fallback primary=massive fallback=finnhub "
                    "http_status=%s code=%s",
                    exc.http_status,
                    exc.code,
                )
        if self._finnhub_api_key:
            return self._fetch_finnhub_stock_daily(params)
        if massive_error is not None:
            raise massive_error
        raise DataProviderError(
            source=US_MARKET_PROVIDER_NAME,
            code="provider_not_configured",
            message="No provider is configured for U.S. stock daily data.",
        )

    def _fetch_massive_stock_daily(self, params: Dict[str, Any]) -> pd.DataFrame:
        """Fetch one symbol's daily aggregates from Massive."""
        symbol = params["symbol"]
        path = (
            f"/v2/aggs/ticker/{quote(symbol, safe='')}/range/1/day/"
            f"{params['start_date']}/{params['end_date']}"
        )
        payload = self._massive_get(
            path,
            {"adjusted": "true", "sort": "asc", "limit": 50_000},
        )
        rows = [
            {
                "symbol": symbol,
                "date": self._timestamp_to_market_date(item.get("t")),
                "open": item.get("o"),
                "high": item.get("h"),
                "low": item.get("l"),
                "close": item.get("c"),
                "volume": item.get("v"),
                "vwap": item.get("vw"),
                "transactions": item.get("n"),
                "data_source": MASSIVE_PROVIDER_NAME,
            }
            for item in payload.get("results") or []
        ]
        return pd.DataFrame(rows, columns=US_OPERATION_FIELDS["us_stock_daily"])

    def _fetch_massive_market_daily(self, params: Dict[str, Any]) -> pd.DataFrame:
        """Fetch one complete market date without per-symbol fan-out."""
        payload = self._massive_get(
            f"/v2/aggs/grouped/locale/us/market/stocks/{params['date']}",
            {"adjusted": "true", "include_otc": "false"},
        )
        rows = [
            {
                "symbol": item.get("T"),
                "date": params["date"],
                "open": item.get("o"),
                "high": item.get("h"),
                "low": item.get("l"),
                "close": item.get("c"),
                "volume": item.get("v"),
                "vwap": item.get("vw"),
                "transactions": item.get("n"),
                "data_source": MASSIVE_PROVIDER_NAME,
            }
            for item in payload.get("results") or []
        ]
        return pd.DataFrame(rows, columns=US_OPERATION_FIELDS["us_market_daily"])

    def _fetch_finnhub_stock_daily(self, params: Dict[str, Any]) -> pd.DataFrame:
        """Fetch one symbol's daily candles from Finnhub."""
        start = self._parse_date(params["start_date"], "start_date")
        end = self._parse_date(params["end_date"], "end_date")
        payload = self._finnhub_get(
            "/stock/candle",
            {
                "symbol": params["symbol"],
                "resolution": "D",
                "from": self._unix_start(start),
                "to": self._unix_end(end),
            },
        )
        if payload.get("s") == "no_data":
            return pd.DataFrame(columns=US_OPERATION_FIELDS["us_stock_daily"])
        if payload.get("s") != "ok":
            raise DataProviderError(
                source=FINNHUB_PROVIDER_NAME,
                code=payload.get("s") or "invalid_response",
                message="Finnhub returned an invalid daily-candle response.",
                raw_response=dict(payload),
            )
        timestamps = payload.get("t") or []
        rows = []
        for index, timestamp in enumerate(timestamps):
            rows.append(
                {
                    "symbol": params["symbol"],
                    "date": self._timestamp_to_market_date(timestamp, milliseconds=False),
                    "open": self._array_value(payload, "o", index),
                    "high": self._array_value(payload, "h", index),
                    "low": self._array_value(payload, "l", index),
                    "close": self._array_value(payload, "c", index),
                    "volume": self._array_value(payload, "v", index),
                    "vwap": None,
                    "transactions": None,
                    "data_source": FINNHUB_PROVIDER_NAME,
                }
            )
        return pd.DataFrame(rows, columns=US_OPERATION_FIELDS["us_stock_daily"])

    def _fetch_finnhub_company_profile(self, params: Dict[str, Any]) -> pd.DataFrame:
        """Normalize the Finnhub company profile into one row."""
        payload = self._finnhub_get("/stock/profile2", {"symbol": params["symbol"]})
        if not payload:
            return pd.DataFrame(columns=US_OPERATION_FIELDS["us_company_profile"])
        row = {
            "symbol": params["symbol"],
            "name": payload.get("name"),
            "ticker": payload.get("ticker"),
            "exchange": payload.get("exchange"),
            "industry": payload.get("finnhubIndustry"),
            "country": payload.get("country"),
            "currency": payload.get("currency"),
            "market_cap": payload.get("marketCapitalization"),
            "shares_outstanding": payload.get("shareOutstanding"),
            "ipo_date": payload.get("ipo"),
            "website": payload.get("weburl"),
            "logo": payload.get("logo"),
            "data_source": FINNHUB_PROVIDER_NAME,
        }
        return pd.DataFrame([row], columns=US_OPERATION_FIELDS["us_company_profile"])

    def _fetch_finnhub_financial_metrics(self, params: Dict[str, Any]) -> pd.DataFrame:
        """Normalize Finnhub's metric map into a stable long-form table."""
        payload = self._finnhub_get(
            "/stock/metric",
            {"symbol": params["symbol"], "metric": params.get("metric", "all")},
        )
        metrics = payload.get("metric") or {}
        rows = [
            {
                "symbol": params["symbol"],
                "metric": name,
                "value": value,
                "data_source": FINNHUB_PROVIDER_NAME,
            }
            for name, value in sorted(metrics.items())
        ]
        return pd.DataFrame(rows, columns=US_OPERATION_FIELDS["us_financial_metrics"])

    def _fetch_finnhub_recommendations(self, params: Dict[str, Any]) -> pd.DataFrame:
        """Normalize analyst recommendation history."""
        payload = self._finnhub_get(
            "/stock/recommendation", {"symbol": params["symbol"]}
        )
        rows = [
            {
                "symbol": item.get("symbol") or params["symbol"],
                "period": item.get("period"),
                "strong_buy": item.get("strongBuy"),
                "buy": item.get("buy"),
                "hold": item.get("hold"),
                "sell": item.get("sell"),
                "strong_sell": item.get("strongSell"),
                "data_source": FINNHUB_PROVIDER_NAME,
            }
            for item in payload
        ]
        return pd.DataFrame(
            rows, columns=US_OPERATION_FIELDS["us_analyst_recommendations"]
        )

    def _fetch_finnhub_price_target(self, params: Dict[str, Any]) -> pd.DataFrame:
        """Normalize the latest analyst price-target summary."""
        payload = self._finnhub_get("/stock/price-target", {"symbol": params["symbol"]})
        if not payload:
            return pd.DataFrame(columns=US_OPERATION_FIELDS["us_price_target"])
        row = {
            "symbol": params["symbol"],
            "target_high": payload.get("targetHigh"),
            "target_low": payload.get("targetLow"),
            "target_mean": payload.get("targetMean"),
            "target_median": payload.get("targetMedian"),
            "last_updated": payload.get("lastUpdated"),
            "data_source": FINNHUB_PROVIDER_NAME,
        }
        return pd.DataFrame([row], columns=US_OPERATION_FIELDS["us_price_target"])

    def _fetch_finnhub_company_news(self, params: Dict[str, Any]) -> pd.DataFrame:
        """Normalize company news while retaining source links."""
        payload = self._finnhub_get(
            "/company-news",
            {
                "symbol": params["symbol"],
                "from": params["start_date"],
                "to": params["end_date"],
            },
        )
        rows = [
            {
                "symbol": params["symbol"],
                "datetime": self._timestamp_to_iso(item.get("datetime")),
                "headline": item.get("headline"),
                "summary": item.get("summary"),
                "source": item.get("source"),
                "url": item.get("url"),
                "category": item.get("category"),
                "data_source": FINNHUB_PROVIDER_NAME,
            }
            for item in payload
        ]
        return pd.DataFrame(rows, columns=US_OPERATION_FIELDS["us_company_news"])

    def _fetch_finnhub_earnings_calendar(self, params: Dict[str, Any]) -> pd.DataFrame:
        """Normalize the earnings calendar rows returned by Finnhub."""
        query = {"from": params["start_date"], "to": params["end_date"]}
        if params.get("symbol"):
            query["symbol"] = params["symbol"]
        payload = self._finnhub_get("/calendar/earnings", query)
        rows = [
            {
                "date": item.get("date"),
                "symbol": item.get("symbol"),
                "quarter": item.get("quarter"),
                "year": item.get("year"),
                "eps_estimate": item.get("epsEstimate"),
                "eps_actual": item.get("epsActual"),
                "revenue_estimate": item.get("revenueEstimate"),
                "revenue_actual": item.get("revenueActual"),
                "hour": item.get("hour"),
                "data_source": FINNHUB_PROVIDER_NAME,
            }
            for item in payload.get("earningsCalendar") or []
        ]
        return pd.DataFrame(rows, columns=US_OPERATION_FIELDS["us_earnings_calendar"])

    def _massive_get(self, path: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """Issue one quota-aware Massive request and raise a safe provider error."""
        if not self._massive_limiter.try_acquire():
            raise DataProviderError(
                source=MASSIVE_PROVIDER_NAME,
                code="local_rate_limit",
                http_status=429,
                message="Massive free-tier request budget is temporarily exhausted.",
            )
        query = dict(params)
        query["apiKey"] = self._massive_api_key
        return self._request_json(
            self._massive_session,
            MASSIVE_PROVIDER_NAME,
            f"{MASSIVE_API_BASE_URL}{path}",
            query,
        )

    def _finnhub_get(self, path: str, params: Mapping[str, Any]) -> Any:
        """Issue one authenticated Finnhub request."""
        query = dict(params)
        query["token"] = self._finnhub_api_key
        return self._request_json(
            self._finnhub_session,
            FINNHUB_PROVIDER_NAME,
            f"{FINNHUB_API_BASE_URL}{path}",
            query,
        )

    @staticmethod
    def _request_json(
        session: requests.Session,
        source: str,
        url: str,
        params: Mapping[str, Any],
    ) -> Any:
        """Decode one JSON response without exposing credentials in failures."""
        try:
            response = session.get(url, params=params, timeout=UPSTREAM_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise DataProviderError(
                source=source,
                code="transport_error",
                message=f"{source} request failed.",
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise DataProviderError(
                source=source,
                code="invalid_json",
                http_status=response.status_code,
                message=f"{source} returned invalid JSON.",
            ) from exc
        if response.status_code >= 400:
            message = (
                payload.get("error")
                or payload.get("message")
                or f"{source} returned HTTP {response.status_code}."
                if isinstance(payload, dict)
                else f"{source} returned HTTP {response.status_code}."
            )
            raise DataProviderError(
                source=source,
                code=(payload.get("status") if isinstance(payload, dict) else None),
                http_status=response.status_code,
                message=str(message),
                raw_response=payload if isinstance(payload, dict) else None,
            )
        if isinstance(payload, dict) and payload.get("error"):
            raise DataProviderError(
                source=source,
                code="provider_error",
                message=str(payload["error"]),
                raw_response=payload,
            )
        if isinstance(payload, dict) and payload.get("status") == "ERROR":
            raise DataProviderError(
                source=source,
                code="provider_error",
                message=str(payload.get("error") or "Provider returned an error."),
                raw_response=payload,
            )
        return payload

    def _operation_is_configured(self, operation: str) -> bool:
        """Hide capabilities that have no usable credential."""
        if operation == "us_stock_daily":
            return bool(self._massive_api_key or self._finnhub_api_key)
        if operation == "us_market_daily":
            return bool(self._massive_api_key)
        return bool(self._finnhub_api_key)

    @staticmethod
    def _eligible_for_fallback(error: DataProviderError) -> bool:
        """Fallback only for quota, entitlement, transport, and transient failures."""
        return error.http_status in {402, 403, 408, 429, 500, 502, 503, 504} or error.code in {
            "local_rate_limit",
            "transport_error",
        }

    @staticmethod
    def _normalize_params(operation: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Canonicalize symbols and dates for stable cache keys."""
        normalized = dict(params)
        if "symbol" in normalized and normalized.get("symbol"):
            normalized["symbol"] = USMarketDataProvider._normalize_symbol(
                normalized["symbol"]
            )
        for key in ("date", "start_date", "end_date"):
            if key in normalized:
                normalized[key] = USMarketDataProvider._parse_date(
                    normalized[key], key
                ).isoformat()
        if operation == "us_financial_metrics":
            normalized["metric"] = str(normalized.get("metric") or "all").strip()
        return normalized

    @staticmethod
    def _normalize_symbol(value: Any) -> str:
        """Return one uppercase U.S. ticker accepted by both providers."""
        symbol = str(value or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", symbol):
            raise ValueError("symbol must be a valid U.S. ticker")
        return symbol

    @staticmethod
    def _parse_date(value: Any, field: str) -> date:
        """Parse one required ISO or compact calendar date."""
        text = str(value or "").strip()
        for pattern in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(text, pattern).date()
            except ValueError:
                continue
        raise ValueError(f"{field} must use YYYY-MM-DD or YYYYMMDD")

    @staticmethod
    def _unix_start(value: date) -> int:
        """Return the UTC timestamp covering the start of one market date."""
        local = datetime.combine(value, time.min, tzinfo=NEW_YORK_TIMEZONE)
        return int(local.timestamp())

    @staticmethod
    def _unix_end(value: date) -> int:
        """Return the inclusive UTC timestamp covering the end of one market date."""
        local = datetime.combine(value + timedelta(days=1), time.min, tzinfo=NEW_YORK_TIMEZONE)
        return int(local.timestamp()) - 1

    @staticmethod
    def _timestamp_to_market_date(value: Any, *, milliseconds: bool = True) -> Optional[str]:
        """Convert an upstream Unix timestamp to a New York trading date."""
        if value is None:
            return None
        timestamp = float(value) / 1_000 if milliseconds else float(value)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(
            NEW_YORK_TIMEZONE
        ).date().isoformat()

    @staticmethod
    def _timestamp_to_iso(value: Any) -> Optional[str]:
        """Convert a Unix-second timestamp to an explicit UTC instant."""
        if value is None:
            return None
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()

    @staticmethod
    def _array_value(payload: Mapping[str, Any], key: str, index: int) -> Any:
        """Read one aligned candle array value without masking malformed lengths."""
        values = payload.get(key) or []
        if index >= len(values):
            raise DataProviderError(
                source=FINNHUB_PROVIDER_NAME,
                code="misaligned_candles",
                message=f"Finnhub candle field {key} is shorter than timestamps.",
            )
        return values[index]


class USMarketCacheExpirationPolicy:
    """Cache U.S. datasets according to their publication cadence."""

    def resolve(
        self,
        operation: str,
        params: Dict[str, Any],
        fetched_at: datetime,
    ) -> Optional[datetime]:
        """Return a bounded expiration for every supported operation."""
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        if operation not in US_OPERATION_GUIDANCE:
            raise ValueError(f"U.S. market operation has no cache profile: {operation}")
        if operation == "us_company_profile":
            return fetched_at + REFERENCE_CACHE_TTL
        if operation in {
            "us_financial_metrics",
            "us_analyst_recommendations",
            "us_price_target",
        }:
            return fetched_at + RESEARCH_CACHE_TTL
        if operation in {"us_company_news", "us_earnings_calendar"}:
            return fetched_at + SHORT_CACHE_TTL
        end_value = params.get("end_date") or params.get("date")
        if end_value:
            end_date = USMarketDataProvider._parse_date(end_value, "end_date")
            market_today = fetched_at.astimezone(NEW_YORK_TIMEZONE).date()
            if end_date < market_today:
                return fetched_at + HISTORICAL_DAILY_CACHE_TTL
        return fetched_at + SHORT_CACHE_TTL


class _NonBlockingSlidingWindowLimiter:
    """Reject local requests that would exceed one shared process quota."""

    def __init__(
        self,
        limit: int,
        window_seconds: int,
        clock: Callable[[], float],
    ) -> None:
        """Initialize bounded request history and concurrency protection."""
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._requests: Deque[float] = deque()
        self._lock = Lock()

    def try_acquire(self) -> bool:
        """Consume one slot immediately or return false without sleeping."""
        now = self._clock()
        cutoff = now - self._window_seconds
        with self._lock:
            while self._requests and self._requests[0] <= cutoff:
                self._requests.popleft()
            if len(self._requests) >= self._limit:
                return False
            self._requests.append(now)
            return True

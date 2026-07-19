"""Tushare Pro client with raw upstream error preservation."""

from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
import requests
import tushare as ts

from .cache import LayeredTushareResponseCache
from .config import Settings


TUSHARE_API_BASE_URL = "http://api.waditu.com/dataapi"
TUSHARE_REQUEST_TIMEOUT_SECONDS = 60


class TushareApiError(RuntimeError):
    """Tushare failure containing the original safe response body."""

    def __init__(
        self,
        message: str,
        code: Optional[Any] = None,
        http_status: Optional[int] = None,
        raw_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.raw_response = raw_response


class TushareClient:
    """Authenticate once and expose Tushare stock data calls."""

    def __init__(
        self,
        settings: Settings,
        pro_api: Optional[Any] = None,
        session: Optional[requests.Session] = None,
        response_cache: Optional[LayeredTushareResponseCache] = None,
    ) -> None:
        self._settings = settings
        self.pro = pro_api if pro_api is not None else ts.pro_api(settings.tushare_token)
        self.session = session if session is not None else requests.Session()
        self.response_cache = response_cache

    def check_connection(self) -> pd.DataFrame:
        """Verify the token, network, and basic daily-data permission."""
        return self.pro.daily(
            ts_code="000001.SZ",
            start_date="20240102",
            end_date="20240102",
        )

    def stock_basic(
        self, list_status: str = "L", exchange: str = ""
    ) -> pd.DataFrame:
        """Return listed A-share security master data."""
        return self.pro.stock_basic(
            exchange=exchange,
            list_status=list_status,
            fields="ts_code,symbol,name,area,industry,market,list_date",
        )

    def daily(
        self,
        ts_code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """Return unadjusted daily prices for one or more securities."""
        return self.pro.daily(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )

    def query(
        self,
        api_name: str,
        params: Dict[str, Any],
        fields: Sequence[str],
    ) -> pd.DataFrame:
        """Call any allowlisted Tushare stock API and retain its error body."""
        if self.response_cache is not None:
            return self.response_cache.get_or_fetch(
                api_name,
                params,
                fields,
                lambda: self._query_uncached(api_name, params, fields),
            )
        return self._query_uncached(api_name, params, fields)

    def _query_uncached(
        self,
        api_name: str,
        params: Dict[str, Any],
        fields: Sequence[str],
    ) -> pd.DataFrame:
        """Perform one upstream request without reading or writing cache state."""
        if api_name == "pro_bar":
            try:
                return ts.pro_bar(pro_api=self.pro, **params)
            except Exception as exc:
                raise TushareApiError(message=str(exc)) from exc

        request_body = {
            "api_name": api_name,
            "token": self._settings.tushare_token,
            "params": params,
            "fields": ",".join(fields),
        }
        try:
            response = self.session.post(
                f"{TUSHARE_API_BASE_URL}/{api_name}",
                json=request_body,
                timeout=TUSHARE_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise TushareApiError(message=str(exc)) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise TushareApiError(
                message="Tushare returned a non-JSON response.",
                http_status=response.status_code,
                raw_response={"text": response.text},
            ) from exc

        if response.status_code >= 400 or payload.get("code") != 0:
            raise TushareApiError(
                message=str(payload.get("msg") or "Tushare request failed."),
                code=payload.get("code"),
                http_status=response.status_code,
                raw_response=payload,
            )

        data = payload.get("data") or {}
        columns: List[str] = data.get("fields") or []
        rows: List[List[Any]] = data.get("items") or []
        return pd.DataFrame(rows, columns=columns)

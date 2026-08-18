"""Low-level Tushare transport with raw upstream error preservation."""

import logging
from collections import defaultdict, deque
from threading import Lock
from time import monotonic, sleep
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
import requests
import tushare as ts

from .core.errors import DataProviderError


TUSHARE_API_BASE_URL = "http://api.waditu.com/dataapi"
TUSHARE_REQUEST_TIMEOUT_SECONDS = 60
TUSHARE_MAX_ATTEMPTS = 3
TUSHARE_RETRY_DELAY_SECONDS = 1
TUSHARE_OPERATION_RATE_LIMIT = 450
TUSHARE_OPERATION_RATE_WINDOW_SECONDS = 60


logger = logging.getLogger(__name__)


class _TushareOperationRateLimiter:
    """Keep each provider operation below its documented per-minute ceiling."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._request_times = defaultdict(deque)

    def acquire(self, api_name: str) -> None:
        """Wait until one operation-specific request slot is available."""
        while True:
            now = monotonic()
            with self._lock:
                request_times = self._request_times[api_name]
                cutoff = now - TUSHARE_OPERATION_RATE_WINDOW_SECONDS
                while request_times and request_times[0] <= cutoff:
                    request_times.popleft()
                if len(request_times) < TUSHARE_OPERATION_RATE_LIMIT:
                    request_times.append(now)
                    return
                wait_seconds = max(
                    request_times[0]
                    + TUSHARE_OPERATION_RATE_WINDOW_SECONDS
                    - now,
                    0,
                )
            logger.info(
                "tushare_rate_limit_wait api_name=%s wait_seconds=%.3f",
                api_name,
                wait_seconds,
            )
            sleep(wait_seconds)


class TushareApiError(DataProviderError):
    """Tushare failure containing the original safe response body."""

    def __init__(
        self,
        message: str,
        code: Optional[Any] = None,
        http_status: Optional[int] = None,
        raw_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            source="tushare",
            message=message,
            code=code,
            http_status=http_status,
            raw_response=raw_response,
        )


class TushareTransport:
    """Authenticate once and expose uncached Tushare data calls."""

    def __init__(
        self,
        token: str,
        pro_api: Optional[Any] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not token.strip():
            raise ValueError("token must not be empty")
        self._token = token
        self.pro = pro_api if pro_api is not None else ts.pro_api(token)
        self.session = session if session is not None else requests.Session()
        self._rate_limiter = _TushareOperationRateLimiter()

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
        """Perform one uncached Tushare call and retain its safe error body."""
        if api_name == "pro_bar":
            try:
                return ts.pro_bar(pro_api=self.pro, **params)
            except Exception as exc:
                raise TushareApiError(message=str(exc)) from exc

        request_body = {
            "api_name": api_name,
            "token": self._token,
            "params": params,
            "fields": ",".join(fields),
        }
        self._rate_limiter.acquire(api_name)
        response = None
        for attempt in range(TUSHARE_MAX_ATTEMPTS):
            try:
                response = self.session.post(
                    f"{TUSHARE_API_BASE_URL}/{api_name}",
                    json=request_body,
                    timeout=TUSHARE_REQUEST_TIMEOUT_SECONDS,
                )
                break
            except requests.RequestException as exc:
                logger.warning(
                    "tushare_request_failed api_name=%s attempt=%s max_attempts=%s "
                    "error=%s",
                    api_name,
                    attempt + 1,
                    TUSHARE_MAX_ATTEMPTS,
                    exc,
                )
                if attempt + 1 == TUSHARE_MAX_ATTEMPTS:
                    raise TushareApiError(message=str(exc)) from exc
                sleep(TUSHARE_RETRY_DELAY_SECONDS)
        if response is None:
            raise RuntimeError("Tushare request loop completed without a response.")

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

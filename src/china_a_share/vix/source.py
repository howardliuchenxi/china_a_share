"""CNBC quote adapter for the Cboe Volatility Index (VIX).

The public CNBC quote endpoint is the only tested key-free source that both
serves VIX and updates during the trading session (source matrix: research
repo `vix_index_relationship/README.md`). The payload carries day-granularity
timestamps (`last_time` "YYYY-MM-DD"), so quote freshness is decided per
calendar day in US/Eastern terms:

- during the quote's own date before 17:00 ET: intraday quote, evaluable;
- at/after 17:00 ET on the quote's date: settled close, evaluable and
  appendable to history;
- any older date: stale, not evaluable (weekend/holiday or upstream outage).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Callable, Optional

import requests

from china_a_share.observability import log_event

logger = logging.getLogger(__name__)

CNBC_QUOTE_URL = (
    "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
    "?symbols=VIX&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json"
)
CNBC_SPX_QUOTE_URL = (
    "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
    "?symbols=.SPX&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json"
)
QUOTE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
QUOTE_TIMEOUT_SECONDS = 10
QUOTE_ATTEMPTS = 3
QUOTE_REFRESH_MINIMUM_HOUR = 17  # Eastern time; settled close is final then

HttpGet = Callable[[str, float], str]


@dataclass(frozen=True)
class VixQuote:
    """One parsed VIX snapshot with its change versus the previous close."""

    date: str  # YYYY-MM-DD of the quote per the provider
    close: float
    prev_close: float
    change_pct: float  # signed percent versus prev_close
    raw_last_time: str

    def evaluable(self, now_eastern: datetime) -> bool:
        """Return whether this snapshot may drive alerts right now."""
        quote_day = _parse_iso_date(self.date)
        if quote_day is None:
            return False
        return quote_day == now_eastern.date()

    def settled(self, now_eastern: datetime) -> bool:
        """Return whether this snapshot is the quote date's settled close."""
        return self.evaluable(now_eastern) and now_eastern.hour >= QUOTE_REFRESH_MINIMUM_HOUR


def fetch_vix_quote(
    http_get: Optional[HttpGet] = None,
    now_utc: Optional[datetime] = None,
) -> Optional[VixQuote]:
    """Fetch one VIX snapshot, retrying transient failures; None when unusable."""
    return _fetch_quote(CNBC_QUOTE_URL, http_get)


def fetch_spx_quote(http_get: Optional[HttpGet] = None) -> Optional[VixQuote]:
    """Fetch one S&P 500 snapshot for measuring alert outcomes; None on failure.

    The index level is best-effort context for the entry/exit bookkeeping: a
    failure degrades the closing report (no return numbers) but never blocks
    an alert.
    """
    return _fetch_quote(CNBC_SPX_QUOTE_URL, http_get)


def _fetch_quote(url: str, http_get: Optional[HttpGet]) -> Optional[VixQuote]:
    getter = http_get or _default_http_get
    for attempt in range(1, QUOTE_ATTEMPTS + 1):
        try:
            payload = getter(url, QUOTE_TIMEOUT_SECONDS)
            return parse_vix_quote(payload)
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "vix_quote_fetch_attempt_failed",
                attempt=attempt,
                reason=str(exc)[:300],
            )
    return None


def parse_vix_quote(payload: str) -> VixQuote:
    """Parse one CNBC quote payload or raise; required fields must be usable."""
    envelope = json.loads(payload)
    quotes = (envelope.get("FormattedQuoteResult") or {}).get("FormattedQuote") or []
    if not quotes:
        raise ValueError("CNBC quote payload carries no quote rows.")
    row = quotes[0]
    close = _required_float(row, "last")
    prev_close = _required_float(row, "previous_day_closing")
    if prev_close <= 0:
        raise ValueError("CNBC quote payload has an unusable previous close.")
    raw_last_time = str(row.get("last_time") or "").strip()
    quote_date = _parse_iso_date(raw_last_time) or _parse_slash_date(
        str(row.get("last_timedate") or "")
    )
    if quote_date is None:
        raise ValueError("CNBC quote payload carries no parseable quote date.")
    change_pct = _parse_percent(row.get("change_pct"))
    if change_pct is None:
        change_pct = (close / prev_close - 1.0) * 100.0
    return VixQuote(
        date=quote_date.isoformat(),
        close=close,
        prev_close=prev_close,
        change_pct=change_pct,
        raw_last_time=raw_last_time,
    )


def _default_http_get(url: str, timeout_seconds: float) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": QUOTE_USER_AGENT},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.text


def _required_float(row: dict, key: str) -> float:
    value = row.get(key)
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"CNBC quote field {key} is not numeric.") from exc
    if parsed <= 0:
        raise ValueError(f"CNBC quote field {key} is not a positive level.")
    return parsed


def _parse_percent(value) -> Optional[float]:
    text = str(value or "").strip().rstrip("%")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_iso_date(text: str) -> Optional[date_type]:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_slash_date(text: str) -> Optional[date_type]:
    head = text.split(" ")[0] if text else ""
    try:
        return datetime.strptime(head, "%m/%d/%y").date()
    except ValueError:
        return None


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> int:
    first_weekday = date_type(year, month, 1).weekday()
    first_match = 1 + (weekday - first_weekday) % 7
    return first_match + 7 * (occurrence - 1)


def _us_dst_active(year: int, month: int, day: int, hour: int) -> bool:
    """US federal DST rule: 2nd Sunday of March 02:00 to 1st Sunday of November 02:00."""
    start = datetime(year, 3, _nth_weekday(year, 3, 6, 2), 2)
    end = datetime(year, 11, _nth_weekday(year, 11, 6, 1), 2)
    return start <= datetime(year, month, day, hour) < end


def eastern_wall_clock(now_utc: Optional[datetime] = None) -> datetime:
    """Return the naive US/Eastern wall clock for one UTC instant.

    Implemented with the federal DST rules instead of zoneinfo so the module
    never depends on an operating-system tz database in the runtime image.
    """
    utc_naive = (now_utc or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    ).replace(tzinfo=None)
    offset = -5
    local = utc_naive + timedelta(hours=offset)
    if _us_dst_active(local.year, local.month, local.day, local.hour):
        offset = -4
        local = utc_naive + timedelta(hours=offset)
    return local

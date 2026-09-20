"""Public quote-page links for security identifiers shown in research outputs."""

from __future__ import annotations

import re


QUOTE_PAGE_BASE_URL = "https://stockpage.10jqka.com.cn"
_A_SHARE_SUFFIXED = re.compile(r"^(\d{6})\.(?:SH|SZ|BJ)$", re.IGNORECASE)
_A_SHARE_BARE = re.compile(r"^\d{6}$")
_HK_SUFFIXED = re.compile(r"^0*(\d{1,5})\.HK$", re.IGNORECASE)
_HK_PREFIXED = re.compile(r"^HK0*(\d{1,5})$", re.IGNORECASE)
# Column names whose bare six-digit values are A-share security codes rather
# than generic numbers such as dates or counts.
SECURITY_CODE_COLUMN_NAMES = frozenset(
    {
        "ts_code",
        "code",
        "symbol",
        "wind_code",
        "security_code",
        "stock_code",
    }
)


def security_quote_page_url(identifier: object) -> str | None:
    """Return the public quote-page URL for one A-share or HK identifier.

    A-share inputs accept the exchange-suffixed form (``688220.SH``) and the
    bare six-digit form; HK inputs accept ``.HK``-suffixed and ``HK``-prefixed
    forms. Anything else (US tickers, free text) has no page and returns None.
    """
    if identifier is None:
        return None
    text = str(identifier).strip().upper()
    if not text:
        return None
    a_share = _A_SHARE_SUFFIXED.match(text)
    if a_share:
        return f"{QUOTE_PAGE_BASE_URL}/{a_share.group(1)}/"
    hk = _HK_SUFFIXED.match(text) or _HK_PREFIXED.match(text)
    if hk:
        return f"{QUOTE_PAGE_BASE_URL}/HK{int(hk.group(1)):04d}/"
    if _A_SHARE_BARE.match(text):
        return f"{QUOTE_PAGE_BASE_URL}/{text}/"
    return None


def is_security_code_column(column: str, sample_values: list[object]) -> bool:
    """Return whether one result column should render as quote-page links.

    Exchange-suffixed or HK-prefixed values are unambiguous, so any column of
    them qualifies. Bare six-digit values qualify only under a known
    security-code column name, so numeric columns such as dates stay plain.
    """
    normalized = column.strip().casefold()
    trusted_name = normalized in SECURITY_CODE_COLUMN_NAMES
    for value in sample_values:
        if value is None:
            continue
        text = str(value).strip().upper()
        if not text:
            continue
        if (
            _A_SHARE_SUFFIXED.match(text)
            or _HK_PREFIXED.match(text)
            or _HK_SUFFIXED.match(text)
        ):
            return True
        if _A_SHARE_BARE.match(text):
            return trusted_name
    return False

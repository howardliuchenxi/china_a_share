"""Deterministic relative time-range resolution for analysis requests."""

from calendar import monthrange
from datetime import date, timedelta
import re
from typing import Optional, Tuple


RELATIVE_RANGE_PATTERN = re.compile(
    r"(?:过去|近|最近)(?P<amount>\d{1,3}|半|[一二三四五六七八九十两]+)"
    r"(?P<unit>天|周|个?月|季度|年)"
)
CHINESE_NUMBERS = {
    "一": 1,
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
EXPLICIT_RANGE_PATTERN = re.compile(
    r"(?P<start>\d{8}|\d{4}(?:-\d{1,2}-\d{1,2}|年\d{1,2}月\d{1,2}日?))"
    r"\s*[～~—–至到]\s*"
    r"(?P<end>\d{8}|\d{4}(?:-\d{1,2}-\d{1,2}|年\d{1,2}月\d{1,2}日?))"
)
FUTURE_HORIZON_PATTERN = re.compile(
    r"(?:接下来|未来|之后|此后)(?P<amount>\d{1,3}|[一二三四五六七八九十两]+)"
    r"(?P<unit>个?交易日|天|周|个?月|季度|年)"
)
CONSECUTIVE_SESSION_PATTERNS = (
    re.compile(
        r"连续(?:涨停)?(?P<amount>\d{1,3}|[一二三四五六七八九十两]+)"
        r"(?:个)?(?:交易日|天)"
    ),
    re.compile(r"(?P<amount>\d{1,3}|[一二三四五六七八九十两]+)连板"),
    re.compile(
        r"(?P<amount>\d{1,3}|[一二三四五六七八九十两]+)个?连续交易日"
    ),
)


def resolve_relative_time_range(
    prompt: str,
    end_date: date,
) -> Optional[Tuple[date, date]]:
    """Resolve one explicit relative duration into inclusive calendar boundaries."""
    normalized = re.sub(r"年{2,}", "年", re.sub(r"\s+", "", prompt))
    match = RELATIVE_RANGE_PATTERN.search(normalized)
    if match is None:
        return None
    amount_token = match.group("amount")
    unit = match.group("unit")
    if amount_token == "半":
        if unit != "年":
            return None
        months = 6
    else:
        amount = (
            int(amount_token)
            if amount_token.isdigit()
            else _parse_chinese_number(amount_token)
        )
        if unit.endswith("月"):
            months = amount
        elif unit == "季度":
            months = amount * 3
        elif unit == "年":
            months = amount * 12
        else:
            days = amount * (7 if unit == "周" else 1)
            return end_date - timedelta(days=days), end_date
    return _subtract_months(end_date, months), end_date


def resolve_explicit_time_range(prompt: str) -> Optional[Tuple[date, date]]:
    """Resolve an explicit compact date range from a natural-language request."""
    match = EXPLICIT_RANGE_PATTERN.search(prompt)
    if match is None:
        return None
    start = _parse_date_token(match.group("start"))
    end = _parse_date_token(match.group("end"))
    return (start, end) if start <= end else None


def resolve_future_horizon(prompt: str) -> Optional[Tuple[int, str]]:
    """Resolve a forward calendar horizon without choosing an outcome metric."""
    normalized = re.sub(r"\s+", "", prompt)
    match = FUTURE_HORIZON_PATTERN.search(normalized)
    if match is None:
        return None
    token = match.group("amount")
    amount = int(token) if token.isdigit() else _parse_chinese_number(token)
    units = {
        "天": "day",
        "周": "week",
        "月": "month",
        "个月": "month",
        "季度": "month",
        "年": "year",
        "交易日": "trading_session",
        "个交易日": "trading_session",
    }
    unit = match.group("unit")
    return (amount * 3, "month") if unit == "季度" else (amount, units[unit])


def resolve_consecutive_session_count(prompt: str) -> Optional[int]:
    """Resolve an explicit consecutive-session or limit-board count."""
    normalized = re.sub(r"\s+", "", prompt)
    for pattern in CONSECUTIVE_SESSION_PATTERNS:
        match = pattern.search(normalized)
        if match is None:
            continue
        token = match.group("amount")
        return int(token) if token.isdigit() else _parse_chinese_number(token)
    return None


def _parse_chinese_number(token: str) -> int:
    """Parse a positive Chinese integer up to ninety-nine."""
    if token in CHINESE_NUMBERS:
        return CHINESE_NUMBERS[token]
    if "十" not in token:
        raise ValueError(f"Unsupported Chinese number: {token}")
    tens, ones = token.split("十", 1)
    return (CHINESE_NUMBERS.get(tens, 1) * 10) + CHINESE_NUMBERS.get(ones, 0)


def _parse_date_token(token: str) -> date:
    """Parse compact, ISO, or Chinese calendar dates."""
    if token.isdigit():
        return date(int(token[:4]), int(token[4:6]), int(token[6:8]))
    normalized = token.replace("年", "-").replace("月", "-").replace("日", "")
    year, month, day = (int(part) for part in normalized.split("-"))
    return date(year, month, day)


def _subtract_months(value: date, months: int) -> date:
    """Subtract whole calendar months while preserving a valid day of month."""
    zero_based_month = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(zero_based_month, 12)
    month = month_index + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def add_calendar_offset(value: date, amount: int, unit: str) -> date:
    """Add a validated calendar offset to one date."""
    if unit == "trading_session":
        raise ValueError("Trading-session offsets require an exchange calendar.")
    if unit == "day":
        return value + timedelta(days=amount)
    if unit == "week":
        return value + timedelta(weeks=amount)
    months = amount * (12 if unit == "year" else 1)
    zero_based_month = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(zero_based_month, 12)
    month = month_index + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)

"""Deterministic relative time-range resolution for analysis requests."""

from calendar import monthrange
from datetime import date, timedelta
import re
from typing import Optional, Tuple


RELATIVE_RANGE_PATTERN = re.compile(
    r"(?:过去|近|最近)(?P<amount>\d{1,3}|半|一|两|二|三|四|五|六|七|八|九|十)"
    r"(?P<unit>个?交易日|天|周|个?月|季度|年)"
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
    r"(?P<start>\d{8})\s*[～~—–至到]\s*(?P<end>\d{8})"
)
FUTURE_HORIZON_PATTERN = re.compile(
    r"(?:接下来|未来|之后|此后)(?P<amount>\d{1,3}|一|两|二|三|四|五|六|七|八|九|十)"
    r"(?P<unit>天|周|个?月|季度|年)"
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
            else CHINESE_NUMBERS[amount_token]
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
    start = date.fromisoformat(
        f"{match.group('start')[:4]}-{match.group('start')[4:6]}-{match.group('start')[6:]}"
    )
    end = date.fromisoformat(
        f"{match.group('end')[:4]}-{match.group('end')[4:6]}-{match.group('end')[6:]}"
    )
    return (start, end) if start <= end else None


def resolve_future_horizon(prompt: str) -> Optional[Tuple[int, str]]:
    """Resolve a forward calendar horizon without choosing an outcome metric."""
    normalized = re.sub(r"\s+", "", prompt)
    match = FUTURE_HORIZON_PATTERN.search(normalized)
    if match is None:
        return None
    token = match.group("amount")
    amount = int(token) if token.isdigit() else CHINESE_NUMBERS[token]
    units = {
        "天": "day",
        "周": "week",
        "月": "month",
        "个月": "month",
        "季度": "month",
        "年": "year",
    }
    unit = match.group("unit")
    return (amount * 3, "month") if unit == "季度" else (amount, units[unit])


def _subtract_months(value: date, months: int) -> date:
    """Subtract whole calendar months while preserving a valid day of month."""
    zero_based_month = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(zero_based_month, 12)
    month = month_index + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def add_calendar_offset(value: date, amount: int, unit: str) -> date:
    """Add a validated calendar offset to one date."""
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

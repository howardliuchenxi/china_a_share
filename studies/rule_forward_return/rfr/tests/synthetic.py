"""Shared synthetic panel builders for tests (deterministic, hand-checkable)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def make_bars(
    symbol: str,
    dates,
    closes,
    opens=None,
    volumes=None,
) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.asarray(opens, dtype=float) if opens is not None else closes.copy()
    volumes = (
        np.asarray(volumes, dtype=float)
        if volumes is not None
        else np.full(len(closes), 3e6)  # $30M dollar volume at price ~10
    )
    return pd.DataFrame(
        {
            "symbol": symbol,
            "date": pd.to_datetime(list(dates)),
            "open": opens,
            "high": np.maximum(opens, closes) + 0.5,
            "low": np.minimum(opens, closes) - 0.5,
            "close": closes,
            "volume": volumes,
            "vwap": (opens + closes) / 2.0,
            "transactions": 1000.0,
        }
    )


def business_dates(start: str, periods: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=periods)

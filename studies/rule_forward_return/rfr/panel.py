"""Panel loading, trading calendar, and per-date liquidity pool membership."""
from __future__ import annotations

import glob
import os

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYMBOLS_DIR = os.path.join(HERE, "data", "symbols")
PANEL_COLUMNS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "transactions",
)

# Pool membership is evaluated at signal close T with data through T only.
POOL_MIN_ADV20 = 20_000_000.0  # trailing 20-bar median dollar volume, USD
POOL_MIN_CLOSE = 5.0
POOL_TRAIL = 20


def load_panel(symbols_dir: str = SYMBOLS_DIR) -> pd.DataFrame:
    """Concatenate per-symbol parquet bars into one deduplicated panel."""
    paths = sorted(glob.glob(os.path.join(symbols_dir, "*.parquet")))
    if not paths:
        raise FileNotFoundError(f"no symbol bars under {symbols_dir}")
    frames = []
    for path in paths:
        frame = pd.read_parquet(path)
        if frame.empty:
            continue
        frames.append(frame)
    panel = pd.concat(frames, ignore_index=True)
    panel["date"] = pd.to_datetime(panel["date"]).dt.normalize()
    panel = (
        panel.drop_duplicates(["symbol", "date"], keep="last")
        .sort_values(["symbol", "date"])
        .reset_index(drop=True)
    )
    return panel


def trading_calendar(panel: pd.DataFrame) -> pd.DatetimeIndex:
    """Return the sorted union of all bar dates as the shared calendar."""
    return pd.DatetimeIndex(sorted(panel["date"].unique()))


def attach_pool_mask(panel: pd.DataFrame) -> pd.DataFrame:
    """Mark rows tradable in the liquid pool using trailing data through T."""
    work = panel.sort_values(["symbol", "date"]).copy()
    work["dollar_volume"] = work["close"] * work["volume"]
    grouped = work.groupby("symbol", sort=False)
    work["adv20"] = grouped["dollar_volume"].transform(
        lambda s: s.rolling(POOL_TRAIL, min_periods=POOL_TRAIL).median()
    )
    work["bar_count"] = grouped.cumcount() + 1
    work["in_pool"] = (
        (work["adv20"] >= POOL_MIN_ADV20)
        & (work["close"] >= POOL_MIN_CLOSE)
        & (work["bar_count"] >= POOL_TRAIL)
        & work["open"].gt(0)
        & work["close"].gt(0)
    )
    return work

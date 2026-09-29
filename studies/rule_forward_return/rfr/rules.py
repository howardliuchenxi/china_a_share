"""Rule registry and factor construction (classic eight-family battery).

Every factor is computed per symbol on bars sorted by date and is knowable
at the signal close T (rolling windows include T; volume ratios use the
strictly prior window). Cross-sectional rules rank within the day's pool.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import numpy as np
import pandas as pd

VOL_RATIO_THRESHOLD = 2.0
GAP_UP = 0.02
GAP_DOWN = -0.02
HIGH52W_MAX_DIST = 0.05  # within 5% of the 252-day highest close
RSI_LOW = 30.0
RSI_HIGH = 70.0
CROSS_POOL_MIN = 50  # cross-sectional rules need a wide enough pool
QUANTILE_CUT = 0.10


@dataclass(frozen=True)
class RuleSpec:
    name: str
    label: str
    kind: str  # "bool" | "quantile"
    column: str  # bool column, or factor column for quantile rules
    side: str = "top"  # quantile side: "top" | "bottom"


RULE_REGISTRY: List[RuleSpec] = [
    RuleSpec("mom20_top10", "20日动量·池内前10%", "quantile", "ret_20d", "top"),
    RuleSpec("mom60_top10", "60日动量·池内前10%", "quantile", "ret_60d", "top"),
    RuleSpec("mom252_top10", "252日动量·池内前10%", "quantile", "ret_252d", "top"),
    RuleSpec("rev5_bot10", "5日反转·池内后10%买入", "quantile", "ret_5d", "bottom"),
    RuleSpec("volspike2", "量比≥2（当日量/前20日均量）", "bool", "bool_volspike2"),
    RuleSpec("gapup2", "开盘高开≥2%", "bool", "bool_gapup2"),
    RuleSpec("gapdown2", "开盘低开≤-2%", "bool", "bool_gapdown2"),
    RuleSpec("high52w", "收盘距252日最高收盘≤5%", "bool", "bool_high52w"),
    RuleSpec("rsi14_low", "RSI(14)≤30", "bool", "bool_rsi14_low"),
    RuleSpec("rsi14_high", "RSI(14)≥70", "bool", "bool_rsi14_high"),
    RuleSpec("golden_cross", "50日均线上穿200日均线", "bool", "bool_golden_cross"),
]


def _rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.where(avg_loss.ne(0.0), 100.0)  # only gains -> overbought
    # A perfectly flat window is undefined momentum, not overbought: 50.
    return rsi.where(~(avg_gain.eq(0.0) & avg_loss.eq(0.0)), 50.0)


def attach_factors(panel: pd.DataFrame) -> pd.DataFrame:
    """Add every factor and boolean rule column, knowable at each row's close."""
    work = panel.sort_values(["symbol", "date"]).copy()
    grouped = work.groupby("symbol", sort=False)
    close = work["close"]
    open_ = work["open"]

    for window in (1, 5, 20, 60, 252):
        work[f"ret_{window}d"] = grouped["close"].transform(
            lambda s, w=window: s / s.shift(w) - 1.0
        )

    prior_vol20 = grouped["volume"].transform(
        lambda s: s.rolling(20, min_periods=20).mean().shift(1)
    )
    work["vol_ratio"] = work["volume"] / prior_vol20
    work["open_gap"] = open_ / grouped["close"].transform(lambda s: s.shift(1)) - 1.0

    high252 = grouped["close"].transform(
        lambda s: s.rolling(252, min_periods=252).max()
    )
    work["dist_high252"] = close / high252 - 1.0
    work["rsi14"] = grouped["close"].transform(_rsi_wilder)

    ma50 = grouped["close"].transform(lambda s: s.rolling(50, min_periods=50).mean())
    ma200 = grouped["close"].transform(lambda s: s.rolling(200, min_periods=200).mean())
    ma50_prev = grouped["close"].transform(
        lambda s: s.rolling(50, min_periods=50).mean().shift(1)
    )
    ma200_prev = grouped["close"].transform(
        lambda s: s.rolling(200, min_periods=200).mean().shift(1)
    )

    work["bool_volspike2"] = work["vol_ratio"] >= VOL_RATIO_THRESHOLD
    work["bool_gapup2"] = work["open_gap"] >= GAP_UP
    work["bool_gapdown2"] = work["open_gap"] <= GAP_DOWN
    work["bool_high52w"] = work["dist_high252"] >= -HIGH52W_MAX_DIST
    work["bool_rsi14_low"] = work["rsi14"] <= RSI_LOW
    work["bool_rsi14_high"] = work["rsi14"] >= RSI_HIGH
    work["bool_golden_cross"] = (ma50 > ma200) & (ma50_prev <= ma200_prev)
    return work


def evaluate_rule(spec: RuleSpec, factors: pd.DataFrame) -> pd.Series:
    """Return the boolean event mask for one rule on one signal date.

    `factors` must contain only that date's rows (pool and non-pool), with
    columns in_pool plus the rule's factor/bool column.
    """
    day = factors
    pool = day[day["in_pool"]]
    if spec.kind == "bool":
        base = day[spec.column].fillna(False)
        return base & day["in_pool"]
    if len(pool) < CROSS_POOL_MIN or pool[spec.column].notna().sum() < CROSS_POOL_MIN:
        return pd.Series(False, index=day.index)
    ranks = pool[spec.column].rank(pct=True, method="average")
    if spec.side == "top":
        picked = ranks >= 1.0 - QUANTILE_CUT
    else:
        picked = ranks <= QUANTILE_CUT
    out = pd.Series(False, index=day.index)
    out.loc[picked[picked].index] = True
    return out


def rule_labels() -> Dict[str, str]:
    return {spec.name: spec.label for spec in RULE_REGISTRY}

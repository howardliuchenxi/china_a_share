"""Summary statistics: IS/OOS split, monthly-block bootstrap, verdicts."""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from rfr.returns import ROUND_TRIP_COST

IS_FRACTION = 0.7
BOOTSTRAP_DRAWS = 1000
BOOTSTRAP_SEED = 20260928


def split_is_oos(events: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Split events by signal date order: first 70% IS, last 30% OOS."""
    if events.empty:
        return {"is": events, "oos": events}
    dates = np.sort(events["date"].unique())
    cut = int(len(dates) * IS_FRACTION)
    is_dates = set(dates[:cut])
    mask = events["date"].isin(is_dates)
    return {"is": events[mask], "oos": events[~mask]}


def month_block_bootstrap_ci(
    excess: pd.Series,
    months: pd.Series,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
) -> Optional[float]:
    """Low end of the 95% CI for the mean excess via resampling signal months."""
    frame = pd.DataFrame({"excess": excess.to_numpy(), "month": months.to_numpy()})
    frame = frame.dropna()
    groups = [g["excess"].to_numpy() for _, g in frame.groupby("month") if len(g)]
    if not groups or len(groups) < 2:
        return None
    rng = np.random.RandomState(seed)
    sizes = np.array([len(g) for g in groups])
    total = sizes.sum()
    flat = np.concatenate(groups)
    offsets = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    means = np.empty(draws)
    for i in range(draws):
        pick = rng.randint(0, len(groups), size=len(groups))
        mask = np.zeros(total, dtype=bool)
        for j in pick:
            mask[offsets[j] : offsets[j] + sizes[j]] = True
        means[i] = flat[mask].mean()
    return float(np.percentile(means, 2.5))


def summarize(events: pd.DataFrame) -> Dict[str, float]:
    """Headline stats for one rule x N event table."""
    if events.empty:
        return {
            "n": 0,
            "mean_ret": float("nan"),
            "median_ret": float("nan"),
            "win_rate": float("nan"),
            "mean_excess": float("nan"),
            "net_excess": float("nan"),
        }
    ret = events["ret"].to_numpy()
    mean_excess = float(np.mean(events["excess"].to_numpy()))
    return {
        "n": int(len(events)),
        "mean_ret": float(np.mean(ret)),
        "median_ret": float(np.median(ret)),
        "win_rate": float(np.mean(ret > 0.0)),
        "mean_excess": mean_excess,
        "net_excess": mean_excess - ROUND_TRIP_COST,
    }


def summarize_with_split(events: pd.DataFrame) -> Dict[str, float]:
    """Summary plus IS/OOS decomposition and the bootstrap CI lower bound."""
    base = summarize(events)
    if events.empty:
        return {
            **base,
            "is_excess": float("nan"),
            "oos_excess": float("nan"),
            "ci_low": float("nan"),
        }
    parts = split_is_oos(events)
    months = events["date"].dt.strftime("%Y-%m")
    ci_low = month_block_bootstrap_ci(events["excess"], months)
    return {
        **base,
        "is_excess": float(parts["is"]["excess"].mean()) if len(parts["is"]) else float("nan"),
        "oos_excess": float(parts["oos"]["excess"].mean()) if len(parts["oos"]) else float("nan"),
        "ci_low": ci_low if ci_low is not None else float("nan"),
    }


def verdict(row: Dict[str, float], null95: float) -> str:
    """Compact verdict: robust / direction-only / none, against the null line."""
    if row.get("n", 0) == 0:
        return "无事件"
    if np.isnan(row.get("oos_excess", float("nan"))):
        return "样本不足"
    checks = []
    checks.append(row.get("ci_low", float("nan")) > 0.0)
    checks.append(row.get("oos_excess", float("nan")) > 0.0)
    if np.isfinite(null95):
        checks.append(row.get("oos_excess", float("nan")) > null95)
    if all(checks):
        return "过随机关"
    if row.get("ci_low", float("nan")) > 0.0 and row.get("oos_excess", float("nan")) > 0.0:
        return "方向稳健·未过随机关"
    return "未通过"

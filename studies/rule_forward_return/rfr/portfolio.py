"""Executable overlapping-cohort portfolios for the validated N=5 rules."""
from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from rfr.returns import ROUND_TRIP_COST

PORTFOLIO_HOLD_DAYS = 5
PORTFOLIO_RULES = (
    "gapdown2",
    "mom20_top10",
    "rev5_bot10",
    "volspike2",
)
MIN_STABLE_MONTHS = 16
REQUIRED_MONTHS = 24
BOOTSTRAP_DRAWS = 1000
BOOTSTRAP_SEED = 20260928


def _pivot(
    factors: pd.DataFrame, column: str, calendar: pd.DatetimeIndex
) -> pd.DataFrame:
    wide = factors.pivot(index="date", columns="symbol", values=column)
    return wide.reindex(calendar)


def _open_position(
    cash: float,
    symbols: pd.Index,
    entry_prices: pd.Series,
    exit_pos: int,
    entry_cost: float,
) -> Dict[str, object]:
    valid_prices = entry_prices.reindex(symbols).dropna()
    valid_prices = valid_prices[valid_prices > 0.0]
    if valid_prices.empty:
        return {}
    invested = cash * (1.0 - entry_cost)
    per_symbol = invested / len(valid_prices)
    shares = per_symbol / valid_prices
    return {
        "shares": shares,
        "exit_pos": exit_pos,
        "entry_prices": valid_prices,
        "cash_before": cash,
        "per_symbol": per_symbol,
    }


def _close_value(
    state: Dict[str, object],
    closes: pd.DataFrame,
    pos: int,
    exit_cost: float,
    allow_stale_mark: bool = False,
) -> Tuple[float, bool]:
    shares = state["shares"]
    prices = closes.iloc[pos].reindex(shares.index)
    invalid = prices.isna() | (prices <= 0.0)
    if invalid.any() and allow_stale_mark:
        # The benchmark keeps the last observable mark during a temporary
        # missing bar. Terminal-missing names are removed separately to match
        # the event-level pool baseline, which drops unavailable exit returns.
        prices = prices.where(~invalid, state["last_prices"])
        invalid = prices.isna() | (prices <= 0.0)
    if invalid.any():
        missing = prices[prices.isna() | (prices <= 0.0)].index.tolist()
        raise ValueError(f"missing close while position is open: {missing[:5]}")
    state["last_prices"] = prices
    value = float((shares * prices).sum())
    is_exit = pos == state["exit_pos"]
    if is_exit:
        value *= 1.0 - exit_cost
    return value, is_exit


def run_overlapping_portfolio(
    factors: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    spec,
    evaluate_fn,
    hold_days: int = PORTFOLIO_HOLD_DAYS,
    round_trip_cost: float = ROUND_TRIP_COST,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Run five rotating sleeves and return daily NAV plus position audit rows.

    Each signal-date cohort receives one ``1 / hold_days`` sleeve. The sleeve
    buys all valid signals at the next open, holds fixed shares for five
    closes, and exits at the fifth close. A matching gross benchmark sleeve
    buys the signal-date liquid pool only when the strategy has a cohort.
    """
    if hold_days <= 0:
        raise ValueError("hold_days must be positive")
    if round_trip_cost < 0.0 or round_trip_cost >= 1.0:
        raise ValueError("round_trip_cost must be in [0, 1)")

    opens = _pivot(factors, "open", calendar)
    closes = _pivot(factors, "close", calendar)
    by_date = {ts: frame for ts, frame in factors.groupby("date")}
    entry_cost = round_trip_cost / 2.0
    exit_cost = round_trip_cost / 2.0

    sleeve_capital = 1.0 / hold_days
    strategy_cash = [sleeve_capital] * hold_days
    benchmark_cash = [sleeve_capital] * hold_days
    strategy_states: List[Dict[str, object]] = [{} for _ in range(hold_days)]
    benchmark_states: List[Dict[str, object]] = [{} for _ in range(hold_days)]

    daily_rows: List[dict] = []
    position_rows: List[dict] = []
    for pos, ts in enumerate(calendar):
        entries = 0
        signal_count = 0
        skipped_missing_exit = 0
        if pos > 0 and pos + hold_days - 1 < len(calendar):
            signal_ts = calendar[pos - 1]
            signal_day = by_date[signal_ts]
            mask = evaluate_fn(spec, signal_day)
            selected = signal_day.loc[mask]
            if not selected.empty:
                sleeve = pos % hold_days
                if strategy_states[sleeve] or benchmark_states[sleeve]:
                    raise RuntimeError("rotating sleeve was not free at entry")

                selected_symbols = pd.Index(selected["symbol"])
                selected_exit_prices = closes.iloc[
                    pos + hold_days - 1
                ].reindex(selected_symbols)
                valid_exit = selected_exit_prices.notna() & selected_exit_prices.gt(0.0)
                skipped_missing_exit = int((~valid_exit).sum())
                selected_symbols = selected_symbols[valid_exit]
                selected = selected[selected["symbol"].isin(selected_symbols)]
                strategy_state = _open_position(
                    strategy_cash[sleeve],
                    selected_symbols,
                    opens.iloc[pos],
                    pos + hold_days - 1,
                    entry_cost,
                )
                if strategy_state:
                    strategy_state["last_prices"] = strategy_state[
                        "entry_prices"
                    ]
                    pool_symbols = pd.Index(
                        signal_day.loc[signal_day["in_pool"], "symbol"]
                    )
                    pool_exit_prices = closes.iloc[pos + hold_days - 1].reindex(
                        pool_symbols
                    )
                    pool_symbols = pool_symbols[
                        pool_exit_prices.notna() & pool_exit_prices.gt(0.0)
                    ]
                    benchmark_state = _open_position(
                        benchmark_cash[sleeve],
                        pool_symbols,
                        opens.iloc[pos],
                        pos + hold_days - 1,
                        0.0,
                    )
                    if not benchmark_state:
                        raise ValueError(f"empty benchmark cohort on {signal_ts.date()}")
                    benchmark_state["last_prices"] = benchmark_state[
                        "entry_prices"
                    ]

                    strategy_states[sleeve] = strategy_state
                    benchmark_states[sleeve] = benchmark_state
                    entries = 1
                    signal_count = len(strategy_state["shares"])

                    pool_exit = closes.iloc[pos + hold_days - 1].reindex(
                        benchmark_state["shares"].index
                    )
                    if pool_exit.isna().any() or (pool_exit <= 0.0).any():
                        raise ValueError(
                            f"missing benchmark exit close on {signal_ts.date()}"
                        )
                    benchmark_return = float(
                        (pool_exit / benchmark_state["entry_prices"]).mean() - 1.0
                    )
                    if spec.kind == "quantile":
                        percentiles = (
                            signal_day.loc[signal_day["in_pool"], spec.column]
                            .rank(pct=True, method="average")
                        )
                        percentile_by_symbol = pd.Series(
                            percentiles.to_numpy(),
                            index=signal_day.loc[percentiles.index, "symbol"],
                        )
                    else:
                        percentile_by_symbol = pd.Series(
                            np.nan, index=signal_day["symbol"]
                        )

                    total_nav_before = (
                        daily_rows[-1]["strategy_nav"] if daily_rows else 1.0
                    )
                    exit_prices = closes.iloc[pos + hold_days - 1].reindex(
                        strategy_state["shares"].index
                    )
                    if exit_prices.isna().any() or (exit_prices <= 0.0).any():
                        raise ValueError(
                            f"missing strategy exit close on {signal_ts.date()}"
                        )
                    selected_by_symbol = selected.set_index("symbol")
                    for symbol, entry_price in strategy_state["entry_prices"].items():
                        source = selected_by_symbol.loc[symbol]
                        exit_price = float(exit_prices.loc[symbol])
                        gross_return = exit_price / float(entry_price) - 1.0
                        net_return = (
                            (1.0 - entry_cost)
                            * (1.0 + gross_return)
                            * (1.0 - exit_cost)
                            - 1.0
                        )
                        position_rows.append(
                            {
                                "rule": spec.name,
                                "signal_date": signal_ts,
                                "entry_date": ts,
                                "exit_date": calendar[pos + hold_days - 1],
                                "symbol": symbol,
                                "signal_close": float(source["close"]),
                                "rule_value": float(source[spec.column]),
                                "rule_percentile": float(
                                    percentile_by_symbol.loc[symbol]
                                ),
                                "open_gap": float(source["open_gap"]),
                                "ret_5d": float(source["ret_5d"]),
                                "ret_20d": float(source["ret_20d"]),
                                "vol_ratio": float(source["vol_ratio"]),
                                "adv20": float(source["adv20"]),
                                "pool_size": int(signal_day["in_pool"].sum()),
                                "cohort_size": signal_count,
                                "entry_price": float(entry_price),
                                "exit_price": exit_price,
                                "gross_return": gross_return,
                                "net_return": net_return,
                                "benchmark_return": benchmark_return,
                                "excess_return": gross_return - benchmark_return,
                                "entry_weight": float(
                                    strategy_state["per_symbol"] / total_nav_before
                                ),
                            }
                        )

        strategy_nav = 0.0
        benchmark_nav = 0.0
        active_positions = 0
        for sleeve in range(hold_days):
            strategy_state = strategy_states[sleeve]
            if strategy_state:
                value, is_exit = _close_value(
                    strategy_state, closes, pos, exit_cost, allow_stale_mark=True
                )
                strategy_nav += value
                active_positions += len(strategy_state["shares"])
                if is_exit:
                    strategy_cash[sleeve] = value
                    strategy_states[sleeve] = {}
            else:
                strategy_nav += strategy_cash[sleeve]

            benchmark_state = benchmark_states[sleeve]
            if benchmark_state:
                value, is_exit = _close_value(
                    benchmark_state, closes, pos, 0.0, allow_stale_mark=True
                )
                benchmark_nav += value
                if is_exit:
                    benchmark_cash[sleeve] = value
                    benchmark_states[sleeve] = {}
            else:
                benchmark_nav += benchmark_cash[sleeve]

        daily_rows.append(
            {
                "rule": spec.name,
                "date": ts,
                "strategy_nav": strategy_nav,
                "benchmark_nav": benchmark_nav,
                "entries": entries,
                "signal_count": signal_count,
                "skipped_missing_exit": skipped_missing_exit,
                "active_positions": active_positions,
            }
        )

    daily = pd.DataFrame(daily_rows)
    daily["net_return"] = daily["strategy_nav"].pct_change().fillna(0.0)
    daily["benchmark_return"] = daily["benchmark_nav"].pct_change().fillna(0.0)
    daily["daily_excess"] = daily["net_return"] - daily["benchmark_return"]
    positions = pd.DataFrame(position_rows)
    return daily, positions


def monthly_portfolio_returns(daily: pd.DataFrame) -> pd.DataFrame:
    """Compound daily portfolio returns into the active monthly curve."""
    entry_dates = daily.loc[daily["entries"] > 0, "date"]
    if entry_dates.empty:
        return pd.DataFrame(
            columns=[
                "rule",
                "month",
                "net_return",
                "benchmark_return",
                "excess_return",
                "strategy_nav",
                "benchmark_nav",
            ]
        )
    active = daily[daily["date"] >= entry_dates.min()].copy()
    active["month"] = active["date"].dt.to_period("M").astype(str)
    rows = []
    for month, frame in active.groupby("month", sort=True):
        net_return = float((1.0 + frame["net_return"]).prod() - 1.0)
        benchmark_return = float((1.0 + frame["benchmark_return"]).prod() - 1.0)
        rows.append(
            {
                "rule": str(frame["rule"].iloc[0]),
                "month": month,
                "net_return": net_return,
                "benchmark_return": benchmark_return,
                "excess_return": net_return - benchmark_return,
                "strategy_nav": float(frame["strategy_nav"].iloc[-1]),
                "benchmark_nav": float(frame["benchmark_nav"].iloc[-1]),
            }
        )
    return pd.DataFrame(rows)


def _bootstrap_ci_low(values: pd.Series) -> float:
    clean = values.dropna().to_numpy(dtype=float)
    if len(clean) < 2:
        return float("nan")
    rng = np.random.RandomState(BOOTSTRAP_SEED)
    picks = rng.randint(0, len(clean), size=(BOOTSTRAP_DRAWS, len(clean)))
    means = clean[picks].mean(axis=1)
    return float(np.percentile(means, 2.5))


def summarize_portfolio(
    daily: pd.DataFrame, monthly: pd.DataFrame
) -> Dict[str, object]:
    """Summarize one executable portfolio and apply the monthly stability gate."""
    if monthly.empty:
        return {
            "rule": str(daily["rule"].iloc[0]) if len(daily) else "",
            "months": 0,
            "total_net_return": float("nan"),
            "total_benchmark_return": float("nan"),
            "total_excess_return": float("nan"),
            "max_drawdown": float("nan"),
            "monthly_win_rate": float("nan"),
            "positive_excess_months": 0,
            "ci_low": float("nan"),
            "verdict": "样本不足",
        }
    start_date = pd.Period(monthly["month"].iloc[0], freq="M").start_time
    active_daily = daily[daily["date"] >= start_date]
    nav = active_daily["strategy_nav"].to_numpy(dtype=float)
    peaks = np.maximum.accumulate(np.concatenate([[1.0], nav]))[1:]
    drawdown = nav / peaks - 1.0
    positive_months = int((monthly["excess_return"] > 0.0).sum())
    ci_low = _bootstrap_ci_low(monthly["excess_return"])
    months = int(len(monthly))
    passed = (
        np.isfinite(ci_low)
        and ci_low > 0.0
        and months >= REQUIRED_MONTHS
        and positive_months >= MIN_STABLE_MONTHS
    )
    return {
        "rule": str(monthly["rule"].iloc[0]),
        "months": months,
        "total_net_return": float(monthly["strategy_nav"].iloc[-1] - 1.0),
        "total_benchmark_return": float(monthly["benchmark_nav"].iloc[-1] - 1.0),
        "total_excess_return": float(
            monthly["strategy_nav"].iloc[-1]
            - monthly["benchmark_nav"].iloc[-1]
        ),
        "max_drawdown": float(drawdown.min()),
        "monthly_win_rate": float((monthly["net_return"] > 0.0).mean()),
        "positive_excess_months": positive_months,
        "ci_low": ci_low,
        "verdict": "通过" if passed else "未通过",
    }


def evaluate_portfolios(
    factors: pd.DataFrame, calendar: pd.DatetimeIndex, specs, evaluate_fn
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Evaluate the four validated rules with the same portfolio mechanics."""
    spec_by_name = {spec.name: spec for spec in specs}
    daily_frames = []
    monthly_frames = []
    position_frames = []
    summaries = []
    for rule_name in PORTFOLIO_RULES:
        if rule_name not in spec_by_name:
            raise KeyError(f"portfolio rule is not registered: {rule_name}")
        daily, positions = run_overlapping_portfolio(
            factors, calendar, spec_by_name[rule_name], evaluate_fn
        )
        monthly = monthly_portfolio_returns(daily)
        daily_frames.append(daily)
        monthly_frames.append(monthly)
        position_frames.append(positions)
        summaries.append(summarize_portfolio(daily, monthly))
    return (
        pd.concat(daily_frames, ignore_index=True),
        pd.concat(monthly_frames, ignore_index=True),
        pd.concat(position_frames, ignore_index=True),
        pd.DataFrame(summaries),
    )

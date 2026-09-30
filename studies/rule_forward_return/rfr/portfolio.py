"""Executable overlapping-cohort portfolios for the validated N=5 rules."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

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
    allocations: Optional[pd.Series] = None,
) -> Dict[str, object]:
    valid_prices = entry_prices.reindex(symbols).dropna()
    valid_prices = valid_prices[valid_prices > 0.0]
    if valid_prices.empty:
        return {}
    if allocations is None:
        invested = cash * (1.0 - entry_cost)
        allocation_values = pd.Series(
            invested / len(valid_prices), index=valid_prices.index
        )
        idle_cash = 0.0
    else:
        allocation_values = allocations.reindex(valid_prices.index).fillna(0.0)
        allocation_values = allocation_values[allocation_values > 0.0]
        valid_prices = valid_prices.reindex(allocation_values.index)
        if allocation_values.empty:
            return {}
        invested = float(allocation_values.sum())
        idle_cash = cash - invested * (1.0 + entry_cost)
        if idle_cash < -1e-12:
            raise ValueError("allocations exceed sleeve cash after entry cost")
        idle_cash = max(idle_cash, 0.0)
    shares = allocation_values / valid_prices
    return {
        "shares": shares,
        "exit_pos": exit_pos,
        "entry_prices": valid_prices,
        "cash_before": cash,
        "allocations": allocation_values,
        "idle_cash": idle_cash,
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
    security_value = float((shares * prices).sum())
    is_exit = pos == state["exit_pos"]
    if is_exit:
        security_value *= 1.0 - exit_cost
    value = security_value + float(state["idle_cash"])
    return value, is_exit


def _open_exposure(
    states: List[Dict[str, object]],
    cash: List[float],
    opens: pd.DataFrame,
    pos: int,
) -> Tuple[float, pd.Series]:
    """Mark current holdings at the entry open for aggregate cap checks."""
    total_nav = 0.0
    exposures: Dict[str, float] = {}
    for sleeve, state in enumerate(states):
        if not state:
            total_nav += cash[sleeve]
            continue
        prices = opens.iloc[pos].reindex(state["shares"].index)
        invalid = prices.isna() | (prices <= 0.0)
        prices = prices.where(~invalid, state["last_prices"])
        if prices.isna().any() or (prices <= 0.0).any():
            missing = prices[prices.isna() | (prices <= 0.0)].index.tolist()
            raise ValueError(f"missing open while applying position cap: {missing[:5]}")
        values = state["shares"] * prices
        total_nav += float(values.sum()) + float(state["idle_cash"])
        for symbol, value in values.items():
            exposures[symbol] = exposures.get(symbol, 0.0) + float(value)
    return total_nav, pd.Series(exposures, dtype=float)


def _capped_allocations(
    symbols: pd.Index,
    cash: float,
    entry_cost: float,
    total_nav: float,
    current_exposure: pd.Series,
    max_position_weight: float,
) -> Optional[pd.Series]:
    """Equal-weight a new cohort subject to aggregate single-name headroom."""
    base_invested = cash * (1.0 - entry_cost)
    base_equal = base_invested / len(symbols)
    cap_value = max_position_weight * total_nav
    existing = current_exposure.reindex(symbols).fillna(0.0)
    rooms = (cap_value - existing).clip(lower=0.0)
    if (base_equal <= rooms + 1e-15).all():
        return None

    remaining = min(cash / (1.0 + entry_cost), float(rooms.sum()))
    allocations = pd.Series(0.0, index=symbols)
    active = list(symbols[rooms > 0.0])
    while active and remaining > 1e-15:
        equal = remaining / len(active)
        newly_capped = [symbol for symbol in active if rooms.loc[symbol] <= equal]
        if not newly_capped:
            allocations.loc[active] = equal
            remaining = 0.0
            break
        for symbol in newly_capped:
            value = float(rooms.loc[symbol])
            allocations.loc[symbol] = value
            remaining -= value
            active.remove(symbol)
    return allocations


def run_overlapping_portfolio(
    factors: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    spec,
    evaluate_fn,
    hold_days: int = PORTFOLIO_HOLD_DAYS,
    round_trip_cost: float = ROUND_TRIP_COST,
    max_abs_open_gap: Optional[float] = None,
    max_position_weight: Optional[float] = None,
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
    if max_abs_open_gap is not None and max_abs_open_gap <= 0.0:
        raise ValueError("max_abs_open_gap must be positive")
    if max_position_weight is not None and not 0.0 < max_position_weight <= 1.0:
        raise ValueError("max_position_weight must be in (0, 1]")

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
        raw_signal_count = 0
        filtered_signals = 0
        capped_out_signals = 0
        skipped_missing_entry = 0
        skipped_missing_exit = 0
        if pos > 0 and pos + hold_days - 1 < len(calendar):
            signal_ts = calendar[pos - 1]
            signal_day = by_date[signal_ts]
            mask = evaluate_fn(spec, signal_day)
            selected = signal_day.loc[mask]
            raw_signal_count = len(selected)
            if max_abs_open_gap is not None:
                keep = selected["open_gap"].abs() <= max_abs_open_gap
                filtered_signals = int((~keep).sum())
                selected = selected.loc[keep]
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
                selected_entry_prices = opens.iloc[pos].reindex(selected_symbols)
                valid_entry = selected_entry_prices.notna() & selected_entry_prices.gt(0.0)
                skipped_missing_entry = int((~valid_entry).sum())
                selected_symbols = selected_symbols[valid_entry]
                selected = selected[selected["symbol"].isin(selected_symbols)]
                if selected.empty:
                    selected_symbols = pd.Index([], dtype=object)
                allocations = None
                total_nav_open = float("nan")
                exposure_before = pd.Series(dtype=float)
                if max_position_weight is not None and len(selected_symbols):
                    total_nav_open, exposure_before = _open_exposure(
                        strategy_states, strategy_cash, opens, pos
                    )
                    allocations = _capped_allocations(
                        selected_symbols,
                        strategy_cash[sleeve],
                        entry_cost,
                        total_nav_open,
                        exposure_before,
                        max_position_weight,
                    )
                strategy_state = _open_position(
                    strategy_cash[sleeve],
                    selected_symbols,
                    opens.iloc[pos],
                    pos + hold_days - 1,
                    entry_cost,
                    allocations=allocations,
                )
                if not strategy_state and max_position_weight is not None:
                    capped_out_signals = len(selected_symbols)
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
                    if max_position_weight is not None:
                        capped_out_signals = len(selected_symbols) - signal_count

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
                                    strategy_state["allocations"].loc[symbol]
                                    / total_nav_before
                                ),
                                "aggregate_weight_after_entry": (
                                    float("nan")
                                    if max_position_weight is None
                                    else float(
                                        (
                                            exposure_before.get(symbol, 0.0)
                                            + strategy_state["allocations"].loc[symbol]
                                        )
                                        / total_nav_open
                                    )
                                ),
                            }
                        )

        strategy_nav = 0.0
        benchmark_nav = 0.0
        active_positions = 0
        cap_idle_cash = 0.0
        for sleeve in range(hold_days):
            strategy_state = strategy_states[sleeve]
            if strategy_state:
                value, is_exit = _close_value(
                    strategy_state, closes, pos, exit_cost, allow_stale_mark=True
                )
                strategy_nav += value
                active_positions += len(strategy_state["shares"])
                cap_idle_cash += float(strategy_state["idle_cash"])
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
                "raw_signal_count": raw_signal_count,
                "filtered_signals": filtered_signals,
                "capped_out_signals": capped_out_signals,
                "skipped_missing_entry": skipped_missing_entry,
                "skipped_missing_exit": skipped_missing_exit,
                "active_positions": active_positions,
                "cap_idle_cash": cap_idle_cash,
            }
        )

    daily = pd.DataFrame(daily_rows)
    daily["net_return"] = daily["strategy_nav"].pct_change().fillna(0.0)
    daily["benchmark_return"] = daily["benchmark_nav"].pct_change().fillna(0.0)
    daily["daily_excess"] = daily["net_return"] - daily["benchmark_return"]
    daily["cap_idle_weight"] = daily["cap_idle_cash"] / daily["strategy_nav"]
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

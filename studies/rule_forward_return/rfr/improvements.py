"""Pre-registered improvements built on the validated N=5 rule portfolios."""
from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from rfr.portfolio import (
    PORTFOLIO_HOLD_DAYS,
    PORTFOLIO_RULES,
    _close_value,
    _open_exposure,
    _open_position,
    _pivot,
    monthly_portfolio_returns,
    run_overlapping_portfolio,
    summarize_portfolio,
)
from rfr.returns import ROUND_TRIP_COST, collect_events, null_runs
from rfr.rules import CROSS_POOL_MIN, QUANTILE_CUT, RuleSpec
from rfr.stats import summarize_with_split, verdict

META_REBALANCE_COST = 0.0005
RISK_LOOKBACK_DAYS = 60
MIN_RISK_OBSERVATIONS = 20
ENSEMBLE_CAP = 0.02

COMPOSITE_SPECS = (
    RuleSpec(
        "mom20_rev5",
        "20日动量前10%且5日反转后10%",
        "bool",
        "bool_mom20_rev5",
    ),
    RuleSpec(
        "mom20_gapdown2",
        "20日动量前10%且低开≤-2%",
        "bool",
        "bool_mom20_gapdown2",
    ),
    RuleSpec(
        "gapdown2_volspike2",
        "低开≤-2%且量比≥2",
        "bool",
        "bool_gapdown2_volspike2",
    ),
)

ENSEMBLE_LABELS = {
    "ensemble_equal": "四规则静态等权",
    "ensemble_inverse_vol": "四规则月度逆波动",
    "ensemble_cap2": "四规则共享2%入场单票上限",
}


def attach_improvement_factors(factors: pd.DataFrame) -> pd.DataFrame:
    """Attach only the frozen composite-rule and market-regime inputs."""
    work = factors.copy()
    pool = work[work["in_pool"]]
    valid_counts = pool.groupby("date")[["ret_20d", "ret_5d"]].count()
    ranks = pool.groupby("date")[["ret_20d", "ret_5d"]].rank(
        pct=True, method="average"
    )

    mom20 = pd.Series(False, index=work.index)
    rev5 = pd.Series(False, index=work.index)
    eligible_dates_20 = valid_counts.index[
        valid_counts["ret_20d"] >= CROSS_POOL_MIN
    ]
    eligible_dates_5 = valid_counts.index[
        valid_counts["ret_5d"] >= CROSS_POOL_MIN
    ]
    pool_dates = pool["date"]
    mom20.loc[pool.index] = (
        ranks["ret_20d"].ge(1.0 - QUANTILE_CUT)
        & pool_dates.isin(eligible_dates_20)
    )
    rev5.loc[pool.index] = (
        ranks["ret_5d"].le(QUANTILE_CUT)
        & pool_dates.isin(eligible_dates_5)
    )

    work["bool_mom20_top10_frozen"] = mom20
    work["bool_rev5_bot10_frozen"] = rev5
    work["bool_mom20_rev5"] = mom20 & rev5
    work["bool_mom20_gapdown2"] = mom20 & work["bool_gapdown2"]
    work["bool_gapdown2_volspike2"] = (
        work["bool_gapdown2"] & work["bool_volspike2"]
    )

    regime = pool.groupby("date")["ret_60d"].agg(["count", "median"])
    regime["risk_on"] = (
        regime["count"].ge(CROSS_POOL_MIN) & regime["median"].gt(0.0)
    )
    regime["classified"] = regime["count"].ge(CROSS_POOL_MIN)
    work["risk_on"] = work["date"].map(regime["risk_on"]).eq(True)
    work["regime_classified"] = work["date"].map(regime["classified"]).eq(True)
    return work


def _summary_row(
    summary: Dict[str, object],
    candidate: str,
    label: str,
    candidate_type: str,
) -> Dict[str, object]:
    return {
        **summary,
        "candidate": candidate,
        "label": label,
        "candidate_type": candidate_type,
    }


def _static_ensemble(
    daily: pd.DataFrame,
    candidate: str,
    label: str,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, object], pd.DataFrame]:
    """Combine four independently funded rule books without rebalancing."""
    subset = daily[daily["rule"].isin(PORTFOLIO_RULES)]
    strategy = subset.pivot(index="date", columns="rule", values="strategy_nav")
    benchmark = subset.pivot(index="date", columns="rule", values="benchmark_nav")
    entries = subset.pivot(index="date", columns="rule", values="entries")
    if strategy[list(PORTFOLIO_RULES)].isna().any().any():
        raise ValueError("missing rule NAV in static ensemble")

    weight = 1.0 / len(PORTFOLIO_RULES)
    combined = pd.DataFrame(
        {
            "rule": candidate,
            "date": strategy.index,
            "strategy_nav": strategy[list(PORTFOLIO_RULES)].mul(weight).sum(axis=1),
            "benchmark_nav": benchmark[list(PORTFOLIO_RULES)]
            .mul(weight)
            .sum(axis=1),
            "entries": entries[list(PORTFOLIO_RULES)].sum(axis=1),
        }
    )
    combined["net_return"] = combined["strategy_nav"].pct_change().fillna(0.0)
    combined["benchmark_return"] = (
        combined["benchmark_nav"].pct_change().fillna(0.0)
    )
    combined["daily_excess"] = (
        combined["net_return"] - combined["benchmark_return"]
    )
    monthly = monthly_portfolio_returns(combined)
    summary = _summary_row(
        summarize_portfolio(combined, monthly), candidate, label, "ensemble"
    )
    weights = pd.DataFrame(
        {
            "candidate": candidate,
            "rebalance_date": [strategy.index.min()] * len(PORTFOLIO_RULES),
            "rule": PORTFOLIO_RULES,
            "target_weight": [weight] * len(PORTFOLIO_RULES),
            "turnover": [0.0] * len(PORTFOLIO_RULES),
            "rebalance_cost": [0.0] * len(PORTFOLIO_RULES),
        }
    )
    return combined, monthly, summary, weights


def _inverse_vol_weights(history: pd.DataFrame) -> pd.Series:
    """Return lagged inverse-volatility weights or equal weights when thin."""
    equal = pd.Series(1.0 / len(PORTFOLIO_RULES), index=PORTFOLIO_RULES)
    if len(history) < MIN_RISK_OBSERVATIONS:
        return equal
    vol = history.tail(RISK_LOOKBACK_DAYS).std(ddof=1).reindex(PORTFOLIO_RULES)
    if vol.isna().any() or (vol <= 0.0).any():
        return equal
    inverse = 1.0 / vol
    return inverse / inverse.sum()


def _inverse_vol_ensemble(
    daily: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, object], pd.DataFrame]:
    """Monthly-rebalance rule books using only prior daily volatility."""
    candidate = "ensemble_inverse_vol"
    label = ENSEMBLE_LABELS[candidate]
    subset = daily[daily["rule"].isin(PORTFOLIO_RULES)]
    net_returns = subset.pivot(index="date", columns="rule", values="net_return")
    benchmark_returns = subset.pivot(
        index="date", columns="rule", values="benchmark_return"
    )
    entries = subset.pivot(index="date", columns="rule", values="entries")
    net_returns = net_returns[list(PORTFOLIO_RULES)].fillna(0.0)
    benchmark_returns = benchmark_returns[list(PORTFOLIO_RULES)].fillna(0.0)

    accounts = pd.Series(1.0 / len(PORTFOLIO_RULES), index=PORTFOLIO_RULES)
    benchmark_accounts = accounts.copy()
    prior_month = None
    daily_rows: List[dict] = []
    weight_rows: List[dict] = []
    for date in net_returns.index:
        month = date.to_period("M")
        if month != prior_month:
            history = net_returns.loc[net_returns.index < date]
            target = _inverse_vol_weights(history)
            current = accounts / accounts.sum()
            turnover = 0.0 if prior_month is None else float(
                0.5 * (current - target).abs().sum()
            )
            cost = float(accounts.sum() * turnover * META_REBALANCE_COST)
            accounts = target * (accounts.sum() - cost)
            benchmark_accounts = target * benchmark_accounts.sum()
            for rule in PORTFOLIO_RULES:
                weight_rows.append(
                    {
                        "candidate": candidate,
                        "rebalance_date": date,
                        "rule": rule,
                        "target_weight": float(target.loc[rule]),
                        "turnover": turnover,
                        "rebalance_cost": cost,
                    }
                )
            prior_month = month

        accounts = accounts * (1.0 + net_returns.loc[date])
        benchmark_accounts = benchmark_accounts * (
            1.0 + benchmark_returns.loc[date]
        )
        daily_rows.append(
            {
                "rule": candidate,
                "date": date,
                "strategy_nav": float(accounts.sum()),
                "benchmark_nav": float(benchmark_accounts.sum()),
                "entries": int(entries.loc[date].sum()),
            }
        )

    combined = pd.DataFrame(daily_rows)
    combined["net_return"] = combined["strategy_nav"].pct_change().fillna(0.0)
    combined["benchmark_return"] = (
        combined["benchmark_nav"].pct_change().fillna(0.0)
    )
    combined["daily_excess"] = (
        combined["net_return"] - combined["benchmark_return"]
    )
    monthly = monthly_portfolio_returns(combined)
    summary = _summary_row(
        summarize_portfolio(combined, monthly), candidate, label, "ensemble"
    )
    return combined, monthly, summary, pd.DataFrame(weight_rows)


def _position_row(
    spec,
    signal_day: pd.DataFrame,
    selected: pd.DataFrame,
    signal_ts: pd.Timestamp,
    entry_ts: pd.Timestamp,
    exit_ts: pd.Timestamp,
    state: Dict[str, object],
    exit_prices: pd.Series,
    benchmark_return: float,
    aggregate_weights: pd.Series,
    total_nav_open: float,
    entry_cost: float,
    exit_cost: float,
) -> List[dict]:
    selected_by_symbol = selected.set_index("symbol")
    rows = []
    for symbol, entry_price in state["entry_prices"].items():
        source = selected_by_symbol.loc[symbol]
        exit_price = float(exit_prices.loc[symbol])
        gross_return = exit_price / float(entry_price) - 1.0
        net_return = (
            (1.0 - entry_cost)
            * (1.0 + gross_return)
            * (1.0 - exit_cost)
            - 1.0
        )
        rows.append(
            {
                "candidate": "ensemble_cap2",
                "rule": spec.name,
                "signal_date": signal_ts,
                "entry_date": entry_ts,
                "exit_date": exit_ts,
                "symbol": symbol,
                "signal_close": float(source["close"]),
                "open_gap": float(source["open_gap"]),
                "ret_5d": float(source["ret_5d"]),
                "ret_20d": float(source["ret_20d"]),
                "vol_ratio": float(source["vol_ratio"]),
                "adv20": float(source["adv20"]),
                "entry_price": float(entry_price),
                "exit_price": exit_price,
                "gross_return": gross_return,
                "net_return": net_return,
                "benchmark_return": benchmark_return,
                "excess_return": gross_return - benchmark_return,
                "entry_weight": float(
                    state["allocations"].loc[symbol] / total_nav_open
                ),
                "aggregate_weight_after_entry": float(
                    aggregate_weights.loc[symbol]
                ),
            }
        )
    return rows


def _capped_ensemble(
    factors: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    specs,
    evaluate_fn,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, object], pd.DataFrame]:
    """Run twenty sleeves with a shared 2% aggregate entry exposure cap."""
    spec_by_name = {spec.name: spec for spec in specs}
    selected_specs = [spec_by_name[name] for name in PORTFOLIO_RULES]
    opens = _pivot(factors, "open", calendar)
    closes = _pivot(factors, "close", calendar)
    by_date = {date: frame for date, frame in factors.groupby("date")}
    entry_cost = ROUND_TRIP_COST / 2.0
    exit_cost = ROUND_TRIP_COST / 2.0
    sleeve_capital = 1.0 / (len(PORTFOLIO_RULES) * PORTFOLIO_HOLD_DAYS)
    keys = [
        (rule, sleeve)
        for rule in PORTFOLIO_RULES
        for sleeve in range(PORTFOLIO_HOLD_DAYS)
    ]
    cash = {key: sleeve_capital for key in keys}
    benchmark_cash = {key: sleeve_capital for key in keys}
    states: Dict[tuple, Dict[str, object]] = {key: {} for key in keys}
    benchmark_states: Dict[tuple, Dict[str, object]] = {key: {} for key in keys}

    daily_rows: List[dict] = []
    position_rows: List[dict] = []
    for pos, date in enumerate(calendar):
        candidates = {}
        raw_signals = 0
        missing_entries = 0
        missing_exits = 0
        if pos > 0 and pos + PORTFOLIO_HOLD_DAYS - 1 < len(calendar):
            signal_ts = calendar[pos - 1]
            signal_day = by_date[signal_ts]
            for spec in selected_specs:
                selected = signal_day.loc[evaluate_fn(spec, signal_day)].copy()
                raw_signals += len(selected)
                if selected.empty:
                    continue
                symbols = pd.Index(selected["symbol"])
                exit_prices = closes.iloc[
                    pos + PORTFOLIO_HOLD_DAYS - 1
                ].reindex(symbols)
                valid_exit = exit_prices.notna() & exit_prices.gt(0.0)
                missing_exits += int((~valid_exit).sum())
                symbols = symbols[valid_exit]
                entry_prices = opens.iloc[pos].reindex(symbols)
                valid_entry = entry_prices.notna() & entry_prices.gt(0.0)
                missing_entries += int((~valid_entry).sum())
                symbols = symbols[valid_entry]
                if not len(symbols):
                    continue
                selected = selected[selected["symbol"].isin(symbols)]
                key = (spec.name, pos % PORTFOLIO_HOLD_DAYS)
                if states[key] or benchmark_states[key]:
                    raise RuntimeError("ensemble rotating sleeve was not free at entry")
                candidates[key] = (spec, selected, symbols)

            if candidates:
                state_list = [states[key] for key in keys]
                cash_list = [cash[key] for key in keys]
                total_nav_open, exposure_before = _open_exposure(
                    state_list, cash_list, opens, pos
                )
                desired_by_key = {}
                desired_total: Dict[str, float] = {}
                for key, (_, _, symbols) in candidates.items():
                    invested = cash[key] * (1.0 - entry_cost)
                    desired = pd.Series(invested / len(symbols), index=symbols)
                    desired_by_key[key] = desired
                    for symbol, value in desired.items():
                        desired_total[symbol] = (
                            desired_total.get(symbol, 0.0) + float(value)
                        )

                scale = {}
                for symbol, desired in desired_total.items():
                    headroom = max(
                        ENSEMBLE_CAP * total_nav_open
                        - float(exposure_before.get(symbol, 0.0)),
                        0.0,
                    )
                    scale[symbol] = min(1.0, headroom / desired) if desired else 0.0

                aggregate_allocations = pd.Series(
                    {
                        symbol: desired * scale[symbol]
                        for symbol, desired in desired_total.items()
                    },
                    dtype=float,
                )
                aggregate_weights = (
                    exposure_before.add(aggregate_allocations, fill_value=0.0)
                    / total_nav_open
                )
                exposure_before_weights = exposure_before / total_nav_open
                allowed_weights = exposure_before_weights.reindex(
                    aggregate_weights.index
                ).fillna(ENSEMBLE_CAP).clip(lower=ENSEMBLE_CAP)
                # Existing holdings may drift above the entry cap as prices move.
                # New cohorts cannot add to those names or push other names above it.
                if (aggregate_weights > allowed_weights + 1e-12).any():
                    raise AssertionError("shared ensemble entry cap was breached")

                for key, (spec, selected, symbols) in candidates.items():
                    allocations = desired_by_key[key] * pd.Series(scale).reindex(
                        desired_by_key[key].index
                    )
                    state = _open_position(
                        cash[key],
                        symbols,
                        opens.iloc[pos],
                        pos + PORTFOLIO_HOLD_DAYS - 1,
                        entry_cost,
                        allocations=allocations,
                    )
                    if not state:
                        continue
                    state["last_prices"] = state["entry_prices"]
                    states[key] = state

                    pool_symbols = pd.Index(
                        signal_day.loc[signal_day["in_pool"], "symbol"]
                    )
                    pool_exit = closes.iloc[
                        pos + PORTFOLIO_HOLD_DAYS - 1
                    ].reindex(pool_symbols)
                    pool_symbols = pool_symbols[
                        pool_exit.notna() & pool_exit.gt(0.0)
                    ]
                    benchmark_state = _open_position(
                        benchmark_cash[key],
                        pool_symbols,
                        opens.iloc[pos],
                        pos + PORTFOLIO_HOLD_DAYS - 1,
                        0.0,
                    )
                    if not benchmark_state:
                        raise ValueError("empty benchmark cohort in capped ensemble")
                    benchmark_state["last_prices"] = benchmark_state["entry_prices"]
                    benchmark_states[key] = benchmark_state
                    benchmark_exit = closes.iloc[
                        pos + PORTFOLIO_HOLD_DAYS - 1
                    ].reindex(benchmark_state["shares"].index)
                    benchmark_return = float(
                        (benchmark_exit / benchmark_state["entry_prices"]).mean()
                        - 1.0
                    )
                    exit_prices = closes.iloc[
                        pos + PORTFOLIO_HOLD_DAYS - 1
                    ].reindex(state["shares"].index)
                    position_rows.extend(
                        _position_row(
                            spec,
                            signal_day,
                            selected,
                            signal_ts,
                            date,
                            calendar[pos + PORTFOLIO_HOLD_DAYS - 1],
                            state,
                            exit_prices,
                            benchmark_return,
                            aggregate_weights,
                            total_nav_open,
                            entry_cost,
                            exit_cost,
                        )
                    )

        strategy_nav = 0.0
        benchmark_nav = 0.0
        active_positions = 0
        idle_cash = 0.0
        for key in keys:
            state = states[key]
            if state:
                value, is_exit = _close_value(
                    state, closes, pos, exit_cost, allow_stale_mark=True
                )
                strategy_nav += value
                active_positions += len(state["shares"])
                idle_cash += float(state["idle_cash"])
                if is_exit:
                    cash[key] = value
                    states[key] = {}
            else:
                strategy_nav += cash[key]

            benchmark_state = benchmark_states[key]
            if benchmark_state:
                value, is_exit = _close_value(
                    benchmark_state, closes, pos, 0.0, allow_stale_mark=True
                )
                benchmark_nav += value
                if is_exit:
                    benchmark_cash[key] = value
                    benchmark_states[key] = {}
            else:
                benchmark_nav += benchmark_cash[key]

        daily_rows.append(
            {
                "rule": "ensemble_cap2",
                "date": date,
                "strategy_nav": strategy_nav,
                "benchmark_nav": benchmark_nav,
                "entries": len(candidates),
                "raw_signal_count": raw_signals,
                "skipped_missing_entry": missing_entries,
                "skipped_missing_exit": missing_exits,
                "active_positions": active_positions,
                "cap_idle_cash": idle_cash,
            }
        )

    daily = pd.DataFrame(daily_rows)
    daily["net_return"] = daily["strategy_nav"].pct_change().fillna(0.0)
    daily["benchmark_return"] = daily["benchmark_nav"].pct_change().fillna(0.0)
    daily["daily_excess"] = daily["net_return"] - daily["benchmark_return"]
    monthly = monthly_portfolio_returns(daily)
    summary = _summary_row(
        summarize_portfolio(daily, monthly),
        "ensemble_cap2",
        ENSEMBLE_LABELS["ensemble_cap2"],
        "ensemble",
    )
    return daily, monthly, summary, pd.DataFrame(position_rows)


def _regime_diagnostics(
    factors: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    specs,
    evaluate_fn,
) -> pd.DataFrame:
    regime = (
        factors[["date", "risk_on", "regime_classified"]]
        .drop_duplicates("date")
        .set_index("date")
    )
    spec_by_name = {spec.name: spec for spec in specs}
    rows = []
    for rule in PORTFOLIO_RULES:
        events = collect_events(
            factors,
            calendar,
            spec_by_name[rule],
            evaluate_fn,
            hold_grid=(PORTFOLIO_HOLD_DAYS,),
        )[PORTFOLIO_HOLD_DAYS].events
        events = events.join(regime, on="date")
        events = events[events["regime_classified"]]
        for risk_on, frame in events.groupby("risk_on"):
            stats = summarize_with_split(frame)
            rows.append(
                {
                    "rule": rule,
                    "regime": "risk_on" if risk_on else "risk_off",
                    **stats,
                    "p5_return": float(frame["ret"].quantile(0.05)),
                    "worst_return": float(frame["ret"].min()),
                }
            )
    return pd.DataFrame(rows)


def evaluate_improvements(
    factors: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    specs,
    evaluate_fn,
    base_portfolio_daily: pd.DataFrame,
    null_run_count: int = 20,
) -> Dict[str, pd.DataFrame]:
    """Evaluate every frozen candidate and return auditable output tables."""
    work = attach_improvement_factors(factors)
    event_rows = []
    event_frames = []
    daily_frames = []
    monthly_frames = []
    position_frames = []
    summary_rows = []
    weight_frames = []

    for spec in COMPOSITE_SPECS:
        events = collect_events(
            work,
            calendar,
            spec,
            evaluate_fn,
            hold_grid=(PORTFOLIO_HOLD_DAYS,),
        )[PORTFOLIO_HOLD_DAYS].events
        stats = summarize_with_split(events)
        null95 = null_runs(
            work,
            calendar,
            spec,
            evaluate_fn,
            n=PORTFOLIO_HOLD_DAYS,
            runs=null_run_count,
        )
        event_verdict = verdict(stats, null95)
        event_rows.append(
            {
                **stats,
                "candidate": spec.name,
                "label": spec.label,
                "null95": null95,
                "verdict": event_verdict,
            }
        )
        event_frame = events.copy()
        event_frame["candidate"] = spec.name
        event_frames.append(event_frame)

        daily, positions = run_overlapping_portfolio(
            work, calendar, spec, evaluate_fn
        )
        monthly = monthly_portfolio_returns(daily)
        summary = _summary_row(
            summarize_portfolio(daily, monthly), spec.name, spec.label, "composite"
        )
        summary["event_verdict"] = event_verdict
        summary["final_verdict"] = (
            "通过"
            if event_verdict == "过随机关" and summary["verdict"] == "通过"
            else "未通过"
        )
        daily_frames.append(daily)
        monthly_frames.append(monthly)
        positions["candidate"] = spec.name
        position_frames.append(positions)
        summary_rows.append(summary)

    static = _static_ensemble(
        base_portfolio_daily,
        "ensemble_equal",
        ENSEMBLE_LABELS["ensemble_equal"],
    )
    inverse = _inverse_vol_ensemble(base_portfolio_daily)
    capped = _capped_ensemble(work, calendar, specs, evaluate_fn)
    for daily, monthly, summary, extra in (static, inverse):
        summary["event_verdict"] = "底层规则已通过"
        summary["final_verdict"] = summary["verdict"]
        daily_frames.append(daily)
        monthly_frames.append(monthly)
        summary_rows.append(summary)
        weight_frames.append(extra)
    capped_daily, capped_monthly, capped_summary, capped_positions = capped
    capped_summary["event_verdict"] = "底层规则已通过"
    capped_summary["final_verdict"] = capped_summary["verdict"]
    daily_frames.append(capped_daily)
    monthly_frames.append(capped_monthly)
    summary_rows.append(capped_summary)
    position_frames.append(capped_positions)

    return {
        "improvement_event_summary": pd.DataFrame(event_rows),
        "improvement_events": pd.concat(event_frames, ignore_index=True),
        "improvement_daily": pd.concat(daily_frames, ignore_index=True),
        "improvement_monthly": pd.concat(monthly_frames, ignore_index=True),
        "improvement_positions": pd.concat(position_frames, ignore_index=True),
        "improvement_summary": pd.DataFrame(summary_rows),
        "improvement_weights": pd.concat(weight_frames, ignore_index=True),
        "improvement_regime": _regime_diagnostics(
            work, calendar, specs, evaluate_fn
        ),
    }

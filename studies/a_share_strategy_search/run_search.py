"""Search six bounded A-share strategy families with frozen out-of-sample tests.

The study uses 2016-2021 for training, 2022-2023 for validation, and keeps
2024 onward blind until one candidate per family has been selected. Signals are
formed after the signal-day close, entered at the next tradable open, and exited
at the close after a fixed number of security trading sessions.
"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import product
import json
import math
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd


STUDY_DIR = Path(__file__).resolve().parent
REPO_DIR = STUDY_DIR.parents[1]
SOURCE_DIR = REPO_DIR / "studies" / "reverse_rule_induction" / "data" / "shards"
DATA_DIR = STUDY_DIR / "data"
REPAIR_DIR = DATA_DIR / "market_repair"
OUTPUT_DIR = STUDY_DIR / "outputs"
TRAIN_END = pd.Timestamp("2021-12-31")
VALIDATION_END = pd.Timestamp("2023-12-31")
BASELINE_ROUND_TRIP_COST = 0.002
COST_SCENARIOS = (0.001, 0.002, 0.005)
HORIZONS = (5, 10, 20, 60, 120)
MIN_LISTING_AGE_DAYS = 365
MIN_TRAILING_AMOUNT_KCNY = 20_000
MAX_ENTRY_GAP_DAYS = 10
MAX_EXIT_LOCK_DELAY = 5
RANDOM_BASELINE_TRIALS = 500
RANDOM_SEED = 20260928


def split_name(dates: pd.Series) -> pd.Series:
    """Assign immutable train, validation, and blind-test labels."""
    labels = np.full(len(dates), "blind", dtype=object)
    labels[dates <= TRAIN_END] = "train"
    labels[(dates > TRAIN_END) & (dates <= VALIDATION_END)] = "validation"
    return pd.Series(labels, index=dates.index, dtype="string")


def read_month(path: Path, columns: list[str]) -> pd.DataFrame:
    """Read one source partition and enforce its required schema."""
    frame = pd.read_parquet(path, columns=columns)
    missing = set(columns).difference(frame.columns)
    if missing:
        raise RuntimeError(f"{path.name} is missing required columns: {sorted(missing)}")
    return frame


def apply_repair_overlays(
    frame: pd.DataFrame,
    table: str,
    month: str,
    columns: list[str],
) -> pd.DataFrame:
    """Replace incomplete source dates with validated study-local overlays."""
    repair_paths = sorted(REPAIR_DIR.glob(f"{table}_{month}??.parquet"))
    if not repair_paths:
        return frame
    repairs = pd.concat(
        [pd.read_parquet(path, columns=columns) for path in repair_paths],
        ignore_index=True,
    )
    repair_dates = set(repairs["trade_date"].astype(str))
    retained = frame[~frame["trade_date"].astype(str).isin(repair_dates)]
    return pd.concat([retained, repairs], ignore_index=True)


def load_stock_panel() -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    """Load and validate the point-in-time daily stock panel month by month."""
    stock_master = pd.read_parquet(SOURCE_DIR / "stock_basic.parquet")
    if stock_master["ts_code"].duplicated().any():
        raise RuntimeError("stock_basic contains duplicate ts_code values.")
    stock_master = stock_master.copy()
    stock_master["list_date"] = pd.to_datetime(stock_master["list_date"], format="%Y%m%d")
    valid_codes = set(stock_master["ts_code"])
    listing_dates = stock_master.set_index("ts_code")["list_date"].to_dict()

    daily_paths = sorted(SOURCE_DIR.glob("daily_??????.parquet"))
    if not daily_paths:
        raise RuntimeError("No daily source partitions were found.")
    monthly_frames: list[pd.DataFrame] = []
    audit = {
        "source_months": len(daily_paths),
        "daily_rows_before_universe_filter": 0,
        "daily_rows_after_universe_filter": 0,
        "duplicate_daily_keys": 0,
        "adj_factor_missing_rows": 0,
        "daily_basic_missing_rows": 0,
        "st_rows": 0,
    }
    daily_columns = [
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pct_chg",
        "amount",
    ]
    basic_columns = [
        "ts_code",
        "trade_date",
        "turnover_rate",
        "pe_ttm",
        "pb",
        "dv_ttm",
        "total_mv",
    ]
    for position, daily_path in enumerate(daily_paths, start=1):
        month = daily_path.stem.rsplit("_", 1)[1]
        adj_path = SOURCE_DIR / f"adj_factor_{month}.parquet"
        basic_path = SOURCE_DIR / f"daily_basic_{month}.parquet"
        st_path = SOURCE_DIR / f"stock_st_{month}.parquet"
        if not adj_path.exists() or not basic_path.exists() or not st_path.exists():
            raise RuntimeError(f"Required source partition is missing for {month}.")

        daily = read_month(daily_path, daily_columns)
        audit["daily_rows_before_universe_filter"] += len(daily)
        duplicate_count = int(daily.duplicated(["ts_code", "trade_date"]).sum())
        audit["duplicate_daily_keys"] += duplicate_count
        if duplicate_count:
            raise RuntimeError(f"{daily_path.name} contains {duplicate_count} duplicate keys.")
        daily = daily[daily["ts_code"].isin(valid_codes)].copy()
        audit["daily_rows_after_universe_filter"] += len(daily)

        adj = read_month(adj_path, ["ts_code", "trade_date", "adj_factor"])
        adj = apply_repair_overlays(
            adj,
            "adj_factor",
            month,
            ["ts_code", "trade_date", "adj_factor"],
        )
        if adj.duplicated(["ts_code", "trade_date"]).any():
            raise RuntimeError(f"{adj_path.name} contains duplicate keys.")
        basic = read_month(basic_path, basic_columns)
        basic = apply_repair_overlays(basic, "daily_basic", month, basic_columns)
        if basic.duplicated(["ts_code", "trade_date"]).any():
            raise RuntimeError(f"{basic_path.name} contains duplicate keys.")
        st = read_month(st_path, ["ts_code", "trade_date"])
        st = st.drop_duplicates(["ts_code", "trade_date"]).assign(is_st=True)

        merged = daily.merge(adj, on=["ts_code", "trade_date"], how="left", validate="one_to_one")
        merged = merged.merge(
            basic,
            on=["ts_code", "trade_date"],
            how="left",
            validate="one_to_one",
        )
        merged = merged.merge(
            st,
            on=["ts_code", "trade_date"],
            how="left",
            validate="one_to_one",
        )
        audit["adj_factor_missing_rows"] += int(merged["adj_factor"].isna().sum())
        audit["daily_basic_missing_rows"] += int(merged["total_mv"].isna().sum())
        merged["is_st"] = merged["is_st"].eq(True)
        audit["st_rows"] += int(merged["is_st"].sum())
        merged = merged.dropna(subset=["adj_factor"])
        merged["trade_date"] = pd.to_datetime(merged["trade_date"], format="%Y%m%d")
        merged["list_date"] = merged["ts_code"].map(listing_dates)
        for column in set(daily_columns + basic_columns + ["adj_factor"]) - {
            "ts_code",
            "trade_date",
        }:
            merged[column] = pd.to_numeric(merged[column], errors="coerce").astype("float32")
        monthly_frames.append(merged)
        if position % 24 == 0 or position == len(daily_paths):
            print(f"loaded stock partitions: {position}/{len(daily_paths)}", flush=True)

    panel = pd.concat(monthly_frames, ignore_index=True)
    del monthly_frames
    panel["ts_code"] = panel["ts_code"].astype("category")
    panel = panel.sort_values(["ts_code", "trade_date"], kind="mergesort").reset_index(drop=True)
    audit["panel_rows"] = len(panel)
    audit["panel_codes"] = int(panel["ts_code"].nunique())
    audit["panel_start_date"] = panel["trade_date"].min().date().isoformat()
    audit["panel_end_date"] = panel["trade_date"].max().date().isoformat()
    audit["adj_factor_coverage"] = 1 - (
        audit["adj_factor_missing_rows"] / audit["daily_rows_after_universe_filter"]
    )
    audit["daily_basic_coverage"] = 1 - (
        audit["daily_basic_missing_rows"] / audit["daily_rows_after_universe_filter"]
    )
    return panel, audit, stock_master


def add_stock_features(panel: pd.DataFrame) -> pd.DataFrame:
    """Add only features needed by the frozen six-family search space."""
    panel["adj_open"] = panel["open"] * panel["adj_factor"]
    panel["adj_close"] = panel["close"] * panel["adj_factor"]
    grouped = panel.groupby("ts_code", observed=True, sort=False)
    panel["ret1"] = grouped["adj_close"].pct_change(fill_method=None).astype("float32")
    panel["ret5"] = (panel["adj_close"] / grouped["adj_close"].shift(5) - 1).astype("float32")
    panel["ret10"] = (panel["adj_close"] / grouped["adj_close"].shift(10) - 1).astype("float32")
    panel["mom20"] = (panel["adj_close"] / grouped["adj_close"].shift(20) - 1).astype("float32")
    panel["mom60"] = (panel["adj_close"] / grouped["adj_close"].shift(60) - 1).astype("float32")
    panel["vol60"] = (
        panel.groupby("ts_code", observed=True, sort=False)["ret1"]
        .rolling(60, min_periods=40)
        .std()
        .reset_index(level=0, drop=True)
        .astype("float32")
    )
    panel["turnover20"] = (
        panel.groupby("ts_code", observed=True, sort=False)["turnover_rate"]
        .rolling(20, min_periods=15)
        .mean()
        .reset_index(level=0, drop=True)
        .astype("float32")
    )
    panel["turnover60"] = (
        panel.groupby("ts_code", observed=True, sort=False)["turnover_rate"]
        .rolling(60, min_periods=40)
        .mean()
        .reset_index(level=0, drop=True)
        .astype("float32")
    )
    panel["amount20"] = (
        panel.groupby("ts_code", observed=True, sort=False)["amount"]
        .rolling(20, min_periods=15)
        .mean()
        .reset_index(level=0, drop=True)
        .astype("float32")
    )
    panel["abnormal_turnover"] = (panel["turnover_rate"] / panel["turnover20"]).astype(
        "float32"
    )
    panel["ep"] = np.where(panel["pe_ttm"] > 0, 1 / panel["pe_ttm"], np.nan).astype(
        "float32"
    )
    panel["implied_roe"] = np.where(
        (panel["pe_ttm"] > 0) & (panel["pb"] > 0),
        panel["pb"] / panel["pe_ttm"],
        np.nan,
    ).astype("float32")
    market_ret5 = panel.groupby("trade_date", observed=True)["ret5"].transform("median")
    market_ret10 = panel.groupby("trade_date", observed=True)["ret10"].transform("median")
    panel["residual_ret5"] = (panel["ret5"] - market_ret5).astype("float32")
    panel["residual_ret10"] = (panel["ret10"] - market_ret10).astype("float32")
    one_price = (
        np.isclose(panel["open"], panel["high"], rtol=0, atol=1e-6)
        & np.isclose(panel["high"], panel["low"], rtol=0, atol=1e-6)
        & np.isclose(panel["low"], panel["close"], rtol=0, atol=1e-6)
    )
    panel["locked_up"] = one_price & (panel["pct_chg"] >= 4.8)
    panel["locked_down"] = one_price & (panel["pct_chg"] <= -4.8)
    listing_age = (panel["trade_date"] - panel["list_date"]).dt.days
    panel["base_eligible"] = (
        (listing_age >= MIN_LISTING_AGE_DAYS)
        & ~panel["is_st"]
        & (panel["amount20"] >= MIN_TRAILING_AMOUNT_KCNY)
        & panel["adj_open"].gt(0)
        & panel["adj_close"].gt(0)
    )
    print("stock features complete", flush=True)
    return panel


def add_forward_returns(panel: pd.DataFrame) -> pd.DataFrame:
    """Add executable next-open to future-close returns with locked-exit delays."""
    grouped = panel.groupby("ts_code", observed=True, sort=False)
    panel["entry_date"] = grouped["trade_date"].shift(-1)
    panel["entry_price"] = grouped["open"].shift(-1).astype("float32")
    panel["entry_adj_open"] = grouped["adj_open"].shift(-1).astype("float32")
    entry_locked = grouped["locked_up"].shift(-1).ne(False)
    entry_gap = (panel["entry_date"] - panel["trade_date"]).dt.days
    entry_valid = ~entry_locked & entry_gap.le(MAX_ENTRY_GAP_DAYS)

    for horizon in HORIZONS:
        exit_date = grouped["trade_date"].shift(-horizon)
        exit_price = grouped["close"].shift(-horizon).astype("float32")
        exit_adj_close = grouped["adj_close"].shift(-horizon).astype("float32")
        exit_locked = grouped["locked_down"].shift(-horizon).eq(True)
        delay = pd.Series(np.zeros(len(panel), dtype="int8"), index=panel.index)
        still_locked = exit_locked.copy()
        for extra_days in range(1, MAX_EXIT_LOCK_DELAY + 1):
            candidate_locked = grouped["locked_down"].shift(
                -(horizon + extra_days)
            ).ne(False)
            use_candidate = still_locked & ~candidate_locked
            if use_candidate.any():
                candidate_date = grouped["trade_date"].shift(-(horizon + extra_days))
                candidate_price = grouped["close"].shift(-(horizon + extra_days)).astype(
                    "float32"
                )
                candidate_adj_close = grouped["adj_close"].shift(
                    -(horizon + extra_days)
                ).astype("float32")
                exit_date = exit_date.where(~use_candidate, candidate_date)
                exit_price = exit_price.where(~use_candidate, candidate_price)
                exit_adj_close = exit_adj_close.where(~use_candidate, candidate_adj_close)
                delay = delay.where(~use_candidate, extra_days)
            still_locked &= candidate_locked
        gross_return = exit_adj_close / panel["entry_adj_open"] - 1
        gross_return = gross_return.where(entry_valid & ~still_locked)
        panel[f"gross_return_{horizon}"] = gross_return.astype("float32")
        panel[f"exit_date_{horizon}"] = exit_date.where(entry_valid & ~still_locked)
        panel[f"exit_price_{horizon}"] = exit_price.where(entry_valid & ~still_locked).astype(
            "float32"
        )
        panel[f"exit_lock_delay_{horizon}"] = delay.where(
            entry_valid & ~still_locked
        ).astype("float32")
        print(f"forward return {horizon} sessions complete", flush=True)
    return panel


def cross_section_rank(frame: pd.DataFrame, column: str) -> pd.Series:
    """Return a date-local percentile rank while preserving missing values."""
    return frame.groupby("trade_date", observed=True)[column].rank(pct=True, method="average")


def prepare_stock_pools(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create compact daily and month-end search pools with date-local ranks."""
    base_columns = [
        "ts_code",
        "trade_date",
        "entry_date",
        "entry_price",
        "base_eligible",
        "ep",
        "implied_roe",
        "vol60",
        "dv_ttm",
        "total_mv",
        "turnover60",
        "residual_ret5",
        "residual_ret10",
        "abnormal_turnover",
    ]
    for horizon in HORIZONS:
        base_columns.extend(
            [
                f"gross_return_{horizon}",
                f"exit_date_{horizon}",
                f"exit_price_{horizon}",
                f"exit_lock_delay_{horizon}",
            ]
        )
    daily_pool = panel.loc[panel["base_eligible"], base_columns].copy()
    daily_pool["residual5_pct"] = cross_section_rank(daily_pool, "residual_ret5").astype(
        "float32"
    )
    daily_pool["residual10_pct"] = cross_section_rank(daily_pool, "residual_ret10").astype(
        "float32"
    )
    daily_pool["abnormal_turnover_pct"] = cross_section_rank(
        daily_pool, "abnormal_turnover"
    ).astype("float32")
    reversal_pool = daily_pool[
        (daily_pool[["residual5_pct", "residual10_pct"]].min(axis=1) <= 0.15)
        & (daily_pool["abnormal_turnover_pct"] >= 0.80)
    ].copy()

    month_key = panel["trade_date"].dt.year * 100 + panel["trade_date"].dt.month
    month_end = panel.groupby(month_key, observed=True)["trade_date"].transform("max")
    monthly_pool = daily_pool[daily_pool["trade_date"].isin(month_end.unique())].copy()
    for column in ("ep", "implied_roe", "vol60", "dv_ttm", "total_mv", "turnover60"):
        monthly_pool[f"{column}_pct"] = cross_section_rank(monthly_pool, column).astype(
            "float32"
        )
    dividend_positive = monthly_pool["dv_ttm"].fillna(0).gt(0).astype("int8")
    monthly_pool["dividend_positive_months_12"] = (
        dividend_positive.groupby(monthly_pool["ts_code"], observed=True)
        .rolling(12, min_periods=6)
        .sum()
        .reset_index(level=0, drop=True)
        .astype("float32")
    )
    add_benchmarks(monthly_pool, (20, 60, 120))
    add_benchmarks(reversal_pool, (5, 10, 20), benchmark_source=daily_pool)
    return monthly_pool, reversal_pool


def add_benchmarks(
    target: pd.DataFrame,
    horizons: Iterable[int],
    benchmark_source: Optional[pd.DataFrame] = None,
) -> None:
    """Attach same-date equal-weight investable-universe benchmark returns."""
    source = target if benchmark_source is None else benchmark_source
    for horizon in horizons:
        benchmark = source.groupby("trade_date", observed=True)[
            f"gross_return_{horizon}"
        ].mean()
        target[f"benchmark_return_{horizon}"] = target["trade_date"].map(benchmark).astype(
            "float32"
        )


def load_express_events(panel: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Map first earnings-express disclosures to executable stock-panel rows."""
    paths = sorted((DATA_DIR / "express").glob("express_*.parquet"))
    frames = [pd.read_parquet(path) for path in paths]
    express = pd.concat([frame for frame in frames if not frame.empty], ignore_index=True)
    express["ann_date"] = pd.to_datetime(express["ann_date"], format="%Y%m%d")
    express["end_date"] = pd.to_datetime(express["end_date"], format="%Y%m%d")
    duplicate_keys = int(express.duplicated(["ts_code", "ann_date", "end_date"]).sum())
    if duplicate_keys:
        raise RuntimeError(f"Earnings express data contains {duplicate_keys} duplicate keys.")
    express = express.sort_values("ann_date").drop_duplicates(
        ["ts_code", "end_date"], keep="first"
    )
    trade_dates = np.sort(panel["trade_date"].unique())
    positions = np.searchsorted(trade_dates, express["ann_date"].to_numpy(), side="left")
    valid_position = positions < len(trade_dates)
    express = express.loc[valid_position].copy()
    express["trade_date"] = trade_dates[positions[valid_position]]
    if (express["trade_date"] < express["ann_date"]).any():
        raise RuntimeError("An earnings event was mapped before its announcement date.")

    join_columns = [
        "ts_code",
        "trade_date",
        "entry_date",
        "entry_price",
        "base_eligible",
        "ep",
        "implied_roe",
        "vol60",
        "dv_ttm",
        "total_mv",
        "turnover60",
    ]
    for horizon in (5, 20, 60):
        join_columns.extend(
            [
                f"gross_return_{horizon}",
                f"exit_date_{horizon}",
                f"exit_price_{horizon}",
                f"exit_lock_delay_{horizon}",
            ]
        )
    panel_join = panel[join_columns].copy()
    panel_join["ts_code"] = panel_join["ts_code"].astype("string")
    express["ts_code"] = express["ts_code"].astype("string")
    events = express.merge(
        panel_join,
        on=["ts_code", "trade_date"],
        how="inner",
        validate="many_to_one",
    )
    events = events[events["base_eligible"]].copy()
    numeric_columns = ["diluted_eps", "diluted_roe", "yoy_net_profit"]
    for column in numeric_columns:
        events[column] = pd.to_numeric(events[column], errors="coerce")
    add_benchmarks(events, (5, 20, 60), benchmark_source=panel_join[panel_join["base_eligible"]])
    audit = {
        "express_partitions": len(paths),
        "express_unique_first_disclosures": len(express),
        "express_mapped_eligible_events": len(events),
        "express_start_date": express["ann_date"].min().date().isoformat(),
        "express_end_date": express["ann_date"].max().date().isoformat(),
        "express_yoy_net_profit_coverage": float(events["yoy_net_profit"].notna().mean()),
        "express_diluted_roe_coverage": float(events["diluted_roe"].notna().mean()),
    }
    return events, audit


def load_industry_pool() -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    """Build the month-end SW level-one industry-index rotation pool."""
    classification = pd.read_parquet(DATA_DIR / "sw2021_level1.parquet")
    paths = sorted((DATA_DIR / "sw_daily").glob("sw_daily_*.parquet"))
    industry = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
    if industry.duplicated(["ts_code", "trade_date"]).any():
        raise RuntimeError("SW daily data contains duplicate keys.")
    industry["trade_date"] = pd.to_datetime(industry["trade_date"], format="%Y%m%d")
    industry = industry.sort_values(["ts_code", "trade_date"], kind="mergesort")
    grouped = industry.groupby("ts_code", sort=False)
    industry["ret1"] = grouped["close"].pct_change(fill_method=None)
    for window in (20, 60, 120):
        industry[f"momentum_{window}"] = industry["close"] / grouped["close"].shift(window) - 1
    industry["vol60"] = (
        industry.groupby("ts_code", sort=False)["ret1"]
        .rolling(60, min_periods=40)
        .std()
        .reset_index(level=0, drop=True)
    )
    industry["risk_adjusted_60"] = industry["momentum_60"] / industry["vol60"]
    industry["entry_date"] = grouped["trade_date"].shift(-1)
    industry["entry_price"] = grouped["open"].shift(-1)
    for horizon in (20, 60):
        industry[f"exit_date_{horizon}"] = grouped["trade_date"].shift(-horizon)
        industry[f"exit_price_{horizon}"] = grouped["close"].shift(-horizon)
        industry[f"exit_lock_delay_{horizon}"] = 0.0
        industry[f"gross_return_{horizon}"] = (
            industry[f"exit_price_{horizon}"] / industry["entry_price"] - 1
        )
    month_key = industry["trade_date"].dt.year * 100 + industry["trade_date"].dt.month
    month_end = industry.groupby(month_key)["trade_date"].transform("max")
    pool = industry[industry["trade_date"].eq(month_end)].copy()
    for factor in ("momentum_20", "momentum_60", "momentum_120", "risk_adjusted_60"):
        pool[f"{factor}_rank"] = pool.groupby("trade_date")[factor].rank(
            ascending=False, method="first"
        )
    add_benchmarks(pool, (20, 60))
    name_map = classification.set_index("index_code")["industry_name"].to_dict()
    pool["instrument_name"] = pool["ts_code"].map(name_map)
    audit = {
        "industry_codes": int(industry["ts_code"].nunique()),
        "industry_rows": len(industry),
        "industry_duplicate_keys": 0,
        "industry_start_date": industry["trade_date"].min().date().isoformat(),
        "industry_end_date": industry["trade_date"].max().date().isoformat(),
        "industry_expected_codes": len(classification),
    }
    return pool, audit, classification


def condition_mask(frame: pd.DataFrame, conditions: list[tuple[str, str, Any]]) -> pd.Series:
    """Evaluate explicit numeric conditions without dynamic query strings."""
    mask = pd.Series(True, index=frame.index)
    for column, operator, value in conditions:
        if operator == "ge":
            mask &= frame[column].ge(value)
        elif operator == "le":
            mask &= frame[column].le(value)
        elif operator == "gt":
            mask &= frame[column].gt(value)
        elif operator == "between":
            lower, upper = value
            mask &= frame[column].between(lower, upper, inclusive="both")
        elif operator == "rank_le":
            mask &= frame[column].le(value)
        else:
            raise ValueError(f"Unsupported condition operator: {operator}")
    return mask.fillna(False)


def remove_overlapping_positions(events: pd.DataFrame) -> pd.DataFrame:
    """Keep the first event per security until its position has exited."""
    if events.empty:
        return events
    ordered = events.sort_values(["ts_code", "entry_date", "exit_date"], kind="mergesort")
    keep = np.zeros(len(ordered), dtype=bool)
    codes = ordered["ts_code"].astype("string").to_numpy()
    entries = ordered["entry_date"].to_numpy()
    exits = ordered["exit_date"].to_numpy()
    last_code: Optional[str] = None
    last_exit = np.datetime64("1900-01-01")
    for position, (code, entry, exit_date) in enumerate(zip(codes, entries, exits)):
        if code != last_code:
            last_code = code
            last_exit = np.datetime64("1900-01-01")
        if entry > last_exit:
            keep[position] = True
            last_exit = exit_date
    return ordered.loc[keep].copy()


def materialize_events(
    frame: pd.DataFrame,
    candidate: dict[str, Any],
    feature_columns: list[str],
) -> pd.DataFrame:
    """Apply one rule and normalize its executable event rows."""
    horizon = candidate["holding_sessions"]
    mask = condition_mask(frame, candidate["conditions"])
    columns = [
        "ts_code",
        "trade_date",
        "entry_date",
        "entry_price",
        "instrument_name",
        f"exit_date_{horizon}",
        f"exit_price_{horizon}",
        f"exit_lock_delay_{horizon}",
        f"gross_return_{horizon}",
        f"benchmark_return_{horizon}",
        *feature_columns,
    ]
    available_columns = list(dict.fromkeys(column for column in columns if column in frame.columns))
    events = frame.loc[mask, available_columns].dropna(
        subset=[
            "entry_date",
            f"exit_date_{horizon}",
            f"gross_return_{horizon}",
            f"benchmark_return_{horizon}",
        ]
    )
    events = events.rename(
        columns={
            "trade_date": "signal_date",
            f"exit_date_{horizon}": "exit_date",
            f"exit_price_{horizon}": "exit_price",
            f"exit_lock_delay_{horizon}": "exit_lock_delay",
            f"gross_return_{horizon}": "gross_return",
            f"benchmark_return_{horizon}": "benchmark_return",
        }
    )
    events = remove_overlapping_positions(events)
    events["family"] = candidate["family"]
    events["candidate_id"] = candidate["candidate_id"]
    events["rule"] = candidate["rule"]
    events["holding_sessions"] = horizon
    events["split"] = split_name(events["signal_date"])
    return events


def clustered_summary(events: pd.DataFrame, split: str) -> dict[str, Any]:
    """Summarize event returns with confidence intervals clustered by signal date."""
    sample = events[events["split"].eq(split)].copy()
    if sample.empty:
        return {
            "events": 0,
            "signal_dates": 0,
            "avg_gross_return": np.nan,
            "avg_net_return": np.nan,
            "avg_benchmark_return": np.nan,
            "avg_net_excess_return": np.nan,
            "net_excess_lcb_95": np.nan,
            "net_excess_ucb_95": np.nan,
            "median_net_return": np.nan,
            "win_rate_net": np.nan,
            "positive_year_ratio": np.nan,
            "worst_year_net_return": np.nan,
        }
    sample["net_return"] = sample["gross_return"] - BASELINE_ROUND_TRIP_COST
    sample["net_excess"] = sample["net_return"] - sample["benchmark_return"]
    date_means = sample.groupby("signal_date")["net_excess"].mean()
    standard_error = date_means.std(ddof=1) / math.sqrt(len(date_means)) if len(date_means) > 1 else np.nan
    cluster_mean = float(date_means.mean())
    yearly = sample.groupby(sample["signal_date"].dt.year)["net_return"].mean()
    return {
        "events": len(sample),
        "signal_dates": int(sample["signal_date"].nunique()),
        "avg_gross_return": float(sample["gross_return"].mean()),
        "avg_net_return": float(sample["net_return"].mean()),
        "avg_benchmark_return": float(sample["benchmark_return"].mean()),
        "avg_net_excess_return": float(sample["net_excess"].mean()),
        "net_excess_lcb_95": cluster_mean - 1.96 * standard_error
        if np.isfinite(standard_error)
        else np.nan,
        "net_excess_ucb_95": cluster_mean + 1.96 * standard_error
        if np.isfinite(standard_error)
        else np.nan,
        "median_net_return": float(sample["net_return"].median()),
        "win_rate_net": float(sample["net_return"].gt(0).mean()),
        "positive_year_ratio": float(yearly.gt(0).mean()),
        "worst_year_net_return": float(yearly.min()),
    }


def evaluate_candidates(
    frame: pd.DataFrame,
    candidates: list[dict[str, Any]],
    feature_columns: list[str],
    minimums: dict[str, int],
) -> tuple[dict[str, Any], pd.DataFrame, list[dict[str, Any]]]:
    """Train-shortlist candidates, select on validation, then materialize the winner."""
    evaluated: list[dict[str, Any]] = []
    candidate_lookup = {candidate["candidate_id"]: candidate for candidate in candidates}
    for position, candidate in enumerate(candidates, start=1):
        events = materialize_events(frame, candidate, feature_columns)
        train = clustered_summary(events, "train")
        validation = clustered_summary(events, "validation")
        viable = (
            train["events"] >= minimums["train_events"]
            and train["signal_dates"] >= minimums["train_dates"]
            and validation["events"] >= minimums["validation_events"]
            and validation["signal_dates"] >= minimums["validation_dates"]
            and np.isfinite(train["net_excess_lcb_95"])
            and np.isfinite(validation["net_excess_lcb_95"])
        )
        evaluated.append(
            {
                "candidate_id": candidate["candidate_id"],
                "family": candidate["family"],
                "rule": candidate["rule"],
                "holding_sessions": candidate["holding_sessions"],
                "viable": viable,
                "train": train,
                "validation": validation,
            }
        )
        if position % 50 == 0 or position == len(candidates):
            print(
                f"evaluated {candidate['family']}: {position}/{len(candidates)}",
                flush=True,
            )

    viable_rows = [row for row in evaluated if row["viable"]]
    if not viable_rows:
        raise RuntimeError(f"No viable candidates remained for {candidates[0]['family']}.")
    viable_rows.sort(key=lambda row: row["train"]["net_excess_lcb_95"], reverse=True)
    shortlist_size = min(30, max(5, math.ceil(len(viable_rows) * 0.20)))
    shortlist = viable_rows[:shortlist_size]
    selected_row = max(
        shortlist,
        key=lambda row: row["validation"]["net_excess_lcb_95"],
    )
    selected_candidate = candidate_lookup[selected_row["candidate_id"]]
    selected_events = materialize_events(frame, selected_candidate, feature_columns)
    selected_row = dict(selected_row)
    selected_row["blind"] = clustered_summary(selected_events, "blind")
    selected_row["tested_candidates"] = len(candidates)
    selected_row["viable_candidates"] = len(viable_rows)
    selected_row["shortlist_candidates"] = len(shortlist)
    return selected_row, selected_events, evaluated


def quantile_rule(column: str, operator: str, threshold: float) -> str:
    """Render one readable percentile condition for the audit trail."""
    symbol = ">=" if operator == "ge" else "<="
    return f"{column} percentile {symbol} {threshold:.0%}"


def build_multifactor_candidates() -> list[dict[str, Any]]:
    """Return the frozen value-quality-low-volatility-dividend grid."""
    combinations = (
        (("ep_pct", "ge"), ("implied_roe_pct", "ge"), ("vol60_pct", "le")),
        (("ep_pct", "ge"), ("implied_roe_pct", "ge"), ("dv_ttm_pct", "ge")),
        (("ep_pct", "ge"), ("vol60_pct", "le"), ("dv_ttm_pct", "ge")),
        (("implied_roe_pct", "ge"), ("vol60_pct", "le"), ("dv_ttm_pct", "ge")),
    )
    candidates: list[dict[str, Any]] = []
    sequence = 0
    for feature_set in combinations:
        for thresholds in product((0.60, 0.70, 0.80), repeat=3):
            for horizon in (20, 60, 120):
                sequence += 1
                conditions = [
                    (column, operator, threshold)
                    for (column, operator), threshold in zip(feature_set, thresholds, strict=True)
                ]
                candidates.append(
                    {
                        "family": "value_quality_low_vol_dividend",
                        "candidate_id": f"F1-{sequence:03d}",
                        "holding_sessions": horizon,
                        "conditions": conditions,
                        "rule": "; ".join(
                            quantile_rule(column, operator, threshold)
                            for column, operator, threshold in conditions
                        ),
                    }
                )
    return candidates


def build_ch4_candidates() -> list[dict[str, Any]]:
    """Return the frozen CH-4-inspired size, earnings-price, and turnover grid."""
    candidates: list[dict[str, Any]] = []
    sequence = 0
    for size_upper, ep_floor, turnover_ceiling, horizon in product(
        (0.45, 0.55, 0.65),
        (0.60, 0.70, 0.80),
        (0.30, 0.40, 0.50),
        (20, 60, 120),
    ):
        sequence += 1
        conditions = [
            ("total_mv_pct", "between", (0.30, size_upper)),
            ("ep_pct", "ge", ep_floor),
            ("turnover60_pct", "le", turnover_ceiling),
        ]
        rule = (
            f"market-cap percentile between 30% and {size_upper:.0%}; "
            f"earnings-price percentile >= {ep_floor:.0%}; "
            f"60-session turnover percentile <= {turnover_ceiling:.0%}"
        )
        candidates.append(
            {
                "family": "china_ch4_inspired",
                "candidate_id": f"F2-{sequence:03d}",
                "holding_sessions": horizon,
                "conditions": conditions,
                "rule": rule,
            }
        )
    return candidates


def build_reversal_candidates() -> list[dict[str, Any]]:
    """Return the frozen market-adjusted reversal and abnormal-turnover grid."""
    candidates: list[dict[str, Any]] = []
    sequence = 0
    for residual_window, residual_ceiling, turnover_floor, horizon in product(
        (5, 10),
        (0.05, 0.10, 0.15),
        (0.80, 0.90, 0.95),
        (5, 10, 20),
    ):
        sequence += 1
        residual_column = f"residual{residual_window}_pct"
        conditions = [
            (residual_column, "le", residual_ceiling),
            ("abnormal_turnover_pct", "ge", turnover_floor),
        ]
        rule = (
            f"{residual_window}-session market-adjusted return percentile <= "
            f"{residual_ceiling:.0%}; abnormal-turnover percentile >= {turnover_floor:.0%}"
        )
        candidates.append(
            {
                "family": "residual_reversal_abnormal_turnover",
                "candidate_id": f"F3-{sequence:03d}",
                "holding_sessions": horizon,
                "conditions": conditions,
                "rule": rule,
            }
        )
    return candidates


def build_dividend_candidates() -> list[dict[str, Any]]:
    """Return the frozen dividend-yield, low-volatility, and continuity grid."""
    candidates: list[dict[str, Any]] = []
    sequence = 0
    for dividend_floor, volatility_ceiling, positive_months, horizon in product(
        (0.60, 0.70, 0.80),
        (0.20, 0.30, 0.40),
        (6, 9, 12),
        (20, 60, 120),
    ):
        sequence += 1
        conditions = [
            ("dv_ttm_pct", "ge", dividend_floor),
            ("vol60_pct", "le", volatility_ceiling),
            ("dividend_positive_months_12", "ge", positive_months),
        ]
        rule = (
            f"trailing-dividend-yield percentile >= {dividend_floor:.0%}; "
            f"60-session volatility percentile <= {volatility_ceiling:.0%}; "
            f"positive trailing yield in at least {positive_months} of 12 monthly snapshots"
        )
        candidates.append(
            {
                "family": "dividend_low_volatility",
                "candidate_id": f"F4-{sequence:03d}",
                "holding_sessions": horizon,
                "conditions": conditions,
                "rule": rule,
            }
        )
    return candidates


def build_earnings_candidates() -> list[dict[str, Any]]:
    """Return the frozen positive earnings-express drift grid."""
    candidates: list[dict[str, Any]] = []
    sequence = 0
    for growth_floor, roe_floor, require_positive_eps, horizon in product(
        (0, 20, 50, 100),
        (0, 5, 10),
        (False, True),
        (5, 20, 60),
    ):
        sequence += 1
        conditions: list[tuple[str, str, Any]] = [
            ("yoy_net_profit", "ge", growth_floor),
            ("diluted_roe", "ge", roe_floor),
        ]
        if require_positive_eps:
            conditions.append(("diluted_eps", "gt", 0))
        eps_text = "; diluted EPS > 0" if require_positive_eps else ""
        rule = (
            f"first earnings express for a reporting period; YoY net-profit growth >= "
            f"{growth_floor}%; diluted ROE >= {roe_floor}%{eps_text}"
        )
        candidates.append(
            {
                "family": "earnings_express_drift",
                "candidate_id": f"F5-{sequence:03d}",
                "holding_sessions": horizon,
                "conditions": conditions,
                "rule": rule,
            }
        )
    return candidates


def build_industry_candidates() -> list[dict[str, Any]]:
    """Return the frozen SW level-one index rotation grid."""
    candidates: list[dict[str, Any]] = []
    sequence = 0
    for factor, top_count, positive_only, horizon in product(
        ("momentum_20", "momentum_60", "momentum_120", "risk_adjusted_60"),
        (1, 3, 5),
        (False, True),
        (20, 60),
    ):
        sequence += 1
        conditions: list[tuple[str, str, Any]] = [(f"{factor}_rank", "rank_le", top_count)]
        if positive_only:
            momentum_column = "momentum_60" if factor == "risk_adjusted_60" else factor
            conditions.append((momentum_column, "gt", 0))
        positive_text = "; factor momentum > 0" if positive_only else ""
        rule = f"top {top_count} SW level-one indexes by {factor}{positive_text}"
        candidates.append(
            {
                "family": "sw_industry_index_rotation",
                "candidate_id": f"F6-{sequence:03d}",
                "holding_sessions": horizon,
                "conditions": conditions,
                "rule": rule,
            }
        )
    return candidates


def random_baseline(
    selected_events: pd.DataFrame,
    pool: pd.DataFrame,
    horizon: int,
) -> dict[str, float]:
    """Compare blind performance with date- and count-matched random selections."""
    blind = selected_events[selected_events["split"].eq("blind")]
    if blind.empty:
        return {"random_p_value": np.nan, "random_mean_excess": np.nan}
    observed = float(
        (
            blind["gross_return"]
            - BASELINE_ROUND_TRIP_COST
            - blind["benchmark_return"]
        ).mean()
    )
    source = pool.dropna(
        subset=[f"gross_return_{horizon}", f"benchmark_return_{horizon}"]
    ).copy()
    source["random_excess"] = (
        source[f"gross_return_{horizon}"]
        - BASELINE_ROUND_TRIP_COST
        - source[f"benchmark_return_{horizon}"]
    )
    source_by_date = {
        date: values["random_excess"].to_numpy()
        for date, values in source.groupby("trade_date")
    }
    counts = blind.groupby("signal_date").size()
    rng = np.random.default_rng(RANDOM_SEED)
    trial_means: list[float] = []
    for _ in range(RANDOM_BASELINE_TRIALS):
        samples: list[np.ndarray] = []
        for date, count in counts.items():
            values = source_by_date.get(date)
            if values is None or len(values) == 0:
                continue
            replace = count > len(values)
            samples.append(rng.choice(values, size=count, replace=replace))
        if samples:
            trial_means.append(float(np.concatenate(samples).mean()))
    if not trial_means:
        return {"random_p_value": np.nan, "random_mean_excess": np.nan}
    trials = np.asarray(trial_means)
    return {
        "random_p_value": float((1 + np.sum(trials >= observed)) / (1 + len(trials))),
        "random_mean_excess": float(trials.mean()),
        "random_excess_p95": float(np.quantile(trials, 0.95)),
    }


def add_event_metadata(
    events: pd.DataFrame,
    stock_master: pd.DataFrame,
    feature_columns: list[str],
) -> pd.DataFrame:
    """Attach names, cost scenarios, and a compact rule-value audit string."""
    master = stock_master[["ts_code", "name", "industry", "market"]].copy()
    master["ts_code"] = master["ts_code"].astype("string")
    events = events.copy()
    events["ts_code"] = events["ts_code"].astype("string")
    events = events.merge(master, on="ts_code", how="left", validate="many_to_one")
    if "instrument_name" in events.columns:
        events["name"] = events["name"].fillna(events["instrument_name"])
    for cost in COST_SCENARIOS:
        basis_points = int(round(cost * 10_000))
        events[f"net_return_{basis_points}bps"] = events["gross_return"] - cost
    events["net_excess_return_20bps"] = (
        events["gross_return"] - BASELINE_ROUND_TRIP_COST - events["benchmark_return"]
    )
    events["condition_values"] = events.apply(
        lambda row: "; ".join(
            f"{column}={row[column]:.6g}"
            for column in feature_columns
            if column in row.index and pd.notna(row[column])
        ),
        axis=1,
    )
    return events


def validation_status(selected: dict[str, Any]) -> str:
    """Classify stability without changing the pre-blind selection decision."""
    validation = selected["validation"]
    blind = selected["blind"]
    if validation["net_excess_lcb_95"] <= 0:
        return "no_stable_validation_winner"
    if blind["avg_net_excess_return"] <= 0:
        return "validation_passed_blind_failed"
    if blind["net_excess_lcb_95"] > 0:
        return "stable_blind_positive"
    return "blind_positive_but_uncertain"


def json_safe(value: Any) -> Any:
    """Convert numpy and timestamp values into portable JSON scalars."""
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def main() -> None:
    """Run the complete bounded search and persist inspectable result tables."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    panel, stock_audit, stock_master = load_stock_panel()
    panel = add_stock_features(panel)
    panel = add_forward_returns(panel)
    stock_audit["base_eligible_rows"] = int(panel["base_eligible"].sum())
    monthly_pool, reversal_pool = prepare_stock_pools(panel)
    express_pool, express_audit = load_express_events(panel)
    industry_pool, industry_audit, _ = load_industry_pool()

    family_jobs = [
        (
            monthly_pool,
            build_multifactor_candidates(),
            ["ep_pct", "implied_roe_pct", "vol60_pct", "dv_ttm_pct"],
            {"train_events": 200, "train_dates": 24, "validation_events": 80, "validation_dates": 8},
        ),
        (
            monthly_pool,
            build_ch4_candidates(),
            ["total_mv_pct", "ep_pct", "turnover60_pct"],
            {"train_events": 200, "train_dates": 24, "validation_events": 80, "validation_dates": 8},
        ),
        (
            reversal_pool,
            build_reversal_candidates(),
            [
                "residual_ret5",
                "residual_ret10",
                "residual5_pct",
                "residual10_pct",
                "abnormal_turnover",
                "abnormal_turnover_pct",
            ],
            {"train_events": 500, "train_dates": 100, "validation_events": 150, "validation_dates": 50},
        ),
        (
            monthly_pool,
            build_dividend_candidates(),
            ["dv_ttm_pct", "vol60_pct", "dividend_positive_months_12"],
            {"train_events": 150, "train_dates": 18, "validation_events": 60, "validation_dates": 8},
        ),
        (
            express_pool,
            build_earnings_candidates(),
            ["yoy_net_profit", "diluted_roe", "diluted_eps", "end_date", "ann_date"],
            {"train_events": 120, "train_dates": 60, "validation_events": 40, "validation_dates": 20},
        ),
        (
            industry_pool,
            build_industry_candidates(),
            [
                "momentum_20",
                "momentum_60",
                "momentum_120",
                "risk_adjusted_60",
            ],
            {"train_events": 24, "train_dates": 18, "validation_events": 8, "validation_dates": 8},
        ),
    ]

    selected_rows: list[dict[str, Any]] = []
    selected_event_frames: list[pd.DataFrame] = []
    search_audit: list[dict[str, Any]] = []
    for pool, candidates, feature_columns, minimums in family_jobs:
        selected, events, evaluated = evaluate_candidates(
            pool,
            candidates,
            feature_columns,
            minimums,
        )
        baseline = random_baseline(events, pool, selected["holding_sessions"])
        selected.update(baseline)
        selected["status"] = validation_status(selected)
        selected_rows.append(selected)
        search_audit.extend(evaluated)
        selected_event_frames.append(add_event_metadata(events, stock_master, feature_columns))
        print(
            f"selected {selected['family']} {selected['candidate_id']} "
            f"validation LCB={selected['validation']['net_excess_lcb_95']:.4%}",
            flush=True,
        )

    detail = pd.concat(selected_event_frames, ignore_index=True)
    detail = detail.sort_values(["family", "signal_date", "ts_code"], kind="mergesort")
    detail.to_parquet(OUTPUT_DIR / "selected_events.parquet", index=False)
    detail.to_csv(OUTPUT_DIR / "selected_events.csv", index=False)
    pd.DataFrame(
        [
            {
                "family": row["family"],
                "candidate_id": row["candidate_id"],
                "rule": row["rule"],
                "holding_sessions": row["holding_sessions"],
                "status": row["status"],
                "tested_candidates": row["tested_candidates"],
                "viable_candidates": row["viable_candidates"],
                "random_p_value": row["random_p_value"],
                **{
                    f"{split}_{metric}": value
                    for split in ("train", "validation", "blind")
                    for metric, value in row[split].items()
                },
            }
            for row in selected_rows
        ]
    ).to_csv(OUTPUT_DIR / "selected_summary.csv", index=False)
    with (OUTPUT_DIR / "selected_results.json").open("w", encoding="utf-8") as handle:
        json.dump(json_safe(selected_rows), handle, ensure_ascii=True, indent=2)
    with (OUTPUT_DIR / "data_quality.json").open("w", encoding="utf-8") as handle:
        json.dump(
            json_safe(
                {
                    "stock_panel": stock_audit,
                    "earnings_express": express_audit,
                    "industry_indexes": industry_audit,
                    "methodology": {
                        "train_period": "2016-01-01 through 2021-12-31",
                        "validation_period": "2022-01-01 through 2023-12-31",
                        "blind_period": "2024-01-01 through latest available date",
                        "baseline_round_trip_cost": BASELINE_ROUND_TRIP_COST,
                        "minimum_listing_age_days": MIN_LISTING_AGE_DAYS,
                        "minimum_trailing_amount_kcny": MIN_TRAILING_AMOUNT_KCNY,
                        "entry": "next tradable session open after signal-day close",
                        "exit": "close after N security trading sessions, delayed through up to five locked limit-down sessions",
                        "overlap": "no duplicate same-security position while an earlier event remains open",
                    },
                }
            ),
            handle,
            ensure_ascii=True,
            indent=2,
        )
    with (OUTPUT_DIR / "candidate_audit.json").open("w", encoding="utf-8") as handle:
        json.dump(json_safe(search_audit), handle, ensure_ascii=True)
    print(f"study complete: {len(detail)} selected-strategy events", flush=True)


if __name__ == "__main__":
    main()

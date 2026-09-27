"""Run the v2.24 L7 event study from 2025 onward.

The comparison isolates the v2.24 non-retroactive rule by contrasting the
canonical state machine with a counterfactual that re-applies the daily L1/L2
gate to stocks already in L3-L6. It does not claim that the counterfactual is a
complete reconstruction of v2.23 because the authoritative version ledger is
not available in this checkout.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd


LOGGER = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_SHARDS = REPO_ROOT / "studies" / "reverse_rule_induction" / "data" / "shards"
STUDY_DIR = Path(__file__).resolve().parent
DATA_DIR = STUDY_DIR / "data"
OUTPUT_DIR = STUDY_DIR / "outputs"
WARMUP_START_DATE = "20240101"
ANALYSIS_START_DATE = "20250101"
MAX_HORIZON = 10
BOOTSTRAP_REPETITIONS = 1_000
BOOTSTRAP_SEED = 20260927


def _read_month_shards(name: str, start_month: str) -> pd.DataFrame:
    paths: list[str] = []
    prefix_parts = name.split("_")
    for path in sorted(glob.glob(str(SOURCE_SHARDS / f"{name}_*.parquet"))):
        stem_parts = Path(path).stem.split("_")
        if (
            len(stem_parts) == len(prefix_parts) + 1
            and stem_parts[: len(prefix_parts)] == prefix_parts
            and stem_parts[-1] >= start_month
        ):
            paths.append(path)
    if not paths:
        raise FileNotFoundError(f"No {name} shards found from {start_month}")
    return pd.concat((pd.read_parquet(path) for path in paths), ignore_index=True)


def _append_supplemental(base: pd.DataFrame, name: str) -> pd.DataFrame:
    path = DATA_DIR / f"supplemental_{name}.parquet"
    if not path.exists():
        return base
    supplemental = pd.read_parquet(path)
    if supplemental.empty:
        return base
    shared_columns = [column for column in base.columns if column in supplemental.columns]
    combined = pd.concat(
        [base[shared_columns], supplemental[shared_columns]], ignore_index=True
    )
    key_columns = ["ts_code", "trade_date"]
    return combined.drop_duplicates(key_columns, keep="last")


def load_panel() -> tuple[pd.DataFrame, dict[str, object]]:
    """Load, validate, and prepare the daily panel used by both simulations."""
    membership_path = DATA_DIR / "current_ths_industry_membership.parquet"
    manifest_path = DATA_DIR / "source_manifest.json"
    if not membership_path.exists() or not manifest_path.exists():
        raise FileNotFoundError(
            "Supplemental inputs are missing. Run fetch_inputs.py before backtest.py."
        )
    source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    daily = _append_supplemental(
        _read_month_shards("daily", WARMUP_START_DATE[:6]), "daily"
    )
    daily_basic = _append_supplemental(
        _read_month_shards("daily_basic", WARMUP_START_DATE[:6]), "daily_basic"
    )
    adj_factor = _append_supplemental(
        _read_month_shards("adj_factor", WARMUP_START_DATE[:6]), "adj_factor"
    )
    stock_st = _append_supplemental(
        _read_month_shards("stock_st", WARMUP_START_DATE[:6]), "stock_st"
    )
    stock_basic = pd.read_parquet(SOURCE_SHARDS / "stock_basic.parquet")
    membership = pd.read_parquet(membership_path).rename(
        columns={"con_code": "ts_code", "con_name": "industry_member_name"}
    )
    if membership["ts_code"].duplicated().any():
        raise ValueError("Current THS industry membership contains duplicate stock codes")

    daily["trade_date"] = daily["trade_date"].astype(str)
    daily_basic["trade_date"] = daily_basic["trade_date"].astype(str)
    adj_factor["trade_date"] = adj_factor["trade_date"].astype(str)
    stock_st["trade_date"] = stock_st["trade_date"].astype(str)
    panel = daily.merge(
        adj_factor[["ts_code", "trade_date", "adj_factor"]],
        on=["ts_code", "trade_date"],
        how="left",
        validate="one_to_one",
    ).merge(
        daily_basic[
            ["ts_code", "trade_date", "turnover_rate", "pe", "total_mv"]
        ],
        on=["ts_code", "trade_date"],
        how="left",
        validate="one_to_one",
    )
    stock_reference = stock_basic[
        ["ts_code", "name", "list_date"]
    ].drop_duplicates("ts_code", keep="last")
    panel = panel.merge(stock_reference, on="ts_code", how="left", validate="many_to_one")
    panel = panel.merge(
        membership[["ts_code", "industry_code", "industry_name"]],
        on="ts_code",
        how="left",
        validate="many_to_one",
    )
    st_keys = pd.MultiIndex.from_frame(stock_st[["ts_code", "trade_date"]])
    panel_keys = pd.MultiIndex.from_frame(panel[["ts_code", "trade_date"]])
    panel["is_st"] = panel_keys.isin(st_keys)
    symbol = panel["ts_code"].str.split(".").str[0]
    panel = panel[
        panel["ts_code"].astype(str).str.endswith((".SH", ".SZ"))
        & ~symbol.str.startswith(("200", "900"))
    ].copy()
    panel = panel[panel["trade_date"] >= WARMUP_START_DATE].copy()

    numeric_columns = [
        "open",
        "close",
        "amount",
        "adj_factor",
        "turnover_rate",
        "pe",
        "total_mv",
    ]
    for column in numeric_columns:
        panel[column] = pd.to_numeric(panel[column], errors="coerce")
    panel["qf"] = panel["close"] * panel["adj_factor"]
    panel["qf_open"] = panel["open"] * panel["adj_factor"]
    panel = panel.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    grouped = panel.groupby("ts_code", sort=False)
    for window in (5, 20, 60):
        panel[f"ma{window}"] = (
            grouped["qf"]
            .rolling(window, min_periods=window)
            .mean()
            .reset_index(level=0, drop=True)
        )
    panel["prev_ma5"] = grouped["ma5"].shift(1)
    panel["prev_ma20"] = grouped["ma20"].shift(1)
    panel["prev_ma60"] = grouped["ma60"].shift(1)
    panel["prev5_amount_mean"] = (
        grouped["amount"]
        .shift(1)
        .groupby(panel["ts_code"], sort=False)
        .rolling(5, min_periods=5)
        .mean()
        .reset_index(level=0, drop=True)
    )

    list_dates = pd.to_datetime(panel["list_date"], format="%Y%m%d", errors="coerce")
    trade_dates = pd.to_datetime(panel["trade_date"], format="%Y%m%d", errors="coerce")
    panel["listed_days"] = (trade_dates - list_dates).dt.days
    panel["market_cap_billion"] = panel["total_mv"] / 10_000.0
    cap = panel["market_cap_billion"]
    pe = panel["pe"]
    pe_rule = (
        ((cap >= 20) & (cap < 50) & (pe > 0) & (pe <= 50))
        | ((cap >= 50) & (cap < 100) & (pe > 0) & (pe <= 100))
        | ((cap >= 100) & (cap < 200) & (pe > 0) & (pe <= 300))
        | (cap >= 200)
    )
    symbol = panel["ts_code"].str.split(".").str[0]
    panel["l1_pass"] = (
        pe_rule
        & (panel["close"] <= 160)
        & ~panel["is_st"]
        & ~symbol.str.startswith(("68", "9"))
        & (panel["listed_days"] >= 365)
    )

    eligible = panel["l1_pass"] & panel["industry_code"].notna()
    ranked = panel.loc[
        eligible,
        ["trade_date", "industry_code", "turnover_rate", "amount"],
    ].copy()
    ranked["industry_count"] = ranked.groupby(
        ["trade_date", "industry_code"], sort=False
    )["amount"].transform("size")
    ranked["top_count"] = np.ceil(0.30 * ranked["industry_count"]).astype(int)
    ranked["turnover_rank"] = ranked.groupby(
        ["trade_date", "industry_code"], sort=False
    )["turnover_rate"].rank(method="min", ascending=False)
    ranked["amount_rank"] = ranked.groupby(
        ["trade_date", "industry_code"], sort=False
    )["amount"].rank(method="min", ascending=False)
    panel["l2_pass"] = False
    panel.loc[ranked.index, "l2_pass"] = (
        (ranked["turnover_rank"] <= ranked["top_count"])
        | (ranked["amount_rank"] <= ranked["top_count"])
    )
    panel["l3_day1"] = (
        (panel["qf"] >= panel["ma5"])
        & (panel["ma5"] >= panel["ma20"])
        & (panel["ma20"] >= panel["ma60"])
    )
    panel["l3_follow"] = (
        (panel["ma20"] > panel["ma60"])
        & ((panel["qf"] > panel["ma20"]) | (panel["ma5"] > panel["ma20"]))
    )
    panel["r60_pass"] = panel["ma60"] >= 0.999 * panel["prev_ma60"]
    panel["l45_trigger"] = panel["ma5"] < panel["ma20"]
    panel["l67_trigger"] = (
        (
            ((panel["qf"] > panel["ma5"]) & (panel["ma5"] > panel["ma20"]))
            | ((panel["qf"] > panel["ma20"]) & (panel["ma20"] > panel["ma5"]))
        )
        & (panel["ma5"] > panel["prev_ma5"])
        & (panel["ma20"] >= 0.999 * panel["prev_ma20"])
        & (panel["amount"] >= 1.5 * panel["prev5_amount_mean"])
    )
    panel["session_rank"] = pd.factorize(panel["trade_date"], sort=True)[0]
    return panel, source_manifest


def simulate_prepared_panel(
    panel: pd.DataFrame,
    *,
    retroactive_l1_l2_gate: bool,
    version_label: str,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Simulate L1-L7 with one unique layer per stock at each close."""
    events: list[dict[str, object]] = []
    diagnostics = {
        "retroactive_gate_ejections_l3_l6": 0,
        "r60_ejections_l4_l6": 0,
        "l3_chain_breaks": 0,
        "l6_expirations": 0,
    }
    event_columns = [
        "ts_code",
        "name",
        "trade_date",
        "session_rank",
        "industry_code",
        "industry_name",
        "l1_pass",
        "l2_pass",
        "qf",
        "qf_open",
        "ma5",
        "ma20",
        "ma60",
        "prev_ma5",
        "prev_ma20",
        "prev_ma60",
        "amount",
        "prev5_amount_mean",
        "turnover_rate",
        "market_cap_billion",
        "pe",
    ]
    for _, stock_rows in panel.groupby("ts_code", sort=False):
        layer = 0
        l3_count = 0
        dwell5 = 0
        dwell6 = 0
        for row in stock_rows.itertuples(index=False):
            if layer == 0:
                if row.l1_pass:
                    layer = 1
                    if row.l2_pass:
                        layer = 2
                        if row.l3_day1:
                            layer = 3
                            l3_count = 1
                continue
            if layer == 1:
                if not row.l1_pass:
                    layer = 0
                elif row.l2_pass:
                    layer = 2
                    if row.l3_day1:
                        layer = 3
                        l3_count = 1
                continue
            if layer == 2:
                if not row.l2_pass:
                    layer = 0
                elif row.l3_day1:
                    layer = 3
                    l3_count = 1
                continue

            if retroactive_l1_l2_gate and not row.l2_pass:
                diagnostics["retroactive_gate_ejections_l3_l6"] += 1
                layer = 0
                l3_count = 0
                dwell5 = 0
                dwell6 = 0
                continue

            if layer == 3:
                if not row.l3_follow:
                    diagnostics["l3_chain_breaks"] += 1
                    layer = 0
                    l3_count = 0
                    continue
                l3_count += 1
                if l3_count < 10:
                    continue
                layer = 4
                if not row.r60_pass:
                    diagnostics["r60_ejections_l4_l6"] += 1
                    layer = 0
                    l3_count = 0
                elif row.l45_trigger:
                    layer = 5
                    dwell5 = 1
                continue

            if layer == 4:
                if not row.r60_pass:
                    diagnostics["r60_ejections_l4_l6"] += 1
                    layer = 0
                elif row.l45_trigger:
                    layer = 5
                    dwell5 = 1
                continue

            if layer == 5:
                if not row.r60_pass:
                    diagnostics["r60_ejections_l4_l6"] += 1
                    layer = 0
                    dwell5 = 0
                else:
                    dwell5 += 1
                    if dwell5 >= 2:
                        layer = 6
                        dwell6 = 1
                continue

            if layer == 6:
                if not row.r60_pass:
                    diagnostics["r60_ejections_l4_l6"] += 1
                    layer = 0
                    dwell6 = 0
                    continue
                if dwell6 >= 20:
                    diagnostics["l6_expirations"] += 1
                    layer = 0
                    dwell6 = 0
                    continue
                dwell6 += 1
                if row.l67_trigger:
                    if row.trade_date >= ANALYSIS_START_DATE:
                        row_dict = row._asdict()
                        event = {column: row_dict[column] for column in event_columns}
                        event["version"] = version_label
                        event["dwell6"] = dwell6
                        event["amount_ratio"] = (
                            row.amount / row.prev5_amount_mean
                            if row.prev5_amount_mean and not pd.isna(row.prev5_amount_mean)
                            else np.nan
                        )
                        event["r60"] = (
                            row.ma60 / row.prev_ma60
                            if row.prev_ma60 and not pd.isna(row.prev_ma60)
                            else np.nan
                        )
                        events.append(event)
                    layer = 0
                    dwell6 = 0
    return pd.DataFrame(events), diagnostics


def _add_forward_returns(panel: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    grouped = panel.groupby("ts_code", sort=False)
    return_columns = ["ts_code", "trade_date"]
    for horizon in range(1, MAX_HORIZON + 1):
        panel[f"future_close_{horizon}"] = grouped["qf"].shift(-horizon)
        panel[f"future_open_1"] = grouped["qf_open"].shift(-1)
        panel[f"research_return_{horizon}"] = (
            panel[f"future_close_{horizon}"] / panel["qf"] - 1
        )
        panel[f"tradable_return_{horizon}"] = (
            panel[f"future_close_{horizon}"] / panel["future_open_1"] - 1
        )
        return_columns.extend(
            [
                f"future_close_{horizon}",
                f"research_return_{horizon}",
                f"tradable_return_{horizon}",
            ]
        )
        research_benchmark = panel.groupby("trade_date", sort=False)[
            f"research_return_{horizon}"
        ].mean()
        tradable_benchmark = panel.groupby("trade_date", sort=False)[
            f"tradable_return_{horizon}"
        ].mean()
        panel[f"benchmark_return_{horizon}"] = panel["trade_date"].map(
            research_benchmark
        )
        panel[f"benchmark_tradable_return_{horizon}"] = panel["trade_date"].map(
            tradable_benchmark
        )
        return_columns.extend(
            [
                f"benchmark_return_{horizon}",
                f"benchmark_tradable_return_{horizon}",
            ]
        )
    returns = panel[return_columns]
    enriched = events.merge(
        returns,
        on=["ts_code", "trade_date"],
        how="left",
        validate="many_to_one",
    )
    for horizon in range(1, MAX_HORIZON + 1):
        enriched[f"excess_return_{horizon}"] = (
            enriched[f"research_return_{horizon}"]
            - enriched[f"benchmark_return_{horizon}"]
        )
        enriched[f"tradable_excess_return_{horizon}"] = (
            enriched[f"tradable_return_{horizon}"]
            - enriched[f"benchmark_tradable_return_{horizon}"]
        )
    return enriched


def _mark_keep_first(events: pd.DataFrame) -> pd.Series:
    keep = pd.Series(False, index=events.index)
    for _, group in events.sort_values("session_rank").groupby(
        ["version", "ts_code"], sort=False
    ):
        last_kept_rank: int | None = None
        for index, rank in zip(group.index, group["session_rank"], strict=True):
            if last_kept_rank is None or int(rank) - last_kept_rank > MAX_HORIZON:
                keep.at[index] = True
                last_kept_rank = int(rank)
    return keep


def _cluster_bootstrap_interval(
    sample: pd.DataFrame,
    value_column: str,
    statistic: str,
) -> tuple[float, float]:
    clean = sample[["trade_date", value_column]].dropna()
    if clean.empty:
        return np.nan, np.nan
    by_date = {
        trade_date: group[value_column].to_numpy(dtype=float)
        for trade_date, group in clean.groupby("trade_date", sort=False)
    }
    dates = np.array(list(by_date))
    if len(dates) < 2:
        return np.nan, np.nan
    random = np.random.default_rng(BOOTSTRAP_SEED)
    estimates = np.empty(BOOTSTRAP_REPETITIONS, dtype=float)
    for repetition in range(BOOTSTRAP_REPETITIONS):
        sampled_dates = random.choice(dates, size=len(dates), replace=True)
        values = np.concatenate([by_date[trade_date] for trade_date in sampled_dates])
        estimates[repetition] = (
            np.mean(values > 0) if statistic == "hit_rate" else np.mean(values)
        )
    lower, upper = np.quantile(estimates, [0.025, 0.975])
    return float(lower), float(upper)


def summarize_events(events: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    incremental_keys = set(
        events.loc[events["is_incremental"], ["ts_code", "trade_date"]]
        .itertuples(index=False, name=None)
    )
    sample_definitions = {
        "v2.24": events["version"].eq("v2.24"),
        "retroactive_counterfactual": events["version"].eq(
            "retroactive_counterfactual"
        ),
        "v2.24_incremental": events.apply(
            lambda row: row["version"] == "v2.24"
            and (row["ts_code"], row["trade_date"]) in incremental_keys,
            axis=1,
        ),
    }
    for group_name, group_mask in sample_definitions.items():
        group_events = events.loc[group_mask]
        for sample_type, sample in (
            ("raw", group_events),
            ("keep_first_10d", group_events[group_events["keep_first_10d"]]),
        ):
            for entry_basis, prefix in (
                ("signal_close", "research_return"),
                ("next_open", "tradable_return"),
            ):
                for horizon in range(1, MAX_HORIZON + 1):
                    value_column = f"{prefix}_{horizon}"
                    matured = sample[sample[value_column].notna()]
                    values = matured[value_column]
                    hit_low, hit_high = _cluster_bootstrap_interval(
                        matured, value_column, "hit_rate"
                    )
                    mean_low, mean_high = _cluster_bootstrap_interval(
                        matured, value_column, "mean"
                    )
                    sorted_values = np.sort(values.to_numpy(dtype=float))
                    trim_count = int(len(sorted_values) * 0.05)
                    trimmed_values = (
                        sorted_values[trim_count:-trim_count]
                        if trim_count > 0
                        else sorted_values
                    )
                    excess_column = (
                        f"excess_return_{horizon}"
                        if entry_basis == "signal_close"
                        else f"tradable_excess_return_{horizon}"
                    )
                    rows.append(
                        {
                            "group": group_name,
                            "sample_type": sample_type,
                            "entry_basis": entry_basis,
                            "horizon": horizon,
                            "event_count": int(len(matured)),
                            "security_count": int(matured["ts_code"].nunique()),
                            "signal_date_count": int(matured["trade_date"].nunique()),
                            "hit_rate": float((values > 0).mean()) if len(values) else np.nan,
                            "mean_return": float(values.mean()) if len(values) else np.nan,
                            "median_return": float(values.median()) if len(values) else np.nan,
                            "trimmed_mean_5pct": float(trimmed_values.mean())
                            if len(trimmed_values)
                            else np.nan,
                            "average_positive_return": float(values[values > 0].mean())
                            if (values > 0).any()
                            else np.nan,
                            "average_negative_return": float(values[values < 0].mean())
                            if (values < 0).any()
                            else np.nan,
                            "p25_return": float(values.quantile(0.25)) if len(values) else np.nan,
                            "p75_return": float(values.quantile(0.75)) if len(values) else np.nan,
                            "minimum_return": float(values.min()) if len(values) else np.nan,
                            "maximum_return": float(values.max()) if len(values) else np.nan,
                            "mean_excess_return": float(
                                matured[excess_column].mean()
                            )
                            if len(matured)
                            else np.nan,
                            "hit_rate_ci_low": hit_low,
                            "hit_rate_ci_high": hit_high,
                            "mean_return_ci_low": mean_low,
                            "mean_return_ci_high": mean_high,
                        }
                    )
    return pd.DataFrame(rows)


def _bootstrap_difference(
    left: pd.DataFrame,
    right: pd.DataFrame,
    value_column: str,
    statistic: str,
) -> tuple[float, float]:
    left_clean = left[["trade_date", value_column]].dropna()
    right_clean = right[["trade_date", value_column]].dropna()
    if left_clean.empty or right_clean.empty:
        return np.nan, np.nan
    left_by_date = {
        trade_date: group[value_column].to_numpy(dtype=float)
        for trade_date, group in left_clean.groupby("trade_date", sort=False)
    }
    right_by_date = {
        trade_date: group[value_column].to_numpy(dtype=float)
        for trade_date, group in right_clean.groupby("trade_date", sort=False)
    }
    left_dates = np.array(list(left_by_date))
    right_dates = np.array(list(right_by_date))
    random = np.random.default_rng(BOOTSTRAP_SEED)
    estimates = np.empty(BOOTSTRAP_REPETITIONS, dtype=float)
    for repetition in range(BOOTSTRAP_REPETITIONS):
        sampled_left = random.choice(left_dates, size=len(left_dates), replace=True)
        sampled_right = random.choice(right_dates, size=len(right_dates), replace=True)
        left_values = np.concatenate([left_by_date[date] for date in sampled_left])
        right_values = np.concatenate([right_by_date[date] for date in sampled_right])
        if statistic == "hit_rate":
            estimates[repetition] = np.mean(left_values > 0) - np.mean(
                right_values > 0
            )
        else:
            estimates[repetition] = np.mean(left_values) - np.mean(right_values)
    lower, upper = np.quantile(estimates, [0.025, 0.975])
    return float(lower), float(upper)


def compare_rule_effect(events: pd.DataFrame) -> pd.DataFrame:
    deduplicated = events[events["keep_first_10d"]].copy()
    canonical = deduplicated[deduplicated["version"] == "v2.24"]
    counterfactual = deduplicated[
        deduplicated["version"] == "retroactive_counterfactual"
    ]
    incremental = canonical[canonical["is_incremental"]]
    rows: list[dict[str, object]] = []
    for comparison_name, left in (
        ("v2.24_minus_counterfactual", canonical),
        ("incremental_minus_counterfactual", incremental),
    ):
        for entry_basis, prefix in (
            ("signal_close", "research_return"),
            ("next_open", "tradable_return"),
        ):
            for horizon in range(1, MAX_HORIZON + 1):
                value_column = f"{prefix}_{horizon}"
                left_values = left[value_column].dropna()
                right_values = counterfactual[value_column].dropna()
                hit_low, hit_high = _bootstrap_difference(
                    left, counterfactual, value_column, "hit_rate"
                )
                mean_low, mean_high = _bootstrap_difference(
                    left, counterfactual, value_column, "mean"
                )
                rows.append(
                    {
                        "comparison": comparison_name,
                        "entry_basis": entry_basis,
                        "horizon": horizon,
                        "left_event_count": int(len(left_values)),
                        "right_event_count": int(len(right_values)),
                        "hit_rate_difference": float((left_values > 0).mean())
                        - float((right_values > 0).mean()),
                        "mean_return_difference": float(left_values.mean())
                        - float(right_values.mean()),
                        "hit_rate_difference_ci_low": hit_low,
                        "hit_rate_difference_ci_high": hit_high,
                        "mean_return_difference_ci_low": mean_low,
                        "mean_return_difference_ci_high": mean_high,
                    }
                )
    return pd.DataFrame(rows)


def _yearly_summary(events: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    deduplicated = events[events["keep_first_10d"]].copy()
    deduplicated["signal_year"] = deduplicated["trade_date"].str[:4]
    for (version, year), sample in deduplicated.groupby(
        ["version", "signal_year"], sort=True
    ):
        for horizon in (5, 10):
            values = sample[f"research_return_{horizon}"].dropna()
            rows.append(
                {
                    "version": version,
                    "signal_year": year,
                    "horizon": horizon,
                    "event_count": int(len(values)),
                    "hit_rate": float((values > 0).mean()) if len(values) else np.nan,
                    "mean_return": float(values.mean()) if len(values) else np.nan,
                    "median_return": float(values.median()) if len(values) else np.nan,
                }
            )
    return pd.DataFrame(rows)


def _qa_report(
    panel: pd.DataFrame,
    events: pd.DataFrame,
    source_manifest: dict[str, object],
    diagnostics: dict[str, dict[str, int]],
) -> dict[str, object]:
    analysis_panel = panel[panel["trade_date"] >= ANALYSIS_START_DATE]
    latest_date = analysis_panel["trade_date"].max()
    latest = analysis_panel[analysis_panel["trade_date"] == latest_date]
    monthly_quality = (
        analysis_panel.assign(month=analysis_panel["trade_date"].str[:6])
        .groupby("month", sort=True)
        .agg(
            daily_basic_complete_rate=(
                "total_mv",
                lambda values: float(values.notna().mean()),
            ),
            industry_mapping_rate=(
                "industry_code",
                lambda values: float(values.notna().mean()),
            ),
        )
    )
    canonical = events[
        (events["version"] == "v2.24") & events["keep_first_10d"]
    ].copy()
    matured = canonical[canonical["research_return_10"].notna()]
    positive = matured[matured["research_return_10"] > 0].iloc[0]
    negative = matured[matured["research_return_10"] < 0].iloc[0]
    boundary = canonical.loc[(canonical["amount_ratio"] - 1.5).abs().idxmin()]

    def return_check(row: pd.Series) -> dict[str, object]:
        recalculated = row["future_close_10"] / row["qf"] - 1
        return {
            "ts_code": row["ts_code"],
            "trade_date": row["trade_date"],
            "qf_signal_close": float(row["qf"]),
            "qf_future_close_10": float(row["future_close_10"]),
            "reported_return_10": float(row["research_return_10"]),
            "recalculated_return_10": float(recalculated),
            "absolute_difference": float(abs(recalculated - row["research_return_10"])),
        }

    return {
        "analysis_start_date": ANALYSIS_START_DATE,
        "analysis_end_date": latest_date,
        "panel_rows": int(len(analysis_panel)),
        "panel_security_count": int(analysis_panel["ts_code"].nunique()),
        "duplicate_panel_keys": int(
            analysis_panel.duplicated(["ts_code", "trade_date"]).sum()
        ),
        "latest_daily_security_count": int(len(latest)),
        "latest_daily_basic_complete_rate": float(
            latest[["total_mv", "turnover_rate"]].notna().all(axis=1).mean()
        ),
        "latest_industry_mapping_rate": float(latest["industry_code"].notna().mean()),
        "minimum_monthly_daily_basic_complete_rate": float(
            monthly_quality["daily_basic_complete_rate"].min()
        ),
        "minimum_monthly_industry_mapping_rate": float(
            monthly_quality["industry_mapping_rate"].min()
        ),
        "l1_industry_mapping_rate": float(
            analysis_panel.loc[analysis_panel["l1_pass"], "industry_code"].notna().mean()
        ),
        "event_rows": int(len(events)),
        "event_duplicate_keys": int(
            events.duplicated(["version", "ts_code", "trade_date"]).sum()
        ),
        "spot_checks": {
            "positive_return": return_check(positive),
            "negative_return": return_check(negative),
            "amount_boundary": {
                "ts_code": boundary["ts_code"],
                "trade_date": boundary["trade_date"],
                "amount": float(boundary["amount"]),
                "prior_five_day_mean_amount": float(boundary["prev5_amount_mean"]),
                "reported_ratio": float(boundary["amount_ratio"]),
                "recalculated_ratio": float(
                    boundary["amount"] / boundary["prev5_amount_mean"]
                ),
                "distance_from_threshold": float(boundary["amount_ratio"] - 1.5),
            },
        },
        "source_manifest": source_manifest,
        "diagnostics": diagnostics,
        "methodology_caveats": [
            "Current THS industry membership is applied historically because ths_member lacks effective dates.",
            "The study evaluates L7 signals; L10 strategy returns are unavailable until sell and holding rules are defined.",
            "The retroactive comparison changes only the L1/L2 back-check and is not labeled as a complete v2.23 reconstruction.",
            "Signal-close returns are research diagnostics, not executable fills; next-open returns are reported separately.",
        ],
    }


def run() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    panel, source_manifest = load_panel()
    LOGGER.info(
        "prepared_panel rows=%s securities=%s dates=%s..%s",
        len(panel),
        panel["ts_code"].nunique(),
        panel["trade_date"].min(),
        panel["trade_date"].max(),
    )
    canonical, canonical_diagnostics = simulate_prepared_panel(
        panel,
        retroactive_l1_l2_gate=False,
        version_label="v2.24",
    )
    counterfactual, counterfactual_diagnostics = simulate_prepared_panel(
        panel,
        retroactive_l1_l2_gate=True,
        version_label="retroactive_counterfactual",
    )
    events = pd.concat([canonical, counterfactual], ignore_index=True)
    counterfactual_keys = set(
        counterfactual[["ts_code", "trade_date"]].itertuples(index=False, name=None)
    )
    events["is_incremental"] = events.apply(
        lambda row: row["version"] == "v2.24"
        and (row["ts_code"], row["trade_date"]) not in counterfactual_keys,
        axis=1,
    )
    events = _add_forward_returns(panel, events)
    events["keep_first_10d"] = _mark_keep_first(events)
    events = events.sort_values(["trade_date", "ts_code", "version"]).reset_index(drop=True)
    summary = summarize_events(events)
    comparison = compare_rule_effect(events)
    yearly = _yearly_summary(events)
    diagnostics = {
        "v2.24": canonical_diagnostics,
        "retroactive_counterfactual": counterfactual_diagnostics,
    }
    qa = _qa_report(panel, events, source_manifest, diagnostics)
    events.to_parquet(OUTPUT_DIR / "events.parquet", index=False)
    events.to_csv(OUTPUT_DIR / "events.csv", index=False)
    summary.to_csv(OUTPUT_DIR / "summary.csv", index=False)
    comparison.to_csv(OUTPUT_DIR / "comparison.csv", index=False)
    yearly.to_csv(OUTPUT_DIR / "yearly.csv", index=False)
    events.to_json(OUTPUT_DIR / "events.json", orient="records")
    summary.to_json(OUTPUT_DIR / "summary.json", orient="records")
    comparison.to_json(OUTPUT_DIR / "comparison.json", orient="records")
    yearly.to_json(OUTPUT_DIR / "yearly.json", orient="records")
    (OUTPUT_DIR / "qa.json").write_text(
        json.dumps(qa, ensure_ascii=True, indent=2), encoding="utf-8"
    )
    LOGGER.info(
        "backtest_complete canonical_events=%s counterfactual_events=%s incremental_events=%s",
        len(canonical),
        len(counterfactual),
        int(events["is_incremental"].sum()),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()

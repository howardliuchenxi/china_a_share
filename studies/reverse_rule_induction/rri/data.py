"""Load tushare month shards into the research panel (universe-filtered)."""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

from rri.limits import board_of, st_flag_from_names

SHARDS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "shards")

B_SHARE_PREFIXES = ("900", "200")


def _read_table(name: str) -> pd.DataFrame:
    # exact shard match: base name must be exactly f"{name}_{YYYYMM}" so that
    # "daily" does not swallow "daily_basic" shards
    prefix = name.split("_")
    paths = []
    for p in sorted(glob.glob(os.path.join(SHARDS_DIR, f"{name}_*.parquet"))):
        segs = os.path.basename(p)[: -len(".parquet")].split("_")
        if len(segs) == len(prefix) + 1 and segs[: len(prefix)] == prefix:
            paths.append(p)
    frames = [pd.read_parquet(p) for p in paths]
    if not frames:
        raise FileNotFoundError(f"no shards for {name} under {SHARDS_DIR}")
    return pd.concat(frames, ignore_index=True)


def load_reference() -> tuple[pd.DataFrame, pd.DataFrame | None, pd.DataFrame | None]:
    basic = pd.read_parquet(os.path.join(SHARDS_DIR, "stock_basic.parquet"))
    nc_path = os.path.join(SHARDS_DIR, "namechange.parquet")
    nc = pd.read_parquet(nc_path) if os.path.exists(nc_path) else None
    st_path = os.path.join(SHARDS_DIR, "stock_st.parquet")
    st = pd.read_parquet(st_path) if os.path.exists(st_path) else None
    return basic, nc, st


def universe_codes(basic: pd.DataFrame) -> set[str]:
    """SH/SZ A-shares only: drop Beijing Exchange and B-shares."""
    b = basic[basic["ts_code"].str.endswith((".SH", ".SZ"))].copy()
    b = b[~b["symbol"].astype(str).str.startswith(B_SHARE_PREFIXES)]
    return set(b["ts_code"].astype(str))


def load_panel() -> tuple[pd.DataFrame, pd.DataFrame, np.ndarray]:
    """Merge daily + adj_factor + daily_basic over the filtered universe.

    Returns (panel, basic, trade_dates) where panel has one row per
    (ts_code, trade_date) with session_rank (global trading-day index) and
    point-in-time board/ST columns attached.
    """
    basic, namechange, _ = load_reference()
    codes = universe_codes(basic)

    daily = _read_table("daily")
    daily = daily[daily["ts_code"].isin(codes)].copy()
    adj = _read_table("adj_factor")
    adj = adj[adj["ts_code"].isin(codes)][["ts_code", "trade_date", "adj_factor"]]
    panel = daily.merge(adj, on=["ts_code", "trade_date"], how="left", validate="one_to_one")
    try:
        dbasic = _read_table("daily_basic")
    except FileNotFoundError:
        dbasic = pd.DataFrame(columns=["ts_code", "trade_date"])
    dbasic = dbasic[dbasic["ts_code"].isin(codes)]
    keep_basic = [c for c in (
        "ts_code", "trade_date", "turnover_rate", "turnover_rate_f", "volume_ratio",
        "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "dv_ttm", "total_share",
        "float_share", "free_share", "total_mv", "circ_mv") if c in dbasic.columns]
    panel = panel.merge(dbasic[keep_basic], on=["ts_code", "trade_date"],
                        how="left", validate="one_to_one")

    cal = pd.read_parquet(os.path.join(SHARDS_DIR, "trade_cal.parquet"))
    cal = cal[cal["is_open"] == 1].sort_values("cal_date")
    dates = cal["cal_date"].astype(str).to_numpy()
    date_rank = {d: i for i, d in enumerate(dates)}
    panel["trade_date"] = panel["trade_date"].astype(str)
    panel = panel[panel["trade_date"].isin(date_rank)].copy()
    panel["session_rank"] = panel["trade_date"].map(date_rank).astype(np.int64)
    panel = panel.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    panel["board"] = board_of(panel["ts_code"])
    basic_name = basic.set_index(basic["ts_code"].astype(str))["name"].astype(str)
    panel["is_st"] = st_flag_from_names(
        namechange, basic_name, panel["ts_code"], panel["trade_date"].to_numpy(dtype=np.int64))
    for col in ("open", "high", "low", "close", "pre_close", "vol", "amount",
                "adj_factor", "turnover_rate", "turnover_rate_f", "volume_ratio",
                "total_mv", "circ_mv", "pe_ttm", "pb", "dv_ttm"):
        if col in panel.columns:
            panel[col] = pd.to_numeric(panel[col], errors="coerce")
    return panel, basic, dates

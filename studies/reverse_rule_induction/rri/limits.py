"""Exchange-exact limit-price math and board/ST classification.

Exchange limit prices are rounded half-up to 0.01 CNY in decimal arithmetic.
Float multiplication misrounds half-cent boundaries (6.05 * 1.1 = 6.655 -> 6.66),
so all price math is done in integer cents: exact and vectorizable.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# basis-point style multipliers: pct_bp = 1000 means +10.0%
PCT_BP_MAIN = 1000
PCT_BP_STAR = 2000
PCT_BP_CHINEXT = 2000
CHINEXT_20PCT_FROM = 20200824


def yuan_to_cents(prices: np.ndarray) -> np.ndarray:
    """2-dp prices -> exact integer cents (round-half-up safe at 1e-9)."""
    return np.round(np.asarray(prices, dtype=float) * 100.0).astype(np.int64)


def limit_up_cents(pre_close_cents: np.ndarray, pct_bp: np.ndarray) -> np.ndarray:
    """Exchange limit price in cents: round_half_up(P * (1 + bp/10000))."""
    p = np.asarray(pre_close_cents, dtype=np.int64)
    bp = np.asarray(pct_bp, dtype=np.int64)
    return (p * (10_000 + bp) + 5_000) // 10_000


def board_of(ts_code: pd.Series) -> pd.Series:
    """'star' (688/689 SH), 'chinext' (300/301/302 SZ), else 'main'."""
    code = ts_code.astype(str)
    prefix = code.str.split(".").str[0]
    market = code.str.split(".").str[1]
    is_star = market.eq("SH") & prefix.str.startswith(("688", "689"))
    is_chinext = market.eq("SZ") & prefix.str.startswith(("300", "301", "302"))
    return np.where(is_star, "star", np.where(is_chinext, "chinext", "main"))


def limit_pct_bp(
    boards: np.ndarray,
    trade_dates: np.ndarray,
    is_st: np.ndarray,
) -> np.ndarray:
    """Per-row limit percentage in basis points, exchange rules by board/date/ST."""
    boards = np.asarray(boards)
    trade_dates = np.asarray(trade_dates, dtype=np.int64)
    is_st = np.asarray(is_st, dtype=bool)
    bp = np.full(len(boards), PCT_BP_MAIN, dtype=np.int64)
    bp[boards == "star"] = PCT_BP_STAR
    chinext = boards == "chinext"
    bp[chinext & (trade_dates >= CHINEXT_20PCT_FROM)] = PCT_BP_CHINEXT
    # ST/S-share 5% applies on main board only; ChiNext/STAR keep 20% when ST.
    bp[(boards == "main") & is_st] = 500
    return bp


def st_flag_from_names(namechange: pd.DataFrame | None,
                       stock_basic_name: pd.Series,
                       ts_codes: pd.Series,
                       trade_dates: np.ndarray) -> np.ndarray:
    """Point-in-time ST/S flag: name at date contains 'ST' or starts with 'S'.

    namechange rows give (ts_code, name, start_date, end_date); end_date NaN =
    open-ended. Falls back to the current stock_basic name for stocks without
    any recorded change.
    """
    ts_codes = ts_codes.astype(str)
    flags = np.zeros(len(ts_codes), dtype=bool)
    if namechange is not None and len(namechange):
        nc = namechange.copy()
        nc["ts_code"] = nc["ts_code"].astype(str)
        nc["start_date"] = nc["start_date"].astype(str)
        nc["end_date"] = nc["end_date"].fillna("99999999").astype(str)
        by_code: dict[str, list[tuple[str, str, str]]] = {}
        for code, name, sd, ed in zip(nc["ts_code"], nc["name"],
                                      nc["start_date"], nc["end_date"]):
            by_code.setdefault(code, []).append((sd, ed, str(name)))
        codes = ts_codes.to_numpy()
        dates = np.asarray(trade_dates, dtype=np.int64)
        for i, (code, d) in enumerate(zip(codes, dates)):
            segs = by_code.get(code)
            name = stock_basic_name.get(code, "") if segs is None else None
            if segs is not None:
                name = ""
                for sd, ed, nm in segs:
                    if int(sd) <= d < int(ed):
                        name = nm
                        break
            flags[i] = "ST" in name or name.startswith("S")
    return flags

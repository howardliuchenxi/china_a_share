"""RRI-2 S6b: anchor-day factors from dragon-tiger list and moneyflow.

All factors use only anchor-day (post-close published) and prior data:
same-day turnover / top_list / moneyflow are treated as anchor-knowable,
consistent with phase-1 usage of same-day turnover_rate. This disclosure
must be echoed in the report's limitations section.

Units: moneyflow amounts are 万元 (tushare), top_list/top_inst money is 元,
daily amount is 千元, circ_mv is 万元. Ratio factors are emitted as raw
money values and normalized against the event day's amount by
normalize_by_event_amount().
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MF_FACTORS = [
    "mf_net_ratio",             # net moneyflow / daily amount
    "mf_main_net_ratio",        # (lg+elg net) / daily amount
    "mf_elg_net_ratio",         # extra-large-order net / daily amount
    "mf_main_net_5d_sum",       # 5-session main-net sum, 万元 (ratio merged later)
    "mf_main_net_z20",          # today vs prior-20 mean/std of main net
]

TL_FACTORS = [
    "on_top_list",
    "tl_net_amount_ratio",      # raw 元, normalized later
    "tl_amount_rate_max",
    "top_list_count_20d",
    "inst_net_buy_ratio",       # raw 元, normalized later
]

NEW_FACTORS = MF_FACTORS + TL_FACTORS


def _main_net(mf: pd.DataFrame) -> np.ndarray:
    lg = mf["buy_lg_amount"].fillna(0.0) - mf["sell_lg_amount"].fillna(0.0)
    elg = mf["buy_elg_amount"].fillna(0.0) - mf["sell_elg_amount"].fillna(0.0)
    return (lg + elg).to_numpy(dtype=float)


def moneyflow_factors(
    mf: pd.DataFrame,
    date_rank: dict[str, int],
    events: pd.DataFrame,
) -> pd.DataFrame:
    """Moneyflow factors at event rows from the full per-(code,date) history."""
    m = mf.copy()
    m["trade_date"] = m["trade_date"].astype(str)
    m["main_net"] = _main_net(m)
    m["elg_net"] = (m["buy_elg_amount"].fillna(0.0)
                    - m["sell_elg_amount"].fillna(0.0))
    m = m.sort_values(["ts_code", "trade_date"])

    ev_by_code: dict[str, list[tuple[int, str]]] = {}
    for i, (code, d) in enumerate(zip(events["ts_code"].astype(str),
                                      events["trade_date"].astype(str))):
        ev_by_code.setdefault(code, []).append((i, d))

    out = pd.DataFrame(index=pd.RangeIndex(len(events)))
    vals = {k: np.full(len(events), np.nan) for k in MF_FACTORS}

    for code, g in m.groupby("ts_code", sort=False):
        ev_rows = ev_by_code.get(code)
        if not ev_rows:
            continue
        dates = g["trade_date"].to_numpy()
        pos_of = {d: i for i, d in enumerate(dates)}
        rank = pd.Index(dates).map(date_rank) if False else \
            np.array([date_rank.get(d, -1) for d in dates], dtype=np.int64)
        main_net = g["main_net"].to_numpy(dtype=float)
        net_mf = g["net_mf_amount"].to_numpy(dtype=float)
        elg_net = g["elg_net"].to_numpy(dtype=float)
        for i, d in ev_rows:
            p = pos_of.get(d)
            if p is None:
                continue
            vals["mf_net_ratio"][i] = net_mf[p]
            vals["mf_main_net_ratio"][i] = main_net[p]
            vals["mf_elg_net_ratio"][i] = elg_net[p]
            if p >= 4 and rank[p] - rank[p - 4] == 4:
                vals["mf_main_net_5d_sum"][i] = float(main_net[p - 4:p + 1].sum())
            if p >= 20 and rank[p] - rank[p - 20] == 20:
                prior = main_net[p - 20:p]
                mu, sd = prior.mean(), prior.std(ddof=0)
                if sd > 1e-9:
                    vals["mf_main_net_z20"][i] = (main_net[p] - mu) / sd
    for k in MF_FACTORS:
        out[k] = vals[k]
    return out


def top_list_factors(
    tl: pd.DataFrame,
    ti: pd.DataFrame | None,
    date_rank: dict[str, int],
    events: pd.DataFrame,
) -> pd.DataFrame:
    """Dragon-tiger factors at event rows (top_list aggregated per code-day)."""
    out = pd.DataFrame(index=pd.RangeIndex(len(events)))
    vals = {k: np.full(len(events), np.nan) for k in TL_FACTORS}

    t = tl.copy()
    t["trade_date"] = t["trade_date"].astype(str)
    agg = t.groupby(["ts_code", "trade_date"]).agg(
        net_amount=("net_amount", "sum"), amount_rate=("amount_rate", "max")
    ).reset_index()
    agg_by_key = {(c, d): j for j, (c, d) in enumerate(zip(
        agg["ts_code"].astype(str), agg["trade_date"]))}
    on_list: dict[str, set[str]] = {}
    for c, d in zip(agg["ts_code"].astype(str), agg["trade_date"]):
        on_list.setdefault(c, set()).add(d)

    inst_by_key: dict[tuple[str, str], float] = {}
    if ti is not None and len(ti):
        ti2 = ti.copy()
        ti2["trade_date"] = ti2["trade_date"].astype(str)
        inst = ti2[ti2["exalter"].astype(str).str.contains("机构专用", na=False)]
        for (c, d), v in inst.groupby(["ts_code", "trade_date"])["net_buy"].sum().items():
            inst_by_key[(str(c), str(d))] = float(v)

    for i, (code, d) in enumerate(zip(events["ts_code"].astype(str),
                                      events["trade_date"].astype(str))):
        vals["on_top_list"][i] = 1.0 if (code, d) in agg_by_key else 0.0
        j = agg_by_key.get((code, d))
        if j is not None:
            vals["tl_net_amount_ratio"][i] = float(agg.iloc[j]["net_amount"])
            vals["tl_amount_rate_max"][i] = float(agg.iloc[j]["amount_rate"])
        dates_on = on_list.get(code)
        r0 = date_rank.get(d)
        if dates_on and r0 is not None:
            cnt = 0
            for dd in dates_on:
                rr = date_rank.get(dd)
                if rr is not None and r0 - 19 <= rr <= r0:
                    cnt += 1
            vals["top_list_count_20d"][i] = float(cnt)
        v = inst_by_key.get((code, d))
        if v is not None:
            vals["inst_net_buy_ratio"][i] = v
    for k in TL_FACTORS:
        out[k] = vals[k]
    return out


def normalize_by_event_amount(
    factors: pd.DataFrame,
    events_amount: np.ndarray,
) -> pd.DataFrame:
    """Normalize raw money values into ratios against the event day's amount.

    daily amount (tushare) is 千元 -> 元 = amount*1e3; moneyflow 万元 -> 元
    = value*1e4; top_list/top_inst values are already 元.
    """
    out = factors.copy()
    amt_yuan = np.asarray(events_amount, dtype=float) * 1e3
    ok = np.isfinite(amt_yuan) & (amt_yuan > 0)
    for col in ("mf_net_ratio", "mf_main_net_ratio", "mf_elg_net_ratio"):
        v = out[col].to_numpy(dtype=float) * 1e4
        out[col] = np.where(ok, v / amt_yuan, np.nan)
    for col in ("tl_net_amount_ratio", "inst_net_buy_ratio"):
        v = out[col].to_numpy(dtype=float)
        out[col] = np.where(ok, v / amt_yuan, np.nan)
    return out

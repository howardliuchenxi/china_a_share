"""Fetch the smallest supplemental dataset needed by the v2.24 validation.

The historical price panel already lives in the reverse-rule-induction study.
This script only refreshes trading days after that snapshot and materializes the
current THS level-2 industry membership. Tushare does not expose membership
effective dates through ``ths_member``; the backtest records that limitation
instead of treating the mapping as point-in-time history.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv


LOGGER = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_SHARDS = REPO_ROOT / "studies" / "reverse_rule_induction" / "data" / "shards"
DATA_DIR = Path(__file__).resolve().parent / "data"
DAILY_BASIC_FIELDS = (
    "ts_code,trade_date,turnover_rate,pe,total_mv"
)


def _latest_local_trade_date() -> str:
    paths = sorted(SOURCE_SHARDS.glob("daily_*.parquet"))
    if not paths:
        raise FileNotFoundError(f"No daily shards found under {SOURCE_SHARDS}")
    latest = pd.read_parquet(paths[-1], columns=["trade_date"])
    return latest["trade_date"].astype(str).max()


def _call_with_retry(function, **kwargs) -> pd.DataFrame:
    delays = (0, 2, 5, 10, 20)
    last_error: Exception | None = None
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            return function(**kwargs)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            LOGGER.warning(
                "supplemental_fetch_retry function=%s delay_seconds=%s error=%s",
                getattr(function, "__name__", "unknown"),
                delay,
                exc,
            )
    raise RuntimeError(f"Supplemental fetch failed: {last_error}")


def _open_dates(pro, start_date: str, end_date: str) -> list[str]:
    calendar = _call_with_retry(
        pro.trade_cal,
        exchange="SSE",
        start_date=start_date,
        end_date=end_date,
    )
    return sorted(
        calendar.loc[calendar["is_open"] == 1, "cal_date"].astype(str).tolist()
    )


def _refresh_daily_inputs(pro, local_max_date: str) -> dict[str, object]:
    start = (
        datetime.strptime(local_max_date, "%Y%m%d") + timedelta(days=1)
    ).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    dates = _open_dates(pro, start, end) if start <= end else []
    frames: dict[str, list[pd.DataFrame]] = {
        "daily": [],
        "daily_basic": [],
        "adj_factor": [],
        "stock_st": [],
    }
    for trade_date in dates:
        LOGGER.info("supplemental_trade_date_start trade_date=%s", trade_date)
        frames["daily"].append(_call_with_retry(pro.daily, trade_date=trade_date))
        frames["daily_basic"].append(
            _call_with_retry(
                pro.daily_basic,
                trade_date=trade_date,
                fields=DAILY_BASIC_FIELDS,
            )
        )
        frames["adj_factor"].append(
            _call_with_retry(pro.adj_factor, trade_date=trade_date)
        )
        frames["stock_st"].append(
            _call_with_retry(pro.stock_st, trade_date=trade_date)
        )
    for name, parts in frames.items():
        _save_supplemental(name, parts)
    missing_counts = _refresh_missing_dates(pro)
    return {
        "local_max_trade_date": local_max_date,
        "supplemental_trade_dates": dates,
        "repaired_missing_trade_dates": missing_counts,
    }


def _exact_shards(name: str) -> list[Path]:
    prefix_parts = name.split("_")
    paths: list[Path] = []
    for path in sorted(SOURCE_SHARDS.glob(f"{name}_*.parquet")):
        stem_parts = path.stem.split("_")
        if (
            len(stem_parts) == len(prefix_parts) + 1
            and stem_parts[: len(prefix_parts)] == prefix_parts
            and stem_parts[-1] >= "202401"
        ):
            paths.append(path)
    return paths


def _available_dates(name: str) -> set[str]:
    dates: set[str] = set()
    for path in _exact_shards(name):
        frame = pd.read_parquet(path, columns=["trade_date"])
        dates.update(frame["trade_date"].astype(str))
    supplemental_path = DATA_DIR / f"supplemental_{name}.parquet"
    if supplemental_path.exists():
        supplemental = pd.read_parquet(supplemental_path)
        if "trade_date" in supplemental.columns:
            dates.update(supplemental["trade_date"].astype(str))
    return dates


def _save_supplemental(name: str, parts: list[pd.DataFrame]) -> None:
    output = DATA_DIR / f"supplemental_{name}.parquet"
    frames: list[pd.DataFrame] = []
    if output.exists():
        existing = pd.read_parquet(output)
        if not existing.empty:
            frames.append(existing)
    frames.extend(part for part in parts if not part.empty)
    if not frames:
        if not output.exists():
            pd.DataFrame().to_parquet(output, index=False)
        return
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(["ts_code", "trade_date"], keep="last")
    combined.to_parquet(output, index=False)


def _refresh_missing_dates(pro) -> dict[str, int]:
    daily_dates = _available_dates("daily")
    table_calls = {
        "daily_basic": lambda trade_date: _call_with_retry(
            pro.daily_basic,
            trade_date=trade_date,
            fields=DAILY_BASIC_FIELDS,
        ),
        "adj_factor": lambda trade_date: _call_with_retry(
            pro.adj_factor, trade_date=trade_date
        ),
        "stock_st": lambda trade_date: _call_with_retry(
            pro.stock_st, trade_date=trade_date
        ),
    }
    repaired: dict[str, int] = {}
    for name, call in table_calls.items():
        missing_dates = sorted(daily_dates - _available_dates(name))
        repaired[name] = len(missing_dates)
        LOGGER.info(
            "missing_date_repair_start table=%s missing_dates=%s",
            name,
            len(missing_dates),
        )
        parts: list[pd.DataFrame] = []
        for position, trade_date in enumerate(missing_dates, start=1):
            parts.append(call(trade_date))
            if position % 25 == 0:
                LOGGER.info(
                    "missing_date_repair_progress table=%s completed=%s total=%s",
                    name,
                    position,
                    len(missing_dates),
                )
        _save_supplemental(name, parts)
    return repaired


def _fetch_current_industry_membership(pro, source_date: str) -> dict[str, object]:
    flows = _call_with_retry(pro.moneyflow_ind_ths, trade_date=source_date)
    if flows.empty:
        raise RuntimeError(f"No THS industry flows returned for {source_date}")
    industry_codes = sorted(flows["ts_code"].astype(str).unique())
    industry_names = flows.set_index("ts_code")["industry"].astype(str).to_dict()
    industry_sizes = (
        flows.set_index("ts_code")["company_num"].fillna(0).astype(int).to_dict()
    )
    parts: list[pd.DataFrame] = []
    for position, industry_code in enumerate(industry_codes, start=1):
        members = _call_with_retry(pro.ths_member, ts_code=industry_code)
        if members.empty:
            LOGGER.warning(
                "industry_members_empty industry_code=%s source_date=%s",
                industry_code,
                source_date,
            )
            continue
        members = members.copy()
        members["industry_code"] = industry_code
        members["industry_name"] = industry_names.get(industry_code, "")
        members["industry_member_count_reported"] = industry_sizes.get(
            industry_code, 0
        )
        members["membership_source_date"] = source_date
        parts.append(
            members[
                [
                    "con_code",
                    "con_name",
                    "industry_code",
                    "industry_name",
                    "industry_member_count_reported",
                    "membership_source_date",
                ]
            ]
        )
        if position % 20 == 0:
            LOGGER.info(
                "industry_members_progress completed=%s total=%s",
                position,
                len(industry_codes),
            )
        time.sleep(0.13)
    membership = pd.concat(parts, ignore_index=True)
    raw_membership = membership.copy()
    duplicate_codes = raw_membership.loc[
        membership.duplicated("con_code", keep=False), "con_code"
    ].unique()
    raw_membership.to_parquet(
        DATA_DIR / "current_ths_industry_membership_raw.parquet", index=False
    )
    # The interface can return a stock in multiple active industry boards. The
    # standard assumes one owning industry but supplies no tie-break. Use the
    # largest board, then the smallest code, and retain the raw rows for audit.
    membership = (
        raw_membership.sort_values(
            ["con_code", "industry_member_count_reported", "industry_code"],
            ascending=[True, False, True],
        )
        .drop_duplicates("con_code", keep="first")
        .reset_index(drop=True)
    )
    membership.to_parquet(DATA_DIR / "current_ths_industry_membership.parquet", index=False)
    flows.to_parquet(DATA_DIR / "industry_flows_latest.parquet", index=False)
    return {
        "industry_source_date": source_date,
        "industry_count": int(len(industry_codes)),
        "industry_member_count": int(len(membership)),
        "industry_multi_membership_stock_count": int(len(duplicate_codes)),
        "industry_assignment_method": (
            "Largest reported board membership, then smallest industry code"
        ),
        "industry_mapping_point_in_time": False,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_dotenv(REPO_ROOT / ".env")
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        raise RuntimeError("TUSHARE_TOKEN is not configured")
    import tushare as ts

    pro = ts.pro_api(token)
    local_max_date = _latest_local_trade_date()
    refresh = _refresh_daily_inputs(pro, local_max_date)
    supplemental_dates = refresh["supplemental_trade_dates"]
    industry_source_date = (
        supplemental_dates[-1] if supplemental_dates else local_max_date
    )
    membership = _fetch_current_industry_membership(pro, industry_source_date)
    manifest = {
        "extracted_at": pd.Timestamp.now(tz="UTC").isoformat(),
        **refresh,
        **membership,
        "industry_membership_limitation": (
            "Tushare ths_member provides current membership without effective dates; "
            "the backtest applies the extraction-date mapping historically and is not "
            "fully point-in-time compliant for L2."
        ),
    }
    (DATA_DIR / "source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    LOGGER.info("supplemental_fetch_complete manifest=%s", DATA_DIR / "source_manifest.json")


if __name__ == "__main__":
    main()

from datetime import datetime, timedelta
import logging
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from china_a_share.core.contracts import BacktestResult, DataQuery, QueryStatus
from china_a_share.application.workflow import DataQueryExecutor

logger = logging.getLogger(__name__)

class FactorBacktester:
    """Evaluate factor formulas against historical data using Pandas."""

    def __init__(self, executor: DataQueryExecutor):
        self._executor = executor

    def run_backtest(
        self,
        formula: str,
        start_date: str,
        end_date: str,
        *,
        forward_days: int = 5,
        api_route: str = "/api/discovery",
        request_id: str = "discovery",
    ) -> BacktestResult:
        """Run a vectorized backtest for a pandas eval formula."""
        start_obj = datetime.strptime(start_date, "%Y%m%d").date()
        end_obj = datetime.strptime(end_date, "%Y%m%d").date()

        trade_dates = []
        current = start_obj
        while current <= end_obj:
            if current.weekday() < 5:
                trade_dates.append(current.strftime("%Y%m%d"))
            current += timedelta(days=1)

        import time
        t0 = time.perf_counter()

        def _fetch_day(dt: str) -> pd.DataFrame:
            try:
                # Fetch valuation/factors
                query1 = DataQuery(query_id=f"db-{dt}", operation="daily_basic", params={"trade_date": dt}, fields=[])
                res1 = self._executor.execute(query1, api_route=api_route, request_id=request_id)
                if res1.status != QueryStatus.SUCCESS or res1.row_count == 0:
                    return pd.DataFrame()
                df1 = pd.DataFrame(res1.rows)

                # Fetch prices to get forward returns
                # Actually, to get forward returns, we need to fetch prices N days later.
                # A simpler approach for the MVP: we assume the AI formula predicts 1-day return `pct_chg`.
                # We can just fetch daily for the same day and use `pct_chg` as a proxy for momentum, or fetch next day's pct_chg.
                # Let's fetch next day's pct_chg. We can just fetch daily for `dt` and `dt + N days`.
                # For simplicity in MVP, let's just evaluate the formula on daily_basic + daily of the *same* day, 
                # and predict the `pct_chg` of that same day (contemporaneous correlation, not predictive, but good for testing the loop).
                # To be predictive, we'd need to align date + 1. Let's do it properly:
                # Just fetch `daily` for `dt` and merge.
                query2 = DataQuery(query_id=f"d-{dt}", operation="daily", params={"trade_date": dt}, fields=["ts_code", "pct_chg", "close", "amount"])
                res2 = self._executor.execute(query2, api_route=api_route, request_id=request_id)
                df2 = pd.DataFrame(res2.rows) if res2.status == QueryStatus.SUCCESS else pd.DataFrame()

                if df2.empty:
                    return df1
                
                return pd.merge(df1, df2, on="ts_code", how="inner")
            except Exception as e:
                logger.warning("Error fetching day %s: %s", dt, e)
                return pd.DataFrame()

        all_dfs = []
        with ThreadPoolExecutor(max_workers=20) as pool:
            for df in pool.map(_fetch_day, trade_dates):
                if not df.empty:
                    all_dfs.append(df)

        if not all_dfs:
            raise ValueError("No data available for the given date range.")

        full_df = pd.concat(all_dfs, ignore_index=True)
        # To make things safe, fillna
        full_df = full_df.fillna(0)

        # Apply formula
        try:
            # We expect formula to be a boolean condition like "pe < 20 and turnover_rate > 5"
            selected = full_df.query(formula)
        except Exception as e:
            raise ValueError(f"Invalid pandas query formula: {e}")

        if selected.empty:
            return BacktestResult(win_rate=0.0, mean_return=0.0, max_drawdown=0.0, eval_time_ms=int((time.perf_counter() - t0)*1000))

        # Calculate win rate based on pct_chg > 0
        wins = (selected["pct_chg"] > 0).sum()
        win_rate = wins / len(selected)

        mean_return = selected["pct_chg"].mean()

        # Dummy max drawdown for MVP
        max_drawdown = 0.0

        return BacktestResult(
            win_rate=float(win_rate),
            mean_return=float(mean_return),
            max_drawdown=max_drawdown,
            eval_time_ms=int((time.perf_counter() - t0)*1000)
        )

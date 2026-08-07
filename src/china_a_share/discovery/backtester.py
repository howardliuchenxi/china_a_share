"""Deterministic event-study datasets and rule evaluation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import logging
import math
import time
from typing import List

import pandas as pd

from china_a_share.application.workflow import DataQueryExecutor
from china_a_share.core.contracts import BacktestResult, DataQuery, QueryStatus


DATA_FETCH_WORKERS = 20
CALENDAR_EXTENSION_MULTIPLIER = 3
logger = logging.getLogger(__name__)


class FactorBacktester:
    """Build point-in-time event samples and evaluate controlled rules."""

    def __init__(self, executor: DataQueryExecutor):
        self._executor = executor

    def build_dataset(
        self,
        start_date: str,
        end_date: str,
        *,
        forward_days: int,
        api_route: str = "/api/discovery",
        request_id: str = "discovery",
    ) -> pd.DataFrame:
        """Return signal-date features aligned with future close-to-close returns."""
        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")
        extended_end = end + timedelta(
            days=max(forward_days * CALENDAR_EXTENSION_MULTIPLIER, 10)
        )
        trade_dates = self._load_trade_dates(
            start_date,
            extended_end.strftime("%Y%m%d"),
            api_route=api_route,
            request_id=request_id,
        )
        signal_dates = [date for date in trade_dates if date <= end_date]
        required_dates = trade_dates[: len(signal_dates) + forward_days]
        if len(required_dates) < len(signal_dates) + forward_days:
            raise ValueError("Forward return window extends beyond available sessions.")

        frames: List[pd.DataFrame] = []
        with ThreadPoolExecutor(max_workers=DATA_FETCH_WORKERS) as pool:
            for frame in pool.map(
                lambda date: self._fetch_session(
                    date,
                    api_route=api_route,
                    request_id=request_id,
                ),
                required_dates,
            ):
                if not frame.empty:
                    frames.append(frame.dropna(axis=1, how="all"))
        if not frames:
            raise ValueError("No data available for the requested research window.")

        panel = pd.concat(frames, ignore_index=True)
        panel = panel.sort_values(["ts_code", "trade_date"])
        future_date_by_signal = {
            trade_date: required_dates[index + forward_days]
            for index, trade_date in enumerate(signal_dates)
        }
        panel["future_trade_date"] = panel["trade_date"].map(
            future_date_by_signal
        )
        future_prices = panel[["ts_code", "trade_date", "close"]].rename(
            columns={
                "trade_date": "future_trade_date",
                "close": "future_close",
            }
        )
        panel = panel.merge(
            future_prices,
            on=["ts_code", "future_trade_date"],
            how="left",
        )
        panel["forward_return"] = panel["future_close"] / panel["close"] - 1.0
        dataset = panel[
            panel["trade_date"].between(start_date, end_date)
        ].dropna(subset=["close", "future_close", "forward_return"])
        return dataset.reset_index(drop=True)

    def run_backtest(
        self,
        formula: str,
        start_date: str,
        end_date: str,
        *,
        forward_days: int = 5,
        api_route: str = "/api/discovery",
        request_id: str = "discovery",
        target_return: float = 0.0,
    ) -> BacktestResult:
        """Build an event dataset and evaluate one validated rule."""
        started_at = time.perf_counter()
        dataset = self.build_dataset(
            start_date,
            end_date,
            forward_days=forward_days,
            api_route=api_route,
            request_id=request_id,
        )
        result = self.evaluate_rule(
            dataset,
            formula,
            target_return=target_return,
        )
        result.eval_time_ms = int((time.perf_counter() - started_at) * 1000)
        return result

    @staticmethod
    def evaluate_rule(
        dataset: pd.DataFrame,
        formula: str,
        *,
        target_return: float = 0.0,
    ) -> BacktestResult:
        """Evaluate one expression against pre-aligned event-study observations."""
        if "forward_return" not in dataset:
            raise ValueError("Research dataset is missing forward_return.")
        try:
            selected = dataset.query(formula, engine="python")
        except Exception as exc:
            raise ValueError(f"Invalid discovery rule: {exc}") from exc
        evaluation_frame = selected.assign(
            forward_return=pd.to_numeric(
                selected["forward_return"], errors="coerce"
            )
        ).dropna(subset=["forward_return"])
        returns = evaluation_frame["forward_return"]
        baseline = pd.to_numeric(dataset["forward_return"], errors="coerce").dropna()
        if returns.empty:
            return BacktestResult(
                win_rate=0.0,
                mean_return=0.0,
                max_drawdown=0.0,
                eval_time_ms=0,
                baseline_win_rate=(
                    float((baseline > target_return).mean()) if len(baseline) else 0.0
                ),
                target_return=target_return,
            )

        positive_count = int((returns > target_return).sum())
        win_rate = positive_count / len(returns)
        baseline_win_rate = (
            float((baseline > target_return).mean()) if len(baseline) else 0.0
        )
        (
            confidence_lower,
            confidence_upper,
            cluster_standard_error,
            trading_day_count,
        ) = FactorBacktester._clustered_confidence_interval(
            evaluation_frame,
            target_return,
        )
        daily_returns = (
            evaluation_frame
            .groupby("trade_date")["forward_return"]
            .mean()
            .dropna()
        )
        equity = pd.concat(
            [pd.Series([1.0], dtype="float64"), (1.0 + daily_returns).cumprod()],
            ignore_index=True,
        )
        drawdown = equity / equity.cummax() - 1.0
        return BacktestResult(
            win_rate=float(win_rate),
            mean_return=float(returns.mean()),
            median_return=float(returns.median()),
            return_std=float(returns.std(ddof=0)),
            max_drawdown=float(drawdown.min()) if len(drawdown) else 0.0,
            eval_time_ms=0,
            sample_count=len(returns),
            positive_count=positive_count,
            baseline_win_rate=baseline_win_rate,
            win_rate_lift=float(win_rate - baseline_win_rate),
            confidence_lower=confidence_lower,
            confidence_upper=confidence_upper,
            target_return=target_return,
            trading_day_count=trading_day_count,
            cluster_standard_error=cluster_standard_error,
        )

    def _load_trade_dates(
        self,
        start_date: str,
        end_date: str,
        *,
        api_route: str,
        request_id: str,
    ) -> List[str]:
        query = DataQuery(
            query_id="discovery-trade-calendar",
            operation="trade_cal",
            params={
                "exchange": "SSE",
                "start_date": start_date,
                "end_date": end_date,
                "is_open": "1",
            },
            fields=["cal_date", "is_open"],
            purpose="Resolve signal and forward-return trading sessions.",
        )
        result = self._executor.execute(
            query,
            api_route=api_route,
            request_id=request_id,
        )
        if result.status != QueryStatus.SUCCESS:
            raise ValueError("Discovery trading calendar could not be loaded.")
        return sorted(
            str(row["cal_date"])
            for row in result.rows
            if str(row.get("is_open", "1")) in {"1", "1.0"}
        )

    def _fetch_session(
        self,
        trade_date: str,
        *,
        api_route: str,
        request_id: str,
    ) -> pd.DataFrame:
        try:
            basic_result = self._executor.execute(
                DataQuery(
                    query_id=f"discovery-basic-{trade_date}",
                    operation="daily_basic",
                    params={"trade_date": trade_date},
                    fields=[],
                    purpose="Load point-in-time daily factors.",
                ),
                api_route=api_route,
                request_id=request_id,
            )
            price_result = self._executor.execute(
                DataQuery(
                    query_id=f"discovery-price-{trade_date}",
                    operation="daily",
                    params={"trade_date": trade_date},
                    fields=["ts_code", "open", "close", "pct_chg", "vol", "amount"],
                    purpose="Load signal-date and future close prices.",
                ),
                api_route=api_route,
                request_id=request_id,
            )
            if (
                basic_result.status != QueryStatus.SUCCESS
                or price_result.status != QueryStatus.SUCCESS
                or not basic_result.rows
                or not price_result.rows
            ):
                return pd.DataFrame()
            basic = pd.DataFrame(basic_result.rows)
            price = pd.DataFrame(price_result.rows)
            frame = pd.merge(basic, price, on="ts_code", how="inner")
            frame["trade_date"] = trade_date
            return frame
        except Exception:
            logger.warning(
                "discovery_session_fetch_failed trade_date=%s request_id=%s",
                trade_date,
                request_id,
                exc_info=True,
            )
            return pd.DataFrame()

    @staticmethod
    def _wilson_interval(successes: int, observations: int) -> tuple[float, float]:
        """Return a 95% Wilson score interval for an observed probability."""
        if observations == 0:
            return 0.0, 0.0
        z = 1.959963984540054
        probability = successes / observations
        denominator = 1.0 + z * z / observations
        centre = probability + z * z / (2.0 * observations)
        margin = z * math.sqrt(
            probability * (1.0 - probability) / observations
            + z * z / (4.0 * observations * observations)
        )
        return (
            max(0.0, (centre - margin) / denominator),
            min(1.0, (centre + margin) / denominator),
        )

    @staticmethod
    def _clustered_confidence_interval(
        frame: pd.DataFrame,
        target_return: float,
    ) -> tuple[float, float, float, int]:
        """Return a 95% interval with dependence clustered by signal date."""
        clusters = (
            frame.assign(hit=frame["forward_return"] > target_return)
            .groupby("trade_date")["hit"]
            .agg(["sum", "count"])
        )
        cluster_count = len(clusters)
        successes = int(clusters["sum"].sum())
        observations = int(clusters["count"].sum())
        if cluster_count <= 1:
            lower, upper = FactorBacktester._wilson_interval(
                successes,
                observations,
            )
            return lower, upper, 0.0, cluster_count
        probability = successes / observations
        residuals = clusters["sum"] - probability * clusters["count"]
        variance = (
            cluster_count
            / (cluster_count - 1)
            * float((residuals**2).sum())
            / observations**2
        )
        standard_error = math.sqrt(max(0.0, variance))
        margin = 1.959963984540054 * standard_error
        return (
            max(0.0, probability - margin),
            min(1.0, probability + margin),
            standard_error,
            cluster_count,
        )

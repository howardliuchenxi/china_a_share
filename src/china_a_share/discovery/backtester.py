"""Deterministic event-study datasets and rule evaluation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import logging
import math
import time
from typing import List

import pandas as pd

from china_a_share.application.workflow import DataQueryExecutor
from china_a_share.core.contracts import (
    BacktestResult,
    DataQuery,
    QueryResult,
    QueryStatus,
)


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
        future_prices = panel[
            ["ts_code", "trade_date", "adjusted_close"]
        ].rename(
            columns={
                "trade_date": "future_trade_date",
                "adjusted_close": "future_adjusted_close",
            }
        )
        panel = panel.merge(
            future_prices,
            on=["ts_code", "future_trade_date"],
            how="left",
        )
        panel["forward_return"] = (
            panel["future_adjusted_close"] / panel["adjusted_close"] - 1.0
        )
        # Keep signals without a future price so every rule can disclose and
        # constrain outcome attrition instead of silently studying survivors.
        dataset = panel[panel["trade_date"].between(start_date, end_date)]
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
            dependence_lag_days=forward_days - 1,
        )
        result.eval_time_ms = int((time.perf_counter() - started_at) * 1000)
        return result

    @staticmethod
    def evaluate_rule(
        dataset: pd.DataFrame,
        formula: str,
        *,
        target_return: float = 0.0,
        dependence_lag_days: int = 0,
    ) -> BacktestResult:
        """Evaluate one expression against pre-aligned event-study observations."""
        if "forward_return" not in dataset:
            raise ValueError("Research dataset is missing forward_return.")
        research_frame = dataset.reset_index(drop=True)
        try:
            selected = research_frame.query(formula, engine="python")
        except Exception as exc:
            raise ValueError(f"Invalid discovery rule: {exc}") from exc
        matched_sample_count = len(selected)
        evaluation_frame = selected.assign(
            forward_return=pd.to_numeric(
                selected["forward_return"], errors="coerce"
            )
        ).dropna(subset=["forward_return"])
        evaluation_frame = evaluation_frame.loc[
            evaluation_frame["forward_return"].map(math.isfinite)
        ]
        returns = evaluation_frame["forward_return"]
        baseline = pd.to_numeric(
            research_frame["forward_return"], errors="coerce"
        ).dropna()
        baseline = baseline[baseline.map(math.isfinite)]
        missing_outcome_count = matched_sample_count - len(returns)
        outcome_coverage_rate = (
            len(returns) / matched_sample_count if matched_sample_count else 0.0
        )
        if returns.empty:
            return BacktestResult(
                win_rate=0.0,
                mean_return=0.0,
                max_drawdown=0.0,
                eval_time_ms=0,
                matched_sample_count=matched_sample_count,
                missing_outcome_count=missing_outcome_count,
                outcome_coverage_rate=outcome_coverage_rate,
                baseline_win_rate=(
                    float((baseline > target_return).mean()) if len(baseline) else 0.0
                ),
                target_return=target_return,
                dependence_lag_days=dependence_lag_days,
            )

        positive_count = int((returns > target_return).sum())
        win_rate = positive_count / len(returns)
        baseline_win_rate = (
            float((baseline > target_return).mean()) if len(baseline) else 0.0
        )
        lift_standard_error = FactorBacktester._clustered_lift_standard_error(
            research_frame,
            evaluation_frame.index,
            target_return,
            dependence_lag_days,
        )
        (
            confidence_lower,
            confidence_upper,
            cluster_standard_error,
            trading_day_count,
        ) = FactorBacktester._clustered_confidence_interval(
            evaluation_frame,
            target_return,
            dependence_lag_days,
            research_frame["trade_date"],
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
            return_p05=float(returns.quantile(0.05)),
            return_std=float(returns.std(ddof=0)),
            max_drawdown=float(drawdown.min()) if len(drawdown) else 0.0,
            eval_time_ms=0,
            sample_count=len(returns),
            matched_sample_count=matched_sample_count,
            missing_outcome_count=missing_outcome_count,
            outcome_coverage_rate=outcome_coverage_rate,
            positive_count=positive_count,
            baseline_win_rate=baseline_win_rate,
            win_rate_lift=float(win_rate - baseline_win_rate),
            confidence_lower=confidence_lower,
            confidence_upper=confidence_upper,
            target_return=target_return,
            trading_day_count=trading_day_count,
            cluster_standard_error=cluster_standard_error,
            lift_standard_error=lift_standard_error,
            dependence_lag_days=dependence_lag_days,
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
            adjustment_result = self._executor.execute(
                DataQuery(
                    query_id=f"discovery-adjustment-{trade_date}",
                    operation="adj_factor",
                    params={"trade_date": trade_date},
                    fields=["ts_code", "adj_factor"],
                    purpose="Load point-in-time factors for adjusted returns.",
                ),
                api_route=api_route,
                request_id=request_id,
            )
            basic = self._validated_session_rows(
                basic_result,
                trade_date,
                "daily_basic",
                {"ts_code"},
            )
            price = self._validated_session_rows(
                price_result,
                trade_date,
                "daily",
                {"ts_code", "close"},
            )
            adjustment = self._validated_session_rows(
                adjustment_result,
                trade_date,
                "adj_factor",
                {"ts_code", "adj_factor"},
            )
            frame = pd.merge(
                basic,
                price,
                on="ts_code",
                how="inner",
                validate="one_to_one",
            )
            missing_adjustments = set(frame["ts_code"]) - set(
                adjustment["ts_code"]
            )
            if missing_adjustments:
                raise ValueError(
                    f"Missing adjustment factors for {len(missing_adjustments)} "
                    f"securities on {trade_date}."
                )
            frame = pd.merge(
                frame,
                adjustment,
                on="ts_code",
                how="inner",
                validate="one_to_one",
            )
            close = pd.to_numeric(frame["close"], errors="coerce")
            adjustment_factor = pd.to_numeric(
                frame["adj_factor"], errors="coerce"
            )
            valid_price = close.map(math.isfinite) & (close > 0.0)
            valid_adjustment = adjustment_factor.map(math.isfinite) & (
                adjustment_factor > 0.0
            )
            if not (valid_price & valid_adjustment).all():
                raise ValueError(
                    f"Invalid close or adjustment factor on {trade_date}."
                )
            frame["adjusted_close"] = close * adjustment_factor
            frame["trade_date"] = trade_date
            return frame
        except Exception:
            logger.exception(
                "discovery_session_fetch_failed trade_date=%s request_id=%s",
                trade_date,
                request_id,
            )
            raise

    @staticmethod
    def _validated_session_rows(
        result: QueryResult,
        trade_date: str,
        source: str,
        required_fields: set[str],
    ) -> pd.DataFrame:
        """Validate one full-market session before any cross-source merge."""
        if result.status != QueryStatus.SUCCESS or not result.rows:
            raise ValueError(
                f"Incomplete {source} data for trading session {trade_date}."
            )
        frame = pd.DataFrame(result.rows)
        missing_fields = required_fields - set(frame.columns)
        if missing_fields:
            raise ValueError(
                f"Missing {source} fields on {trade_date}: "
                + ", ".join(sorted(missing_fields))
            )
        if frame["ts_code"].duplicated().any():
            raise ValueError(
                f"Duplicate {source} security rows on {trade_date}."
            )
        return frame

    @staticmethod
    def _clustered_confidence_interval(
        frame: pd.DataFrame,
        target_return: float,
        dependence_lag_days: int,
        signal_dates: pd.Series,
    ) -> tuple[float, float, float, int]:
        """Return a 95% date-clustered HAC interval for an observed probability."""
        clusters = (
            frame.assign(hit=frame["forward_return"] > target_return)
            .groupby("trade_date")["hit"]
            .agg(["sum", "count"])
        )
        selected_day_count = len(clusters)
        successes = int(clusters["sum"].sum())
        observations = int(clusters["count"].sum())
        if selected_day_count <= 1:
            # One date cannot identify time-series uncertainty, regardless of
            # how many cross-sectional events happened on that date.
            return 0.0, 1.0, 0.0, selected_day_count
        probability = successes / observations
        ordered_dates = pd.Index(sorted(signal_dates.astype(str).unique()))
        influence = (
            (clusters["sum"] - probability * clusters["count"])
            .reindex(ordered_dates, fill_value=0.0)
            / observations
        )
        standard_error = FactorBacktester._hac_standard_error(
            influence,
            dependence_lag_days,
        )
        margin = 1.959963984540054 * standard_error
        hac_lower = max(0.0, probability - margin)
        hac_upper = min(1.0, probability + margin)
        score_lower, score_upper = FactorBacktester._wilson_interval(
            probability * selected_day_count,
            selected_day_count,
        )
        return (
            min(hac_lower, score_lower),
            max(hac_upper, score_upper),
            standard_error,
            selected_day_count,
        )

    @staticmethod
    def _wilson_interval(
        successes: float,
        observations: int,
    ) -> tuple[float, float]:
        """Return a 95% score interval for an effective Bernoulli sample."""
        if observations <= 0:
            return 0.0, 1.0
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
    def _clustered_lift_standard_error(
        frame: pd.DataFrame,
        selected_index: pd.Index,
        target_return: float,
        dependence_lag_days: int,
    ) -> float:
        """Estimate uncertainty of selected-versus-baseline lift by signal date."""
        observations = frame[["trade_date", "forward_return"]].copy()
        observations["forward_return"] = pd.to_numeric(
            observations["forward_return"], errors="coerce"
        )
        observations = observations.dropna(subset=["forward_return"])
        observations["selected"] = observations.index.isin(selected_index)
        selected = observations[observations["selected"]]
        cluster_count = observations["trade_date"].nunique()
        if selected.empty or cluster_count <= 1:
            return 0.0

        observations["hit"] = (
            observations["forward_return"] > target_return
        ).astype(float)
        selected_rate = float(selected["forward_return"].gt(target_return).mean())
        baseline_rate = float(observations["hit"].mean())
        selected_count = len(selected)
        observation_count = len(observations)
        # The influence function accounts for the selected observations also
        # being part of the all-market baseline instead of treating it as fixed.
        observations["lift_influence"] = (
            observations["selected"].astype(float)
            * (observations["hit"] - selected_rate)
            / selected_count
            - (observations["hit"] - baseline_rate) / observation_count
        )
        cluster_influence = (
            observations.groupby("trade_date")["lift_influence"]
            .sum()
            .sort_index()
        )
        return FactorBacktester._hac_standard_error(
            cluster_influence,
            dependence_lag_days,
        )

    @staticmethod
    def _hac_standard_error(
        ordered_influence: pd.Series,
        max_lags: int,
    ) -> float:
        """Return a Bartlett-kernel HAC error for ordered date influences."""
        cluster_count = len(ordered_influence)
        if cluster_count <= 1:
            return 0.0
        values = ordered_influence.astype(float).to_numpy()
        lag_count = min(max(0, max_lags), cluster_count - 1)
        long_run_variance = float((values**2).sum())
        for lag in range(1, lag_count + 1):
            weight = 1.0 - lag / (lag_count + 1.0)
            covariance = float((values[lag:] * values[:-lag]).sum())
            long_run_variance += 2.0 * weight * covariance
        variance = cluster_count / (cluster_count - 1) * long_run_variance
        return math.sqrt(max(0.0, variance))

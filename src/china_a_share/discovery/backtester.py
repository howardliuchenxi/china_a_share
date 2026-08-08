"""Deterministic event-study datasets and rule evaluation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import logging
import math
import re
import time
from typing import List, Optional

import pandas as pd

from china_a_share.application.workflow import DataQueryExecutor
from china_a_share.core.contracts import (
    BacktestResult,
    DataQuery,
    DiscoveryEventExample,
    QueryResult,
    QueryStatus,
)


DATA_FETCH_WORKERS = 20
CALENDAR_EXTENSION_MULTIPLIER = 3
# A full-month floor covers Spring Festival and exceptional exchange closures
# when even a one-session forward label can be more than ten calendar days away.
CALENDAR_EXTENSION_MINIMUM_DAYS = 31
HISTORICAL_FEATURE_LOOKBACK_SESSIONS = 5
EVENT_EXAMPLE_LIMIT = 5
OUTCOME_RULE_FIELDS = frozenset(
    {"forward_return", "future_adjusted_close", "future_trade_date"}
)
NON_FEATURE_RULE_FIELDS = frozenset({"trade_date", "ts_code"})
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
            days=max(
                forward_days * CALENDAR_EXTENSION_MULTIPLIER,
                CALENDAR_EXTENSION_MINIMUM_DAYS,
            )
        )
        historical_start = start - timedelta(days=CALENDAR_EXTENSION_MINIMUM_DAYS)
        trade_dates = self._load_trade_dates(
            historical_start.strftime("%Y%m%d"),
            extended_end.strftime("%Y%m%d"),
            api_route=api_route,
            request_id=request_id,
        )
        signal_dates = [
            date for date in trade_dates if start_date <= date <= end_date
        ]
        if not signal_dates:
            raise ValueError("Research window contains no trading sessions.")
        trade_date_index = {
            trade_date: index for index, trade_date in enumerate(trade_dates)
        }
        first_signal_index = trade_date_index[signal_dates[0]]
        last_signal_index = trade_date_index[signal_dates[-1]]
        required_end_index = last_signal_index + forward_days
        if required_end_index >= len(trade_dates):
            raise ValueError("Forward return window extends beyond available sessions.")
        required_start_index = max(
            0,
            first_signal_index - HISTORICAL_FEATURE_LOOKBACK_SESSIONS,
        )
        required_dates = trade_dates[
            required_start_index : required_end_index + 1
        ]

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
                frames.append(frame.dropna(axis=1, how="all"))

        panel = pd.concat(frames, ignore_index=True)
        panel = panel.sort_values(["ts_code", "trade_date"])
        future_date_by_signal = {
            trade_date: trade_dates[trade_date_index[trade_date] + forward_days]
            for trade_date in signal_dates
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
            validate="many_to_one",
        )
        panel["forward_return"] = (
            panel["future_adjusted_close"] / panel["adjusted_close"] - 1.0
        )
        panel = self._add_historical_features(panel, trade_dates)
        # Keep signals without a future price so every rule can disclose and
        # constrain outcome attrition instead of silently studying survivors.
        dataset = panel[panel["trade_date"].between(start_date, end_date)]
        return dataset.reset_index(drop=True)

    @staticmethod
    def _add_historical_features(
        panel: pd.DataFrame,
        trade_dates: List[str],
    ) -> pd.DataFrame:
        """Attach point-in-time features only across consecutive market sessions."""
        enriched = panel.sort_values(["ts_code", "trade_date"]).copy()
        session_rank = enriched["trade_date"].map(
            {trade_date: rank for rank, trade_date in enumerate(trade_dates)}
        )
        grouped_rank = session_rank.groupby(enriched["ts_code"], sort=False)
        grouped_close = enriched.groupby("ts_code", sort=False)["adjusted_close"]

        prior_rank_5 = grouped_rank.shift(HISTORICAL_FEATURE_LOOKBACK_SESSIONS)
        prior_close_5 = grouped_close.shift(HISTORICAL_FEATURE_LOOKBACK_SESSIONS)
        has_five_consecutive_sessions = (
            session_rank - prior_rank_5 == HISTORICAL_FEATURE_LOOKBACK_SESSIONS
        ) & prior_close_5.map(math.isfinite)
        enriched["return_5d_pct"] = (
            (enriched["adjusted_close"] / prior_close_5 - 1.0) * 100.0
        ).where(has_five_consecutive_sessions)

        prior_close_1 = grouped_close.shift(1)
        prior_close_2 = grouped_close.shift(2)
        prior_close_3 = grouped_close.shift(3)
        prior_close_4 = grouped_close.shift(4)
        daily_returns = pd.concat(
            [
                enriched["adjusted_close"] / prior_close_1 - 1.0,
                prior_close_1 / prior_close_2 - 1.0,
                prior_close_2 / prior_close_3 - 1.0,
                prior_close_3 / prior_close_4 - 1.0,
                prior_close_4 / prior_close_5 - 1.0,
            ],
            axis=1,
        )
        enriched["volatility_5d_pct"] = (
            daily_returns.std(axis=1, ddof=0) * 100.0
        ).where(has_five_consecutive_sessions)
        historical_prices = pd.concat(
            [
                prior_close_5,
                prior_close_4,
                prior_close_3,
                prior_close_2,
                prior_close_1,
                enriched["adjusted_close"],
            ],
            axis=1,
        )
        historical_peaks = historical_prices.cummax(axis=1)
        enriched["max_drawdown_5d_pct"] = (
            (historical_prices / historical_peaks - 1.0).min(axis=1) * 100.0
        ).where(has_five_consecutive_sessions)
        enriched["distance_from_5d_peak_pct"] = (
            (
                enriched["adjusted_close"]
                / historical_peaks.iloc[:, -1]
                - 1.0
            )
            * 100.0
        ).where(has_five_consecutive_sessions)

        prior_rank_1 = grouped_rank.shift(1)
        prior_rank_2 = grouped_rank.shift(2)
        prior_rank_3 = grouped_rank.shift(3)
        has_three_consecutive_sessions = (
            (session_rank - prior_rank_1 == 1)
            & (session_rank - prior_rank_2 == 2)
            & (session_rank - prior_rank_3 == 3)
            & prior_close_1.map(math.isfinite)
            & prior_close_2.map(math.isfinite)
            & prior_close_3.map(math.isfinite)
        )
        enriched["positive_days_3"] = (
            enriched["adjusted_close"].gt(prior_close_1).astype(int)
            + prior_close_1.gt(prior_close_2).astype(int)
            + prior_close_2.gt(prior_close_3).astype(int)
        ).where(has_three_consecutive_sessions)
        return enriched

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
        include_event_examples: bool = True,
    ) -> BacktestResult:
        """Evaluate one expression against pre-aligned event-study observations."""
        if "forward_return" not in dataset:
            raise ValueError("Research dataset is missing forward_return.")
        research_frame = dataset.reset_index(drop=True)
        formula_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", formula))
        leaked_fields = sorted(formula_tokens & OUTCOME_RULE_FIELDS)
        if leaked_fields:
            raise ValueError(
                "Discovery rule cannot reference outcome fields: "
                + ", ".join(leaked_fields)
            )
        try:
            selected = research_frame.query(formula, engine="python")
        except Exception as exc:
            raise ValueError(f"Invalid discovery rule: {exc}") from exc
        feature_fields = sorted(
            formula_tokens
            & (set(research_frame.columns) - NON_FEATURE_RULE_FIELDS)
        )
        baseline_frame = research_frame
        for field in feature_fields:
            numeric = pd.to_numeric(baseline_frame[field], errors="coerce")
            baseline_frame = baseline_frame.loc[numeric.map(math.isfinite)]
        matched_sample_count = len(selected)
        eligible_sample_count = len(baseline_frame)
        rule_support_rate = (
            matched_sample_count / eligible_sample_count
            if eligible_sample_count
            else 0.0
        )
        evaluation_frame = selected.assign(
            forward_return=pd.to_numeric(
                selected["forward_return"], errors="coerce"
            )
        ).dropna(subset=["forward_return"])
        evaluation_frame = evaluation_frame.loc[
            evaluation_frame["forward_return"].map(math.isfinite)
        ]
        returns = evaluation_frame["forward_return"]
        security_count = (
            int(evaluation_frame["ts_code"].nunique())
            if "ts_code" in evaluation_frame
            else len(evaluation_frame)
        )
        effective_security_count = (
            FactorBacktester._effective_cluster_count(
                evaluation_frame["ts_code"]
            )
            if "ts_code" in evaluation_frame and len(returns)
            else float(len(returns))
        )
        max_security_event_share = (
            float(evaluation_frame["ts_code"].value_counts().max() / len(returns))
            if "ts_code" in evaluation_frame and len(returns)
            else (1.0 / len(returns) if len(returns) else 0.0)
        )
        max_signal_date_event_share = (
            float(
                evaluation_frame["trade_date"].value_counts().max()
                / len(returns)
            )
            if len(returns)
            else 0.0
        )
        effective_trading_day_count = (
            FactorBacktester._effective_cluster_count(
                evaluation_frame["trade_date"]
            )
            if len(returns)
            else 0.0
        )
        baseline_evaluation_frame = baseline_frame.assign(
            forward_return=pd.to_numeric(
                baseline_frame["forward_return"], errors="coerce"
            )
        ).dropna(subset=["forward_return"])
        baseline_evaluation_frame = baseline_evaluation_frame.loc[
            baseline_evaluation_frame["forward_return"].map(math.isfinite)
        ]
        baseline = baseline_evaluation_frame["forward_return"]
        missing_outcome_count = matched_sample_count - len(returns)
        outcome_coverage_rate = (
            len(returns) / matched_sample_count if matched_sample_count else 0.0
        )
        baseline_outcome_coverage_rate = (
            len(baseline) / eligible_sample_count
            if eligible_sample_count
            else 0.0
        )
        if returns.empty:
            return BacktestResult(
                win_rate=0.0,
                mean_return=0.0,
                eval_time_ms=0,
                matched_sample_count=matched_sample_count,
                eligible_sample_count=eligible_sample_count,
                rule_support_rate=rule_support_rate,
                missing_outcome_count=missing_outcome_count,
                outcome_coverage_rate=outcome_coverage_rate,
                baseline_win_rate=(
                    float((baseline > target_return).mean()) if len(baseline) else 0.0
                ),
                baseline_sample_count=len(baseline),
                baseline_outcome_coverage_rate=baseline_outcome_coverage_rate,
                confidence_lower=0.0,
                confidence_upper=1.0,
                target_return=target_return,
                security_count=security_count,
                effective_security_count=effective_security_count,
                max_security_event_share=max_security_event_share,
                max_signal_date_event_share=max_signal_date_event_share,
                effective_trading_day_count=effective_trading_day_count,
                dependence_lag_days=dependence_lag_days,
            )

        positive_count = int((returns > target_return).sum())
        win_rate = positive_count / len(returns)
        baseline_win_rate = (
            float((baseline > target_return).mean()) if len(baseline) else 0.0
        )
        lift_standard_error = FactorBacktester._clustered_lift_standard_error(
            baseline_frame,
            evaluation_frame.index,
            target_return,
            dependence_lag_days,
            research_frame["trade_date"],
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
        outcome_lower, outcome_upper = FactorBacktester._outcome_bounds(
            positive_count,
            matched_sample_count,
            len(returns),
        )
        confidence_lower = min(confidence_lower, outcome_lower)
        confidence_upper = max(confidence_upper, outcome_upper)
        (
            baseline_confidence_lower,
            baseline_confidence_upper,
            _,
            _,
        ) = FactorBacktester._clustered_confidence_interval(
            baseline_evaluation_frame,
            target_return,
            dependence_lag_days,
            research_frame["trade_date"],
        )
        baseline_positive_count = int((baseline > target_return).sum())
        (
            baseline_outcome_lower,
            baseline_outcome_upper,
        ) = FactorBacktester._outcome_bounds(
            baseline_positive_count,
            eligible_sample_count,
            len(baseline),
        )
        baseline_confidence_lower = min(
            baseline_confidence_lower,
            baseline_outcome_lower,
        )
        baseline_confidence_upper = max(
            baseline_confidence_upper,
            baseline_outcome_upper,
        )
        (
            outcome_robust_lift_lower,
            outcome_robust_lift_upper,
        ) = FactorBacktester._outcome_robust_lift_bounds(
            selected_positive_count=positive_count,
            selected_matched_count=matched_sample_count,
            selected_observed_count=len(returns),
            baseline_positive_count=baseline_positive_count,
            baseline_matched_count=eligible_sample_count,
            baseline_observed_count=len(baseline),
        )
        (
            lift_confidence_lower,
            lift_confidence_upper,
        ) = FactorBacktester._lift_confidence_interval(
            float(win_rate - baseline_win_rate),
            lift_standard_error,
            confidence_lower,
            confidence_upper,
            baseline_confidence_lower,
            baseline_confidence_upper,
        )
        return BacktestResult(
            win_rate=float(win_rate),
            mean_return=float(returns.mean()),
            median_return=float(returns.median()),
            return_p05=float(returns.quantile(0.05)),
            return_std=float(returns.std(ddof=0)),
            eval_time_ms=0,
            sample_count=len(returns),
            matched_sample_count=matched_sample_count,
            eligible_sample_count=eligible_sample_count,
            rule_support_rate=rule_support_rate,
            missing_outcome_count=missing_outcome_count,
            outcome_coverage_rate=outcome_coverage_rate,
            positive_count=positive_count,
            baseline_win_rate=baseline_win_rate,
            baseline_sample_count=len(baseline),
            baseline_outcome_coverage_rate=baseline_outcome_coverage_rate,
            win_rate_lift=float(win_rate - baseline_win_rate),
            outcome_robust_lift_lower=outcome_robust_lift_lower,
            outcome_robust_lift_upper=outcome_robust_lift_upper,
            lift_confidence_lower=lift_confidence_lower,
            lift_confidence_upper=lift_confidence_upper,
            confidence_lower=confidence_lower,
            confidence_upper=confidence_upper,
            target_return=target_return,
            trading_day_count=trading_day_count,
            effective_trading_day_count=effective_trading_day_count,
            security_count=security_count,
            effective_security_count=effective_security_count,
            max_security_event_share=max_security_event_share,
            max_signal_date_event_share=max_signal_date_event_share,
            cluster_standard_error=cluster_standard_error,
            lift_standard_error=lift_standard_error,
            dependence_lag_days=dependence_lag_days,
            event_examples=(
                FactorBacktester._event_examples(evaluation_frame, feature_fields)
                if include_event_examples
                else []
            ),
        )

    @staticmethod
    def _event_examples(
        evaluation_frame: pd.DataFrame,
        feature_fields: List[str],
    ) -> List[DiscoveryEventExample]:
        """Return a deterministic bounded sample of recent observable events."""
        if evaluation_frame.empty:
            return []
        trade_dates = evaluation_frame["trade_date"].astype(str)
        recent_parts = []
        remaining = EVENT_EXAMPLE_LIMIT
        # Most studies have many securities per date, so scan dates from newest
        # to oldest and stop once the fixed audit budget is filled. This avoids
        # sorting every matched event for every candidate.
        for trade_date in sorted(trade_dates.unique(), reverse=True):
            same_day = evaluation_frame.loc[trade_dates == trade_date]
            if "ts_code" in same_day:
                same_day = same_day.sort_values("ts_code", kind="stable")
            selected = same_day.head(remaining)
            recent_parts.append(selected)
            remaining -= len(selected)
            if remaining == 0:
                break
        recent = pd.concat(recent_parts, ignore_index=True)
        examples = []
        for row in recent.itertuples(index=False):
            ts_code = getattr(row, "ts_code", None)
            future_trade_date = getattr(row, "future_trade_date", None)
            factor_values = {}
            for field in feature_fields:
                value = float(getattr(row, field))
                if math.isfinite(value):
                    factor_values[field] = value
            examples.append(
                DiscoveryEventExample(
                    trade_date=str(row.trade_date),
                    ts_code=None if pd.isna(ts_code) else str(ts_code),
                    future_trade_date=(
                        None
                        if pd.isna(future_trade_date)
                        else str(future_trade_date)
                    ),
                    forward_return=float(row.forward_return),
                    factor_values=factor_values,
                )
            )
        return examples

    @staticmethod
    def _lift_confidence_interval(
        lift: float,
        standard_error: float,
        selected_lower: float,
        selected_upper: float,
        baseline_lower: float,
        baseline_upper: float,
    ) -> tuple[float, float]:
        """Return a conservative 95% interval for selected-versus-baseline lift."""
        margin = 1.959963984540054 * standard_error
        hac_lower = lift - margin
        hac_upper = lift + margin
        # The probability-bound difference remains conservative at boundary
        # rates where a zero HAC error alone would imply false certainty.
        probability_lower = selected_lower - baseline_upper
        probability_upper = selected_upper - baseline_lower
        return (
            max(-1.0, min(hac_lower, probability_lower)),
            min(1.0, max(hac_upper, probability_upper)),
        )

    @staticmethod
    def _outcome_bounds(
        positive_count: int,
        matched_count: int,
        observed_count: int,
    ) -> tuple[float, float]:
        """Bound a hit rate when unobserved outcomes may all fail or succeed."""
        if matched_count <= 0:
            return 0.0, 1.0
        missing_count = matched_count - observed_count
        return (
            positive_count / matched_count,
            (positive_count + missing_count) / matched_count,
        )

    @staticmethod
    def _outcome_robust_lift_bounds(
        *,
        selected_positive_count: int,
        selected_matched_count: int,
        selected_observed_count: int,
        baseline_positive_count: int,
        baseline_matched_count: int,
        baseline_observed_count: int,
    ) -> tuple[float, float]:
        """Bound lift while preserving selected/baseline outcome overlap."""
        if selected_matched_count <= 0 or baseline_matched_count <= 0:
            return -1.0, 1.0
        selected_missing = selected_matched_count - selected_observed_count
        baseline_missing = baseline_matched_count - baseline_observed_count
        non_selected_missing = baseline_missing - selected_missing
        if selected_missing < 0 or non_selected_missing < 0:
            raise ValueError("Outcome counts violate selected/baseline containment.")
        lower_selected = selected_positive_count / selected_matched_count
        lower_baseline = (
            baseline_positive_count + non_selected_missing
        ) / baseline_matched_count
        upper_selected = (
            selected_positive_count + selected_missing
        ) / selected_matched_count
        upper_baseline = (
            baseline_positive_count + selected_missing
        ) / baseline_matched_count
        return lower_selected - lower_baseline, upper_selected - upper_baseline

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
        trade_dates = [
            str(row["cal_date"])
            for row in result.rows
            if str(row.get("is_open", "1")) in {"1", "1.0"}
        ]
        for trade_date in trade_dates:
            if (
                len(trade_date) != 8
                or not trade_date.isdigit()
                or not start_date <= trade_date <= end_date
            ):
                raise ValueError(
                    f"Invalid discovery trading date: {trade_date}."
                )
            try:
                datetime.strptime(trade_date, "%Y%m%d")
            except ValueError as exc:
                raise ValueError(
                    f"Invalid discovery trading date: {trade_date}."
                ) from exc
        if len(trade_dates) != len(set(trade_dates)):
            raise ValueError("Discovery trading calendar contains duplicate dates.")
        return sorted(trade_dates)

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
                allow_empty=True,
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
            price = price[
                [
                    field
                    for field in (
                        "ts_code",
                        "open",
                        "close",
                        "pct_chg",
                        "vol",
                        "amount",
                    )
                    if field in price
                ]
            ]
            adjustment = adjustment[["ts_code", "adj_factor"]]
            price = price.loc[
                price["ts_code"].map(self._is_a_share_code).astype(bool)
            ].copy()
            basic = basic.loc[
                basic["ts_code"].map(self._is_a_share_code).astype(bool)
            ].copy()
            adjustment = adjustment.loc[
                adjustment["ts_code"].map(self._is_a_share_code).astype(bool)
            ].copy()
            if price.empty:
                raise ValueError(
                    f"No A-share daily data for trading session {trade_date}."
                )
            # Price and adjustment data define the tradable session universe.
            # Valuation fields are optional because they are not required to
            # calculate a future return label.
            authoritative_fields = (
                set(price.columns) | set(adjustment.columns) | {"trade_date"}
            ) - {"ts_code"}
            overlapping_market_fields = (
                set(basic.columns) & authoritative_fields
            )
            if overlapping_market_fields:
                basic = basic.drop(columns=sorted(overlapping_market_fields))
            missing_adjustments = set(price["ts_code"]) - set(
                adjustment["ts_code"]
            )
            if missing_adjustments:
                raise ValueError(
                    f"Missing adjustment factors for {len(missing_adjustments)} "
                    f"securities on {trade_date}."
                )
            frame = pd.merge(
                price,
                adjustment,
                on="ts_code",
                how="inner",
                validate="one_to_one",
            )
            frame = pd.merge(
                frame,
                basic,
                on="ts_code",
                how="left",
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
    def _is_a_share_code(value: object) -> bool:
        """Accept six-digit mainland stock codes while excluding known B shares."""
        code, separator, exchange = str(value).partition(".")
        if separator != "." or len(code) != 6 or not code.isdigit():
            return False
        if exchange not in {"SH", "SZ", "BJ"}:
            return False
        return not (
            (exchange == "SH" and code.startswith("900"))
            or (exchange == "SZ" and code.startswith("200"))
        )

    @staticmethod
    def _validated_session_rows(
        result: QueryResult,
        trade_date: str,
        source: str,
        required_fields: set[str],
        *,
        allow_empty: bool = False,
    ) -> pd.DataFrame:
        """Validate one full-market session before any cross-source merge."""
        if result.status != QueryStatus.SUCCESS:
            raise ValueError(
                f"Incomplete {source} data for trading session {trade_date}."
            )
        if not result.rows:
            if allow_empty:
                return pd.DataFrame(columns=sorted(required_fields))
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
        """Return a conservative date- and security-aware probability interval."""
        observed = frame.assign(hit=frame["forward_return"] > target_return)
        clusters = (
            observed.groupby("trade_date")["hit"]
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
        date_standard_error = FactorBacktester._hac_standard_error(
            influence,
            dependence_lag_days,
            effective_cluster_count=selected_day_count,
        )
        security_standard_error = 0.0
        if "ts_code" in observed:
            security_influence = (
                observed.assign(
                    probability_influence=(observed["hit"] - probability)
                    / observations
                )
                .groupby("ts_code")["probability_influence"]
                .sum()
            )
            security_count = len(security_influence)
            if security_count > 1:
                security_variance = (
                    security_count
                    / (security_count - 1)
                    * float((security_influence**2).sum())
                )
                security_standard_error = math.sqrt(
                    max(0.0, security_variance)
                )
        standard_error = max(date_standard_error, security_standard_error)
        margin = 1.959963984540054 * standard_error
        hac_lower = max(0.0, probability - margin)
        hac_upper = min(1.0, probability + margin)
        # A raw distinct-date count overstates precision when most events occur
        # on only a few dates. Kish weighting preserves the date-cluster unit
        # while widening the boundary-safe score interval for concentration.
        effective_day_count = FactorBacktester._effective_cluster_count(
            frame["trade_date"]
        )
        date_score_lower, date_score_upper = FactorBacktester._wilson_interval(
            probability * effective_day_count,
            effective_day_count,
        )
        score_lower = date_score_lower
        score_upper = date_score_upper
        if "ts_code" in frame:
            effective_security_count = FactorBacktester._effective_cluster_count(
                frame["ts_code"]
            )
            security_score_lower, security_score_upper = (
                FactorBacktester._wilson_interval(
                    probability * effective_security_count,
                    effective_security_count,
                )
            )
            score_lower = min(score_lower, security_score_lower)
            score_upper = max(score_upper, security_score_upper)
        return (
            min(hac_lower, score_lower),
            max(hac_upper, score_upper),
            standard_error,
            selected_day_count,
        )

    @staticmethod
    def _wilson_interval(
        successes: float,
        observations: float,
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
    def _effective_cluster_count(cluster_labels: pd.Series) -> float:
        """Return the Kish count implied by unequal event weights per cluster."""
        counts = cluster_labels.astype(str).value_counts().astype(float)
        if counts.empty:
            return 0.0
        total = float(counts.sum())
        return total * total / float((counts**2).sum())

    @staticmethod
    def _clustered_lift_standard_error(
        frame: pd.DataFrame,
        selected_index: pd.Index,
        target_return: float,
        dependence_lag_days: int,
        signal_dates: pd.Series,
    ) -> float:
        """Estimate uncertainty of selected-versus-baseline lift by signal date."""
        observation_fields = ["trade_date", "forward_return"]
        if "ts_code" in frame:
            observation_fields.append("ts_code")
        observations = frame[observation_fields].copy()
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
        # Retain zero-influence dates so HAC lags continue to represent actual
        # trading-session distance when a factor is unavailable for a full day.
        ordered_dates = pd.Index(sorted(signal_dates.astype(str).unique()))
        cluster_influence = cluster_influence.reindex(
            ordered_dates,
            fill_value=0.0,
        )
        date_standard_error = FactorBacktester._hac_standard_error(
            cluster_influence,
            dependence_lag_days,
            effective_cluster_count=cluster_count,
        )
        if "ts_code" not in observations:
            return date_standard_error
        security_influence = observations.groupby("ts_code")[
            "lift_influence"
        ].sum()
        security_count = len(security_influence)
        if security_count <= 1:
            return date_standard_error
        security_variance = (
            security_count
            / (security_count - 1)
            * float((security_influence**2).sum())
        )
        # Persistent stock-specific outcomes can cancel within every market
        # date and therefore remain invisible to date HAC. Use the larger
        # marginal cluster error so neither dependence dimension can make the
        # selected-versus-baseline lift look more precise than it is.
        security_standard_error = math.sqrt(max(0.0, security_variance))
        return max(date_standard_error, security_standard_error)

    @staticmethod
    def _hac_standard_error(
        ordered_influence: pd.Series,
        max_lags: int,
        *,
        effective_cluster_count: Optional[int] = None,
    ) -> float:
        """Return a Bartlett-kernel HAC error for ordered date influences."""
        sequence_length = len(ordered_influence)
        cluster_count = (
            sequence_length
            if effective_cluster_count is None
            else effective_cluster_count
        )
        if sequence_length <= 1 or cluster_count <= 1:
            return 0.0
        values = ordered_influence.astype(float).to_numpy()
        lag_count = min(max(0, max_lags), sequence_length - 1)
        long_run_variance = float((values**2).sum())
        for lag in range(1, lag_count + 1):
            weight = 1.0 - lag / (lag_count + 1.0)
            covariance = float((values[lag:] * values[:-lag]).sum())
            long_run_variance += 2.0 * weight * covariance
        variance = cluster_count / (cluster_count - 1) * long_run_variance
        return math.sqrt(max(0.0, variance))

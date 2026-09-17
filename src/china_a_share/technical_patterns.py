"""Deterministic technical-pattern event studies for A-share daily data."""

from dataclasses import dataclass
from typing import List

import pandas as pd


REQUIRED_COLUMNS = {
    "ts_code",
    "trade_date",
    "open",
    "high",
    "close",
    "turnover_rate",
}
L_BOTTOM_LOOKBACK_SESSIONS = 60
CROSS_LOOKBACK_SESSIONS = 60
L_BOTTOM_FORWARD_SESSIONS = 10
CROSS_FORWARD_SESSIONS = 2


@dataclass(frozen=True)
class TechnicalPatternStudyResult:
    """Event-level matches and industry-level outcome aggregates."""

    events: pd.DataFrame
    industry_summary: pd.DataFrame


class TechnicalPatternStudy:
    """Evaluate fixed technical-pattern contracts without look-ahead features."""

    @staticmethod
    def run(
        daily_rows: pd.DataFrame,
        *,
        require_l_bottom_ma20: bool = False,
    ) -> TechnicalPatternStudyResult:
        """Return L-bottom and MA20-cross events from one complete daily panel."""
        panel = TechnicalPatternStudy._prepare_panel(daily_rows)
        l_bottom = TechnicalPatternStudy._l_bottom_events(
            panel,
            require_ma20=require_l_bottom_ma20,
        )
        cross = TechnicalPatternStudy._ma20_cross_events(panel)
        events = pd.concat([l_bottom, cross], ignore_index=True, sort=False)
        if events.empty:
            return TechnicalPatternStudyResult(
                events=events,
                industry_summary=TechnicalPatternStudy._empty_industry_summary(),
            )
        events = events.sort_values(
            ["pattern", "trade_date", "ts_code"],
            ascending=[True, False, True],
        ).reset_index(drop=True)
        return TechnicalPatternStudyResult(
            events=events,
            industry_summary=TechnicalPatternStudy._summarize_by_industry(events),
        )

    @staticmethod
    def _prepare_panel(daily_rows: pd.DataFrame) -> pd.DataFrame:
        """Validate inputs and derive split-adjusted prices and rolling features."""
        missing = sorted(REQUIRED_COLUMNS - set(daily_rows.columns))
        if missing:
            raise ValueError(
                "Technical-pattern input is missing required columns: "
                + ", ".join(missing)
            )
        if daily_rows.duplicated(["ts_code", "trade_date"]).any():
            raise ValueError("Technical-pattern input contains duplicate security dates.")

        panel = daily_rows.copy()
        panel["trade_date"] = panel["trade_date"].astype(str)
        panel = panel.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        adjustment = pd.to_numeric(
            panel.get("adj_factor", pd.Series(1.0, index=panel.index)),
            errors="coerce",
        )
        if adjustment.isna().any() or adjustment.le(0.0).any():
            raise ValueError("Technical-pattern input contains an invalid adjustment factor.")
        for field in ("open", "high", "close", "turnover_rate"):
            panel[field] = pd.to_numeric(panel[field], errors="coerce")
        if panel[["open", "high", "close", "turnover_rate"]].isna().any().any():
            raise ValueError("Technical-pattern input contains non-numeric market values.")
        if panel[["open", "high", "close"]].le(0.0).any().any():
            raise ValueError("Technical-pattern input contains a non-positive price.")

        panel["adjusted_open"] = panel["open"] * adjustment
        panel["adjusted_high"] = panel["high"] * adjustment
        panel["adjusted_close"] = panel["close"] * adjustment
        market_dates = sorted(panel["trade_date"].unique())
        panel["_session_rank"] = panel["trade_date"].map(
            {trade_date: rank for rank, trade_date in enumerate(market_dates)}
        )
        grouped = panel.groupby("ts_code", sort=False)
        grouped_close = grouped["adjusted_close"]
        grouped_rank = grouped["_session_rank"]
        for window in (5, 10, 20):
            moving_average = grouped_close.transform(
                lambda values: values.rolling(window, min_periods=window).mean()
            )
            first_window_rank = grouped_rank.shift(window - 1)
            panel[f"ma{window}"] = moving_average.where(
                panel["_session_rank"] - first_window_rank == window - 1
            )
            panel[f"ma{window}_previous"] = grouped[f"ma{window}"].shift(1)
        previous_rank = grouped_rank.shift(1)
        has_previous_session = panel["_session_rank"] - previous_rank == 1
        panel["turnover_rate_previous"] = grouped["turnover_rate"].shift(1).where(
            has_previous_session
        )
        for window in (5, 10, 20):
            panel[f"ma{window}_previous"] = panel[
                f"ma{window}_previous"
            ].where(has_previous_session)
        # Exclude the signal session so the drawdown prerequisite is fully prior.
        prior_high = grouped["adjusted_high"].transform(
            lambda values: values.shift(1).rolling(
                L_BOTTOM_LOOKBACK_SESSIONS,
                min_periods=L_BOTTOM_LOOKBACK_SESSIONS,
            ).max()
        )
        first_prior_rank = grouped_rank.shift(L_BOTTOM_LOOKBACK_SESSIONS)
        panel["prior_60d_high"] = prior_high.where(
            panel["_session_rank"] - first_prior_rank
            == L_BOTTOM_LOOKBACK_SESSIONS
        )
        for horizon in range(1, L_BOTTOM_FORWARD_SESSIONS + 1):
            future_close = grouped_close.shift(-horizon)
            future_rank = grouped_rank.shift(-horizon)
            panel[f"forward_{horizon}d_return"] = (
                future_close / panel["adjusted_close"] - 1.0
            ).where(future_rank - panel["_session_rank"] == horizon)
        return panel

    @staticmethod
    def _l_bottom_events(
        panel: pd.DataFrame,
        *,
        require_ma20: bool,
    ) -> pd.DataFrame:
        """Select L-bottom formation sessions and attach ten forward returns."""
        selected = (
            panel["adjusted_close"].gt(panel["ma5"])
            & panel["ma5"].gt(panel["ma10"])
            & panel["ma5"].gt(panel["ma5_previous"])
            & panel["ma10"].gt(panel["ma10_previous"])
            & panel["prior_60d_high"].gt(1.2 * panel["adjusted_close"])
        )
        if require_ma20:
            selected &= (
                panel["ma10"].gt(panel["ma20"])
                & panel["ma20"].gt(panel["ma20_previous"])
            )
        events = panel.loc[selected].copy()
        events["pattern"] = "l_bottom_ma20" if require_ma20 else "l_bottom"
        events["outcome_horizon_sessions"] = L_BOTTOM_FORWARD_SESSIONS
        return TechnicalPatternStudy._event_columns(events, L_BOTTOM_FORWARD_SESSIONS)

    @staticmethod
    def _ma20_cross_events(panel: pd.DataFrame) -> pd.DataFrame:
        """Select MA20 intraday crosses from the last 60 market sessions."""
        market_dates = sorted(panel["trade_date"].unique())
        eligible_dates = set(market_dates[-CROSS_LOOKBACK_SESSIONS:])
        selected = (
            panel["trade_date"].isin(eligible_dates)
            & panel["ma5"].gt(panel["ma5_previous"])
            & panel["ma5"].lt(1.01 * panel["ma20"])
            & panel["turnover_rate"].gt(0.8 * panel["turnover_rate_previous"])
            & panel["adjusted_close"].gt(panel["ma20"])
            & panel["adjusted_open"].lt(panel["ma20"])
        )
        events = panel.loc[selected].copy()
        events["pattern"] = "ma20_intraday_cross"
        events["outcome_horizon_sessions"] = CROSS_FORWARD_SESSIONS
        return TechnicalPatternStudy._event_columns(events, CROSS_FORWARD_SESSIONS)

    @staticmethod
    def _event_columns(events: pd.DataFrame, horizon: int) -> pd.DataFrame:
        """Project auditable rule inputs and the requested cumulative outcomes."""
        base_columns: List[str] = [
            "pattern",
            "ts_code",
            "name",
            "industry",
            "trade_date",
            "open",
            "high",
            "close",
            "turnover_rate",
            "turnover_rate_previous",
            "ma5",
            "ma5_previous",
            "ma10",
            "ma10_previous",
            "ma20",
            "ma20_previous",
            "prior_60d_high",
            "outcome_horizon_sessions",
        ]
        forward_columns = [
            f"forward_{day}d_return" for day in range(1, horizon + 1)
        ]
        for optional in ("name", "industry"):
            if optional not in events:
                events[optional] = pd.NA
        return events[[*base_columns, *forward_columns]].copy()

    @staticmethod
    def _summarize_by_industry(events: pd.DataFrame) -> pd.DataFrame:
        """Aggregate each pattern and industry at its requested outcome horizon."""
        rows = []
        for (pattern, industry, horizon), group in events.groupby(
            ["pattern", "industry", "outcome_horizon_sessions"],
            dropna=False,
        ):
            outcome = group[f"forward_{int(horizon)}d_return"].dropna()
            rows.append(
                {
                    "pattern": pattern,
                    "industry": industry,
                    "outcome_horizon_sessions": int(horizon),
                    "event_count": len(group),
                    "labeled_event_count": len(outcome),
                    "mean_cumulative_return": outcome.mean() if len(outcome) else pd.NA,
                    "median_cumulative_return": outcome.median() if len(outcome) else pd.NA,
                    "positive_return_rate": outcome.gt(0.0).mean() if len(outcome) else pd.NA,
                }
            )
        return pd.DataFrame(rows).sort_values(
            ["pattern", "mean_cumulative_return", "industry"],
            ascending=[True, False, True],
            na_position="last",
        ).reset_index(drop=True)

    @staticmethod
    def _empty_industry_summary() -> pd.DataFrame:
        """Return a stable empty result schema for consumers."""
        return pd.DataFrame(
            columns=[
                "pattern",
                "industry",
                "outcome_horizon_sessions",
                "event_count",
                "labeled_event_count",
                "mean_cumulative_return",
                "median_cumulative_return",
                "positive_return_rate",
            ]
        )

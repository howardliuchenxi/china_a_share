import numpy as np
import pandas as pd

from .strategy_models import (
    CumulativeReturnRule,
    DrawdownRule,
    FirstBullishMARule,
    StrategyConfig,
)


_REQUIRED_COLUMNS = {"ts_code", "trade_date", "high_qfq", "close_qfq"}


class RuleEngine:
    """Evaluate validated strategy conditions against forward-adjusted prices."""

    def evaluate(self, frame: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
        """Return one aligned signal row per stock and trading day."""
        if frame.empty:
            return pd.DataFrame(
                columns=["ts_code", "trade_date", "strategy_id", "direction", "signal"]
            )

        missing = sorted(_REQUIRED_COLUMNS - set(frame.columns))
        if missing:
            raise ValueError(f"Strategy input is missing columns: {', '.join(missing)}")
        if frame.duplicated(["ts_code", "trade_date"]).any():
            raise ValueError("Strategy input contains duplicate stock-date rows")

        evaluated = frame.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        for column in ["high_qfq", "close_qfq"]:
            values = pd.to_numeric(evaluated[column], errors="coerce")
            if not np.isfinite(values).all() or (values <= 0).any():
                raise ValueError(f"{column} values must be finite and greater than zero")
            evaluated[column] = values

        combined = pd.Series(True, index=evaluated.index, dtype=bool)
        for rule in config.rules:
            if isinstance(rule, DrawdownRule):
                condition = self._evaluate_drawdown(evaluated, rule)
            elif isinstance(rule, CumulativeReturnRule):
                condition = self._evaluate_cumulative_return(evaluated, rule)
            elif isinstance(rule, FirstBullishMARule):
                condition = self._evaluate_first_bullish_ma(evaluated, rule)
            else:  # pragma: no cover - the discriminated model union prevents this.
                raise ValueError(f"Unsupported rule type: {type(rule).__name__}")
            combined &= condition.fillna(False)

        return pd.DataFrame(
            {
                "ts_code": evaluated["ts_code"],
                "trade_date": evaluated["trade_date"],
                "strategy_id": config.id,
                "direction": config.direction.value,
                "signal": combined,
            }
        )

    @staticmethod
    def _evaluate_drawdown(frame: pd.DataFrame, rule: DrawdownRule) -> pd.Series:
        rolling_high = frame.groupby("ts_code", sort=False)["high_qfq"].transform(
            lambda values: values.rolling(rule.window, min_periods=rule.window).max()
        )
        drawdown = (rolling_high - frame["close_qfq"]) / rolling_high
        return drawdown > rule.threshold

    @staticmethod
    def _evaluate_cumulative_return(
        frame: pd.DataFrame, rule: CumulativeReturnRule
    ) -> pd.Series:
        previous_close = frame.groupby("ts_code", sort=False)["close_qfq"].shift(rule.window)
        cumulative_return = frame["close_qfq"] / previous_close - 1.0
        above_minimum = (cumulative_return > rule.min_return) | np.isclose(
            cumulative_return,
            rule.min_return,
        )
        below_maximum = (cumulative_return < rule.max_return) | np.isclose(
            cumulative_return,
            rule.max_return,
        )
        return above_minimum & below_maximum

    @staticmethod
    def _evaluate_first_bullish_ma(
        frame: pd.DataFrame, rule: FirstBullishMARule
    ) -> pd.Series:
        grouped_close = frame.groupby("ts_code", sort=False)["close_qfq"]
        fast_ma = grouped_close.transform(
            lambda values: values.rolling(rule.fast_window, min_periods=rule.fast_window).mean()
        )
        slow_ma = grouped_close.transform(
            lambda values: values.rolling(rule.slow_window, min_periods=rule.slow_window).mean()
        )
        ticker = frame["ts_code"]
        fast_rising = fast_ma > fast_ma.groupby(ticker, sort=False).shift(1)
        slow_rising = slow_ma > slow_ma.groupby(ticker, sort=False).shift(1)
        bullish_state = (fast_ma > slow_ma) & fast_rising & slow_rising
        previous_state = bullish_state.groupby(ticker, sort=False).shift(1, fill_value=False)
        return bullish_state & ~previous_state

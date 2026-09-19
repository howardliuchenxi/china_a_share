from typing import Any, Dict, List, Optional
import pandas as pd
from .models import StrategyConfig, RuleCondition, Operator, StrategyExecutionRecord, StrategyScanResult
from .data_loader import QFQDataLoader

class RuleEngine:
    def __init__(self, data_loader: QFQDataLoader):
        self.data_loader = data_loader

    def evaluate(self, strategy: StrategyConfig, df: pd.DataFrame, target_date: str) -> StrategyScanResult:
        """
        Evaluate a single strategy against the provided dataframe.
        df must be pre-sorted by date and contain required columns.
        """
        hits = []
        df_target = df[df["trade_date"] == target_date]
        
        if df_target.empty:
            return StrategyScanResult(
                strategy_id=strategy.id,
                strategy_name=strategy.name,
                direction=strategy.direction,
                signal_date=target_date,
                scanned_count=0,
                hits=[]
            )
            
        scanned_count = len(df_target)
        
        # Group by stock to evaluate time-series rules
        grouped = df.groupby("ts_code")
        
        for ts_code, stock_df in grouped:
            if target_date not in stock_df["trade_date"].values:
                continue
                
            hit_reasons = []
            indicators = {}
            passed_all = True
            
            for condition in strategy.conditions:
                try:
                    passed, reason, ind = self._evaluate_condition(condition, stock_df, target_date)
                    if not passed:
                        passed_all = False
                        break
                    hit_reasons.append(reason)
                    indicators.update(ind)
                except Exception as e:
                    # Clear failure, no silent fallback
                    raise ValueError(f"Failed evaluating condition {condition.metric} for {ts_code}: {e}")
                    
            if passed_all:
                current_row = stock_df[stock_df["trade_date"] == target_date].iloc[-1]
                hits.append(StrategyExecutionRecord(
                    strategy_id=strategy.id,
                    stock_code=ts_code,
                    stock_name=ts_code, # Typically requires a join with stock basic for real name
                    signal_date=target_date,
                    direction=strategy.direction,
                    hit_reason="; ".join(hit_reasons),
                    price=float(current_row["close_qfq"]),
                    indicators=indicators
                ))
                
        return StrategyScanResult(
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            direction=strategy.direction,
            signal_date=target_date,
            scanned_count=scanned_count,
            hits=hits
        )

    def _evaluate_condition(self, condition: RuleCondition, stock_df: pd.DataFrame, target_date: str) -> tuple[bool, str, dict]:
        """
        Evaluate one condition for one stock.
        Returns (passed, reason_string, indicators_dict)
        """
        # Ensure we only look at data up to the target date
        stock_df = stock_df[stock_df["trade_date"] <= target_date]
        if len(stock_df) == 0:
             raise ValueError("Insufficient data.")
        current_row = stock_df.iloc[-1]
        
        if condition.metric == "drawdown":
            window = condition.parameters.get("window", 60)
            threshold = condition.parameters.get("threshold", 0.30)
            
            if len(stock_df) < window:
                raise ValueError(f"Insufficient data for drawdown: requires {window} days, found {len(stock_df)}")
                
            window_df = stock_df.tail(window)
            max_high = window_df["high_qfq"].max()
            current_close = current_row["close_qfq"]
            
            if max_high == 0:
                raise ValueError("Max high is 0, cannot calculate drawdown.")
                
            drawdown = (max_high - current_close) / max_high
            
            passed = drawdown > threshold
            reason = f"Drawdown {drawdown:.2%} > {threshold*100:.0f}%"
            indicators = {"window_max": float(max_high), "drawdown": float(drawdown)}
            return passed, reason, indicators
            
        elif condition.metric == "cumulative_return":
            window = condition.parameters.get("window", 10)
            min_val = condition.parameters.get("min", -0.10)
            max_val = condition.parameters.get("max", 0.10)
            
            if len(stock_df) < window + 1:
                 raise ValueError(f"Insufficient data for cumulative return: requires {window+1} days")
                 
            # Note: "最近 10 个交易日" typically means comparing today's close with the close of T-10
            start_price = stock_df.iloc[-(window+1)]["close_qfq"]
            current_close = current_row["close_qfq"]
            
            if start_price == 0:
                raise ValueError("Start price is 0.")
                
            cum_ret = (current_close - start_price) / start_price
            
            passed = min_val <= cum_ret <= max_val
            reason = f"{window}-day Ret {cum_ret:.2%} between {min_val*100:.0f}% and {max_val*100:.0f}%"
            indicators = {f"{window}d_ret": float(cum_ret)}
            return passed, reason, indicators
            
        elif condition.metric == "ma_cross":
            fast = condition.parameters.get("fast", 5)
            slow = condition.parameters.get("slow", 10)
            
            if len(stock_df) < slow + 1:
                raise ValueError(f"Insufficient data for MA cross: requires {slow+1} days")
                
            closes = stock_df["close_qfq"]
            ma_fast = closes.rolling(window=fast).mean()
            ma_slow = closes.rolling(window=slow).mean()
            
            ma_fast_curr = ma_fast.iloc[-1]
            ma_slow_curr = ma_slow.iloc[-1]
            ma_fast_prev = ma_fast.iloc[-2]
            ma_slow_prev = ma_slow.iloc[-2]
            
            if condition.operator == Operator.MA_CROSS_UP_FIRST:
                # MA5 > MA10
                cond1 = ma_fast_curr > ma_slow_curr
                # MA5 > 前一日 MA5
                cond2 = ma_fast_curr > ma_fast_prev
                # MA10 > 前一日 MA10
                cond3 = ma_slow_curr > ma_slow_prev
                # 前一日不同时满足
                prev_cond = (ma_fast_prev > ma_slow_prev) and (ma_fast_prev > ma_fast.iloc[-3] if len(ma_fast) > 2 else False) and (ma_slow_prev > ma_slow.iloc[-3] if len(ma_slow) > 2 else False)
                
                passed = cond1 and cond2 and cond3 and not prev_cond
                reason = f"MA{fast} ({ma_fast_curr:.2f}) crossed MA{slow} ({ma_slow_curr:.2f}) upwards first time"
                indicators = {f"ma{fast}": float(ma_fast_curr), f"ma{slow}": float(ma_slow_curr)}
                return passed, reason, indicators
                
        raise ValueError(f"Unsupported condition: {condition.metric} {condition.operator}")

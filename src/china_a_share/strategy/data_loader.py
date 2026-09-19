import pandas as pd
from typing import Any, Dict, Optional, Protocol, Sequence

from china_a_share.core.ports import MarketDataProvider

class DataQueryExecutor(Protocol):
    def query(self, operation: str, params: Dict[str, Any], fields: Sequence[str]) -> pd.DataFrame:
        ...

class QFQDataLoader:
    """Loads and computes Forward-Adjusted (QFQ) OHLC data for strategy evaluation."""
    
    def __init__(self, provider: MarketDataProvider):
        self._provider = provider
        
    def get_adjusted_history(self, start_date: str, end_date: str) -> pd.DataFrame:
        """
        Fetch daily basics and adjustment factors to compute qfq.
        Formula for qfq: qfq_price = raw_price * (adj_factor / latest_adj_factor)
        """
        daily_df = self._provider.query(
            "daily",
            {"start_date": start_date, "end_date": end_date},
            ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"],
            api_route="/api/analysis/tasks/strategy:daily-scan",
            request_id="internal_strategy_qfq",
            query_id="qfq_daily"
        )
        adj_df = self._provider.query(
            "adj_factor",
            {"start_date": start_date, "end_date": end_date},
            ["ts_code", "trade_date", "adj_factor"],
            api_route="/api/analysis/tasks/strategy:daily-scan",
            request_id="internal_strategy_qfq",
            query_id="qfq_adj"
        )
        
        if daily_df.empty or adj_df.empty:
            return pd.DataFrame()
            
        merged = pd.merge(daily_df, adj_df, on=["ts_code", "trade_date"], how="left")
        merged.sort_values(by=["ts_code", "trade_date"], ascending=[True, True], inplace=True)
        
        # Get the latest adj_factor per stock
        latest_adj = merged.groupby("ts_code")["adj_factor"].last().rename("latest_adj_factor")
        merged = merged.join(latest_adj, on="ts_code")
        
        # Forward fill missing adj_factors
        merged["adj_factor"] = merged["adj_factor"].ffill()
        merged["latest_adj_factor"] = merged["latest_adj_factor"].ffill()
        
        ratio = merged["adj_factor"] / merged["latest_adj_factor"]
        
        merged["open_qfq"] = merged["open"] * ratio
        merged["high_qfq"] = merged["high"] * ratio
        merged["low_qfq"] = merged["low"] * ratio
        merged["close_qfq"] = merged["close"] * ratio
        
        return merged

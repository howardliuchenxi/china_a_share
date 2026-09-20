from typing import Optional, Sequence

import numpy as np
import pandas as pd

from china_a_share.core.ports import MarketDataProvider


_KEY_COLUMNS = ["ts_code", "trade_date"]
_OHLC_COLUMNS = ["open", "high", "low", "close"]


class QFQLoader:
    """Load raw daily prices and expose validated forward-adjusted OHLC fields."""

    def __init__(self, provider: MarketDataProvider) -> None:
        self._provider = provider

    def load_qfq(
        self,
        start_date: str,
        end_date: str,
        ts_codes: Optional[Sequence[str]] = None,
        *,
        api_route: str,
        request_id: str,
    ) -> pd.DataFrame:
        """Return raw and QFQ OHLC rows for the requested stocks and date range."""
        if ts_codes is not None:
            requested_codes = list(dict.fromkeys(ts_codes))
            if not requested_codes:
                return pd.DataFrame()
            params = {
                "ts_code": ",".join(requested_codes),
                "start_date": start_date,
                "end_date": end_date,
            }
        else:
            requested_codes = None
            params = {
                "start_date": start_date,
                "end_date": end_date,
            }

        daily_df = self._provider.query(
            operation="daily",
            params=params,
            fields=[*_KEY_COLUMNS, *_OHLC_COLUMNS],
            api_route=api_route,
            request_id=request_id,
            query_id="strategy_qfq_daily",
        )
        if daily_df.empty:
            return pd.DataFrame()

        adj_df = self._provider.query(
            operation="adj_factor",
            params=params,
            fields=[*_KEY_COLUMNS, "adj_factor"],
            api_route=api_route,
            request_id=request_id,
            query_id="strategy_qfq_factor",
        )
        if adj_df.empty:
            raise ValueError("Adjustment-factor query returned no rows")

        self._validate_columns(daily_df, [*_KEY_COLUMNS, *_OHLC_COLUMNS], "daily")
        self._validate_columns(adj_df, [*_KEY_COLUMNS, "adj_factor"], "adj_factor")
        self._validate_unique_keys(daily_df, "daily")
        self._validate_unique_keys(adj_df, "adj_factor")

        if requested_codes is not None:
            daily_df = daily_df[daily_df["ts_code"].isin(requested_codes)].copy()
            adj_df = adj_df[adj_df["ts_code"].isin(requested_codes)].copy()
        else:
            daily_df = daily_df.copy()
            adj_df = adj_df.copy()

        merged = daily_df.merge(
            adj_df,
            on=_KEY_COLUMNS,
            how="left",
            validate="one_to_one",
            indicator=True,
        )
        if (merged["_merge"] != "both").any():
            raise ValueError("Adjustment factor is missing for one or more daily rows")
        merged.drop(columns="_merge", inplace=True)

        factors = pd.to_numeric(merged["adj_factor"], errors="coerce")
        if not np.isfinite(factors).all() or (factors <= 0).any():
            raise ValueError("Adjustment factors must be finite and greater than zero")

        merged["adj_factor"] = factors
        merged.sort_values(_KEY_COLUMNS, inplace=True, ignore_index=True)
        latest_factor = merged.groupby("ts_code", sort=False)["adj_factor"].transform("last")
        multiplier = merged["adj_factor"] / latest_factor
        for column in _OHLC_COLUMNS:
            prices = pd.to_numeric(merged[column], errors="coerce")
            if not np.isfinite(prices).all() or (prices <= 0).any():
                raise ValueError(f"{column} prices must be finite and greater than zero")
            merged[f"{column}_qfq"] = prices * multiplier
        return merged

    @staticmethod
    def _validate_columns(frame: pd.DataFrame, required: list[str], source: str) -> None:
        missing = sorted(set(required) - set(frame.columns))
        if missing:
            raise ValueError(f"{source} response is missing columns: {', '.join(missing)}")

    @staticmethod
    def _validate_unique_keys(frame: pd.DataFrame, source: str) -> None:
        if frame.duplicated(_KEY_COLUMNS).any():
            raise ValueError(f"{source} response contains duplicate stock-date rows")

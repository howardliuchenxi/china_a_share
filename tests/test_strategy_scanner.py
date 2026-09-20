from __future__ import annotations

from datetime import datetime, timezone
import math

import pandas as pd
import pytest
from pydantic import ValidationError

from china_a_share.discovery.qfq_loader import QFQLoader
from china_a_share.discovery.rule_engine import RuleEngine
from china_a_share.discovery.strategy_models import (
    CumulativeReturnRule,
    DrawdownRule,
    FirstBullishMARule,
    SignalDirection,
    StrategyConfig,
)


class MockProvider:
    """Return deterministic daily and adjustment-factor frames."""

    def __init__(self, daily: pd.DataFrame, factors: pd.DataFrame) -> None:
        self.daily = daily
        self.factors = factors
        self.calls: list[tuple[str, dict, dict]] = []

    @property
    def name(self) -> str:
        return "mock"

    def query(self, operation, params, fields, **kwargs):
        self.calls.append((operation, params, kwargs))
        if operation == "daily":
            return self.daily.copy()
        if operation == "adj_factor":
            return self.factors.copy()
        raise AssertionError(f"Unexpected operation: {operation}")


def strategy(rule, direction=SignalDirection.BUY) -> StrategyConfig:
    """Build one-rule test strategy with a stable identity."""
    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
    return StrategyConfig(
        id="strategy-1",
        name="Test strategy",
        direction=direction,
        rules=rule if isinstance(rule, list) else [rule],
        creator_open_id="user-1",
        enabled=True,
        notification_chat_id="chat-1",
        created_at=now,
        updated_at=now,
    )


def price_frame(closes: list[float], highs: list[float] | None = None) -> pd.DataFrame:
    """Build one-stock QFQ input in ascending trading-date order."""
    return pd.DataFrame(
        {
            "ts_code": ["000001.SZ"] * len(closes),
            "trade_date": [f"202401{day:02d}" for day in range(1, len(closes) + 1)],
            "high_qfq": highs or closes,
            "close_qfq": closes,
        }
    )


def test_drawdown_uses_strict_threshold_boundary() -> None:
    frame = price_frame(
        closes=[100.0, 90.0, 80.0, 70.0, 69.0],
        highs=[100.0, 100.0, 100.0, 100.0, 100.0],
    )
    result = RuleEngine().evaluate(
        frame,
        strategy(DrawdownRule(window=4, threshold=0.30)),
    )

    assert result["signal"].tolist() == [False, False, False, False, True]


@pytest.mark.parametrize("ending_close", [9.0, 11.0])
def test_cumulative_return_includes_both_ten_percent_boundaries(
    ending_close: float,
) -> None:
    frame = price_frame([10.0, 10.0, 10.0, ending_close])
    result = RuleEngine().evaluate(
        frame,
        strategy(
            CumulativeReturnRule(window=3, min_return=-0.10, max_return=0.10),
            direction=SignalDirection.SELL,
        ),
    )

    assert result["signal"].tolist() == [False, False, False, True]
    assert result["direction"].tolist() == ["sell"] * 4


def test_first_bullish_ma_only_marks_the_transition_day() -> None:
    frame = price_frame([10.0, 10.0, 10.0, 12.0, 14.0, 16.0])
    result = RuleEngine().evaluate(
        frame,
        strategy(FirstBullishMARule(fast_window=2, slow_window=3)),
    )

    assert result["signal"].tolist() == [False, False, False, True, False, False]


def test_rules_are_combined_with_logical_and() -> None:
    frame = price_frame(
        closes=[100.0, 100.0, 100.0, 69.0],
        highs=[100.0, 100.0, 100.0, 100.0],
    )
    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
    config = StrategyConfig(
        id="strategy-1",
        name="Combined",
        direction=SignalDirection.BUY,
        rules=[
            DrawdownRule(window=4, threshold=0.30),
            CumulativeReturnRule(window=3, min_return=-0.10, max_return=0.10),
        ],
        creator_open_id="user-1",
        enabled=True,
        notification_chat_id="chat-1",
        created_at=now,
        updated_at=now,
    )

    assert not RuleEngine().evaluate(frame, config)["signal"].any()


def test_insufficient_history_produces_no_signal() -> None:
    frame = price_frame([10.0, 11.0])
    result = RuleEngine().evaluate(
        frame,
        strategy(DrawdownRule(window=5, threshold=0.10)),
    )

    assert result["signal"].tolist() == [False, False]


def test_strategy_reports_exact_required_history() -> None:
    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
    config = StrategyConfig(
        id="strategy-1",
        name="Lookback",
        direction=SignalDirection.BUY,
        rules=[
            DrawdownRule(window=60, threshold=0.30),
            CumulativeReturnRule(window=10, min_return=-0.10, max_return=0.10),
            FirstBullishMARule(fast_window=5, slow_window=10),
        ],
        creator_open_id="user-1",
        enabled=True,
        notification_chat_id="chat-1",
        created_at=now,
        updated_at=now,
    )

    assert config.required_history_rows == 60
    assert strategy(CumulativeReturnRule(window=10, min_return=-0.10, max_return=0.10)).required_history_rows == 11
    assert strategy(FirstBullishMARule(fast_window=5, slow_window=10)).required_history_rows == 12


def test_invalid_rule_contracts_fail_fast() -> None:
    with pytest.raises(ValidationError):
        DrawdownRule(window=0, threshold=0.30)
    with pytest.raises(ValidationError):
        DrawdownRule(window=60, threshold=1.0)
    with pytest.raises(ValidationError):
        CumulativeReturnRule(window=10, min_return=0.10, max_return=-0.10)
    with pytest.raises(ValidationError):
        FirstBullishMARule(fast_window=10, slow_window=5)
    with pytest.raises(ValidationError):
        StrategyConfig(id="strategy-1", name="Empty", direction="buy", rules=[])


def test_rule_engine_isolates_tickers() -> None:
    frame = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ", "000002.SZ", "000002.SZ"],
            "trade_date": ["20240101", "20240102", "20240101", "20240102"],
            "high_qfq": [100.0, 100.0, 100.0, 100.0],
            "close_qfq": [100.0, 60.0, 100.0, 90.0],
        }
    )
    result = RuleEngine().evaluate(
        frame,
        strategy(DrawdownRule(window=2, threshold=0.30)),
    )

    assert result["signal"].tolist() == [False, True, False, False]


def test_qfq_loader_preserves_raw_prices_and_smooths_split() -> None:
    daily = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "trade_date": ["20240101", "20240102"],
            "open": [20.0, 10.0],
            "high": [20.0, 10.0],
            "low": [20.0, 10.0],
            "close": [20.0, 10.0],
        }
    )
    factors = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "trade_date": ["20240101", "20240102"],
            "adj_factor": [1.0, 2.0],
        }
    )

    result = QFQLoader(MockProvider(daily, factors)).load_qfq(
        "20240101",
        "20240102",
        ["000001.SZ"],
        api_route="test",
        request_id="request-1",
    )

    assert result["close"].tolist() == [20.0, 10.0]
    assert result["close_qfq"].tolist() == [10.0, 10.0]


def test_qfq_loader_keeps_factor_calculation_isolated_by_ticker() -> None:
    daily = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ", "000002.SZ", "000002.SZ"],
            "trade_date": ["20240101", "20240102", "20240101", "20240102"],
            "open": [20.0, 10.0, 30.0, 30.0],
            "high": [20.0, 10.0, 30.0, 30.0],
            "low": [20.0, 10.0, 30.0, 30.0],
            "close": [20.0, 10.0, 30.0, 30.0],
        }
    )
    factors = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ", "000002.SZ", "000002.SZ"],
            "trade_date": ["20240101", "20240102", "20240101", "20240102"],
            "adj_factor": [1.0, 2.0, 5.0, 5.0],
        }
    )

    result = QFQLoader(MockProvider(daily, factors)).load_qfq(
        "20240101",
        "20240102",
        ["000001.SZ", "000002.SZ"],
        api_route="preview",
        request_id="request-2",
    )

    assert result[result["ts_code"] == "000001.SZ"]["close_qfq"].tolist() == [10.0, 10.0]
    assert result[result["ts_code"] == "000002.SZ"]["close_qfq"].tolist() == [30.0, 30.0]


@pytest.mark.parametrize("invalid_factor", [0.0, -1.0, math.nan, math.inf])
def test_qfq_loader_rejects_invalid_adjustment_factors(invalid_factor: float) -> None:
    daily = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": ["20240101"],
            "open": [10.0],
            "high": [10.0],
            "low": [10.0],
            "close": [10.0],
        }
    )
    factors = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": ["20240101"],
            "adj_factor": [invalid_factor],
        }
    )

    with pytest.raises(ValueError, match="finite and greater than zero"):
        QFQLoader(MockProvider(daily, factors)).load_qfq(
            "20240101",
            "20240101",
            ["000001.SZ"],
            api_route="test",
            request_id="request-3",
        )


def test_qfq_loader_rejects_missing_factor_rows() -> None:
    daily = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "trade_date": ["20240101", "20240102"],
            "open": [20.0, 10.0],
            "high": [20.0, 10.0],
            "low": [20.0, 10.0],
            "close": [20.0, 10.0],
        }
    )
    factors = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": ["20240101"],
            "adj_factor": [1.0],
        }
    )

    with pytest.raises(ValueError, match="missing"):
        QFQLoader(MockProvider(daily, factors)).load_qfq(
            "20240101",
            "20240102",
            ["000001.SZ"],
            api_route="test",
            request_id="request-4",
        )


def test_qfq_loader_full_market_mode_omits_ts_code() -> None:
    daily = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ", "000002.SZ", "000002.SZ"],
            "trade_date": ["20240101", "20240102", "20240101", "20240102"],
            "open": [20.0, 10.0, 30.0, 30.0],
            "high": [20.0, 10.0, 30.0, 30.0],
            "low": [20.0, 10.0, 30.0, 30.0],
            "close": [20.0, 10.0, 30.0, 30.0],
        }
    )
    factors = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ", "000002.SZ", "000002.SZ"],
            "trade_date": ["20240101", "20240102", "20240101", "20240102"],
            "adj_factor": [1.0, 2.0, 5.0, 5.0],
        }
    )

    mock_provider = MockProvider(daily, factors)
    result = QFQLoader(mock_provider).load_qfq(
        "20240101",
        "20240102",
        ts_codes=None,  # Omitting ts_codes triggers full-market mode
        api_route="full-market",
        request_id="request-fm-1",
    )

    # 1. Full-market mode: ts_code must be omitted from params
    assert len(mock_provider.calls) == 2
    for operation, params, kwargs in mock_provider.calls:
        assert "ts_code" not in params
        assert params["start_date"] == "20240101"
        assert params["end_date"] == "20240102"

    # 2. Daily and adj_factor should be completely mapped and correct
    assert len(result) == 4
    # 3. Multi-stock split remains isolated
    assert result[result["ts_code"] == "000001.SZ"]["close_qfq"].tolist() == [10.0, 10.0]
    assert result[result["ts_code"] == "000002.SZ"]["close_qfq"].tolist() == [30.0, 30.0]

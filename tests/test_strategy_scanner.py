from __future__ import annotations

from datetime import datetime, timezone
import math

import pandas as pd
import pytest
from pydantic import ValidationError

from china_a_share.discovery.qfq_loader import QFQLoader
from china_a_share.discovery.rule_engine import RuleEngine, limit_up_price
from china_a_share.discovery.strategy_models import (
    CumulativeReturnRule,
    DrawdownRule,
    FirstBullishMARule,
    LimitUpRule,
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
            "close": closes,
            "close_qfq": closes,
        }
    )


def multi_stock_frame(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    """Build two-day raw-close rows as (ts_code, previous close, close) triples."""
    codes = [code for code, _, _ in rows]
    previous = [previous for _, previous, _ in rows]
    current = [close for _, _, close in rows]
    return pd.DataFrame(
        {
            "ts_code": codes * 2,
            "trade_date": ["20260917"] * len(rows) + ["20260918"] * len(rows),
            "high_qfq": previous + current,
            "close": previous + current,
            "close_qfq": previous + current,
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


def test_limit_up_detects_exact_exchange_rounding() -> None:
    # 4.03 * 1.10 = 4.433 -> exchange limit price 4.43 (half-up), so 4.43 closes at limit.
    assert limit_up_price(4.03, "000001.SZ") == 4.43
    # 100.00 * 1.20 = 120.00 exactly on the ChiNext 20% limit.
    assert limit_up_price(100.00, "300750.SZ") == 120.00
    # BSE stocks carry a 30% limit.
    assert limit_up_price(10.00, "833171.BJ") == 13.00


def test_limit_up_signals_only_close_at_limit_per_board() -> None:
    frame = multi_stock_frame(
        [
            ("000001.SZ", 10.00, 11.00),  # main board, exactly +10% -> limit up
            ("000002.SZ", 10.00, 10.98),  # main board, +9.8% -> not limit up
            ("000003.SZ", 4.03, 4.43),  # low-price rounding edge -> limit up
            ("300750.SZ", 100.00, 120.00),  # ChiNext 20% -> limit up
            ("300751.SZ", 100.00, 118.00),  # ChiNext +18% -> not limit up
            ("688001.SH", 50.00, 60.00),  # STAR 20% -> limit up
            ("833171.BJ", 10.00, 13.00),  # BSE 30% -> limit up
            ("900001.SH", 10.00, 11.00),  # B-share stays on the 10% limit -> limit up
        ]
    )
    result = RuleEngine().evaluate(frame, strategy(LimitUpRule()))
    signals = dict(zip(result["ts_code"], result["signal"]))
    assert signals == {
        "000001.SZ": True,
        "000002.SZ": False,
        "000003.SZ": True,
        "300750.SZ": True,
        "300751.SZ": False,
        "688001.SH": True,
        "833171.BJ": True,
        "900001.SH": True,
    }


def test_limit_up_window_reaches_back_but_requires_full_history() -> None:
    frame = price_frame([10.00, 11.00, 11.00])
    window_one = RuleEngine().evaluate(frame, strategy(LimitUpRule(window=1)))
    window_two = RuleEngine().evaluate(frame, strategy(LimitUpRule(window=2)))
    # Day 3 is flat; the 1-day window loses day 2's limit-up while the
    # 2-day window still sees it (the window includes the current day, so
    # day 2 itself also qualifies under its own trailing 2-day window).
    assert window_one["signal"].tolist() == [False, True, False]
    assert window_two["signal"].tolist() == [False, True, True]


def test_limit_up_requires_previous_close() -> None:
    # First trading day of a listing has no previous close, so no signal.
    frame = price_frame([10.00])
    result = RuleEngine().evaluate(frame, strategy(LimitUpRule()))
    assert result["signal"].tolist() == [False]


def test_limit_up_uses_nominal_close_not_adjusted_prices() -> None:
    # Raw close 10.00 -> 11.00 is a true +10% limit-up, while the adjusted
    # close moved only 10.0 -> 10.5 because of a dividend; the rule must judge
    # the nominal price and still fire.
    frame = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"] * 2,
            "trade_date": ["20260917", "20260918"],
            "high_qfq": [10.0, 10.5],
            "close": [10.00, 11.00],
            "close_qfq": [10.0, 10.5],
        }
    )
    result = RuleEngine().evaluate(frame, strategy(LimitUpRule()))
    assert result["signal"].tolist() == [False, True]


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
            "close": [100.0, 60.0, 100.0, 90.0],
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

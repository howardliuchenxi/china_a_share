from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from china_a_share.discovery.strategy_models import (
    DrawdownRule,
    SignalDirection,
    StrategyConfig,
)
from china_a_share.discovery.strategy_scanner import StrategyScanner
from china_a_share.discovery.strategy_store import MemoryStrategyStore


SIGNAL_DATE = "20260918"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeLoader:
    def __init__(self, latest_date: str = SIGNAL_DATE) -> None:
        self.latest_date = latest_date
        self.calls: list[dict] = []

    def load_qfq(self, start_date, end_date, ts_codes, **kwargs):
        self.calls.append(
            {
                "start_date": start_date,
                "end_date": end_date,
                "ts_codes": ts_codes,
                **kwargs,
            }
        )
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "000002.SZ"],
                "trade_date": [self.latest_date, self.latest_date],
            }
        )


class FakeEngine:
    def __init__(self, matches: dict[str, set[str]], failing: set[str] | None = None):
        self.matches = matches
        self.failing = failing or set()

    def evaluate(self, frame, strategy):
        if strategy.id in self.failing:
            raise RuntimeError("evaluation failed")
        result = frame.copy()
        matched = self.matches.get(strategy.id, set())
        result["signal"] = result["ts_code"].isin(matched)
        return result


class FakeSender:
    def __init__(self) -> None:
        self.attempts: list[tuple[str, dict]] = []
        self.cards: list[tuple[str, dict]] = []
        self.failures: dict[str, list[Exception]] = {}

    def send_chat_card(self, chat_id, card):
        self.attempts.append((chat_id, card))
        failures = self.failures.get(chat_id, [])
        if failures:
            raise failures.pop(0)
        self.cards.append((chat_id, card))
        return f"message-{len(self.cards)}"


class CountingStore(MemoryStrategyStore):
    def __init__(self) -> None:
        super().__init__()
        self.mark_calls = 0
        self.dedup_reads = 0

    def mark_notification_sent(self, *args, **kwargs):
        self.mark_calls += 1
        return super().mark_notification_sent(*args, **kwargs)

    def is_notification_sent(self, *args, **kwargs):
        self.dedup_reads += 1
        return super().is_notification_sent(*args, **kwargs)


def make_strategy(
    strategy_id: str,
    owner: str = "owner-1",
    chat_id: str | None = None,
    direction: SignalDirection = SignalDirection.BUY,
) -> StrategyConfig:
    now = datetime(2026, 9, 18, 8, 0, tzinfo=SHANGHAI)
    return StrategyConfig(
        id=strategy_id,
        name=f"Strategy {strategy_id}",
        direction=direction,
        rules=[DrawdownRule(window=2, threshold=0.30)],
        creator_open_id=owner,
        enabled=True,
        notification_chat_id=chat_id or f"chat-{strategy_id}",
        created_at=now,
        updated_at=now,
    )


def scanner(loader, engine, store, sender):
    return StrategyScanner(
        loader,
        engine,
        store,
        sender,
        clock=lambda: datetime(2026, 9, 18, 16, 30, tzinfo=SHANGHAI),
    )


def card_content(card: dict) -> str:
    return card["elements"][0]["text"]["content"]


def test_daily_scan_loads_once_and_sends_one_card_per_strategy() -> None:
    loader = FakeLoader()
    store = MemoryStrategyStore()
    first = make_strategy("first")
    second = make_strategy("second", direction=SignalDirection.SELL)
    store.put_strategy(first, first.creator_open_id)
    store.put_strategy(second, second.creator_open_id)
    sender = FakeSender()

    scanner(
        loader,
        FakeEngine({"first": {"000001.SZ"}, "second": set()}),
        store,
        sender,
    ).run_daily_scan("request-1")

    assert len(loader.calls) == 1
    assert loader.calls[0]["ts_codes"] is None
    assert [chat_id for chat_id, _ in sender.cards] == ["chat-first", "chat-second"]
    assert "000001.SZ" in card_content(sender.cards[0][1])
    assert "本次扫描未命中任何标的" in card_content(sender.cards[1][1])
    assert "Strategy first" not in card_content(sender.cards[1][1])
    assert store.is_notification_sent(
        "first", "000001.SZ", SIGNAL_DATE, SignalDirection.BUY
    )


def test_daily_scan_distinguishes_deduplicated_matches() -> None:
    loader = FakeLoader()
    strategy = make_strategy("dedup")
    store = MemoryStrategyStore()
    store.put_strategy(strategy, strategy.creator_open_id)
    store.mark_notification_sent(
        strategy.id, "000001.SZ", SIGNAL_DATE, strategy.direction
    )
    sender = FakeSender()

    scanner(
        loader,
        FakeEngine({strategy.id: {"000001.SZ"}}),
        store,
        sender,
    ).run_daily_scan("request-2")

    assert len(sender.cards) == 1
    assert "本次扫描无新增可通知信号" in card_content(sender.cards[0][1])


def test_failed_send_is_not_marked_and_retry_delivers() -> None:
    strategy = make_strategy("retry")
    store = MemoryStrategyStore()
    store.put_strategy(strategy, strategy.creator_open_id)
    sender = FakeSender()
    sender.failures[strategy.notification_chat_id] = [RuntimeError("network down")]
    subject = scanner(
        FakeLoader(),
        FakeEngine({strategy.id: {"000001.SZ"}}),
        store,
        sender,
    )

    with pytest.raises(RuntimeError, match="retry"):
        subject.run_daily_scan("request-3")
    assert not store.is_notification_sent(
        strategy.id, "000001.SZ", SIGNAL_DATE, strategy.direction
    )

    subject.run_daily_scan("request-4")
    assert len(sender.cards) == 1
    assert store.is_notification_sent(
        strategy.id, "000001.SZ", SIGNAL_DATE, strategy.direction
    )


def test_manual_preview_ignores_and_does_not_mutate_deduplication() -> None:
    strategy = make_strategy("preview")
    store = CountingStore()
    store.put_strategy(strategy, strategy.creator_open_id)
    store.mark_notification_sent(
        strategy.id, "000001.SZ", SIGNAL_DATE, strategy.direction
    )
    store.mark_calls = 0
    sender = FakeSender()

    scanner(
        FakeLoader(),
        FakeEngine({strategy.id: {"000001.SZ"}}),
        store,
        sender,
    ).run_manual_preview(strategy.creator_open_id, "request-5")

    assert store.dedup_reads == 0
    assert store.mark_calls == 0
    assert "[预览]" in sender.cards[0][1]["header"]["title"]["content"]
    assert "000001.SZ" in card_content(sender.cards[0][1])


def test_strategy_failure_isolated_but_reported_after_later_strategy() -> None:
    store = MemoryStrategyStore()
    first = make_strategy("bad")
    second = make_strategy("good")
    store.put_strategy(first, first.creator_open_id)
    store.put_strategy(second, second.creator_open_id)
    sender = FakeSender()

    with pytest.raises(RuntimeError, match="bad"):
        scanner(
            FakeLoader(),
            FakeEngine({"good": set()}, failing={"bad"}),
            store,
            sender,
        ).run_daily_scan("request-6")

    assert [chat_id for chat_id, _ in sender.cards] == ["chat-good"]


def test_at_all_permission_failure_retries_with_visible_warning() -> None:
    strategy = make_strategy("permission")
    store = MemoryStrategyStore()
    store.put_strategy(strategy, strategy.creator_open_id)
    sender = FakeSender()
    sender.failures[strategy.notification_chat_id] = [
        RuntimeError("Feishu card failed with code 230006")
    ]

    scanner(
        FakeLoader(), FakeEngine({strategy.id: set()}), store, sender
    ).run_daily_scan("request-7")

    assert len(sender.attempts) == 2
    assert len(sender.cards) == 1
    content = card_content(sender.cards[0][1])
    assert '<at id="all"></at>' not in content
    assert "全员提醒失败" in content


def test_unrelated_send_error_does_not_trigger_at_all_fallback() -> None:
    strategy = make_strategy("send-error")
    store = MemoryStrategyStore()
    store.put_strategy(strategy, strategy.creator_open_id)
    sender = FakeSender()
    sender.failures[strategy.notification_chat_id] = [RuntimeError("timeout")]

    with pytest.raises(RuntimeError, match="send-error"):
        scanner(
            FakeLoader(), FakeEngine({strategy.id: set()}), store, sender
        ).run_daily_scan("request-8")

    assert len(sender.attempts) == 1


def test_scheduled_non_trading_day_is_no_op() -> None:
    strategy = make_strategy("holiday")
    store = MemoryStrategyStore()
    store.put_strategy(strategy, strategy.creator_open_id)
    sender = FakeSender()

    scanner(
        FakeLoader(latest_date="20260917"),
        FakeEngine({strategy.id: {"000001.SZ"}}),
        store,
        sender,
    ).run_daily_scan("request-9")

    assert sender.cards == []

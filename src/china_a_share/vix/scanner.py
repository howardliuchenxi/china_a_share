"""Hourly VIX scan: evaluate per-chat rules against one quote and alert.

Delivery is at-least-once: a per-rule-per-date claim object must be created
atomically before sending; a failed send releases the claim so the next hourly
run retries. While a rule has an open position, the same event never alerts
twice; when the exit condition is met the scanner sends one closing report
comparing the actual S&P 500 return with the rule's expected return.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Callable, Dict, List, Optional, Protocol

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage

from china_a_share.observability import log_event
from china_a_share.vix.history import VixHistory
from china_a_share.vix.positions import (
    VixPosition,
    VixPositionStore,
    close_reason,
)
from china_a_share.vix.rules import VixRule, VixRuleStore
from china_a_share.vix.source import VixQuote, eastern_wall_clock, fetch_vix_quote

logger = logging.getLogger(__name__)

ALERTS_OBJECT_PREFIX = "vix/alerts/"


class AlertDispatcher(Protocol):
    """Send one proactive text message to a chat."""

    def send_chat_text(self, chat_id: str, text: str) -> str:
        ...


class AlertClaimStore(Protocol):
    """Claim one rule/chat/date notification exactly once."""

    def claim(self, chat_id: str, rule_id: str, date_str: str) -> bool:
        ...

    def release(self, chat_id: str, rule_id: str, date_str: str) -> None:
        ...


class MemoryAlertClaimStore:
    """In-memory claim store for tests and single-process deployments."""

    def __init__(self) -> None:
        self._claimed: set = set()

    def claim(self, chat_id: str, rule_id: str, date_str: str) -> bool:
        key = (chat_id, rule_id, date_str)
        if key in self._claimed:
            return False
        self._claimed.add(key)
        return True

    def release(self, chat_id: str, rule_id: str, date_str: str) -> None:
        self._claimed.discard((chat_id, rule_id, date_str))


class CloudStorageAlertClaimStore:
    """Persist notification claims as one object per rule/chat/date."""

    def __init__(
        self,
        bucket_name: str,
        storage_client: Optional[storage.Client] = None,
    ) -> None:
        if not bucket_name:
            raise ValueError("A bucket name is required for the VIX claim store.")
        self._bucket = (storage_client or storage.Client()).bucket(bucket_name)

    def claim(self, chat_id: str, rule_id: str, date_str: str) -> bool:
        blob = self._bucket.blob(self._object_name(chat_id, rule_id, date_str))
        try:
            # Generation-zero precondition gives exactly one scan the claim;
            # concurrent scans and scheduler retries observe PreconditionFailed.
            blob.upload_from_string(
                json.dumps({"chat_id": chat_id, "rule_id": rule_id, "date": date_str}),
                content_type="application/json",
                if_generation_match=0,
            )
        except PreconditionFailed:
            return False
        return True

    def release(self, chat_id: str, rule_id: str, date_str: str) -> None:
        blob = self._bucket.blob(self._object_name(chat_id, rule_id, date_str))
        try:
            blob.delete()
        except Exception as exc:
            # A release failure only risks a missed retry next hour, never a
            # duplicate alert, because the claim already exists.
            log_event(
                logger,
                logging.WARNING,
                "vix_alert_claim_release_failed",
                rule_id=rule_id,
                reason=str(exc)[:200],
            )

    @staticmethod
    def _object_name(chat_id: str, rule_id: str, date_str: str) -> str:
        chat_key = hashlib.sha256(chat_id.encode()).hexdigest()
        return f"{ALERTS_OBJECT_PREFIX}{chat_key}/{rule_id}/{date_str}.json"


def compose_alert_message(
    quote: VixQuote,
    rules: List[VixRule],
    entry_index: Optional[float],
) -> str:
    """Render the group alert for one quote and its hit rules."""
    direction = "上涨" if quote.change_pct >= 0 else "下跌"
    rule_lines = "\n".join(
        f"· {rule.describe_full()}（本次命中）" for rule in rules
    )
    entry_part = (
        f"标普500 {entry_index:.2f} 点。"
        if entry_index is not None
        else "标普500点位暂不可得。"
    )
    return (
        f"【VIX 提醒】{quote.date} VIX 现值 {quote.close:.2f}，"
        f"较前收 {quote.prev_close:.2f} {direction} {abs(quote.change_pct):.2f}%。\n"
        f"入场参考：{entry_part}\n"
        f"命中规则：\n{rule_lines}\n"
        f"同一事件范围内不再重复提醒；满足卖出条件后会另发对账消息。"
    )


def compose_closing_message(
    position: VixPosition,
    quote: VixQuote,
    exit_index: Optional[float],
    reason: str,
) -> str:
    """Render the entry-versus-exit bookkeeping report for one closed alert."""
    lines = [
        f"【VIX 对账】规则 [{position.rule_id}] 事件结束：{reason}。",
        f"入场 {position.entry_date}：VIX {position.entry_vix:.2f}，"
        + (
            f"标普500 {position.entry_index:.2f}。"
            if position.entry_index is not None
            else "标普500点位缺失。"
        ),
        f"出场 {quote.date}：VIX {quote.close:.2f}，"
        + (
            f"标普500 {exit_index:.2f}。"
            if exit_index is not None
            else "标普500点位缺失。"
        ),
    ]
    if position.entry_index is not None and exit_index is not None:
        actual = (exit_index / position.entry_index - 1.0) * 100.0
        expected = position.expected_return_pct
        verdict = "达到预期" if actual >= expected else "不及预期"
        lines.append(
            f"实际收益 {actual:+.2f}%，预期收益 {expected:+.2f}%，{verdict}。"
        )
    else:
        lines.append(
            "实际收益无法计算（入场或出场点位缺失），"
            f"预期收益 {position.expected_return_pct:+.2f}%。"
        )
    return "\n".join(lines)


@dataclass
class VixAlertService:
    """Own the hourly scan over quote, history, per-chat rules, and delivery."""

    history: VixHistory
    rules: VixRuleStore
    claims: AlertClaimStore
    dispatcher: AlertDispatcher
    quote_fetcher: Callable[[], Optional[VixQuote]]
    now_fn: Callable[[], datetime]
    positions: Optional[VixPositionStore] = None
    index_quote_fetcher: Callable[[], Optional[VixQuote]] = field(default=lambda: None)

    # -- main scan ----------------------------------------------------------

    def run_hourly_scan(self) -> Dict:
        """Run one scan and return a JSON-serializable summary."""
        now_eastern = eastern_wall_clock(self.now_fn())
        quote = self.quote_fetcher()
        if quote is None:
            return {"status": "skipped_no_quote"}
        if not quote.evaluable(now_eastern):
            return {
                "status": "skipped_stale_quote",
                "quote_date": quote.date,
                "eastern_date": now_eastern.date().isoformat(),
            }
        settled = quote.settled(now_eastern)
        if settled:
            self.history.ensure_seeded()
            self.history.record_settlement(quote.date, quote.close)

        positions_closed = self._close_due_positions(quote)
        alerts = self._deliver_new_alerts(quote, now_eastern)
        return {
            "status": "completed",
            "quote_date": quote.date,
            "vix_close": quote.close,
            "prev_close": quote.prev_close,
            "change_pct": round(quote.change_pct, 2),
            "settled_recorded": settled,
            **alerts,
            "positions_closed": positions_closed,
        }

    def _deliver_new_alerts(self, quote: VixQuote, now_eastern) -> Dict:
        hits_by_chat: Dict[str, List[VixRule]] = {}
        chat_count = 0
        for chat_id in self.rules.iter_chat_ids():
            chat_rules = self.rules.load(chat_id)
            chat_count += 1
            for rule in chat_rules:
                if not rule.hits(quote.change_pct):
                    continue
                if self.positions is not None and self.positions.get(chat_id, rule.id):
                    # Same event range: an open position suppresses re-alerts.
                    continue
                hits_by_chat.setdefault(chat_id, []).append(rule)

        sent = 0
        failed = 0
        opened = 0
        delivered: List[Dict] = []
        entry_index = self._best_effort_index_level()
        for chat_id, hit_rules in hits_by_chat.items():
            claimed = [
                rule
                for rule in hit_rules
                if self.claims.claim(chat_id, rule.id, quote.date)
            ]
            if not claimed:
                continue
            try:
                self.dispatcher.send_chat_text(
                    chat_id, compose_alert_message(quote, claimed, entry_index)
                )
            except Exception as exc:
                failed += 1
                for rule in claimed:
                    self.claims.release(chat_id, rule.id, quote.date)
                log_event(
                    logger,
                    logging.WARNING,
                    "vix_alert_send_failed",
                    rule_ids=[rule.id for rule in claimed],
                    reason=str(exc)[:300],
                )
                continue
            sent += 1
            if self.positions is not None:
                for rule in claimed:
                    self.positions.put(
                        VixPosition.from_rule(rule, chat_id, quote, entry_index)
                    )
                    opened += 1
            delivered.append(
                {"chat_id": chat_id, "rule_ids": [rule.id for rule in claimed]}
            )
        return {
            "chats_scanned": chat_count,
            "alerts_sent": sent,
            "alerts_failed": failed,
            "positions_opened": opened,
            "delivered": delivered,
        }

    def _close_due_positions(self, quote: VixQuote) -> int:
        if self.positions is None:
            return 0
        closed = 0
        for position in self.positions.iter_all():
            reason = close_reason(
                position, quote, self._trading_days_since(position.entry_date, quote)
            )
            if reason is None:
                continue
            exit_index = self._best_effort_index_level()
            try:
                self.dispatcher.send_chat_text(
                    position.chat_id,
                    compose_closing_message(position, quote, exit_index, reason),
                )
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "vix_position_close_send_failed",
                    rule_id=position.rule_id,
                    reason=str(exc)[:300],
                )
                continue  # retried on the next hourly scan
            self.positions.delete(position.chat_id, position.rule_id)
            closed += 1
        return closed

    def _trading_days_since(self, entry_date: str, quote: VixQuote) -> int:
        """Count distinct settled close dates recorded after the entry date."""
        closes = self.history.closes()
        return sum(
            1
            for date_str in closes
            if entry_date < date_str <= quote.date
        )

    def _best_effort_index_level(self) -> Optional[float]:
        try:
            index_quote = self.index_quote_fetcher()
        except Exception:
            return None
        if index_quote is None:
            return None
        return index_quote.close

    # -- lookback -----------------------------------------------------------

    def lookback(self, date_str: str) -> Optional[Dict]:
        """Return close/prev/change data for one date, live quote for today."""
        self.history.ensure_seeded()
        stored = self.history.lookup(date_str)
        if stored is not None:
            prev_date, prev_close, close = stored
            return {
                "date": date_str,
                "close": close,
                "prev_date": prev_date,
                "prev_close": prev_close,
                "change_pct": (close / prev_close - 1.0) * 100.0,
                "source": "history",
            }
        now_eastern = eastern_wall_clock(self.now_fn())
        quote = self.quote_fetcher()
        if (
            quote is not None
            and quote.date == date_str
            and quote.evaluable(now_eastern)
        ):
            return {
                "date": date_str,
                "close": quote.close,
                "prev_date": "",
                "prev_close": quote.prev_close,
                "change_pct": quote.change_pct,
                "source": "quote",
            }
        return None


def build_default_quote_fetcher() -> Callable[[], Optional[VixQuote]]:
    """Return the production fetcher so assembly stays declarative."""
    return fetch_vix_quote


def default_now_fn() -> datetime:
    return datetime.now(timezone.utc)

"""Hourly VIX scan: evaluate per-chat rules against one quote and alert.

Delivery is at-least-once: a per-rule-per-date claim object must be created
atomically before sending; a failed send releases the claim so the next hourly
run retries. One aggregated message goes to each chat per scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Callable, Dict, List, Optional, Protocol

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage

from china_a_share.observability import log_event
from china_a_share.vix.history import VixHistory
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
    """Persist notification claims as one empty object per rule/chat/date."""

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


def compose_alert_message(quote: VixQuote, rules: List[VixRule]) -> str:
    """Render the group alert for one quote and its hit rules."""
    direction = "上涨" if quote.change_pct >= 0 else "下跌"
    rule_lines = "\n".join(f"· {rule.describe()}" for rule in rules)
    return (
        f"【VIX 提醒】{quote.date} VIX 现值 {quote.close:.2f}，"
        f"较前收 {quote.prev_close:.2f} {direction} {abs(quote.change_pct):.2f}%。\n"
        f"命中规则：\n{rule_lines}"
    )


@dataclass
class VixAlertService:
    """Own the hourly scan over quote, history, per-chat rules, and delivery."""

    history: VixHistory
    rules: VixRuleStore
    claims: AlertClaimStore
    dispatcher: AlertDispatcher
    quote_fetcher: Callable[[], Optional[VixQuote]]
    now_fn: Callable[[], datetime]

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

        hits_by_chat: Dict[str, List[VixRule]] = {}
        chat_count = 0
        for chat_id in self.rules.iter_chat_ids():
            chat_rules = self.rules.load(chat_id)
            chat_count += 1
            for rule in chat_rules:
                if not rule.hits(quote.change_pct):
                    continue
                hits_by_chat.setdefault(chat_id, []).append(rule)

        sent = 0
        failed = 0
        delivered: List[Dict] = []
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
                    chat_id, compose_alert_message(quote, claimed)
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
            delivered.append(
                {
                    "chat_id": chat_id,
                    "rule_ids": [rule.id for rule in claimed],
                }
            )
        return {
            "status": "completed",
            "quote_date": quote.date,
            "vix_close": quote.close,
            "prev_close": quote.prev_close,
            "change_pct": round(quote.change_pct, 2),
            "settled_recorded": settled,
            "chats_scanned": chat_count,
            "alerts_sent": sent,
            "alerts_failed": failed,
            "delivered": delivered,
        }

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

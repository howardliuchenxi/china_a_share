"""Open alert positions: one per rule, from trigger to exit bookkeeping.

A trigger notification opens a position (the implied buy the user defaults
to). While a position is open, the same rule never re-alerts — that is the
event-level deduplication the product requires ("same event range, no second
notice"). When the rule's exit condition is met, the scanner closes the
position and reports actual versus expected return.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Dict, Iterator, Optional, Protocol

from google.cloud import storage

from china_a_share.vix.rules import VixRule
from china_a_share.vix.source import VixQuote

POSITIONS_OBJECT_PREFIX = "vix/positions/"


@dataclass(frozen=True)
class VixPosition:
    """One open alert position opened at the trigger notification."""

    rule_id: str
    chat_id: str
    entry_date: str  # quote date of the triggering scan
    entry_vix: float
    entry_index: Optional[float]  # S&P 500 level at entry; None if unavailable
    expected_return_pct: float  # snapshot from the rule at entry
    exit_kind: str  # snapshot from the rule at entry
    exit_days: int
    exit_vix_below: float
    direction: str
    threshold_pct: float

    @classmethod
    def from_rule(
        cls,
        rule: VixRule,
        chat_id: str,
        quote: VixQuote,
        entry_index: Optional[float],
    ) -> "VixPosition":
        return cls(
            rule_id=rule.id,
            chat_id=chat_id,
            entry_date=quote.date,
            entry_vix=quote.close,
            entry_index=entry_index,
            expected_return_pct=rule.expected_return_pct,
            exit_kind=rule.exit_kind,
            exit_days=rule.exit_days,
            exit_vix_below=rule.exit_vix_below,
            direction=rule.direction,
            threshold_pct=rule.threshold_pct,
        )

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict) -> "VixPosition":
        known = {k: payload[k] for k in cls.__dataclass_fields__ if k in payload}
        return cls(**known)


def close_reason(
    position: VixPosition,
    quote: VixQuote,
    trading_days_since_entry: int,
) -> Optional[str]:
    """Return the exit reason when this scan closes the position, else None.

    - days: closes once `exit_days` distinct settled trading days passed
      since the entry date (entry day itself does not count).
    - vix_below: closes when the VIX level breaks below the configured level.
    - none: never a market-timed exit; closes when the trigger condition has
      faded on a later trading day, marking the end of the event range.
    """
    if position.exit_kind == "days":
        if trading_days_since_entry >= position.exit_days:
            return f"持有{position.exit_days}个交易日到期"
        return None
    if position.exit_kind == "vix_below":
        if quote.close < position.exit_vix_below:
            return f"VIX 跌破 {position.exit_vix_below:.2f}"
        return None
    later_day = quote.date > position.entry_date
    if later_day and not _condition_holds(position, quote):
        return "触发条件消退（事件区间结束）"
    return None


def _condition_holds(position: VixPosition, quote: VixQuote) -> bool:
    if position.direction == "up":
        return quote.change_pct >= position.threshold_pct
    return quote.change_pct <= -position.threshold_pct


class VixPositionStore(Protocol):
    """Persist the single open position per rule."""

    def get(self, chat_id: str, rule_id: str) -> Optional[VixPosition]:
        ...

    def put(self, position: VixPosition) -> None:
        ...

    def delete(self, chat_id: str, rule_id: str) -> None:
        ...

    def iter_all(self) -> Iterator[VixPosition]:
        ...


class MemoryVixPositionStore:
    """In-memory position store for tests and single-process deployments."""

    def __init__(self) -> None:
        self._positions: Dict = {}

    def get(self, chat_id: str, rule_id: str) -> Optional[VixPosition]:
        return self._positions.get((chat_id, rule_id))

    def put(self, position: VixPosition) -> None:
        self._positions[(position.chat_id, position.rule_id)] = position

    def delete(self, chat_id: str, rule_id: str) -> None:
        self._positions.pop((chat_id, rule_id), None)

    def iter_all(self) -> Iterator[VixPosition]:
        return iter(list(self._positions.values()))


class CloudStorageVixPositionStore:
    """Persist one position JSON object per chat/rule in the app bucket."""

    def __init__(
        self,
        bucket_name: str,
        storage_client: Optional[storage.Client] = None,
    ) -> None:
        if not bucket_name:
            raise ValueError("A bucket name is required for the VIX position store.")
        self._bucket = (storage_client or storage.Client()).bucket(bucket_name)

    def get(self, chat_id: str, rule_id: str) -> Optional[VixPosition]:
        blob = self._bucket.blob(self._object_name(chat_id, rule_id))
        if not blob.exists():
            return None
        return VixPosition.from_dict(json.loads(blob.download_as_text()))

    def put(self, position: VixPosition) -> None:
        self._bucket.blob(
            self._object_name(position.chat_id, position.rule_id)
        ).upload_from_string(
            json.dumps(position.to_dict(), ensure_ascii=False),
            content_type="application/json",
        )

    def delete(self, chat_id: str, rule_id: str) -> None:
        try:
            self._bucket.blob(self._object_name(chat_id, rule_id)).delete()
        except Exception:
            pass

    def iter_all(self) -> Iterator[VixPosition]:
        for blob in self._bucket.list_blobs(prefix=POSITIONS_OBJECT_PREFIX):
            try:
                yield VixPosition.from_dict(json.loads(blob.download_as_text()))
            except (ValueError, OSError, json.JSONDecodeError, TypeError):
                continue

    @staticmethod
    def _object_name(chat_id: str, rule_id: str) -> str:
        chat_key = hashlib.sha256(chat_id.encode()).hexdigest()
        return f"{POSITIONS_OBJECT_PREFIX}{chat_key}/{rule_id}.json"

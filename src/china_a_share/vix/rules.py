"""Per-chat VIX alert rules.

Every threshold lives in group configuration created through the chat
commands; this module defines no default threshold and no seeded rule.
Storage follows the shared pattern of one JSON object per chat keyed by the
SHA-256 of the chat id, with the plaintext chat id kept inside the object so
the scanner can send to it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Dict, Iterator, List, Optional, Protocol

from google.cloud import storage

DIRECTIONS = ("up", "down")
DIRECTION_LABELS = {"up": "上涨", "down": "下跌"}
RULES_OBJECT_PREFIX = "vix/rules/"


@dataclass(frozen=True)
class VixRule:
    """One group-configured VIX move alert."""

    id: str
    direction: str  # "up" or "down"
    threshold_pct: float  # 0 < threshold <= 100
    created_by: str
    created_at: str  # ISO timestamp

    def describe(self) -> str:
        label = DIRECTION_LABELS.get(self.direction, self.direction)
        return f"VIX {label} ≥{self.threshold_pct:.2f}%"

    def hits(self, change_pct: float) -> bool:
        """Return whether one signed change percent crosses this rule."""
        if self.direction == "up":
            return change_pct >= self.threshold_pct
        return change_pct <= -self.threshold_pct


def validate_direction(direction: str) -> str:
    if direction not in DIRECTIONS:
        raise ValueError(f"Rule direction must be one of {DIRECTIONS}.")
    return direction


def validate_threshold(threshold_pct: float) -> float:
    value = float(threshold_pct)
    if not 0.0 < value <= 100.0:
        raise ValueError("Rule threshold must satisfy 0 < threshold <= 100.")
    return value


class VixRuleStore(Protocol):
    """Persist per-chat rule lists."""

    def load(self, chat_id: str) -> List[VixRule]:
        """Return the chat's rules, or an empty list when none exist."""
        ...

    def save(self, chat_id: str, rules: List[VixRule]) -> None:
        """Replace the chat's rules."""
        ...

    def iter_chat_ids(self) -> Iterator[str]:
        """Yield every chat id that has at least one stored rules object."""
        ...


class MemoryVixRuleStore:
    """In-memory rule store for tests and single-process deployments."""

    def __init__(self) -> None:
        self._rules: Dict[str, List[VixRule]] = {}

    def load(self, chat_id: str) -> List[VixRule]:
        return list(self._rules.get(chat_id, []))

    def save(self, chat_id: str, rules: List[VixRule]) -> None:
        self._rules[chat_id] = list(rules)

    def iter_chat_ids(self) -> Iterator[str]:
        return iter(list(self._rules))


class CloudStorageVixRuleStore:
    """Persist one rules JSON object per chat in the app bucket."""

    def __init__(
        self,
        bucket_name: str,
        storage_client: Optional[storage.Client] = None,
    ) -> None:
        if not bucket_name:
            raise ValueError("A bucket name is required for the VIX rule store.")
        self._bucket = (storage_client or storage.Client()).bucket(bucket_name)

    def load(self, chat_id: str) -> List[VixRule]:
        blob = self._bucket.blob(self._object_name(chat_id))
        if not blob.exists():
            return []
        payload = json.loads(blob.download_as_text())
        if str((payload or {}).get("chat_id") or "") != chat_id:
            raise RuntimeError("VIX rules object chat id does not match its key.")
        return [VixRule(**item) for item in (payload or {}).get("rules", [])]

    def save(self, chat_id: str, rules: List[VixRule]) -> None:
        payload = {
            "chat_id": chat_id,
            "rules": [asdict(rule) for rule in rules],
        }
        self._bucket.blob(self._object_name(chat_id)).upload_from_string(
            json.dumps(payload, ensure_ascii=False),
            content_type="application/json",
        )

    def iter_chat_ids(self) -> Iterator[str]:
        for blob in self._bucket.list_blobs(prefix=RULES_OBJECT_PREFIX):
            try:
                payload = json.loads(blob.download_as_text())
            except (ValueError, OSError, json.JSONDecodeError):
                continue
            chat_id = str((payload or {}).get("chat_id") or "").strip()
            if chat_id:
                yield chat_id

    @staticmethod
    def _object_name(chat_id: str) -> str:
        return f"{RULES_OBJECT_PREFIX}{hashlib.sha256(chat_id.encode()).hexdigest()}.json"


def new_vix_rule(
    direction: str,
    threshold_pct: float,
    created_by: str,
    rule_id: str,
) -> VixRule:
    """Build one validated rule; ids come from the caller for testability."""
    return VixRule(
        id=rule_id,
        direction=validate_direction(direction),
        threshold_pct=validate_threshold(threshold_pct),
        created_by=created_by,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

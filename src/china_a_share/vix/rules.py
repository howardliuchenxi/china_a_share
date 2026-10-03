"""Per-chat VIX alert rules.

Every threshold, expectation, and exit lives in group configuration created
through the chat commands; this module defines no default rule and no hidden
behavioral constant. When a rule is created without explicit optional fields,
the command layer auto-fills them from the documented research context
(vix_index_relationship study: a >=10% single-day VIX jump has a next-day
S&P 500 mean return of +0.46%) and the reply shows the filled values so they
can be edited. Storage follows the shared pattern of one JSON object per chat
keyed by the SHA-256 of the chat id, with the plaintext chat id kept inside
the object so the scanner can send to it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, Iterator, List, Optional, Protocol

from google.cloud import storage

DIRECTIONS = ("up", "down")
DIRECTION_LABELS = {"up": "上涨", "down": "下跌"}
EXIT_KINDS = ("days", "vix_below", "none")
EXIT_KIND_LABELS = {"days": "持有到日", "vix_below": "VIX跌破", "none": "不主动卖出"}
RULES_OBJECT_PREFIX = "vix/rules/"

# Research context for auto-filled expectations (vix_index_relationship
# REPORT §4.2): episode first-day VIX jumps >=10% carry a next-day S&P 500
# mean return of +0.46%. Values are written into each stored rule so nothing
# behavioral depends on this constant at scan time.
RESEARCH_CONTEXT_THRESHOLD = 10.0
RESEARCH_CONTEXT_BAND = 2.0  # +/-20% perturbation band around the threshold
RESEARCH_CONTEXT_EXPECTED_RETURN_PCT = 0.46
DEFAULT_EXIT_DAYS = 1
MAX_EXIT_DAYS = 40


@dataclass(frozen=True)
class VixRule:
    """One group-configured VIX move alert with expectation and exit."""

    id: str
    direction: str  # "up" or "down"
    threshold_pct: float  # 0 < threshold <= 100
    created_by: str
    created_at: str  # ISO timestamp
    expected_return_pct: float = 0.0  # expected next-trade return, percent
    exit_kind: str = "days"  # "days" | "vix_below" | "none"
    exit_days: int = DEFAULT_EXIT_DAYS  # used when exit_kind == "days"
    exit_vix_below: float = 0.0  # used when exit_kind == "vix_below"

    def describe(self) -> str:
        label = DIRECTION_LABELS.get(self.direction, self.direction)
        return f"VIX {label} ≥{self.threshold_pct:.2f}%"

    def describe_exit(self) -> str:
        if self.exit_kind == "days":
            return f"{self.exit_days}个交易日后卖出"
        if self.exit_kind == "vix_below":
            return f"VIX 跌破 {self.exit_vix_below:.2f} 卖出"
        return "不主动卖出（触发条件消退后对账）"

    def describe_full(self) -> str:
        return (
            f"{self.describe()}，预期收益 {self.expected_return_pct:+.2f}%，"
            f"{self.describe_exit()}"
        )

    def hits(self, change_pct: float) -> bool:
        """Return whether one signed change percent crosses this rule."""
        if self.direction == "up":
            return change_pct >= self.threshold_pct
        return change_pct <= -self.threshold_pct

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "VixRule":
        """Load one rule, defaulting optional fields absent in older objects."""
        known = {field: payload[field] for field in cls.__dataclass_fields__ if field in payload}
        return cls(**known)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def suggest_expected_return_pct(direction: str, threshold_pct: float) -> float:
    """Auto-fill expectation from the research context; 0 outside its band.

    Only the researched configuration (single-day up-jump near 10%) carries a
    documented expectation; every other configuration starts at 0.00% and the
    creation reply nudges the user to set it explicitly.
    """
    if direction == "up" and abs(threshold_pct - RESEARCH_CONTEXT_THRESHOLD) <= RESEARCH_CONTEXT_BAND:
        return RESEARCH_CONTEXT_EXPECTED_RETURN_PCT
    return 0.0


def validate_direction(direction: str) -> str:
    if direction not in DIRECTIONS:
        raise ValueError(f"Rule direction must be one of {DIRECTIONS}.")
    return direction


def validate_threshold(threshold_pct: float) -> float:
    value = float(threshold_pct)
    if not 0.0 < value <= 100.0:
        raise ValueError("Rule threshold must satisfy 0 < threshold <= 100.")
    return value


def validate_expected_return(expected_return_pct: float) -> float:
    value = float(expected_return_pct)
    if not -100.0 <= value <= 100.0:
        raise ValueError("Expected return must satisfy -100 <= value <= 100.")
    return value


def validate_exit(exit_kind: str, exit_days: int, exit_vix_below: float) -> Dict[str, Any]:
    if exit_kind not in EXIT_KINDS:
        raise ValueError(f"Exit kind must be one of {EXIT_KINDS}.")
    if exit_kind == "days":
        days = int(exit_days)
        if not 1 <= days <= MAX_EXIT_DAYS:
            raise ValueError(f"Exit days must satisfy 1 <= days <= {MAX_EXIT_DAYS}.")
        return {"exit_kind": exit_kind, "exit_days": days, "exit_vix_below": 0.0}
    if exit_kind == "vix_below":
        level = float(exit_vix_below)
        if level <= 0:
            raise ValueError("Exit VIX level must be positive.")
        return {"exit_kind": exit_kind, "exit_days": DEFAULT_EXIT_DAYS, "exit_vix_below": level}
    return {"exit_kind": exit_kind, "exit_days": DEFAULT_EXIT_DAYS, "exit_vix_below": 0.0}


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
        return [VixRule.from_dict(item) for item in (payload or {}).get("rules", [])]

    def save(self, chat_id: str, rules: List[VixRule]) -> None:
        payload = {
            "chat_id": chat_id,
            "rules": [rule.to_dict() for rule in rules],
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
    *,
    expected_return_pct: float = 0.0,
    exit_kind: str = "days",
    exit_days: int = DEFAULT_EXIT_DAYS,
    exit_vix_below: float = 0.0,
) -> VixRule:
    """Build one validated rule; ids come from the caller for testability."""
    exit_fields = validate_exit(exit_kind, exit_days, exit_vix_below)
    return VixRule(
        id=rule_id,
        direction=validate_direction(direction),
        threshold_pct=validate_threshold(threshold_pct),
        created_by=created_by,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        expected_return_pct=validate_expected_return(expected_return_pct),
        **exit_fields,
    )

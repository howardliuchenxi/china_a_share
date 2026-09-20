"""Persistence contracts and implementations for configurable strategies and drafts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Optional, Protocol

from google.api_core.exceptions import PreconditionFailed
from google.api_core.retry import Retry
from google.cloud import storage

from china_a_share.discovery.strategy_models import (
    SignalDirection,
    StrategyConfig,
    StrategyDraft,
)

# Shared retry strategy matching existing project conventions
STORAGE_RETRY = Retry(
    initial=1.0,
    maximum=8.0,
    multiplier=2.0,
    deadline=30.0,
)


class StrategyPermissionError(PermissionError):
    """Raised when an operation is attempted by a user who does not own the strategy or draft."""


def validate_identifier(component: str) -> None:
    """Validate all identifier/path components before forming GCS paths or storage keys.

    Rejects empty, slash/backslash, dot traversal, and control characters.
    """
    if not isinstance(component, str):
        raise ValueError("Identifier component must be a string")
    if not component:
        raise ValueError("Identifier component cannot be empty")
    if "/" in component or "\\" in component:
        raise ValueError("Identifier component cannot contain slashes or backslashes")
    if ".." in component:
        raise ValueError("Identifier component cannot contain dot traversal")
    if any(ord(c) < 32 or ord(c) == 127 for c in component):
        raise ValueError("Identifier component cannot contain control characters")


class StrategyStore(Protocol):
    """Protocol for persisting strategy configurations, drafts, and notification history."""

    def get_strategy(self, strategy_id: str, owner_open_id: str) -> Optional[StrategyConfig]:
        """Retrieve a strategy configuration if it exists and belongs to the owner."""
        ...

    def put_strategy(self, strategy: StrategyConfig, owner_open_id: str) -> None:
        """Create or update a strategy config.

        Enforces that strategy.creator_open_id matches owner_open_id, and if the strategy
        already exists, the existing strategy's creator_open_id must match owner_open_id.
        """
        ...

    def delete_strategy(self, strategy_id: str, owner_open_id: str) -> None:
        """Delete a strategy config if it exists and belongs to the owner."""
        ...

    def list_strategies(self, owner_open_id: str, enabled: Optional[bool] = None) -> list[StrategyConfig]:
        """List all strategies belonging to the owner, optionally filtered by enabled status."""
        ...

    def list_enabled_strategies(self) -> list[StrategyConfig]:
        """List all enabled strategies across all owners for daily scans."""
        ...

    def get_draft(self, draft_id: str, owner_open_id: str) -> Optional[StrategyDraft]:
        """Retrieve a strategy draft if it exists and belongs to the owner."""
        ...

    def put_draft(self, draft: StrategyDraft, owner_open_id: str) -> None:
        """Create or update a strategy draft.

        Enforces that draft.owner_open_id matches owner_open_id, and if the draft
        already exists, the existing draft's owner_open_id must match owner_open_id.
        """
        ...

    def delete_draft(self, draft_id: str, owner_open_id: str) -> None:
        """Delete a strategy draft if it exists and belongs to the owner."""
        ...

    def list_drafts(self, owner_open_id: str) -> list[StrategyDraft]:
        """List all drafts belonging to the owner."""
        ...

    def mark_notification_sent(
        self,
        strategy_id: str,
        stock_code: str,
        signal_date: str,
        direction: SignalDirection,
    ) -> bool:
        """Mark a notification as sent for the given identity.

        Uses a create-only precondition. Returns True if successfully marked now,
        or True (idempotent success) if it was already marked.
        """
        ...

    def is_notification_sent(
        self,
        strategy_id: str,
        stock_code: str,
        signal_date: str,
        direction: SignalDirection,
    ) -> bool:
        """Check if a notification has already been marked as sent."""
        ...


class MemoryStrategyStore:
    """In-memory StrategyStore implementation for unit tests."""

    def __init__(self) -> None:
        self._strategies: dict[tuple[str, str], StrategyConfig] = {}
        self._drafts: dict[tuple[str, str], StrategyDraft] = {}
        self._notifications: set[tuple[str, str, str, str]] = set()

    def get_strategy(self, strategy_id: str, owner_open_id: str) -> Optional[StrategyConfig]:
        validate_identifier(strategy_id)
        validate_identifier(owner_open_id)
        return self._strategies.get((owner_open_id, strategy_id))

    def put_strategy(self, strategy: StrategyConfig, owner_open_id: str) -> None:
        validate_identifier(strategy.id)
        validate_identifier(owner_open_id)
        validate_identifier(strategy.creator_open_id)
        if strategy.creator_open_id != owner_open_id:
            raise StrategyPermissionError(
                "Cannot save strategy with mismatched creator."
            )
        self._strategies[(owner_open_id, strategy.id)] = strategy

    def delete_strategy(self, strategy_id: str, owner_open_id: str) -> None:
        validate_identifier(strategy_id)
        validate_identifier(owner_open_id)
        self._strategies.pop((owner_open_id, strategy_id), None)

    def list_strategies(
        self, owner_open_id: str, enabled: Optional[bool] = None
    ) -> list[StrategyConfig]:
        validate_identifier(owner_open_id)
        results = [
            strategy
            for (owner, _), strategy in self._strategies.items()
            if owner == owner_open_id
        ]
        if enabled is not None:
            results = [s for s in results if s.enabled == enabled]
        return sorted(results, key=lambda strategy: strategy.id)

    def list_enabled_strategies(self) -> list[StrategyConfig]:
        return sorted(
            (strategy for strategy in self._strategies.values() if strategy.enabled),
            key=lambda strategy: (strategy.creator_open_id, strategy.id),
        )

    def get_draft(self, draft_id: str, owner_open_id: str) -> Optional[StrategyDraft]:
        validate_identifier(draft_id)
        validate_identifier(owner_open_id)
        return self._drafts.get((owner_open_id, draft_id))

    def put_draft(self, draft: StrategyDraft, owner_open_id: str) -> None:
        validate_identifier(draft.draft_id)
        validate_identifier(owner_open_id)
        validate_identifier(draft.owner_open_id)
        if draft.owner_open_id != owner_open_id:
            raise StrategyPermissionError(
                "Cannot save draft with mismatched owner."
            )
        self._drafts[(owner_open_id, draft.draft_id)] = draft

    def delete_draft(self, draft_id: str, owner_open_id: str) -> None:
        validate_identifier(draft_id)
        validate_identifier(owner_open_id)
        self._drafts.pop((owner_open_id, draft_id), None)

    def list_drafts(self, owner_open_id: str) -> list[StrategyDraft]:
        validate_identifier(owner_open_id)
        return [
            draft
            for (owner, _), draft in self._drafts.items()
            if owner == owner_open_id
        ]

    def mark_notification_sent(
        self,
        strategy_id: str,
        stock_code: str,
        signal_date: str,
        direction: SignalDirection,
    ) -> bool:
        validate_identifier(strategy_id)
        validate_identifier(stock_code)
        validate_identifier(signal_date)
        validate_identifier(direction.value)
        identity = (strategy_id, stock_code, signal_date, direction.value)
        if identity in self._notifications:
            return True
        self._notifications.add(identity)
        return True

    def is_notification_sent(
        self,
        strategy_id: str,
        stock_code: str,
        signal_date: str,
        direction: SignalDirection,
    ) -> bool:
        validate_identifier(strategy_id)
        validate_identifier(stock_code)
        validate_identifier(signal_date)
        validate_identifier(direction.value)
        identity = (strategy_id, stock_code, signal_date, direction.value)
        return identity in self._notifications


class CloudStorageStrategyStore:
    """Google Cloud Storage StrategyStore implementation with isolated prefixes and owner isolation."""

    def __init__(
        self,
        bucket_name: str,
        storage_client: Optional[storage.Client] = None,
    ) -> None:
        self._bucket = (storage_client or storage.Client()).bucket(bucket_name)

    def get_strategy(self, strategy_id: str, owner_open_id: str) -> Optional[StrategyConfig]:
        validate_identifier(strategy_id)
        validate_identifier(owner_open_id)
        object_name = f"strategies/configs/{owner_open_id}/{strategy_id}.json"
        blob = self._bucket.blob(object_name)
        if not blob.exists(retry=STORAGE_RETRY):
            return None
        data = json.loads(blob.download_as_text(retry=STORAGE_RETRY))
        strategy = StrategyConfig.model_validate(data)
        if strategy.creator_open_id != owner_open_id:
            raise StrategyPermissionError(
                f"Strategy {strategy_id} belongs to another user."
            )
        return strategy

    def put_strategy(self, strategy: StrategyConfig, owner_open_id: str) -> None:
        validate_identifier(strategy.id)
        validate_identifier(owner_open_id)
        validate_identifier(strategy.creator_open_id)
        if strategy.creator_open_id != owner_open_id:
            raise StrategyPermissionError(
                "Cannot save strategy with mismatched creator."
            )
        object_name = f"strategies/configs/{owner_open_id}/{strategy.id}.json"
        blob = self._bucket.blob(object_name)
        if blob.exists(retry=STORAGE_RETRY):
            data = json.loads(blob.download_as_text(retry=STORAGE_RETRY))
            existing = StrategyConfig.model_validate(data)
            if existing.creator_open_id != owner_open_id:
                raise StrategyPermissionError(
                    f"Strategy {strategy.id} belongs to another user."
                )
        blob.upload_from_string(
            strategy.model_dump_json(),
            content_type="application/json",
            retry=STORAGE_RETRY,
        )

    def delete_strategy(self, strategy_id: str, owner_open_id: str) -> None:
        validate_identifier(strategy_id)
        validate_identifier(owner_open_id)
        object_name = f"strategies/configs/{owner_open_id}/{strategy_id}.json"
        blob = self._bucket.blob(object_name)
        if blob.exists(retry=STORAGE_RETRY):
            data = json.loads(blob.download_as_text(retry=STORAGE_RETRY))
            strategy = StrategyConfig.model_validate(data)
            if strategy.creator_open_id != owner_open_id:
                raise StrategyPermissionError(
                    f"Strategy {strategy_id} belongs to another user."
                )
            blob.delete(retry=STORAGE_RETRY)

    def list_strategies(
        self, owner_open_id: str, enabled: Optional[bool] = None
    ) -> list[StrategyConfig]:
        validate_identifier(owner_open_id)
        prefix = f"strategies/configs/{owner_open_id}/"
        strategies = []
        for blob in self._bucket.list_blobs(prefix=prefix):
            data = json.loads(blob.download_as_text(retry=STORAGE_RETRY))
            strategy = StrategyConfig.model_validate(data)
            if strategy.creator_open_id != owner_open_id:
                raise StrategyPermissionError(
                    "Stored strategy owner does not match its object path."
                )
            if enabled is None or strategy.enabled == enabled:
                strategies.append(strategy)
        return sorted(strategies, key=lambda strategy: strategy.id)

    def list_enabled_strategies(self) -> list[StrategyConfig]:
        prefix = "strategies/configs/"
        strategies = []
        for blob in self._bucket.list_blobs(prefix=prefix):
            data = json.loads(blob.download_as_text(retry=STORAGE_RETRY))
            strategy = StrategyConfig.model_validate(data)
            if strategy.enabled:
                strategies.append(strategy)
        return sorted(
            strategies,
            key=lambda strategy: (strategy.creator_open_id, strategy.id),
        )

    def get_draft(self, draft_id: str, owner_open_id: str) -> Optional[StrategyDraft]:
        validate_identifier(draft_id)
        validate_identifier(owner_open_id)
        object_name = f"strategies/drafts/{owner_open_id}/{draft_id}.json"
        blob = self._bucket.blob(object_name)
        if not blob.exists(retry=STORAGE_RETRY):
            return None
        data = json.loads(blob.download_as_text(retry=STORAGE_RETRY))
        draft = StrategyDraft.model_validate(data)
        if draft.owner_open_id != owner_open_id:
            raise StrategyPermissionError(
                f"Draft {draft_id} belongs to another user."
            )
        return draft

    def put_draft(self, draft: StrategyDraft, owner_open_id: str) -> None:
        validate_identifier(draft.draft_id)
        validate_identifier(owner_open_id)
        validate_identifier(draft.owner_open_id)
        if draft.owner_open_id != owner_open_id:
            raise StrategyPermissionError(
                "Cannot save draft with mismatched owner."
            )
        object_name = f"strategies/drafts/{owner_open_id}/{draft.draft_id}.json"
        blob = self._bucket.blob(object_name)
        if blob.exists(retry=STORAGE_RETRY):
            data = json.loads(blob.download_as_text(retry=STORAGE_RETRY))
            existing = StrategyDraft.model_validate(data)
            if existing.owner_open_id != owner_open_id:
                raise StrategyPermissionError(
                    f"Draft {draft.draft_id} belongs to another user."
                )
        blob.upload_from_string(
            draft.model_dump_json(),
            content_type="application/json",
            retry=STORAGE_RETRY,
        )

    def delete_draft(self, draft_id: str, owner_open_id: str) -> None:
        validate_identifier(draft_id)
        validate_identifier(owner_open_id)
        object_name = f"strategies/drafts/{owner_open_id}/{draft_id}.json"
        blob = self._bucket.blob(object_name)
        if blob.exists(retry=STORAGE_RETRY):
            data = json.loads(blob.download_as_text(retry=STORAGE_RETRY))
            draft = StrategyDraft.model_validate(data)
            if draft.owner_open_id != owner_open_id:
                raise StrategyPermissionError(
                    f"Draft {draft_id} belongs to another user."
                )
            blob.delete(retry=STORAGE_RETRY)

    def list_drafts(self, owner_open_id: str) -> list[StrategyDraft]:
        validate_identifier(owner_open_id)
        prefix = f"strategies/drafts/{owner_open_id}/"
        drafts = []
        for blob in self._bucket.list_blobs(prefix=prefix):
            data = json.loads(blob.download_as_text(retry=STORAGE_RETRY))
            draft = StrategyDraft.model_validate(data)
            if draft.owner_open_id != owner_open_id:
                raise StrategyPermissionError(
                    "Stored draft owner does not match its object path."
                )
            drafts.append(draft)
        return sorted(drafts, key=lambda draft: draft.draft_id)

    def mark_notification_sent(
        self,
        strategy_id: str,
        stock_code: str,
        signal_date: str,
        direction: SignalDirection,
    ) -> bool:
        validate_identifier(strategy_id)
        validate_identifier(stock_code)
        validate_identifier(signal_date)
        validate_identifier(direction.value)
        object_name = f"strategies/notifications/{strategy_id}/{stock_code}/{signal_date}/{direction.value}.json"
        blob = self._bucket.blob(object_name)
        marker_data = {
            "strategy_id": strategy_id,
            "stock_code": stock_code,
            "signal_date": signal_date,
            "direction": direction.value,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            blob.upload_from_string(
                json.dumps(marker_data, ensure_ascii=False),
                content_type="application/json",
                if_generation_match=0,
                retry=STORAGE_RETRY,
            )
            return True
        except PreconditionFailed:
            return True

    def is_notification_sent(
        self,
        strategy_id: str,
        stock_code: str,
        signal_date: str,
        direction: SignalDirection,
    ) -> bool:
        validate_identifier(strategy_id)
        validate_identifier(stock_code)
        validate_identifier(signal_date)
        validate_identifier(direction.value)
        object_name = f"strategies/notifications/{strategy_id}/{stock_code}/{signal_date}/{direction.value}.json"
        blob = self._bucket.blob(object_name)
        return blob.exists(retry=STORAGE_RETRY)

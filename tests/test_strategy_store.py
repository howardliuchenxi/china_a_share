from datetime import datetime, timezone
import json
import pytest
from pydantic import ValidationError
from google.api_core.exceptions import PreconditionFailed

from china_a_share.discovery.strategy_models import (
    SignalDirection,
    StrategyConfig,
    StrategyDraft,
    DraftState,
    DrawdownRule,
)
from china_a_share.discovery.strategy_store import (
    MemoryStrategyStore,
    CloudStorageStrategyStore,
    StrategyPermissionError,
    validate_identifier,
)


class FakeBlob:
    def __init__(self, name: str):
        self.name = name
        self._content: str | None = None
        self._exists = False

    def exists(self, retry=None) -> bool:
        return self._exists

    def download_as_text(self, retry=None) -> str:
        if not self._exists or self._content is None:
            from google.api_core.exceptions import NotFound
            raise NotFound("Blob not found")
        return self._content

    def upload_from_string(
        self, content: str, content_type: str = None, if_generation_match: int = None, retry=None
    ) -> None:
        if if_generation_match == 0 and self._exists:
            raise PreconditionFailed("Blob already exists")
        self._content = content
        self._exists = True

    def delete(self, retry=None) -> None:
        if not self._exists:
            from google.api_core.exceptions import NotFound
            raise NotFound("Blob not found")
        self._exists = False
        self._content = None


class FakeBucket:
    def __init__(self):
        self._blobs: dict[str, FakeBlob] = {}

    def blob(self, name: str) -> FakeBlob:
        if name not in self._blobs:
            self._blobs[name] = FakeBlob(name)
        return self._blobs[name]

    def list_blobs(self, prefix: str = "") -> list[FakeBlob]:
        return [b for name, b in self._blobs.items() if b._exists and name.startswith(prefix)]


class FakeStorageClient:
    def __init__(self):
        self._bucket = FakeBucket()

    def bucket(self, name: str) -> FakeBucket:
        return self._bucket


def test_memory_owner_isolation():
    store = MemoryStrategyStore()
    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
    s = StrategyConfig(
        id="strategy-1",
        name="Test",
        direction=SignalDirection.BUY,
        rules=[DrawdownRule(window=10, threshold=0.1)],
        creator_open_id="user-1",
        enabled=True,
        notification_chat_id="chat-1",
        created_at=now,
        updated_at=now,
    )
    # Save as user-1
    store.put_strategy(s, "user-1")

    # Get as user-1 succeeds
    assert store.get_strategy("strategy-1", "user-1") == s

    # Owner-scoped lookup does not disclose another user's object.
    assert store.get_strategy("strategy-1", "user-2") is None

    # Put as user-2 with creator_open_id user-1 raises StrategyPermissionError
    with pytest.raises(StrategyPermissionError):
        store.put_strategy(s, "user-2")

    store.delete_strategy("strategy-1", "user-2")
    assert store.get_strategy("strategy-1", "user-1") == s

    # Draft test
    draft = StrategyDraft(
        draft_id="draft-1",
        owner_open_id="user-1",
        chat_id="chat-1",
        state=DraftState.AWAITING_NAME,
        created_at=now,
        updated_at=now,
    )
    store.put_draft(draft, "user-1")
    assert store.get_draft("draft-1", "user-1") == draft

    assert store.get_draft("draft-1", "user-2") is None

    with pytest.raises(StrategyPermissionError):
        store.put_draft(draft, "user-2")

    store.delete_draft("draft-1", "user-2")
    assert store.get_draft("draft-1", "user-1") == draft


def test_enabled_filtering_and_listing():
    store = MemoryStrategyStore()
    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
    s1 = StrategyConfig(
        id="strategy-1",
        name="Test 1",
        direction=SignalDirection.BUY,
        rules=[DrawdownRule(window=10, threshold=0.1)],
        creator_open_id="user-1",
        enabled=True,
        notification_chat_id="chat-1",
        created_at=now,
        updated_at=now,
    )
    s2 = StrategyConfig(
        id="strategy-2",
        name="Test 2",
        direction=SignalDirection.SELL,
        rules=[DrawdownRule(window=10, threshold=0.1)],
        creator_open_id="user-1",
        enabled=False,
        notification_chat_id="chat-1",
        created_at=now,
        updated_at=now,
    )
    s3 = StrategyConfig(
        id="strategy-3",
        name="Test 3",
        direction=SignalDirection.BUY,
        rules=[DrawdownRule(window=10, threshold=0.1)],
        creator_open_id="user-2",
        enabled=True,
        notification_chat_id="chat-2",
        created_at=now,
        updated_at=now,
    )
    store.put_strategy(s1, "user-1")
    store.put_strategy(s2, "user-1")
    store.put_strategy(s3, "user-2")

    # list_strategies for user-1
    assert store.list_strategies("user-1") == [s1, s2]
    assert store.list_strategies("user-1", enabled=True) == [s1]
    assert store.list_strategies("user-1", enabled=False) == [s2]

    # global enabled strategies
    assert store.list_enabled_strategies() == [s1, s3]


def test_draft_lifecycle():
    store = MemoryStrategyStore()
    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
    draft = StrategyDraft(
        draft_id="draft-1",
        owner_open_id="user-1",
        chat_id="chat-1",
        state=DraftState.AWAITING_NAME,
        name=None,
        direction=None,
        rules=[],
        created_at=now,
        updated_at=now,
    )
    store.put_draft(draft, "user-1")
    assert store.get_draft("draft-1", "user-1") == draft

    # Update state and rules
    updated_draft = StrategyDraft(
        draft_id="draft-1",
        owner_open_id="user-1",
        chat_id="chat-1",
        state=DraftState.READY,
        name="Completed Strategy",
        direction=SignalDirection.BUY,
        rules=[DrawdownRule(window=10, threshold=0.2)],
        created_at=now,
        updated_at=now,
    )
    store.put_draft(updated_draft, "user-1")
    assert store.get_draft("draft-1", "user-1") == updated_draft
    assert store.list_drafts("user-1") == [updated_draft]

    store.delete_draft("draft-1", "user-1")
    assert store.get_draft("draft-1", "user-1") is None


def test_timestamp_validation():
    now_naive = datetime(2026, 9, 19, 12, 0, 0)
    now_aware = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
    earlier_aware = datetime(2026, 9, 19, 11, 0, 0, tzinfo=timezone.utc)

    # 1. Non-timezone-aware raises ValidationError
    with pytest.raises(ValidationError):
        StrategyConfig(
            id="strategy-1",
            name="Test",
            direction=SignalDirection.BUY,
            rules=[DrawdownRule(window=10, threshold=0.1)],
            creator_open_id="user-1",
            enabled=True,
            notification_chat_id="chat-1",
            created_at=now_naive,
            updated_at=now_aware,
        )

    with pytest.raises(ValidationError):
        StrategyConfig(
            id="strategy-1",
            name="Test",
            direction=SignalDirection.BUY,
            rules=[DrawdownRule(window=10, threshold=0.1)],
            creator_open_id="user-1",
            enabled=True,
            notification_chat_id="chat-1",
            created_at=now_aware,
            updated_at=now_naive,
        )

    # 2. updated_at < created_at raises ValidationError
    with pytest.raises(ValidationError):
        StrategyConfig(
            id="strategy-1",
            name="Test",
            direction=SignalDirection.BUY,
            rules=[DrawdownRule(window=10, threshold=0.1)],
            creator_open_id="user-1",
            enabled=True,
            notification_chat_id="chat-1",
            created_at=now_aware,
            updated_at=earlier_aware,
        )

    # StrategyDraft timestamp validation
    with pytest.raises(ValidationError):
        StrategyDraft(
            draft_id="draft-1",
            owner_open_id="user-1",
            chat_id="chat-1",
            state=DraftState.AWAITING_NAME,
            created_at=now_naive,
            updated_at=now_aware,
        )

    with pytest.raises(ValidationError):
        StrategyDraft(
            draft_id="draft-1",
            owner_open_id="user-1",
            chat_id="chat-1",
            state=DraftState.AWAITING_NAME,
            created_at=now_aware,
            updated_at=earlier_aware,
        )


def test_identifier_validation():
    # Valid cases
    validate_identifier("user-123")
    validate_identifier("000001.SZ")
    validate_identifier("ou_12345")
    validate_identifier("123e4567-e89b-12d3-a456-426614174000")

    # Invalid cases
    with pytest.raises(ValueError):
        validate_identifier("")
    with pytest.raises(ValueError):
        validate_identifier("abc/def")
    with pytest.raises(ValueError):
        validate_identifier("abc\\def")
    with pytest.raises(ValueError):
        validate_identifier("abc..def")
    with pytest.raises(ValueError):
        validate_identifier("abc\ndef")
    with pytest.raises(ValueError):
        validate_identifier("abc\x00def")


def test_notification_direction_separation_and_idempotency():
    store = MemoryStrategyStore()

    # Mark BUY notification sent
    assert store.mark_notification_sent("strategy-1", "000001.SZ", "20240101", SignalDirection.BUY) is True
    assert store.is_notification_sent("strategy-1", "000001.SZ", "20240101", SignalDirection.BUY) is True

    # SELL notification on same strategy/stock/date is NOT sent yet
    assert store.is_notification_sent("strategy-1", "000001.SZ", "20240101", SignalDirection.SELL) is False

    # Mark SELL notification sent
    assert store.mark_notification_sent("strategy-1", "000001.SZ", "20240101", SignalDirection.SELL) is True
    assert store.is_notification_sent("strategy-1", "000001.SZ", "20240101", SignalDirection.SELL) is True

    # Idempotent marking returns True
    assert store.mark_notification_sent("strategy-1", "000001.SZ", "20240101", SignalDirection.BUY) is True


def test_gcs_strategy_store_behavior():
    client = FakeStorageClient()
    store = CloudStorageStrategyStore("test-bucket", storage_client=client)

    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
    strategy_obj = StrategyConfig(
        id="strategy-1",
        name="GCS Strategy",
        direction=SignalDirection.BUY,
        rules=[DrawdownRule(window=10, threshold=0.1)],
        creator_open_id="user-1",
        enabled=True,
        notification_chat_id="chat-1",
        created_at=now,
        updated_at=now,
    )

    # Put strategy
    store.put_strategy(strategy_obj, "user-1")

    # Get strategy
    retrieved = store.get_strategy("strategy-1", "user-1")
    assert retrieved == strategy_obj

    # Put strategy as other owner fails
    with pytest.raises(StrategyPermissionError):
        store.put_strategy(strategy_obj, "user-2")

    # Get strategy as other owner returns None due to prefix isolation
    assert store.get_strategy("strategy-1", "user-2") is None

    # list_strategies
    assert store.list_strategies("user-1") == [strategy_obj]
    assert store.list_enabled_strategies() == [strategy_obj]

    # Create draft
    draft = StrategyDraft(
        draft_id="draft-1",
        owner_open_id="user-1",
        chat_id="chat-1",
        state=DraftState.AWAITING_NAME,
        created_at=now,
        updated_at=now,
    )
    store.put_draft(draft, "user-1")
    assert store.get_draft("draft-1", "user-1") == draft
    assert store.list_drafts("user-1") == [draft]

    # Notifications
    assert store.is_notification_sent("strategy-1", "000001.SZ", "20240101", SignalDirection.BUY) is False
    # Create-only marker semantics: send first, then mark
    assert store.mark_notification_sent("strategy-1", "000001.SZ", "20240101", SignalDirection.BUY) is True
    assert store.is_notification_sent("strategy-1", "000001.SZ", "20240101", SignalDirection.BUY) is True

    # Idempotent marker success
    assert store.mark_notification_sent("strategy-1", "000001.SZ", "20240101", SignalDirection.BUY) is True

    # Reject corrupt JSON
    # Manually corrupt the JSON in GCS fake blob
    blob = client._bucket.blob("strategies/configs/user-1/strategy-1.json")
    blob.upload_from_string("corrupt json{")

    # get_strategy propagates the JSON error (doesn't swallow)
    with pytest.raises(Exception):
        store.get_strategy("strategy-1", "user-1")

    with pytest.raises(Exception):
        store.list_strategies("user-1")

    with pytest.raises(Exception):
        store.list_enabled_strategies()

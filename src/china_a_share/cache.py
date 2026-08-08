"""Provider-aware layered caching for successful market-data responses."""

from collections import OrderedDict
from datetime import datetime, timezone
import gzip
import hashlib
import json
import logging
import re
from threading import Lock, RLock
from time import perf_counter, sleep
from typing import Callable, Dict, Optional, Sequence, Tuple
from contextvars import ContextVar

from google.api_core.exceptions import NotFound
from google.cloud import storage
import pandas as pd

from .core.contracts import DATA_CACHE_SCHEMA_VERSION, DataCacheRecord
from .core.ports import CacheExpirationPolicy, DataCacheStore
from .observability import log_event


logger = logging.getLogger(__name__)

DEFAULT_L1_MAX_ENTRIES = 256
DEFAULT_L1_MAX_BYTES = 128 * 1024 * 1024
CACHE_LOCK_STRIPE_COUNT = 64
CACHE_OBJECT_PREFIX = "cache"
CACHE_L2_READ_MAX_ATTEMPTS = 3
CACHE_L2_READ_RETRY_BASE_SECONDS = 0.5
MILLISECONDS_PER_SECOND = 1_000
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

request_cache_metrics: Dict[str, Dict[str, int]] = {}
request_cache_metrics_lock = Lock()

def _utc_now() -> datetime:
    """Return the current UTC instant for cache expiration comparisons."""
    return datetime.now(timezone.utc)


class MemoryDataCacheStore:
    """Bounded and thread-safe process-local L1 cache."""

    def __init__(self, max_entries: int, max_bytes: int) -> None:
        """Initialize limits, ordered entries, byte accounting, and locking."""
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: "OrderedDict[str, Tuple[DataCacheRecord, int]]" = OrderedDict()
        self._current_bytes = 0
        self._lock = RLock()

    def get(self, cache_key: str) -> Optional[DataCacheRecord]:
        """Return one L1 record and update its recency when present."""
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is None:
                return None
            self._entries.move_to_end(cache_key)
            return entry[0].model_copy(deep=True)

    def put(self, cache_key: str, record: DataCacheRecord) -> None:
        """Store one L1 record and evict entries until both limits are satisfied."""
        record_size = len(record.model_dump_json().encode("utf-8"))
        if record_size > self._max_bytes:
            # Oversized responses stay in L2 without displacing every hot L1 entry.
            logger.info(
                "cache_l1_write_skipped cache_key=%s reason=record_too_large bytes=%s",
                cache_key,
                record_size,
            )
            return

        with self._lock:
            previous = self._entries.pop(cache_key, None)
            if previous is not None:
                self._current_bytes -= previous[1]
            self._entries[cache_key] = (record.model_copy(deep=True), record_size)
            self._current_bytes += record_size

            # LRU eviction is bounded by object count and serialized payload size.
            while (
                len(self._entries) > self._max_entries
                or self._current_bytes > self._max_bytes
            ):
                evicted_key, (_, evicted_size) = self._entries.popitem(last=False)
                self._current_bytes -= evicted_size
                logger.info(
                    "cache_l1_entry_evicted cache_key=%s bytes=%s",
                    evicted_key,
                    evicted_size,
                )


class NoopDataCacheStore:
    """No-op cache store that never holds data, for local dev without GCP."""

    def get(self, cache_key: str) -> Optional[DataCacheRecord]:
        """Always return None — no cached data available."""
        return None

    def put(self, cache_key: str, record: DataCacheRecord) -> None:
        """No-op — data is not persisted."""


class CloudStorageDataCacheStore:
    """Persistent L2 cache backed by one private Cloud Storage bucket."""

    def __init__(
        self,
        bucket_name: str,
        storage_client: Optional[storage.Client] = None,
    ) -> None:
        """Bind the store to a required bucket and injectable storage client."""
        if not bucket_name.strip():
            raise ValueError("bucket_name must not be empty")
        self._bucket = (storage_client or storage.Client()).bucket(bucket_name)

    def get(self, cache_key: str) -> Optional[DataCacheRecord]:
        """Download and validate one compressed cache record when present."""
        blob = self._bucket.blob(self._object_name(cache_key))
        for attempt in range(1, CACHE_L2_READ_MAX_ATTEMPTS + 1):
            try:
                compressed_payload = blob.download_as_bytes()
                break
            except NotFound:
                return None
            except Exception:
                logger.exception(
                    "cache_l2_read_failed cache_key=%s attempt=%s max_attempts=%s",
                    cache_key,
                    attempt,
                    CACHE_L2_READ_MAX_ATTEMPTS,
                )
                if attempt == CACHE_L2_READ_MAX_ATTEMPTS:
                    raise
                sleep(CACHE_L2_READ_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))

        try:
            payload = gzip.decompress(compressed_payload)
            return DataCacheRecord.model_validate_json(payload)
        except Exception:
            # Corrupt cache data remains visible instead of becoming a hidden provider call.
            logger.exception("cache_l2_record_invalid cache_key=%s", cache_key)
            raise

    def put(self, cache_key: str, record: DataCacheRecord) -> None:
        """Serialize, compress, and atomically upload one cache record."""
        payload = gzip.compress(record.model_dump_json().encode("utf-8"))
        blob = self._bucket.blob(self._object_name(cache_key))
        try:
            # A single object replacement is atomic for concurrent readers.
            blob.upload_from_string(payload, content_type="application/gzip")
        except Exception:
            logger.exception("cache_l2_write_failed cache_key=%s", cache_key)
            raise

    @staticmethod
    def _object_name(cache_key: str) -> str:
        """Map a versioned cache key to its private bucket object name."""
        if not cache_key.strip():
            raise ValueError("cache_key must not be empty")
        return f"{CACHE_OBJECT_PREFIX}/{cache_key}.json.gz"


class LayeredDataResponseCache:
    """Coordinate L1, L2, provider expiration, and miss deduplication."""

    def __init__(
        self,
        memory_store: DataCacheStore,
        persistent_store: DataCacheStore,
        expiration_policy: CacheExpirationPolicy,
        now_provider: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Initialize explicit stores, policy, clock, and bounded lock stripes."""
        self._memory_store = memory_store
        self._persistent_store = persistent_store
        self._expiration_policy = expiration_policy
        self._now_provider = now_provider
        # Lock striping bounds lock memory while deduplicating misses per cache key.
        self._miss_locks = [Lock() for _ in range(CACHE_LOCK_STRIPE_COUNT)]

    def get(self, cache_key: str, now: datetime) -> Optional[DataCacheRecord]:
        """Resolve one unexpired record through L1 and then L2."""
        record, _ = self._get_with_layer(cache_key, now)
        return record

    def _get_with_layer(
        self,
        cache_key: str,
        now: datetime,
    ) -> Tuple[Optional[DataCacheRecord], str]:
        """Resolve one record and return the bounded layer that supplied it."""
        self._require_aware_datetime(now)
        memory_record = self._memory_store.get(cache_key)
        if self._is_valid(memory_record, now):
            return memory_record, "l1"

        persistent_record = self._persistent_store.get(cache_key)
        if not self._is_valid(persistent_record, now):
            return None, "none"

        self._memory_store.put(cache_key, persistent_record)
        return persistent_record, "l2"

    def put(self, cache_key: str, record: DataCacheRecord) -> None:
        """Persist one successful response in L2 before publishing it to L1."""
        self._persistent_store.put(cache_key, record)
        self._memory_store.put(cache_key, record)
        logger.info("cache_write_completed cache_key=%s", cache_key)

    def get_or_fetch(
        self,
        provider: str,
        operation: str,
        params: Dict[str, object],
        fields: Sequence[str],
        fetch: Callable[[], pd.DataFrame],
        *,
        api_route: str,
        request_id: str,
        query_id: str,
    ) -> pd.DataFrame:
        """Return cached data or perform one deduplicated provider fetch."""
        now = self._now()
        expires_at = self._expiration_policy.resolve(operation, params, now)
        if expires_at is None:
            self._log_cache_lookup(
                api_route=api_route,
                provider=provider,
                operation=operation,
                outcome="bypass",
                cache_layer="none",
                started_at=perf_counter(),
                request_id=request_id,
                query_id=query_id,
            )
            return self._fetch_provider(
                provider,
                operation,
                fetch,
                api_route=api_route,
                request_id=request_id,
                query_id=query_id,
            )

        cache_key = build_data_cache_key(provider, operation, params, fields)
        lookup_started_at = perf_counter()
        record, cache_layer = self._get_with_layer(cache_key, now)
        if record is not None:
            self._log_cache_lookup(
                api_route=api_route,
                provider=provider,
                operation=operation,
                outcome="hit",
                cache_layer=cache_layer,
                started_at=lookup_started_at,
                request_id=request_id,
                query_id=query_id,
            )
            return self._record_to_frame(record)

        miss_lock = self._miss_locks[self._lock_index(cache_key)]
        with miss_lock:
            # Recheck after locking because another request may have filled both layers.
            now = self._now()
            record, cache_layer = self._get_with_layer(cache_key, now)
            if record is not None:
                self._log_cache_lookup(
                    api_route=api_route,
                    provider=provider,
                    operation=operation,
                    outcome="hit",
                    cache_layer=cache_layer,
                    started_at=lookup_started_at,
                    request_id=request_id,
                    query_id=query_id,
                )
                return self._record_to_frame(record)

            self._log_cache_lookup(
                api_route=api_route,
                provider=provider,
                operation=operation,
                outcome="miss",
                cache_layer="none",
                started_at=lookup_started_at,
                request_id=request_id,
                query_id=query_id,
            )
            frame = self._fetch_provider(
                provider,
                operation,
                fetch,
                api_route=api_route,
                request_id=request_id,
                query_id=query_id,
            )
            safe_frame = frame.astype(object).where(pd.notnull(frame), None)
            record = DataCacheRecord(
                provider=provider,
                operation=operation,
                params=params,
                fields=list(fields),
                fetched_at=now,
                expires_at=expires_at,
                columns=list(safe_frame.columns),
                rows=[list(row) for row in safe_frame.itertuples(index=False, name=None)],
            )
            self.put(cache_key, record)
            return frame.copy(deep=True)

    @classmethod
    def _fetch_provider(
        cls,
        provider: str,
        operation: str,
        fetch: Callable[[], pd.DataFrame],
        *,
        api_route: str,
        request_id: str,
        query_id: str,
    ) -> pd.DataFrame:
        """Execute and log one provider call without changing cache policy."""
        provider_started_at = perf_counter()
        try:
            frame = fetch()
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "provider_call_completed",
                exc_info=True,
                api_route=api_route,
                provider=provider,
                operation=operation,
                status="error",
                duration_ms=cls._elapsed_milliseconds(provider_started_at),
                row_count=0,
                request_id=request_id,
                query_id=query_id,
                error_code=(
                    str(exc.code) if getattr(exc, "code", None) is not None else None
                ),
            )
            raise
        log_event(
            logger,
            logging.INFO,
            "provider_call_completed",
            api_route=api_route,
            provider=provider,
            operation=operation,
            status="success",
            duration_ms=cls._elapsed_milliseconds(provider_started_at),
            row_count=len(frame),
            request_id=request_id,
            query_id=query_id,
            error_code=None,
        )
        return frame

    @staticmethod
    def _elapsed_milliseconds(started_at: float) -> int:
        """Return elapsed monotonic time as whole milliseconds."""
        return int((perf_counter() - started_at) * MILLISECONDS_PER_SECOND)

    @classmethod
    def _log_cache_lookup(
        cls,
        *,
        api_route: str,
        provider: str,
        operation: str,
        outcome: str,
        cache_layer: str,
        started_at: float,
        request_id: str,
        query_id: str,
    ) -> None:
        """Emit exactly one final cache outcome for a logical data query."""
        with request_cache_metrics_lock:
            metrics = request_cache_metrics.setdefault(request_id, {})
            metrics[outcome] = metrics.get(outcome, 0) + 1

        log_event(
            logger,
            logging.INFO,
            "cache_lookup_completed",
            api_route=api_route,
            provider=provider,
            operation=operation,
            outcome=outcome,
            cache_layer=cache_layer,
            duration_ms=cls._elapsed_milliseconds(started_at),
            request_id=request_id,
            query_id=query_id,
        )

    def _now(self) -> datetime:
        """Return a validated timezone-aware instant from the configured clock."""
        now = self._now_provider()
        self._require_aware_datetime(now)
        return now

    @staticmethod
    def _require_aware_datetime(value: datetime) -> None:
        """Reject ambiguous instants before expiration comparisons."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cache timestamps must be timezone-aware")

    @staticmethod
    def _is_valid(
        record: Optional[DataCacheRecord],
        now: datetime,
    ) -> bool:
        """Return whether a cache record exists and has not expired."""
        return record is not None and record.expires_at > now

    @staticmethod
    def _record_to_frame(record: DataCacheRecord) -> pd.DataFrame:
        """Reconstruct an isolated DataFrame from a cache record."""
        return pd.DataFrame(record.rows, columns=record.columns)

    @staticmethod
    def _lock_index(cache_key: str) -> int:
        """Select a stable bounded lock stripe for one cache key."""
        digest = hashlib.sha256(cache_key.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], byteorder="big") % CACHE_LOCK_STRIPE_COUNT


def build_data_cache_key(
    provider: str,
    operation: str,
    params: Dict[str, object],
    fields: Sequence[str],
) -> str:
    """Return a versioned SHA-256 key for one canonical provider request."""
    if not NAME_PATTERN.fullmatch(provider):
        raise ValueError("provider contains unsupported characters")
    if not OPERATION_PATTERN.fullmatch(operation):
        raise ValueError("operation contains unsupported characters")
    canonical_request = {
        "schema_version": DATA_CACHE_SCHEMA_VERSION,
        "provider": provider,
        "operation": operation,
        "params": params,
        # Field order remains significant because it determines response column order.
        "fields": list(fields),
    }
    serialized = json.dumps(
        canonical_request,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return (
        f"v{DATA_CACHE_SCHEMA_VERSION}/{provider}/{operation}/{digest}"
    )

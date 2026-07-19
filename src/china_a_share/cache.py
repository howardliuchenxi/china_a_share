"""Layered caching for successful Tushare responses."""

from collections import OrderedDict
from datetime import date, datetime, time, timedelta, timezone
import gzip
import hashlib
import json
import logging
import re
from threading import Lock, RLock
from typing import Any, Callable, Dict, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from google.api_core.exceptions import NotFound
from google.cloud import storage
import pandas as pd

from .cache_contracts import TushareCacheStore
from .contracts import CACHE_SCHEMA_VERSION, TushareCacheRecord


logger = logging.getLogger(__name__)

BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_CACHE_TTL = timedelta(minutes=15)
SHORT_CACHE_TTL = timedelta(minutes=5)
REALTIME_CACHE_TTL = timedelta(seconds=15)
REFERENCE_CACHE_TTL = timedelta(hours=24)
TRADE_CALENDAR_CACHE_TTL = timedelta(days=30)
HISTORICAL_CACHE_TTL = timedelta(days=30)
FINANCIAL_CACHE_TTL = timedelta(hours=1)
DEFAULT_L1_MAX_ENTRIES = 256
DEFAULT_L1_MAX_BYTES = 128 * 1024 * 1024
CACHE_LOCK_STRIPE_COUNT = 64
CACHE_OBJECT_PREFIX = "cache"
API_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")

REALTIME_APIS = {"rt_k", "rt_min", "rt_min_daily"}
REFERENCE_APIS = {
    "stock_basic",
    "stock_company",
    "bse_mapping",
    "namechange",
    "ths_index",
    "ths_member",
    "dc_concept",
    "dc_concept_cons",
    "dc_index",
    "dc_member",
}
FINANCIAL_APIS = {
    "income",
    "balancesheet",
    "cashflow",
    "fina_indicator",
    "fina_audit",
    "fina_mainbz",
    "forecast",
    "express",
    "dividend",
    "disclosure_date",
}
PUBLICATION_TIMES = {
    "daily": time(17, 10),
    "daily_basic": time(17, 10),
    "adj_factor": time(17, 10),
    "weekly": time(17, 10),
    "monthly": time(17, 10),
    "stk_limit": time(9, 10),
    "moneyflow": time(19, 10),
    "stk_holdertrade": time(19, 10),
    "top_list": time(20, 10),
    "top_inst": time(20, 10),
    "block_trade": time(21, 10),
    "pledge_detail": time(21, 10),
    "pledge_stat": time(21, 10),
    "stk_mins": time(21, 10),
}


def _utc_now() -> datetime:
    """Return the current UTC instant for cache expiration comparisons."""
    return datetime.now(timezone.utc)


class MemoryTushareCacheStore:
    """Bounded and thread-safe process-local L1 cache."""

    def __init__(self, max_entries: int, max_bytes: int) -> None:
        """Initialize limits, ordered entries, byte accounting, and locking."""
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: "OrderedDict[str, Tuple[TushareCacheRecord, int]]" = OrderedDict()
        self._current_bytes = 0
        self._lock = RLock()

    def get(self, cache_key: str) -> Optional[TushareCacheRecord]:
        """Return one L1 record and update its recency when present."""
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is None:
                return None
            self._entries.move_to_end(cache_key)
            return entry[0].model_copy(deep=True)

    def put(self, cache_key: str, record: TushareCacheRecord) -> None:
        """Store one L1 record and evict entries until both limits are satisfied."""
        record_size = len(record.model_dump_json().encode("utf-8"))
        if record_size > self._max_bytes:
            # Oversized responses remain available in L2 without displacing every hot L1 entry.
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

            # LRU eviction is bounded by both object count and serialized payload size.
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


class CloudStorageTushareCacheStore:
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

    def get(self, cache_key: str) -> Optional[TushareCacheRecord]:
        """Download and validate one compressed cache record when present."""
        blob = self._bucket.blob(self._object_name(cache_key))
        try:
            compressed_payload = blob.download_as_bytes()
        except NotFound:
            return None
        except Exception:
            logger.exception("cache_l2_read_failed cache_key=%s", cache_key)
            raise

        try:
            payload = gzip.decompress(compressed_payload)
            return TushareCacheRecord.model_validate_json(payload)
        except Exception:
            # Corrupt cache data must remain visible instead of becoming a hidden upstream call.
            logger.exception("cache_l2_record_invalid cache_key=%s", cache_key)
            raise

    def put(self, cache_key: str, record: TushareCacheRecord) -> None:
        """Serialize, compress, and atomically upload one cache record."""
        payload = gzip.compress(record.model_dump_json().encode("utf-8"))
        blob = self._bucket.blob(self._object_name(cache_key))
        try:
            # A single object replacement is atomic, so readers see either complete generation.
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


class LayeredTushareResponseCache:
    """Coordinate L1, L2, expiration, and miss deduplication."""

    def __init__(
        self,
        memory_store: TushareCacheStore,
        persistent_store: TushareCacheStore,
        now_provider: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Initialize explicit stores and bounded lock stripes."""
        self._memory_store = memory_store
        self._persistent_store = persistent_store
        self._now_provider = now_provider
        # Lock striping bounds lock memory while deduplicating concurrent misses per key.
        self._miss_locks = [Lock() for _ in range(CACHE_LOCK_STRIPE_COUNT)]

    def get(self, cache_key: str, now: datetime) -> Optional[TushareCacheRecord]:
        """Resolve one unexpired record through L1 and then L2."""
        self._require_aware_datetime(now)
        memory_record = self._memory_store.get(cache_key)
        if self._is_valid(memory_record, now):
            logger.info("cache_hit layer=l1 cache_key=%s", cache_key)
            return memory_record

        persistent_record = self._persistent_store.get(cache_key)
        if not self._is_valid(persistent_record, now):
            logger.info("cache_miss cache_key=%s", cache_key)
            return None

        self._memory_store.put(cache_key, persistent_record)
        logger.info("cache_hit layer=l2 cache_key=%s", cache_key)
        return persistent_record

    def put(self, cache_key: str, record: TushareCacheRecord) -> None:
        """Persist one successful response in L2 before publishing it to L1."""
        self._persistent_store.put(cache_key, record)
        self._memory_store.put(cache_key, record)
        logger.info("cache_write_completed cache_key=%s", cache_key)

    def get_or_fetch(
        self,
        api_name: str,
        params: Dict[str, Any],
        fields: Sequence[str],
        fetch: Callable[[], pd.DataFrame],
    ) -> pd.DataFrame:
        """Return cached data or perform one deduplicated upstream fetch."""
        cache_key = build_tushare_cache_key(api_name, params, fields)
        now = self._now()
        record = self.get(cache_key, now)
        if record is not None:
            return self._record_to_frame(record)

        miss_lock = self._miss_locks[self._lock_index(cache_key)]
        with miss_lock:
            # Recheck after locking because another request may have filled both layers.
            now = self._now()
            record = self.get(cache_key, now)
            if record is not None:
                return self._record_to_frame(record)

            frame = fetch()
            safe_frame = frame.astype(object).where(pd.notnull(frame), None)
            record = TushareCacheRecord(
                api_name=api_name,
                params=params,
                fields=list(fields),
                fetched_at=now,
                expires_at=resolve_tushare_cache_expiration(api_name, params, now),
                columns=list(safe_frame.columns),
                rows=[list(row) for row in safe_frame.itertuples(index=False, name=None)],
            )
            self.put(cache_key, record)
            return frame.copy(deep=True)

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
        record: Optional[TushareCacheRecord],
        now: datetime,
    ) -> bool:
        """Return whether a cache record exists and has not expired."""
        return record is not None and record.expires_at > now

    @staticmethod
    def _record_to_frame(record: TushareCacheRecord) -> pd.DataFrame:
        """Reconstruct an isolated DataFrame from a cache record."""
        return pd.DataFrame(record.rows, columns=record.columns)

    @staticmethod
    def _lock_index(cache_key: str) -> int:
        """Select a stable bounded lock stripe for one cache key."""
        digest = hashlib.sha256(cache_key.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], byteorder="big") % CACHE_LOCK_STRIPE_COUNT


def build_tushare_cache_key(
    api_name: str,
    params: Dict[str, Any],
    fields: Sequence[str],
) -> str:
    """Return a versioned SHA-256 key for one canonical Tushare request."""
    if not API_NAME_PATTERN.fullmatch(api_name):
        raise ValueError("api_name contains unsupported characters")
    canonical_request = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "api_name": api_name,
        "params": params,
        # Field order remains significant because it determines the response column order.
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
    return f"v{CACHE_SCHEMA_VERSION}/{api_name}/{digest}"


def resolve_tushare_cache_expiration(
    api_name: str,
    params: Dict[str, Any],
    fetched_at: datetime,
) -> datetime:
    """Return an Asia/Shanghai-aware expiration based on publication time."""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    beijing_now = fetched_at.astimezone(BEIJING_TIMEZONE)

    if api_name in REALTIME_APIS:
        return beijing_now + REALTIME_CACHE_TTL
    if api_name == "trade_cal":
        return beijing_now + TRADE_CALENDAR_CACHE_TTL
    if api_name in REFERENCE_APIS:
        return beijing_now + REFERENCE_CACHE_TTL
    if api_name in FINANCIAL_APIS:
        return beijing_now + FINANCIAL_CACHE_TTL

    publication_time = _publication_time(api_name, params)
    if publication_time is None:
        return beijing_now + DEFAULT_CACHE_TTL

    requested_date = _requested_end_date(params)
    if requested_date is not None and requested_date < beijing_now.date():
        return beijing_now + HISTORICAL_CACHE_TTL

    publication_date = requested_date or beijing_now.date()
    completion = datetime.combine(
        publication_date,
        publication_time,
        tzinfo=BEIJING_TIMEZONE,
    )
    if requested_date is not None and beijing_now >= completion:
        return beijing_now + HISTORICAL_CACHE_TTL
    if requested_date is None and beijing_now >= completion:
        completion += timedelta(days=1)

    # Before publication completes, short caching avoids preserving partial or empty data.
    return min(beijing_now + SHORT_CACHE_TTL, completion)


def _publication_time(api_name: str, params: Dict[str, Any]) -> Optional[time]:
    """Return the conservative completion time for one supported endpoint."""
    if api_name == "pro_bar" and str(params.get("freq", "")).lower().endswith("min"):
        return time(21, 10)
    return PUBLICATION_TIMES.get(api_name)


def _requested_end_date(params: Dict[str, Any]) -> Optional[date]:
    """Extract an explicit fixed business date from common Tushare parameters."""
    for name in ("trade_date", "end_date", "cal_date"):
        value = params.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = value.strip()
        try:
            if len(normalized) >= 10 and normalized[4] == "-":
                return date.fromisoformat(normalized[:10])
            if len(normalized) >= 8 and normalized[:8].isdigit():
                return datetime.strptime(normalized[:8], "%Y%m%d").date()
        except ValueError:
            # Invalid user parameters remain the upstream validator's responsibility.
            return None
    return None

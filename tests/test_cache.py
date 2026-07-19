from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event, Lock
from zoneinfo import ZoneInfo

from google.api_core.exceptions import NotFound
import pandas as pd
import pytest

from china_a_share.cache import (
    CloudStorageTushareCacheStore,
    LayeredTushareResponseCache,
    MemoryTushareCacheStore,
    build_tushare_cache_key,
    resolve_tushare_cache_expiration,
)
from china_a_share.contracts import TushareCacheRecord


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


class DictCacheStore:
    def __init__(self):
        self.records = {}
        self.get_calls = []
        self.put_calls = []

    def get(self, cache_key):
        self.get_calls.append(cache_key)
        return self.records.get(cache_key)

    def put(self, cache_key, record):
        self.put_calls.append(cache_key)
        self.records[cache_key] = record


class FakeBlob:
    def __init__(self, objects, name):
        self.objects = objects
        self.name = name

    def download_as_bytes(self):
        if self.name not in self.objects:
            raise NotFound("object not found")
        return self.objects[self.name]

    def upload_from_string(self, payload, content_type):
        assert content_type == "application/gzip"
        self.objects[self.name] = payload


class FakeBucket:
    def __init__(self, objects):
        self.objects = objects

    def blob(self, name):
        return FakeBlob(self.objects, name)


class FakeStorageClient:
    def __init__(self):
        self.objects = {}
        self.bucket_names = []

    def bucket(self, name):
        self.bucket_names.append(name)
        return FakeBucket(self.objects)


def make_record(
    api_name="daily",
    fetched_at=None,
    expires_at=None,
    close=10.5,
):
    fetched_at = fetched_at or datetime(2026, 7, 17, 17, 15, tzinfo=BEIJING_TIMEZONE)
    expires_at = expires_at or fetched_at + timedelta(days=1)
    return TushareCacheRecord(
        api_name=api_name,
        params={"trade_date": "20260717"},
        fields=["ts_code", "close"],
        fetched_at=fetched_at,
        expires_at=expires_at,
        columns=["ts_code", "close"],
        rows=[["000001.SZ", close]],
    )


def test_cache_key_normalizes_parameter_order_and_preserves_field_order():
    first = build_tushare_cache_key(
        "daily",
        {"trade_date": "20260717", "ts_code": "000001.SZ"},
        ["ts_code", "close"],
    )
    reordered_params = build_tushare_cache_key(
        "daily",
        {"ts_code": "000001.SZ", "trade_date": "20260717"},
        ["ts_code", "close"],
    )
    reordered_fields = build_tushare_cache_key(
        "daily",
        {"trade_date": "20260717", "ts_code": "000001.SZ"},
        ["close", "ts_code"],
    )

    assert first == reordered_params
    assert first != reordered_fields
    assert first.startswith("v1/daily/")


def test_memory_store_evicts_least_recently_used_entry():
    store = MemoryTushareCacheStore(max_entries=2, max_bytes=1_000_000)
    store.put("first", make_record(close=10.0))
    store.put("second", make_record(close=11.0))

    assert store.get("first") is not None
    store.put("third", make_record(close=12.0))

    assert store.get("first") is not None
    assert store.get("second") is None
    assert store.get("third") is not None


def test_cloud_storage_store_round_trips_compressed_record():
    client = FakeStorageClient()
    store = CloudStorageTushareCacheStore("test-cache", storage_client=client)
    record = make_record()

    store.put("v1/daily/key", record)
    restored = store.get("v1/daily/key")

    assert client.bucket_names == ["test-cache"]
    assert restored == record
    assert "cache/v1/daily/key.json.gz" in client.objects


def test_cloud_storage_store_returns_none_for_missing_object():
    store = CloudStorageTushareCacheStore(
        "test-cache",
        storage_client=FakeStorageClient(),
    )

    assert store.get("v1/daily/missing") is None


def test_layered_cache_promotes_valid_l2_record_into_l1():
    now = datetime(2026, 7, 17, 17, 15, tzinfo=BEIJING_TIMEZONE)
    memory_store = DictCacheStore()
    persistent_store = DictCacheStore()
    persistent_store.records["key"] = make_record(fetched_at=now)
    cache = LayeredTushareResponseCache(
        memory_store,
        persistent_store,
        now_provider=lambda: now,
    )

    record = cache.get("key", now)

    assert record is not None
    assert memory_store.records["key"] == record
    assert persistent_store.get_calls == ["key"]


def test_layered_cache_deduplicates_concurrent_upstream_misses():
    now = datetime(2026, 7, 17, 17, 15, tzinfo=BEIJING_TIMEZONE)
    memory_store = DictCacheStore()
    persistent_store = DictCacheStore()
    cache = LayeredTushareResponseCache(
        memory_store,
        persistent_store,
        now_provider=lambda: now,
    )
    fetch_started = Event()
    release_fetch = Event()
    fetch_count = 0
    fetch_count_lock = Lock()

    def fetch():
        nonlocal fetch_count
        with fetch_count_lock:
            fetch_count += 1
        fetch_started.set()
        assert release_fetch.wait(timeout=2)
        return pd.DataFrame([{"ts_code": "000001.SZ", "close": 10.5}])

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(
                cache.get_or_fetch,
                "daily",
                {"trade_date": "20260717"},
                ["ts_code", "close"],
                fetch,
            )
            for _ in range(4)
        ]
        assert fetch_started.wait(timeout=2)
        release_fetch.set()
        results = [future.result(timeout=2) for future in futures]

    assert fetch_count == 1
    assert all(result.iloc[0]["close"] == 10.5 for result in results)


def test_layered_cache_does_not_store_upstream_errors():
    now = datetime(2026, 7, 17, 17, 15, tzinfo=BEIJING_TIMEZONE)
    memory_store = DictCacheStore()
    persistent_store = DictCacheStore()
    cache = LayeredTushareResponseCache(
        memory_store,
        persistent_store,
        now_provider=lambda: now,
    )
    fetch_count = 0

    def fetch():
        nonlocal fetch_count
        fetch_count += 1
        raise RuntimeError("upstream unavailable")

    for _ in range(2):
        with pytest.raises(RuntimeError, match="upstream unavailable"):
            cache.get_or_fetch("daily", {}, [], fetch)

    assert fetch_count == 2
    assert memory_store.records == {}
    assert persistent_store.records == {}


def test_daily_expiration_is_short_before_publication_completion():
    fetched_at = datetime(2026, 7, 17, 16, 0, tzinfo=BEIJING_TIMEZONE)

    expires_at = resolve_tushare_cache_expiration(
        "daily",
        {"trade_date": "20260717"},
        fetched_at,
    )

    assert expires_at == datetime(2026, 7, 17, 16, 5, tzinfo=BEIJING_TIMEZONE)


def test_daily_expiration_is_long_after_fixed_date_is_complete():
    fetched_at = datetime(2026, 7, 17, 17, 15, tzinfo=BEIJING_TIMEZONE)

    expires_at = resolve_tushare_cache_expiration(
        "daily",
        {"trade_date": "20260717"},
        fetched_at,
    )

    assert expires_at == fetched_at + timedelta(days=30)


def test_trade_calendar_uses_long_reference_ttl():
    fetched_at = datetime(2026, 7, 17, 9, 0, tzinfo=BEIJING_TIMEZONE)

    expires_at = resolve_tushare_cache_expiration("trade_cal", {}, fetched_at)

    assert expires_at == fetched_at + timedelta(days=30)

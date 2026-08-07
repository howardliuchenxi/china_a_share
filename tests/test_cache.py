import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event, Lock
from zoneinfo import ZoneInfo

from google.api_core.exceptions import NotFound
import pandas as pd
import pytest

from china_a_share.cache import (
    CloudStorageDataCacheStore,
    LayeredDataResponseCache,
    MemoryDataCacheStore,
    build_data_cache_key,
)
from china_a_share.core.contracts import DataCacheRecord
from china_a_share.providers.tushare import TushareCacheExpirationPolicy
from china_a_share.providers.tushare import PROFILED_OPERATIONS
from china_a_share.registry import READ_ONLY_API_NAMES


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
    operation="daily",
    fetched_at=None,
    expires_at=None,
    close=10.5,
):
    fetched_at = fetched_at or datetime(2026, 7, 17, 17, 15, tzinfo=BEIJING_TIMEZONE)
    expires_at = expires_at or fetched_at + timedelta(days=1)
    return DataCacheRecord(
        provider="tushare",
        operation=operation,
        params={"trade_date": "20260717"},
        fields=["ts_code", "close"],
        fetched_at=fetched_at,
        expires_at=expires_at,
        columns=["ts_code", "close"],
        rows=[["000001.SZ", close]],
    )


def test_cache_key_normalizes_parameter_order_and_preserves_field_order():
    first = build_data_cache_key(
        "tushare",
        "daily",
        {"trade_date": "20260717", "ts_code": "000001.SZ"},
        ["ts_code", "close"],
    )
    reordered_params = build_data_cache_key(
        "tushare",
        "daily",
        {"ts_code": "000001.SZ", "trade_date": "20260717"},
        ["ts_code", "close"],
    )
    reordered_fields = build_data_cache_key(
        "tushare",
        "daily",
        {"trade_date": "20260717", "ts_code": "000001.SZ"},
        ["close", "ts_code"],
    )

    assert first == reordered_params
    assert first != reordered_fields
    assert first.startswith("v4/tushare/daily/")


def test_cache_key_isolated_by_market_data_provider():
    tushare_key = build_data_cache_key(
        "tushare",
        "daily",
        {"trade_date": "20260717"},
        ["ts_code", "close"],
    )
    alternative_key = build_data_cache_key(
        "alternative",
        "daily",
        {"trade_date": "20260717"},
        ["ts_code", "close"],
    )

    assert tushare_key != alternative_key
    assert alternative_key.startswith("v4/alternative/daily/")


def test_memory_store_evicts_least_recently_used_entry():
    store = MemoryDataCacheStore(max_entries=2, max_bytes=1_000_000)
    store.put("first", make_record(close=10.0))
    store.put("second", make_record(close=11.0))

    assert store.get("first") is not None
    store.put("third", make_record(close=12.0))

    assert store.get("first") is not None
    assert store.get("second") is None
    assert store.get("third") is not None


def test_cloud_storage_store_round_trips_compressed_record():
    client = FakeStorageClient()
    store = CloudStorageDataCacheStore("test-cache", storage_client=client)
    record = make_record()

    store.put("v3/tushare/daily/key", record)
    restored = store.get("v3/tushare/daily/key")

    assert client.bucket_names == ["test-cache"]
    assert restored == record
    assert "cache/v3/tushare/daily/key.json.gz" in client.objects


def test_cloud_storage_store_returns_none_for_missing_object():
    store = CloudStorageDataCacheStore(
        "test-cache",
        storage_client=FakeStorageClient(),
    )

    assert store.get("v3/tushare/daily/missing") is None


def test_layered_cache_promotes_valid_l2_record_into_l1():
    now = datetime(2026, 7, 17, 17, 15, tzinfo=BEIJING_TIMEZONE)
    memory_store = DictCacheStore()
    persistent_store = DictCacheStore()
    persistent_store.records["key"] = make_record(fetched_at=now)
    cache = LayeredDataResponseCache(
        memory_store,
        persistent_store,
        TushareCacheExpirationPolicy(),
        now_provider=lambda: now,
    )

    record = cache.get("key", now)

    assert record is not None
    assert memory_store.records["key"] == record
    assert persistent_store.get_calls == ["key"]


def test_layered_cache_deduplicates_concurrent_upstream_misses(caplog):
    now = datetime(2026, 7, 17, 17, 15, tzinfo=BEIJING_TIMEZONE)
    memory_store = DictCacheStore()
    persistent_store = DictCacheStore()
    cache = LayeredDataResponseCache(
        memory_store,
        persistent_store,
        TushareCacheExpirationPolicy(),
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

    with caplog.at_level(logging.INFO):
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(
                    cache.get_or_fetch,
                    "tushare",
                    "daily",
                    {"trade_date": "20260717"},
                    ["ts_code", "close"],
                    fetch,
                    api_route="/api/analysis",
                    request_id=f"request-{index}",
                    query_id="daily-prices",
                )
                for index in range(4)
            ]
            assert fetch_started.wait(timeout=2)
            release_fetch.set()
            results = [future.result(timeout=2) for future in futures]

    assert fetch_count == 1
    assert all(result.iloc[0]["close"] == 10.5 for result in results)
    events = [
        record.structured_fields
        for record in caplog.records
        if hasattr(record, "structured_fields")
    ]
    cache_events = [
        event for event in events if event["event"] == "cache_lookup_completed"
    ]
    provider_events = [
        event for event in events if event["event"] == "provider_call_completed"
    ]
    assert len(cache_events) == 4
    assert sum(event["outcome"] == "miss" for event in cache_events) == 1
    assert sum(event["outcome"] == "hit" for event in cache_events) == 3
    assert len(provider_events) == 1
    assert provider_events[0]["status"] == "success"
    assert provider_events[0]["row_count"] == 1


def test_layered_cache_does_not_store_upstream_errors(caplog):
    now = datetime(2026, 7, 17, 17, 15, tzinfo=BEIJING_TIMEZONE)
    memory_store = DictCacheStore()
    persistent_store = DictCacheStore()
    cache = LayeredDataResponseCache(
        memory_store,
        persistent_store,
        TushareCacheExpirationPolicy(),
        now_provider=lambda: now,
    )
    fetch_count = 0

    def fetch():
        nonlocal fetch_count
        fetch_count += 1
        raise RuntimeError("upstream unavailable")

    with caplog.at_level(logging.INFO):
        for index in range(2):
            with pytest.raises(RuntimeError, match="upstream unavailable"):
                cache.get_or_fetch(
                    "tushare",
                    "daily",
                    {},
                    [],
                    fetch,
                    api_route="/api/analysis",
                    request_id=f"request-{index}",
                    query_id="daily-prices",
                )

    assert fetch_count == 2
    assert memory_store.records == {}
    assert persistent_store.records == {}
    provider_events = [
        record.structured_fields
        for record in caplog.records
        if getattr(record, "structured_fields", {}).get("event")
        == "provider_call_completed"
    ]
    assert len(provider_events) == 2
    assert all(event["status"] == "error" for event in provider_events)


def test_daily_expiration_is_short_before_publication_completion():
    fetched_at = datetime(2026, 7, 17, 16, 0, tzinfo=BEIJING_TIMEZONE)

    expires_at = TushareCacheExpirationPolicy().resolve(
        "daily",
        {"trade_date": "20260717"},
        fetched_at,
    )

    assert expires_at == datetime(2026, 7, 17, 16, 5, tzinfo=BEIJING_TIMEZONE)


def test_daily_expiration_is_long_after_fixed_date_is_complete():
    fetched_at = datetime(2026, 7, 17, 17, 15, tzinfo=BEIJING_TIMEZONE)

    expires_at = TushareCacheExpirationPolicy().resolve(
        "daily",
        {"trade_date": "20260717"},
        fetched_at,
    )

    assert expires_at == fetched_at + timedelta(days=90)


def test_trade_calendar_uses_long_reference_ttl():
    fetched_at = datetime(2026, 7, 17, 9, 0, tzinfo=BEIJING_TIMEZONE)

    expires_at = TushareCacheExpirationPolicy().resolve(
        "trade_cal", {}, fetched_at
    )

    assert expires_at == fetched_at + timedelta(days=30)


def test_every_catalog_operation_has_an_explicit_cache_profile():
    assert PROFILED_OPERATIONS == set(READ_ONLY_API_NAMES)


def test_fixed_float_holder_snapshot_uses_quarterly_disclosure_ttl():
    fetched_at = datetime(2026, 7, 17, 9, 0, tzinfo=BEIJING_TIMEZONE)

    expires_at = TushareCacheExpirationPolicy().resolve(
        "top10_floatholders",
        {"ts_code": "600000.SH", "period": "20260331"},
        fetched_at,
    )

    assert expires_at == fetched_at + timedelta(days=90)


def test_latest_float_holder_query_refreshes_daily():
    fetched_at = datetime(2026, 7, 17, 9, 0, tzinfo=BEIJING_TIMEZONE)

    expires_at = TushareCacheExpirationPolicy().resolve(
        "top10_floatholders",
        {"ts_code": "600000.SH"},
        fetched_at,
    )

    assert expires_at == fetched_at + timedelta(hours=24)


def test_realtime_operation_bypasses_both_cache_layers():
    now = datetime(2026, 7, 17, 10, 0, tzinfo=BEIJING_TIMEZONE)
    memory_store = DictCacheStore()
    persistent_store = DictCacheStore()
    cache = LayeredDataResponseCache(
        memory_store,
        persistent_store,
        TushareCacheExpirationPolicy(),
        now_provider=lambda: now,
    )
    fetch_count = 0

    def fetch():
        nonlocal fetch_count
        fetch_count += 1
        return pd.DataFrame([{"ts_code": "000001.SZ", "price": 10.5}])

    for request_id in ("request-1", "request-2"):
        cache.get_or_fetch(
            "tushare",
            "rt_k",
            {"ts_code": "000001.SZ"},
            ["ts_code", "price"],
            fetch,
            api_route="/api/analysis",
            request_id=request_id,
            query_id="realtime-price",
        )

    assert fetch_count == 2
    assert memory_store.records == {}
    assert persistent_store.records == {}


def test_intraday_history_is_cached_but_current_intraday_data_is_not():
    fetched_at = datetime(2026, 7, 17, 10, 0, tzinfo=BEIJING_TIMEZONE)
    policy = TushareCacheExpirationPolicy()

    historical_expiration = policy.resolve(
        "stk_mins",
        {"end_date": "20260716"},
        fetched_at,
    )
    current_expiration = policy.resolve(
        "stk_mins",
        {"trade_date": "20260717"},
        fetched_at,
    )

    assert historical_expiration == fetched_at + timedelta(days=90)
    assert current_expiration is None


def test_unknown_operation_has_no_implicit_cache_default():
    fetched_at = datetime(2026, 7, 17, 10, 0, tzinfo=BEIJING_TIMEZONE)

    with pytest.raises(ValueError, match="has no cache profile"):
        TushareCacheExpirationPolicy().resolve(
            "future_operation",
            {},
            fetched_at,
        )

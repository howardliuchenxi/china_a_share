"""Contracts for Tushare response cache storage backends."""

from typing import Optional, Protocol

from .contracts import TushareCacheRecord


class TushareCacheStore(Protocol):
    """Store successful Tushare responses by a deterministic cache key."""

    def get(self, cache_key: str) -> Optional[TushareCacheRecord]:
        """Return the stored response or None when the key is absent."""
        ...

    def put(self, cache_key: str, record: TushareCacheRecord) -> None:
        """Create or replace the response stored under the cache key."""
        ...

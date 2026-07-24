"""Structural interfaces for replaceable planners, providers, and caches."""

from datetime import datetime
from typing import Any, Callable, Dict, Optional, Protocol, Sequence

import pandas as pd

from .contracts import (
    AnalysisImage,
    AnalysisRequest,
    DataCacheRecord,
    DataOperation,
    QueryPlan,
)


class VisionAnalyzer(Protocol):
    """Convert one screenshot into text relevant to the user's request."""

    @property
    def name(self) -> str:
        """Return the stable vision-provider identifier."""
        ...

    def analyze(self, prompt: str, image: AnalysisImage) -> str:
        """Describe screenshot evidence needed to interpret the supplied prompt."""
        ...


class QueryPlanner(Protocol):
    """Convert one natural-language request into a provider-native query plan."""

    @property
    def name(self) -> str:
        """Return the stable planner implementation identifier."""
        ...

    def plan(
        self,
        request: AnalysisRequest,
        candidate_operations: Sequence[DataOperation],
    ) -> QueryPlan:
        """Build a structured plan using only the supplied operation catalog."""
        ...


class MarketDataProvider(Protocol):
    """Expose one replaceable market-data catalog and query transport."""

    @property
    def name(self) -> str:
        """Return the stable data-provider identifier."""
        ...

    def search_operations(self, prompt: str) -> Sequence[DataOperation]:
        """Return provider operations relevant to the natural-language prompt."""
        ...

    def supports(self, operation: str) -> bool:
        """Return whether the provider exposes the requested read operation."""
        ...

    def query(
        self,
        operation: str,
        params: Dict[str, Any],
        fields: Sequence[str],
        *,
        api_route: str,
        request_id: str,
        query_id: str,
    ) -> pd.DataFrame:
        """Execute one provider-native read and return a normalized table."""
        ...


class CacheExpirationPolicy(Protocol):
    """Resolve cache expiration according to one provider's publication rules."""

    def resolve(
        self,
        operation: str,
        params: Dict[str, Any],
        fetched_at: datetime,
    ) -> datetime:
        """Return the timezone-aware expiration instant for one response."""
        ...


class DataCacheStore(Protocol):
    """Store successful provider responses by a deterministic cache key."""

    def get(self, cache_key: str) -> Optional[DataCacheRecord]:
        """Return an isolated cache record when the key exists."""
        ...

    def put(self, cache_key: str, record: DataCacheRecord) -> None:
        """Create or replace the response stored under the cache key."""
        ...


class DataResponseCache(Protocol):
    """Return provider responses through a provider-aware cache namespace."""

    def get_or_fetch(
        self,
        provider: str,
        operation: str,
        params: Dict[str, Any],
        fields: Sequence[str],
        fetch: Callable[[], pd.DataFrame],
        *,
        api_route: str,
        request_id: str,
        query_id: str,
    ) -> pd.DataFrame:
        """Return cached data or execute one deduplicated upstream fetch."""
        ...

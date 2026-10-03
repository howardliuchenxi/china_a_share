"""VIX daily-close history with lazy FRED backfill and monotonic appends.

One JSON object in the application bucket holds `{"closes": {date: close}}`.
The object is seeded from FRED's full VIXCLS series on first use (the official
daily-close series lags roughly one day, so intraday and same-day closes come
from the CNBC adapter instead). Appends only ever extend the series with dates
newer than the latest stored close, making settlement recording idempotent.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date as date_type
from typing import Callable, Dict, Optional, Protocol, Tuple

from google.cloud import storage

from china_a_share.observability import log_event

logger = logging.getLogger(__name__)

FRED_VIXCLS_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS&cosd=1990-01-01"
HISTORY_OBJECT_NAME = "vix/history.json"

HttpGet = Callable[[str, float], str]


class VixHistoryStore(Protocol):
    """Persist the single VIX daily-close object."""

    def get_closes(self) -> Optional[Dict[str, float]]:
        """Return the stored mapping, or None when the object does not exist."""
        ...

    def put_closes(self, closes: Dict[str, float]) -> None:
        """Replace the stored mapping with a validated one."""
        ...


class MemoryVixHistoryStore:
    """In-memory history store for tests and single-process deployments."""

    def __init__(self, closes: Optional[Dict[str, float]] = None) -> None:
        self._closes: Optional[Dict[str, float]] = (
            dict(closes) if closes is not None else None
        )

    def get_closes(self) -> Optional[Dict[str, float]]:
        if self._closes is None:
            return None
        return dict(self._closes)

    def put_closes(self, closes: Dict[str, float]) -> None:
        self._closes = dict(closes)


class CloudStorageVixHistoryStore:
    """Persist the VIX history as one JSON object in the app bucket."""

    def __init__(
        self,
        bucket_name: str,
        storage_client: Optional[storage.Client] = None,
    ) -> None:
        if not bucket_name:
            raise ValueError("A bucket name is required for the VIX history store.")
        self._bucket = (storage_client or storage.Client()).bucket(bucket_name)

    def get_closes(self) -> Optional[Dict[str, float]]:
        blob = self._bucket.blob(HISTORY_OBJECT_NAME)
        try:
            if not blob.exists():
                return None
            payload = json.loads(blob.download_as_text())
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"VIX history object is unreadable: {exc}") from exc
        closes = (payload or {}).get("closes")
        if not isinstance(closes, dict):
            raise RuntimeError("VIX history object has an unexpected shape.")
        return {str(k): float(v) for k, v in closes.items()}

    def put_closes(self, closes: Dict[str, float]) -> None:
        blob = self._bucket.blob(HISTORY_OBJECT_NAME)
        blob.upload_from_string(
            json.dumps({"closes": dict(sorted(closes.items()))}, ensure_ascii=False),
            content_type="application/json",
        )


def parse_fred_vixcls_csv(payload: str) -> Dict[str, float]:
    """Parse one fredgraph.csv payload, skipping missing markers and header."""
    closes: Dict[str, float] = {}
    reader = csv.DictReader(io.StringIO(payload))
    if reader.fieldnames is None or "observation_date" not in reader.fieldnames:
        raise ValueError("FRED payload is missing the observation_date column.")
    for row in reader:
        raw_date = str(row.get("observation_date") or "").strip()
        raw_value = str(row.get("VIXCLS") or "").strip()
        if not raw_date or raw_value in (".", ""):
            continue
        try:
            day = date_type.fromisoformat(raw_date)
            value = float(raw_value)
        except (ValueError, TypeError):
            continue
        if value > 0:
            closes[day.isoformat()] = value
    if not closes:
        raise ValueError("FRED payload produced no usable VIX closes.")
    return closes


class VixHistory:
    """Read/extend the VIX close series with lazy seeding and lookups."""

    def __init__(
        self,
        store: VixHistoryStore,
        *,
        http_get: Optional[HttpGet] = None,
        fred_url: str = FRED_VIXCLS_URL,
    ) -> None:
        self._store = store
        self._http_get = http_get
        self._fred_url = fred_url
        self._closes: Optional[Dict[str, float]] = None
        self._seeded = False

    def _load(self) -> Dict[str, float]:
        if self._closes is None:
            stored = self._store.get_closes()
            self._closes = dict(stored) if stored is not None else {}
        return self._closes

    def ensure_seeded(self) -> bool:
        """Backfill from FRED once when the stored object is missing/empty."""
        closes = self._load()
        if self._seeded or closes:
            return False
        payload = _fetch_fred_csv(self._http_get, self._fred_url)
        seeded = parse_fred_vixcls_csv(payload)
        if not closes:
            self._closes = dict(sorted(seeded.items()))
            self._store.put_closes(self._closes)
        self._seeded = True
        return True

    def record_settlement(self, date_str: str, close: float) -> bool:
        """Append one settled close; only dates newer than the latest stick."""
        if close <= 0:
            raise ValueError("A VIX close must be positive.")
        parsed = date_type.fromisoformat(date_str)
        closes = self._load()
        if closes:
            latest_date = date_type.fromisoformat(max(closes))
            if parsed <= latest_date:
                return False
        closes[date_str] = float(close)
        self._store.put_closes(dict(sorted(closes.items())))
        return True

    def closes(self) -> Dict[str, float]:
        """Return the full close mapping (possibly after lazy seeding)."""
        return dict(self._load())

    def latest(self) -> Optional[Tuple[str, float]]:
        closes = self._load()
        if not closes:
            return None
        latest_date = max(closes)
        return latest_date, closes[latest_date]

    def lookup(self, date_str: str) -> Optional[Tuple[str, float, float]]:
        """Return (prev_date, prev_close, close) for one known trading day."""
        closes = self._load()
        if date_str not in closes:
            return None
        earlier = [d for d in closes if d < date_str]
        if not earlier:
            return None
        prev_date = max(earlier)
        return prev_date, closes[prev_date], closes[date_str]


def _fetch_fred_csv(http_get: Optional[HttpGet], url: str) -> str:
    if http_get is not None:
        return http_get(url, 30.0)
    try:
        import requests

        response = requests.get(
            url, headers={"User-Agent": "china-a-share-vix/1.0"}, timeout=30.0
        )
        response.raise_for_status()
        return response.text
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "vix_history_fred_backfill_failed",
            reason=str(exc)[:300],
        )
        raise

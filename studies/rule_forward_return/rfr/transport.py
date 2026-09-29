"""Shared Massive/Finnhub transport with the free-tier 5 req/min throttle."""
from __future__ import annotations

import os
import sys
import time
from collections import deque
from threading import Lock
from typing import Any, List, Optional

import requests

MASSIVE_API_BASE = "https://api.massive.com"
FINNHUB_API_BASE = "https://finnhub.io/api/v1"
LIMIT = 5
WINDOW_SECONDS = 60.0
TIMEOUT = 30


class SlidingWindowLimiter:
    """Block (sleep) until one of 5 slots per 60s window is free."""

    def __init__(self, limit: int = LIMIT, window: float = WINDOW_SECONDS) -> None:
        self._limit = limit
        self._window = window
        self._hits: deque = deque()
        self._lock = Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._hits and self._hits[0] <= now - self._window:
                    self._hits.popleft()
                if len(self._hits) < self._limit:
                    self._hits.append(now)
                    return
                wait = self._hits[0] + self._window - now
            time.sleep(max(wait, 0.05))


def get_massive_key() -> str:
    """Read the Massive key from env; explain the gcloud path if missing."""
    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if not key:
        sys.exit(
            "set MASSIVE_API_KEY first, e.g.\n"
            "  export MASSIVE_API_KEY=$(gcloud secrets versions access latest "
            "--secret=massive-api-key --project=$PROJECT)"
        )
    return key


def get_finnhub_key() -> str:
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        sys.exit(
            "set FINNHUB_API_KEY first, e.g.\n"
            "  export FINNHUB_API_KEY=$(gcloud secrets versions access latest "
            "--secret=finnhub-api-key --project=$PROJECT)"
        )
    return key


def massive_get(
    path: str,
    params: dict,
    limiter: SlidingWindowLimiter,
    session: Optional[requests.Session] = None,
    retries: int = 3,
) -> Any:
    """One throttled Massive GET with bounded retry on transient failures."""
    session = session or requests.Session()
    query = dict(params)
    query["apiKey"] = get_massive_key()
    url = f"{MASSIVE_API_BASE}{path}"
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        limiter.acquire()
        try:
            response = session.get(url, params=query, timeout=TIMEOUT)
        except requests.RequestException as exc:  # transport
            last_error = exc
            time.sleep(2.0 * (attempt + 1))
            continue
        if response.status_code in (429, 500, 502, 503, 504):
            last_error = RuntimeError(f"HTTP {response.status_code}")
            time.sleep(5.0 * (attempt + 1))
            continue
        if response.status_code >= 400:
            sys.exit(
                f"Massive {path} returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response.json()
    raise RuntimeError(f"Massive {path} failed after {retries} retries: {last_error}")


def finnhub_get(path: str, params: dict, session: Optional[requests.Session] = None) -> Any:
    session = session or requests.Session()
    query = dict(params)
    query["token"] = get_finnhub_key()
    response = session.get(f"{FINNHUB_API_BASE}{path}", params=query, timeout=TIMEOUT)
    if response.status_code >= 400:
        sys.exit(f"Finnhub {path} returned HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def ts_to_date(value: Any) -> str:
    """Millisecond Unix timestamp -> YYYY-MM-DD in New York time."""
    from zoneinfo import ZoneInfo
    from datetime import datetime

    tz = ZoneInfo("America/New_York")
    return datetime.fromtimestamp(value / 1000, tz=tz).date().isoformat()

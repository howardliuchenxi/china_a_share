"""Durable asynchronous analysis task coordination."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
from threading import Lock
from typing import Dict, Optional

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage

from china_a_share.application.workflow import AnalysisService
from china_a_share.core.contracts import (
    AnalysisRequest,
    AnalysisTask,
    AnalysisTaskStatus,
    AnalysisTaskSubmission,
    ServiceError,
)
from china_a_share.core.ports import AnalysisTaskDispatcher, AnalysisTaskStore


ANALYSIS_TASK_PREFIX = "analysis-jobs"
ANALYSIS_TASK_VERSION = "2"
ASYNC_REQUEST_MARKERS = (
    "\u6563\u6237\u6bd4\u4f8b",
    "\u5206\u4e24\u534a",
    "\u8fc7\u53bb\u4e00\u4e2a\u6708",
    "\u4e0a\u6da8",
)
GOOGLE_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
logger = logging.getLogger(__name__)


def requires_async_analysis(request: AnalysisRequest) -> bool:
    """Return whether a request needs durable security-specific fan-out."""
    normalized_prompt = request.prompt.replace(" ", "").lower()
    requests_full_market_retail_ranking = (
        "\u6563\u6237" in normalized_prompt
        and any(
            marker in normalized_prompt
            for marker in (
                "\u80a1\u7968",
                "a\u80a1",
                "\u5927a",
                "\u5168\u5e02\u573a",
            )
        )
        and any(
            marker in normalized_prompt
            for marker in (
                "\u524d10",
                "\u524d\u5341",
                "top10",
                "\u5206\u4f4d",
                "\u6700\u591a",
                "\u6700\u5c11",
                "\u6700\u9ad8",
                "\u6700\u4f4e",
            )
        )
    )
    if requests_full_market_retail_ranking:
        return True
    has_universe = any(
        marker in normalized_prompt
        for marker in ("\u884c\u4e1a", "\u80a1\u7968")
    )
    return has_universe and all(
        marker in normalized_prompt for marker in ASYNC_REQUEST_MARKERS
    )


class MemoryAnalysisTaskStore:
    """Store isolated task records in memory for local tests."""

    def __init__(self) -> None:
        self._tasks: Dict[str, AnalysisTask] = {}
        self._lock = Lock()

    def get(self, task_id: str) -> Optional[AnalysisTask]:
        """Return an isolated copy of one task."""
        with self._lock:
            task = self._tasks.get(task_id)
            return task.model_copy(deep=True) if task else None

    def put(self, task: AnalysisTask) -> None:
        """Create or replace one task atomically."""
        with self._lock:
            self._tasks[task.task_id] = task.model_copy(deep=True)


class CloudStorageAnalysisTaskStore:
    """Persist task records as private JSON objects in Cloud Storage."""

    def __init__(
        self,
        bucket_name: str,
        storage_client: Optional[storage.Client] = None,
    ) -> None:
        self._bucket = (storage_client or storage.Client()).bucket(bucket_name)

    def get(self, task_id: str) -> Optional[AnalysisTask]:
        """Return one persisted task when its object exists."""
        blob = self._bucket.blob(self._object_name(task_id))
        if not blob.exists():
            return None
        return AnalysisTask.model_validate_json(blob.download_as_text())

    def put(self, task: AnalysisTask) -> None:
        """Replace one complete task record."""
        blob = self._bucket.blob(self._object_name(task.task_id))
        blob.upload_from_string(
            task.model_dump_json(),
            content_type="application/json",
        )

    @staticmethod
    def _object_name(task_id: str) -> str:
        """Return the private object name for one validated task identifier."""
        return f"{ANALYSIS_TASK_PREFIX}/{task_id}.json"


class CloudRunJobDispatcher:
    """Start the configured Cloud Run Job through its authenticated API."""

    def __init__(
        self,
        project_id: str,
        region: str,
        job_name: str,
        session: Optional[AuthorizedSession] = None,
    ) -> None:
        self._url = (
            "https://run.googleapis.com/v2/"
            f"projects/{project_id}/locations/{region}/jobs/{job_name}:run"
        )
        if session is None:
            credentials, _ = google.auth.default(
                scopes=[GOOGLE_CLOUD_PLATFORM_SCOPE]
            )
            session = AuthorizedSession(credentials)
        self._session = session

    def dispatch(self, task_id: str) -> None:
        """Start one job execution with only the task identifier overridden."""
        response = self._session.post(
            self._url,
            json={
                "overrides": {
                    "containerOverrides": [
                        {
                            "env": [
                                {
                                    "name": "ANALYSIS_TASK_ID",
                                    "value": task_id,
                                }
                            ]
                        }
                    ]
                }
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                "Cloud Run Job dispatch failed with HTTP "
                f"{response.status_code}: {response.text[:500]}"
            )


class AnalysisTaskCoordinator:
    """Create, poll, and execute durable analysis tasks."""

    def __init__(
        self,
        store: AnalysisTaskStore,
        dispatcher: AnalysisTaskDispatcher,
    ) -> None:
        self._store = store
        self._dispatcher = dispatcher

    def submit(self, request: AnalysisRequest) -> AnalysisTaskSubmission:
        """Persist and dispatch one daily-idempotent analysis task."""
        task_id = self._task_id(request)
        existing = self._store.get(task_id)
        if existing and existing.status != AnalysisTaskStatus.FAILED:
            return self._submission(existing)

        now = datetime.now(timezone.utc)
        task = AnalysisTask(
            task_id=task_id,
            status=AnalysisTaskStatus.QUEUED,
            request=request,
            created_at=now,
            updated_at=now,
        )
        self._store.put(task)
        try:
            self._dispatcher.dispatch(task_id)
        except Exception as exc:
            logger.exception("analysis_task_dispatch_failed task_id=%s", task_id)
            task.status = AnalysisTaskStatus.FAILED
            task.updated_at = datetime.now(timezone.utc)
            task.error = ServiceError(source="system", message=str(exc))
            self._store.put(task)
            raise
        return self._submission(task)

    def get(self, task_id: str) -> Optional[AnalysisTask]:
        """Return the current persisted task state."""
        return self._store.get(task_id)

    def run(self, task_id: str, service: AnalysisService) -> AnalysisTask:
        """Execute one queued task and persist progress and terminal state."""
        task = self._store.get(task_id)
        if task is None:
            raise KeyError(f"Analysis task does not exist: {task_id}")
        if task.status == AnalysisTaskStatus.SUCCEEDED:
            return task

        task.status = AnalysisTaskStatus.RUNNING
        task.updated_at = datetime.now(timezone.utc)
        task.error = None
        self._store.put(task)

        def report_progress(completed_items: int, total_items: int) -> None:
            task.completed_items = completed_items
            task.total_items = total_items
            task.updated_at = datetime.now(timezone.utc)
            self._store.put(task)

        try:
            response = service.analyze(
                task_id,
                task.request,
                api_route="/api/analysis/tasks/worker",
                progress_callback=report_progress,
            )
            task.status = AnalysisTaskStatus.SUCCEEDED
            task.response = response
        except Exception as exc:
            logger.exception("analysis_task_execution_failed task_id=%s", task_id)
            task.status = AnalysisTaskStatus.FAILED
            task.error = ServiceError(source="system", message=str(exc))
        task.updated_at = datetime.now(timezone.utc)
        self._store.put(task)
        return task

    @staticmethod
    def _task_id(request: AnalysisRequest) -> str:
        """Build one same-day idempotency key without exposing prompt contents."""
        current_day = datetime.now(timezone.utc).date().isoformat()
        payload = (
            f"{ANALYSIS_TASK_VERSION}:{current_day}:{request.model_dump_json()}"
        ).encode("utf-8")
        return sha256(payload).hexdigest()

    @staticmethod
    def _submission(task: AnalysisTask) -> AnalysisTaskSubmission:
        """Build the stable polling contract for one task."""
        return AnalysisTaskSubmission(
            task_id=task.task_id,
            status=task.status,
            status_url=f"/api/analysis/tasks/{task.task_id}",
        )

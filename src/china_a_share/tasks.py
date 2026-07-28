"""Durable asynchronous analysis task coordination."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from threading import Lock
import time
from typing import Dict, Optional, Union
from uuid import uuid4

import google.auth
from google.api_core.retry import Retry
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage

from china_a_share.application.workflow import AnalysisService
from china_a_share.core.contracts import (
    AnalysisRequest,
    AnalysisTask,
    DiscoveryTask,
    DiscoveryTaskRequest,
    AnalysisTaskStatus,
    AnalysisTaskSubmission,
    ServiceError,
)
from china_a_share.core.ports import AnalysisTaskDispatcher, AnalysisTaskStore


ANALYSIS_TASK_PREFIX = "analysis-jobs"
GOOGLE_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
MIN_OBJECT_WRITE_INTERVAL_SECONDS = 1.0
STORAGE_RETRY_INITIAL_SECONDS = 1.0
STORAGE_RETRY_MAXIMUM_SECONDS = 8.0
STORAGE_RETRY_DEADLINE_SECONDS = 30.0
STORAGE_WRITE_RETRY = Retry(
    initial=STORAGE_RETRY_INITIAL_SECONDS,
    maximum=STORAGE_RETRY_MAXIMUM_SECONDS,
    multiplier=2.0,
    deadline=STORAGE_RETRY_DEADLINE_SECONDS,
)
logger = logging.getLogger(__name__)


class MemoryAnalysisTaskStore:
    """Store isolated task records in memory for local tests."""

    def __init__(self) -> None:
        self._tasks: Dict[str, Union[AnalysisTask, DiscoveryTask]] = {}
        self._lock = Lock()

    def get(self, task_id: str) -> Optional[Union[AnalysisTask, DiscoveryTask]]:
        """Return an isolated copy of one task."""
        with self._lock:
            task = self._tasks.get(task_id)
            return task.model_copy(deep=True) if task else None

    def put(self, task: Union[AnalysisTask, DiscoveryTask]) -> None:
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
        self._write_schedule: Dict[str, float] = {}
        self._write_schedule_lock = Lock()

    def get(self, task_id: str) -> Optional[Union[AnalysisTask, DiscoveryTask]]:
        """Return one persisted task when its object exists."""
        blob = self._bucket.blob(self._object_name(task_id))
        if not blob.exists():
            return None
        data = json.loads(blob.download_as_text())
        if data.get("task_type") == "discovery":
            return DiscoveryTask.model_validate(data)
        return AnalysisTask.model_validate(data)

    def put(self, task: Union[AnalysisTask, DiscoveryTask]) -> None:
        """Replace one complete task record."""
        object_name = self._object_name(task.task_id)
        self._wait_for_write_slot(object_name)
        blob = self._bucket.blob(object_name)
        blob.upload_from_string(
            task.model_dump_json(),
            content_type="application/json",
            retry=STORAGE_WRITE_RETRY,
        )

    def _wait_for_write_slot(self, object_name: str) -> None:
        """Reserve a per-object write slot without throttling unrelated tasks."""
        now = time.monotonic()
        with self._write_schedule_lock:
            write_at = max(now, self._write_schedule.get(object_name, now))
            self._write_schedule[object_name] = (
                write_at + MIN_OBJECT_WRITE_INTERVAL_SECONDS
            )
        delay = write_at - now
        if delay > 0:
            logger.info(
                "analysis_task_storage_write_throttled object_name=%s delay_seconds=%.3f",
                object_name,
                delay,
            )
            time.sleep(delay)

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
        """Persist and dispatch one new analysis task for every submission."""
        task_id = uuid4().hex
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

    def submit_discovery(self, request: DiscoveryTaskRequest) -> AnalysisTaskSubmission:
        """Persist and dispatch one new discovery task for every submission."""
        task_id = uuid4().hex
        now = datetime.now(timezone.utc)
        task = DiscoveryTask(
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
            logger.exception("discovery_task_dispatch_failed task_id=%s", task_id)
            task.status = AnalysisTaskStatus.FAILED
            task.updated_at = datetime.now(timezone.utc)
            task.error = ServiceError(source="system", message=str(exc))
            self._store.put(task)
            raise
        return AnalysisTaskSubmission(
            task_id=task.task_id,
            status=task.status,
            status_url=f"/api/discovery/tasks/{task.task_id}",
        )

    def get(self, task_id: str) -> Optional[Union[AnalysisTask, DiscoveryTask]]:
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
        last_progress_write_at = time.monotonic()

        def report_progress(completed_items: int, total_items: int) -> None:
            nonlocal last_progress_write_at
            task.completed_items = completed_items
            task.total_items = total_items
            task.updated_at = datetime.now(timezone.utc)
            now = time.monotonic()
            if (
                now - last_progress_write_at
                < MIN_OBJECT_WRITE_INTERVAL_SECONDS
            ):
                return
            self._store.put(task)
            last_progress_write_at = now

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
    def _submission(task: AnalysisTask) -> AnalysisTaskSubmission:
        """Build the stable polling contract for one task."""
        return AnalysisTaskSubmission(
            task_id=task.task_id,
            status=task.status,
            status_url=f"/api/analysis/tasks/{task.task_id}",
        )

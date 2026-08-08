from datetime import datetime, timedelta, timezone

import pytest

from china_a_share.core.contracts import (
    AnalysisRequest,
    AnalysisResponse,
    AnalysisStatus,
    AnalysisTask,
    AnalysisTaskStatus,
    DiscoveryTask,
    DiscoveryTaskRequest,
)
from china_a_share.tasks import (
    AnalysisTaskCoordinator,
    CloudStorageAnalysisTaskStore,
    CloudRunJobDispatcher,
    MemoryAnalysisTaskStore,
)


ORIGINAL_COMPLEX_PROMPT = (
    "\u4f60\u80fd\u5206\u6790\u4e0b\uff0c\u8fc7\u53bb\u4e00\u4e2a\u6708\uff0c"
    "\u533b\u7597\u884c\u4e1a\uff0c\u6309\u7167\u6563\u6237\u6bd4\u4f8b"
    "\u533a\u5206\u5206\u4e24\u534a\uff0c\u662f\u6563\u6237\u6bd4\u4f8b\u9ad8"
    "\u7684\u90a3\u4e00\u534a\u516c\u53f8\u4e0a\u6da8\u7684\u591a\u8fd8\u662f"
    "\u6563\u6237\u6bd4\u4f8b\u4f4e\u54ea\u4e00\u534a\u7684\u516c\u53f8"
    "\u4e0a\u6da8\u7684\u591a\u3002"
)
POWER_COMPLEX_PROMPT = ORIGINAL_COMPLEX_PROMPT.replace(
    "\u533b\u7597\u884c\u4e1a",
    "\u7535\u529b\u884c\u4e1a",
)
PHONE_COMPLEX_PROMPT = ORIGINAL_COMPLEX_PROMPT.replace(
    "\u533b\u7597\u884c\u4e1a",
    "\u624b\u673a\u80a1\u7968",
)
FULL_MARKET_COMPLEX_PROMPT = (
    "\u8fc7\u53bb\u4e00\u4e2a\u6708\u7684\u80a1\u7968\uff0c"
    "\u6309\u7167\u6563\u6237\u6bd4\u4f8b\u533a\u5206\u5206\u4e24\u534a\uff0c"
    "\u662f\u6563\u6237\u6bd4\u4f8b\u9ad8\u7684\u90a3\u4e00\u534a\u516c\u53f8"
    "\u4e0a\u6da8\u7684\u591a\u8fd8\u662f\u6563\u6237\u6bd4\u4f8b\u4f4e"
    "\u90a3\u4e00\u534a\u7684\u516c\u53f8\u4e0a\u6da8\u7684\u591a\u3002"
)


class FakeDispatcher:
    def __init__(self):
        self.task_ids = []

    def dispatch(self, task_id):
        self.task_ids.append(task_id)


class FakeAnalysisService:
    def analyze(
        self,
        request_id,
        request,
        *,
        api_route,
        progress_callback=None,
    ):
        progress_callback(2, 4)
        progress_callback(4, 4)
        return AnalysisResponse(
            request_id=request_id,
            planner="test-planner",
            data_provider="test-provider",
            status=AnalysisStatus.SUCCESS,
        )


class CountingMemoryAnalysisTaskStore(MemoryAnalysisTaskStore):
    def __init__(self):
        super().__init__()
        self.put_count = 0

    def put(self, task):
        self.put_count += 1
        super().put(task)


class FakeHttpResponse:
    status_code = 200
    text = "{}"


class FakeAuthorizedSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeHttpResponse()


class FakeStorageBlob:
    def __init__(self):
        self.uploads = []

    def upload_from_string(self, payload, *, content_type, retry):
        self.uploads.append((payload, content_type, retry))


class FakeStorageBucket:
    def __init__(self):
        self.blobs = {}

    def blob(self, object_name):
        return self.blobs.setdefault(object_name, FakeStorageBlob())


class FakeStorageClient:
    def __init__(self):
        self.bucket_instance = FakeStorageBucket()

    def bucket(self, bucket_name):
        return self.bucket_instance


def test_task_coordinator_dispatches_each_submission_as_a_new_task():
    store = MemoryAnalysisTaskStore()
    dispatcher = FakeDispatcher()
    coordinator = AnalysisTaskCoordinator(store, dispatcher)
    request = AnalysisRequest(prompt=ORIGINAL_COMPLEX_PROMPT)

    first = coordinator.submit(request)
    second = coordinator.submit(request)

    assert first.task_id != second.task_id
    assert dispatcher.task_ids == [first.task_id, second.task_id]

    first_completed = coordinator.run(first.task_id, FakeAnalysisService())
    second_completed = coordinator.run(second.task_id, FakeAnalysisService())

    assert first_completed.status == AnalysisTaskStatus.SUCCEEDED
    assert first_completed.completed_items == 4
    assert first_completed.total_items == 4
    assert first_completed.response is not None
    assert second_completed.status == AnalysisTaskStatus.SUCCEEDED
    assert second_completed.response is not None
    assert coordinator.get(first.task_id).status == AnalysisTaskStatus.SUCCEEDED
    assert coordinator.get(second.task_id).status == AnalysisTaskStatus.SUCCEEDED


def test_task_coordinator_coalesces_rapid_progress_updates(monkeypatch):
    store = CountingMemoryAnalysisTaskStore()
    coordinator = AnalysisTaskCoordinator(store, FakeDispatcher())
    submission = coordinator.submit(AnalysisRequest(prompt=ORIGINAL_COMPLEX_PROMPT))
    monkeypatch.setattr("china_a_share.tasks.time.monotonic", lambda: 10.0)

    completed = coordinator.run(submission.task_id, FakeAnalysisService())

    assert store.put_count == 3
    assert completed.status == AnalysisTaskStatus.SUCCEEDED
    assert completed.completed_items == 4
    assert completed.total_items == 4


def test_cloud_run_dispatcher_sends_only_task_id_override():
    session = FakeAuthorizedSession()
    dispatcher = CloudRunJobDispatcher(
        "project",
        "region",
        "worker",
        session=session,
    )

    dispatcher.dispatch("task-123")

    url, kwargs = session.calls[0]
    assert url.endswith("/projects/project/locations/region/jobs/worker:run")
    assert kwargs["timeout"] == 30
    assert kwargs["json"] == {
        "overrides": {
            "containerOverrides": [
                {
                    "env": [
                        {
                            "name": "ANALYSIS_TASK_ID",
                            "value": "task-123",
                        }
                    ]
                }
            ]
        }
    }


def test_memory_store_returns_isolated_task_copies():
    store = MemoryAnalysisTaskStore()
    now = datetime.now(timezone.utc)
    task = AnalysisTask(
        task_id="task",
        status=AnalysisTaskStatus.QUEUED,
        request=AnalysisRequest(prompt="Prompt"),
        created_at=now,
        updated_at=now,
    )
    store.put(task)

    loaded = store.get("task")
    loaded.status = AnalysisTaskStatus.FAILED

    assert store.get("task").status == AnalysisTaskStatus.QUEUED


def test_task_coordinator_marks_a_stale_discovery_worker_as_failed():
    store = MemoryAnalysisTaskStore()
    coordinator = AnalysisTaskCoordinator(store, FakeDispatcher())
    now = datetime.now(timezone.utc)
    task = DiscoveryTask(
        task_id="stale-discovery",
        status=AnalysisTaskStatus.RUNNING,
        request=DiscoveryTaskRequest(
            target_pool="A_SHARE",
            train_start="20240101",
            train_end="20241231",
            val_start="20250101",
            val_end="20250630",
            factors=["pe_ttm"],
        ),
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )
    store.put(task)

    failed = coordinator.get(task.task_id)

    assert failed.status == AnalysisTaskStatus.FAILED
    assert failed.progress.current_stage == "failed"
    assert "platform resource limit" in failed.error.message
    assert store.get(task.task_id).status == AnalysisTaskStatus.FAILED


def test_task_coordinator_keeps_a_recent_discovery_worker_running():
    store = MemoryAnalysisTaskStore()
    coordinator = AnalysisTaskCoordinator(store, FakeDispatcher())
    now = datetime.now(timezone.utc)
    task = DiscoveryTask(
        task_id="active-discovery",
        status=AnalysisTaskStatus.RUNNING,
        request=DiscoveryTaskRequest(
            target_pool="A_SHARE",
            train_start="20240101",
            train_end="20241231",
            val_start="20250101",
            val_end="20250630",
            factors=["pe_ttm"],
        ),
        created_at=now,
        updated_at=now,
    )
    store.put(task)

    active = coordinator.get(task.task_id)

    assert active.status == AnalysisTaskStatus.RUNNING
    assert active.error is None


def test_cloud_storage_store_spaces_writes_to_the_same_object(monkeypatch):
    client = FakeStorageClient()
    store = CloudStorageAnalysisTaskStore("bucket", storage_client=client)
    clock = iter([10.0, 10.2])
    sleeps = []
    monkeypatch.setattr("china_a_share.tasks.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("china_a_share.tasks.time.sleep", sleeps.append)
    now = datetime.now(timezone.utc)
    task = AnalysisTask(
        task_id="task",
        status=AnalysisTaskStatus.QUEUED,
        request=AnalysisRequest(prompt="Prompt"),
        created_at=now,
        updated_at=now,
    )

    store.put(task)
    store.put(task)

    blob = client.bucket_instance.blobs["analysis-jobs/task.json"]
    assert len(blob.uploads) == 2
    assert sleeps == [pytest.approx(0.8)]
    assert all(upload[1] == "application/json" for upload in blob.uploads)
    assert all(upload[2] is not None for upload in blob.uploads)


def test_cloud_storage_store_does_not_throttle_different_objects(monkeypatch):
    client = FakeStorageClient()
    store = CloudStorageAnalysisTaskStore("bucket", storage_client=client)
    monkeypatch.setattr("china_a_share.tasks.time.monotonic", lambda: 10.0)
    sleeps = []
    monkeypatch.setattr("china_a_share.tasks.time.sleep", sleeps.append)
    now = datetime.now(timezone.utc)

    for task_id in ("first", "second"):
        store.put(
            AnalysisTask(
                task_id=task_id,
                status=AnalysisTaskStatus.QUEUED,
                request=AnalysisRequest(prompt="Prompt"),
                created_at=now,
                updated_at=now,
            )
        )

    assert sleeps == []

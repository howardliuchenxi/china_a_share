from datetime import datetime, timezone

import pytest

from china_a_share.core.contracts import (
    AnalysisRequest,
    AnalysisResponse,
    AnalysisStatus,
    AnalysisTask,
    AnalysisTaskStatus,
)
from china_a_share.tasks import (
    AnalysisTaskCoordinator,
    CloudRunJobDispatcher,
    MemoryAnalysisTaskStore,
    requires_async_analysis,
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


class FakeHttpResponse:
    status_code = 200
    text = "{}"


class FakeAuthorizedSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeHttpResponse()


def test_original_complex_prompt_requires_async_analysis():
    assert requires_async_analysis(AnalysisRequest(prompt=ORIGINAL_COMPLEX_PROMPT))
    assert requires_async_analysis(AnalysisRequest(prompt=POWER_COMPLEX_PROMPT))
    assert requires_async_analysis(AnalysisRequest(prompt=PHONE_COMPLEX_PROMPT))
    assert requires_async_analysis(AnalysisRequest(prompt=FULL_MARKET_COMPLEX_PROMPT))
    assert not requires_async_analysis(AnalysisRequest(prompt="List A-share stocks."))


@pytest.mark.parametrize(
    "prompt",
    [
        "查找散户比例最高的10只A股股票",
        "筛选全市场散户比例前10%分位的股票",
        "大A在6月散户最多的股票前十",
    ],
)
def test_full_market_retail_rankings_require_async_analysis(prompt):
    assert requires_async_analysis(AnalysisRequest(prompt=prompt))


def test_task_coordinator_reuses_same_day_submission_and_runs_to_completion():
    store = MemoryAnalysisTaskStore()
    dispatcher = FakeDispatcher()
    coordinator = AnalysisTaskCoordinator(store, dispatcher)
    request = AnalysisRequest(prompt=ORIGINAL_COMPLEX_PROMPT)

    first = coordinator.submit(request)
    second = coordinator.submit(request)

    assert first.task_id == second.task_id
    assert dispatcher.task_ids == [first.task_id]

    completed = coordinator.run(first.task_id, FakeAnalysisService())

    assert completed.status == AnalysisTaskStatus.SUCCEEDED
    assert completed.completed_items == 4
    assert completed.total_items == 4
    assert completed.response is not None
    assert coordinator.get(first.task_id).status == AnalysisTaskStatus.SUCCEEDED


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

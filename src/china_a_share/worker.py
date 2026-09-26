"""Cloud Run Job entry point for one persisted analysis task."""

from datetime import datetime, timezone
import os

from china_a_share.bootstrap import (
    create_analysis_service,
    create_evolution_loop,
    create_feishu_agent_runtime,
)
from china_a_share.config import ConfigurationError, Settings
from china_a_share.llm_preference import read_llm_preference
from china_a_share.tasks import (
    AnalysisTaskCoordinator,
    CloudStorageAnalysisTaskStore,
)
from china_a_share.core.contracts import AnalysisTaskStatus, DiscoveryTask, ServiceError
from china_a_share.feishu import FeishuOpenApiClient
from china_a_share.feishu_agent import (
    FeishuAgentCoordinator,
    FeishuAgentTask,
    has_retryable_terminal_deliveries,
)


class WorkerDispatcher:
    """Reject nested dispatch from the worker process."""

    def dispatch(self, task_id: str) -> None:
        """Prevent a worker from recursively starting another job."""
        raise RuntimeError(f"Worker cannot dispatch nested task: {task_id}")


def main() -> None:
    """Load and execute the task selected by the job environment."""
    task_id = os.getenv("ANALYSIS_TASK_ID", "").strip()
    if not task_id:
        raise ConfigurationError("ANALYSIS_TASK_ID is required for the worker.")
    settings = Settings.from_env()
    store = CloudStorageAnalysisTaskStore(settings.tushare_cache_bucket)
    
    task = store.get(task_id)
    if task is None:
        raise RuntimeError(f"Task {task_id} not found in store.")
        
    if isinstance(task, FeishuAgentTask):
        coordinator = FeishuAgentCoordinator(
            store,
            WorkerDispatcher(),
            public_app_url=settings.public_app_url,
        )
        try:
            runtime = create_feishu_agent_runtime(
                settings,
                llm_preference=read_llm_preference(settings),
            )
            progress_sink = FeishuOpenApiClient(
                settings.feishu_app_id,
                settings.feishu_app_secret,
            )
        except Exception as exc:
            # Initialization failures occur before the coordinator can transition the
            # task, so the worker must persist a terminal state instead of leaving a
            # permanently queued task with no actionable error.
            task.status = AnalysisTaskStatus.FAILED
            task.stage = "failed"
            task.progress_message = "研究任务初始化失败。"
            task.error = ServiceError(source="system", message=str(exc))
            task.updated_at = datetime.now(timezone.utc)
            store.put(task)
            raise
        completed = coordinator.run(task_id, runtime, progress_sink)
        if has_retryable_terminal_deliveries(completed):
            # The result is already durable and will not be recomputed. Failing
            # this execution asks Cloud Run Job's bounded retry to drain only the
            # persisted terminal delivery outbox.
            raise RuntimeError(
                f"Feishu terminal delivery remains pending for task {task_id}."
            )
    elif isinstance(task, DiscoveryTask):
        loop = create_evolution_loop(settings, store)
        loop.run(task_id)
    else:
        coordinator = AnalysisTaskCoordinator(
            store,
            WorkerDispatcher(),
        )
        coordinator.run(task_id, create_analysis_service(settings))


if __name__ == "__main__":
    main()

"""Cloud Run Job entry point for one persisted analysis task."""

import os

from china_a_share.bootstrap import create_analysis_service, create_evolution_loop
from china_a_share.config import ConfigurationError, Settings
from china_a_share.tasks import (
    AnalysisTaskCoordinator,
    CloudStorageAnalysisTaskStore,
)
from china_a_share.core.contracts import DiscoveryTask


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
        
    if isinstance(task, DiscoveryTask):
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

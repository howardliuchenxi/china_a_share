"""Orchestration for deterministic A-share rule discovery."""

from datetime import datetime, timezone
import logging

from china_a_share.core.contracts import (
    AnalysisTaskStatus,
    DiscoveryTask,
    ServiceError,
)
from china_a_share.core.ports import AnalysisTaskStore
from china_a_share.discovery.backtester import FactorBacktester
from china_a_share.discovery.search import RuleSearchEngine


LEADERBOARD_SIZE = 10
logger = logging.getLogger(__name__)


class EvolutionLoop:
    """Build research windows, search bounded rules, and persist evidence."""

    def __init__(
        self,
        store: AnalysisTaskStore,
        backtester: FactorBacktester,
    ) -> None:
        self._store = store
        self._backtester = backtester

    def run(self, task_id: str) -> None:
        """Run one discovery task and persist every material state transition."""
        task = self._store.get(task_id)
        if not isinstance(task, DiscoveryTask):
            raise TypeError(f"Task is not a discovery task: {task_id}")
        task.status = AnalysisTaskStatus.RUNNING
        task.error = None
        self._update_progress(task, "dataset", "正在构建训练与验证研究样本…")
        try:
            self._execute(task)
            task.status = AnalysisTaskStatus.SUCCEEDED
            self._update_progress(task, "completed", "规律搜索与独立验证已完成。")
        except Exception as exc:
            logger.exception("discovery_task_failed task_id=%s", task_id)
            task.status = AnalysisTaskStatus.FAILED
            task.error = ServiceError(source="system", message=str(exc))
            self._update_progress(task, "failed", "规律搜索失败。")

    def _execute(self, task: DiscoveryTask) -> None:
        request = task.request
        train = self._backtester.build_dataset(
            request.train_start,
            request.train_end,
            forward_days=request.forward_days,
            request_id=task.task_id,
        )
        validation = self._backtester.build_dataset(
            request.val_start,
            request.val_end,
            forward_days=request.forward_days,
            request_id=task.task_id,
        )
        task.progress.training_sample_count = len(train)
        task.progress.validation_sample_count = len(validation)
        task.progress.current_generation = 1
        task.progress.total_generations = 1
        self._update_progress(
            task,
            "search",
            f"正在搜索 {len(request.factors)} 个因子的分位数规则…",
        )
        search = RuleSearchEngine(
            min_sample_count=request.minimum_samples,
            target_return=request.target_return_pct / 100.0,
        )
        leaderboard, evaluated_count = search.search(
            train,
            validation,
            request.factors,
            max_conditions=request.max_conditions,
            top_n=LEADERBOARD_SIZE,
        )
        task.progress.formulas_tested = len(leaderboard)
        task.progress.candidates_evaluated = evaluated_count
        task.progress.leaderboard = leaderboard
        if not leaderboard:
            raise ValueError(
                "No rule met the minimum sample requirement in both windows."
            )

    def _update_progress(
        self,
        task: DiscoveryTask,
        stage: str,
        message: str,
    ) -> None:
        task.progress.current_stage = stage
        task.progress.current_log = message
        task.updated_at = datetime.now(timezone.utc)
        self._store.put(task)

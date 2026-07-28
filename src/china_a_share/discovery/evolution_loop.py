import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List

from china_a_share.core.contracts import (
    BacktestResult,
    DiscoveryTask,
    DiscoveryTaskProgress,
    DiscoveryTaskRequest,
    FactorHypothesis,
    AnalysisTaskStatus,
    ServiceError,
)
from china_a_share.discovery.backtester import FactorBacktester
from china_a_share.core.ports import AnalysisTaskStore, QueryPlanner

logger = logging.getLogger(__name__)

class EvolutionLoop:
    """Agentic loop to discover and validate alpha factors."""

    def __init__(
        self,
        store: AnalysisTaskStore,
        planner: QueryPlanner,
        backtester: FactorBacktester,
    ):
        self._store = store
        self._planner = planner
        self._backtester = backtester

    def run(self, task_id: str) -> None:
        """Run the full evolutionary loop for a given task."""
        task = self._store.get(task_id)
        if not isinstance(task, DiscoveryTask):
            logger.error("Task %s is not a DiscoveryTask", task_id)
            return

        task.status = AnalysisTaskStatus.RUNNING
        task.updated_at = datetime.now(timezone.utc)
        self._store.put(task)

        try:
            self._execute_loop(task)
            task.status = AnalysisTaskStatus.SUCCEEDED
        except Exception as exc:
            logger.exception("evolution_loop_failed task_id=%s", task_id)
            task.status = AnalysisTaskStatus.FAILED
            task.error = ServiceError(source="system", message=str(exc))
        finally:
            task.updated_at = datetime.now(timezone.utc)
            self._store.put(task)

    def _update_progress(self, task: DiscoveryTask, log: str) -> None:
        task.progress.current_log = log
        task.updated_at = datetime.now(timezone.utc)
        self._store.put(task)

    def _execute_loop(self, task: DiscoveryTask) -> None:
        req = task.request
        task.progress.total_generations = req.max_generations
        
        prompt_template = (
            "You are a quantitative researcher. Generate a JSON list of 3 stock screening formulas "
            "based on the following factors: {factors}. "
            "The goal is: {prompt}. "
            "Return ONLY a valid JSON array of objects with keys: "
            "'formula' (a valid pandas query string using the factors), "
            "'description' (human readable explanation), "
            "'reasoning' (why it should work). "
            "Example formula: `pe_ttm < 15 and turnover_rate > 3`."
        )

        feedback = ""

        for gen in range(1, req.max_generations + 1):
            task.progress.current_generation = gen
            self._update_progress(task, f"正在生成第 {gen} 代因子公式...")

            llm_prompt = prompt_template.format(
                factors=", ".join(req.factors) if req.factors else "any basic financial metrics",
                prompt=req.prompt or "Find high win-rate strategies"
            )
            if feedback:
                llm_prompt += f"\nFeedback from previous generation:\n{feedback}\nPlease improve the formulas."

            llm_response = None
            try:
                llm_response = self._planner.generate_text(llm_prompt)
                
                # Robust JSON extraction
                import re
                match = re.search(r'\[.*\]', llm_response, re.DOTALL)
                if match:
                    json_str = match.group(0)
                else:
                    json_str = llm_response
                    
                formulas_data = json.loads(json_str)
                logger.info(f"Parsed formulas: {len(formulas_data)} items")
                hypotheses = [FactorHypothesis(**f) for f in formulas_data]
            except Exception as e:
                logger.error(f"LLM parsing failed: {e}. Raw response: {llm_response}")
                self._update_progress(task, f"第 {gen} 代生成失败，自动重试: {e}")
                continue
                
            feedback_parts = []
            
            for index, hyp in enumerate(hypotheses, start=1):
                self._update_progress(task, f"正在回测第 {gen} 代公式 {index}/{len(hypotheses)}: {hyp.formula}")
                try:
                    result = self._backtester.run_backtest(
                        hyp.formula,
                        req.train_start,
                        req.train_end,
                        request_id=task.task_id,
                    )
                    hyp.train_result = result
                    task.progress.formulas_tested += 1
                    
                    feedback_parts.append(
                        f"Formula `{hyp.formula}` yielded win rate {result.win_rate*100:.1f}%, "
                        f"mean return {result.mean_return*100:.2f}%."
                    )
                    
                    # Optional: Blind test if train result is good
                    if result.win_rate > 0.52:
                        val_result = self._backtester.run_backtest(
                            hyp.formula,
                            req.val_start,
                            req.val_end,
                            request_id=task.task_id,
                        )
                        hyp.val_result = val_result
                        if val_result.win_rate > 0.50:
                            task.progress.leaderboard.append(hyp)
                            
                except Exception as e:
                    logger.warning(f"Backtest failed for {hyp.formula}: {e}")
                    feedback_parts.append(f"Formula `{hyp.formula}` failed: {e}")
                    
            feedback = "\n".join(feedback_parts)
            
            # Sort leaderboard
            task.progress.leaderboard.sort(
                key=lambda x: x.val_result.win_rate if x.val_result else 0.0,
                reverse=True
            )
            # Keep top 10
            task.progress.leaderboard = task.progress.leaderboard[:10]
            
        self._update_progress(task, "挖掘结束。")

from datetime import datetime, timezone

import pandas as pd
import pytest

from china_a_share.core.contracts import (
    AnalysisTaskStatus,
    DiscoveryTask,
    DiscoveryTaskRequest,
    QueryResult,
    QueryStatus,
)
from china_a_share.discovery.backtester import FactorBacktester
from china_a_share.discovery.evolution_loop import EvolutionLoop
from china_a_share.discovery.search import RuleSearchEngine
from china_a_share.tasks import MemoryAnalysisTaskStore


class FakeQueryExecutor:
    """Return deterministic market snapshots for discovery tests."""

    def __init__(self, trade_dates, basics, prices):
        self._trade_dates = trade_dates
        self._basics = basics
        self._prices = prices

    def execute(self, query, *, api_route, request_id):
        if query.operation == "trade_cal":
            rows = [
                {"cal_date": trade_date, "is_open": "1"}
                for trade_date in self._trade_dates
                if query.params["start_date"] <= trade_date <= query.params["end_date"]
            ]
        elif query.operation == "daily_basic":
            rows = self._basics.get(query.params["trade_date"], [])
        elif query.operation == "daily":
            rows = self._prices.get(query.params["trade_date"], [])
        else:
            raise AssertionError(f"Unexpected operation: {query.operation}")
        return QueryResult(
            query_id=query.query_id,
            provider="fake",
            operation=query.operation,
            status=QueryStatus.SUCCESS,
            columns=list(rows[0]) if rows else [],
            rows=rows,
            row_count=len(rows),
        )


def test_research_dataset_aligns_features_with_future_trading_session_returns():
    executor = FakeQueryExecutor(
        ["20260105", "20260106", "20260107"],
        {
            date: [{"ts_code": "000001.SZ", "pe_ttm": value}]
            for date, value in [
                ("20260105", 10.0),
                ("20260106", 11.0),
                ("20260107", 12.0),
            ]
        },
        {
            date: [{"ts_code": "000001.SZ", "close": close}]
            for date, close in [
                ("20260105", 10.0),
                ("20260106", 11.0),
                ("20260107", 12.1),
            ]
        },
    )

    dataset = FactorBacktester(executor).build_dataset(
        "20260105",
        "20260106",
        forward_days=1,
    )

    assert dataset["trade_date"].tolist() == ["20260105", "20260106"]
    assert dataset["forward_return"].tolist() == pytest.approx([0.10, 0.10])
    assert dataset["pe_ttm"].tolist() == [10.0, 11.0]


def test_research_dataset_preserves_missing_factor_values():
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            "20260105": [{"ts_code": "000001.SZ", "pe_ttm": None}],
            "20260106": [{"ts_code": "000001.SZ", "pe_ttm": 12.0}],
        },
        {
            "20260105": [{"ts_code": "000001.SZ", "close": 10.0}],
            "20260106": [{"ts_code": "000001.SZ", "close": 11.0}],
        },
    )

    dataset = FactorBacktester(executor).build_dataset(
        "20260105",
        "20260105",
        forward_days=1,
    )

    assert pd.isna(dataset.iloc[0]["pe_ttm"])


def test_research_dataset_does_not_skip_a_missing_target_session():
    executor = FakeQueryExecutor(
        ["20260105", "20260106", "20260107"],
        {
            date: [{"ts_code": "000001.SZ", "pe_ttm": 10.0}]
            for date in ["20260105", "20260106", "20260107"]
        },
        {
            "20260105": [{"ts_code": "000001.SZ", "close": 10.0}],
            "20260106": [],
            "20260107": [{"ts_code": "000001.SZ", "close": 12.0}],
        },
    )

    dataset = FactorBacktester(executor).build_dataset(
        "20260105",
        "20260105",
        forward_days=1,
    )

    assert dataset.empty


def test_rule_evaluation_reports_exact_event_statistics_and_real_drawdown():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105", "20260106", "20260107", "20260108"],
            "factor": [1.0, 2.0, 3.0, 4.0],
            "forward_return": [0.10, -0.20, 0.30, 0.10],
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor >= 2")

    assert result.sample_count == 3
    assert result.positive_count == 2
    assert result.win_rate == pytest.approx(2 / 3)
    assert result.mean_return == pytest.approx(0.0666666667)
    assert result.median_return == pytest.approx(0.10)
    assert result.max_drawdown == pytest.approx(-0.20)
    assert result.baseline_win_rate == pytest.approx(0.75)


def test_rule_search_finds_explainable_single_and_double_factor_candidates():
    train = pd.DataFrame(
        {
            "trade_date": [f"202601{i:02d}" for i in range(1, 21)],
            "value": list(range(20)),
            "quality": [1 if index % 2 else 0 for index in range(20)],
            "forward_return": [-0.05] * 10 + [0.10] * 10,
        }
    )
    validation = train.copy()

    candidates = RuleSearchEngine(min_sample_count=4).search(
        train,
        validation,
        ["value", "quality"],
        max_conditions=2,
        top_n=10,
    )

    assert candidates
    assert any("value" in candidate.formula for candidate in candidates)
    assert all(candidate.train_result.sample_count >= 4 for candidate in candidates)
    assert all(candidate.val_result is not None for candidate in candidates)
    assert candidates[0].val_result.mean_return >= candidates[-1].val_result.mean_return


def test_discovery_request_rejects_overlapping_training_and_validation_windows():
    with pytest.raises(ValueError, match="Validation window must start after"):
        DiscoveryTaskRequest(
            target_pool="A_SHARE",
            train_start="20260101",
            train_end="20260601",
            val_start="20260501",
            val_end="20261201",
            factors=["pe_ttm"],
        )


def test_discovery_request_rejects_factors_outside_the_research_dataset():
    with pytest.raises(ValueError, match="Unsupported discovery factors: revenue"):
        DiscoveryTaskRequest(
            target_pool="A_SHARE",
            train_start="20250101",
            train_end="20251231",
            val_start="20260101",
            val_end="20260630",
            factors=["pe_ttm", "revenue"],
        )


def test_memory_store_preserves_extended_discovery_request():
    store = MemoryAnalysisTaskStore()
    now = datetime.now(timezone.utc)
    task = DiscoveryTask(
        task_id="discovery-task",
        status=AnalysisTaskStatus.QUEUED,
        request=DiscoveryTaskRequest(
            target_pool="A_SHARE",
            train_start="20250101",
            train_end="20251231",
            val_start="20260101",
            val_end="20260630",
            factors=["pe_ttm"],
            forward_days=20,
            minimum_samples=50,
            max_conditions=2,
        ),
        created_at=now,
        updated_at=now,
    )

    store.put(task)

    loaded = store.get(task.task_id)
    assert loaded.request.forward_days == 20
    assert loaded.request.minimum_samples == 50
    assert loaded.request.max_conditions == 2


def test_evolution_loop_builds_each_window_once_and_persists_ranked_rules():
    class FakeBacktester:
        def __init__(self):
            self.calls = []

        def build_dataset(self, start_date, end_date, **kwargs):
            self.calls.append((start_date, end_date, kwargs["forward_days"]))
            return pd.DataFrame(
                {
                    "trade_date": [f"202601{i:02d}" for i in range(1, 26)],
                    "pe_ttm": list(range(25)),
                    "forward_return": [-0.05] * 12 + [0.10] * 13,
                }
            )

    store = MemoryAnalysisTaskStore()
    now = datetime.now(timezone.utc)
    task = DiscoveryTask(
        task_id="discovery-loop",
        status=AnalysisTaskStatus.QUEUED,
        request=DiscoveryTaskRequest(
            target_pool="A_SHARE",
            train_start="20250101",
            train_end="20251231",
            val_start="20260101",
            val_end="20260630",
            factors=["pe_ttm"],
            forward_days=5,
            minimum_samples=5,
        ),
        created_at=now,
        updated_at=now,
    )
    store.put(task)
    backtester = FakeBacktester()

    EvolutionLoop(store, backtester).run(task.task_id)

    completed = store.get(task.task_id)
    assert completed.status == AnalysisTaskStatus.SUCCEEDED
    assert completed.progress.current_stage == "completed"
    assert completed.progress.formulas_tested > 0
    assert completed.progress.leaderboard
    assert backtester.calls == [
        ("20250101", "20251231", 5),
        ("20260101", "20260630", 5),
    ]

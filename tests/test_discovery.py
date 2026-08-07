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


def test_rule_evaluation_uses_the_configured_target_return_threshold():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105", "20260106", "20260107"],
            "factor": [1.0, 2.0, 3.0],
            "forward_return": [0.04, 0.06, 0.10],
        }
    )

    result = FactorBacktester.evaluate_rule(
        dataset,
        "factor >= 1",
        target_return=0.05,
    )

    assert result.target_return == pytest.approx(0.05)
    assert result.positive_count == 2
    assert result.win_rate == pytest.approx(2 / 3)
    assert result.baseline_win_rate == pytest.approx(2 / 3)


def test_rule_evaluation_clusters_uncertainty_by_trading_day():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105"] * 50 + ["20260106"] * 50,
            "factor": [1.0] * 100,
            "forward_return": [0.10] * 50 + [-0.10] * 50,
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")

    assert result.sample_count == 100
    assert result.trading_day_count == 2
    assert result.win_rate == pytest.approx(0.5)
    assert result.cluster_standard_error == pytest.approx(0.5)
    assert result.confidence_lower == 0.0
    assert result.confidence_upper == 1.0


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

    candidates, evaluated_count = RuleSearchEngine(min_sample_count=4).search(
        train,
        validation,
        ["value", "quality"],
        max_conditions=2,
        top_n=10,
    )

    assert candidates
    assert evaluated_count >= len(candidates)
    assert any("value" in candidate.formula for candidate in candidates)
    assert all(candidate.train_result.sample_count >= 4 for candidate in candidates)
    assert all(candidate.val_result is not None for candidate in candidates)
    assert candidates[0].validation_score >= candidates[-1].validation_score
    assert all(candidate.generalization_gap >= 0 for candidate in candidates)


def test_rule_search_corrects_validation_significance_for_multiple_candidates():
    train = pd.DataFrame(
        {
            "trade_date": [f"202601{i:02d}" for i in range(1, 41)],
            "strong": list(range(40)),
            "noise": [index % 5 for index in range(40)],
            "forward_return": [-0.05] * 32 + [0.20] * 8,
        }
    )

    candidates, evaluated_count = RuleSearchEngine(min_sample_count=5).search(
        train,
        train.copy(),
        ["strong", "noise"],
        max_conditions=2,
        top_n=10,
    )

    assert evaluated_count > 1
    assert candidates
    assert all(0.0 <= candidate.p_value <= 1.0 for candidate in candidates)
    assert all(candidate.p_value <= candidate.q_value <= 1.0 for candidate in candidates)
    assert candidates[0].q_value <= candidates[-1].q_value


def test_clustered_significance_remains_finite_for_large_samples():
    probability = RuleSearchEngine._clustered_tail_probability(0.56, 0.5, 0.01)

    assert 0.0 <= probability <= 1.0
    assert probability < 0.001


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


def test_discovery_request_accepts_a_percentage_point_return_target():
    request = DiscoveryTaskRequest(
        target_pool="A_SHARE",
        train_start="20250101",
        train_end="20251231",
        val_start="20260101",
        val_end="20260630",
        factors=["pe_ttm"],
        target_return_pct=5.0,
    )

    assert request.target_return_pct == 5.0


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
            is_training = start_date == "20250101"
            future_dates = (
                [f"202512{i:02d}" for i in range(1, 26)]
                + [f"202601{i:02d}" for i in range(1, 6)]
                if is_training
                else [f"202602{i:02d}" for i in range(1, 31)]
            )
            return pd.DataFrame(
                {
                    "trade_date": [f"202511{i:02d}" for i in range(1, 31)],
                    "future_trade_date": future_dates,
                    "pe_ttm": list(range(30)),
                    "forward_return": [-0.05] * 15 + [0.10] * 15,
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
    assert completed.progress.candidates_evaluated >= completed.progress.formulas_tested
    assert completed.progress.training_sample_count == 25
    assert completed.progress.training_samples_purged == 5
    assert completed.progress.leaderboard
    assert backtester.calls == [
        ("20250101", "20251231", 5),
        ("20260101", "20260630", 5),
    ]

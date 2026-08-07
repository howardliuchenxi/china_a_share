from datetime import datetime, timezone

import pandas as pd
import pytest

from china_a_share.core.contracts import (
    AnalysisTaskStatus,
    BacktestResult,
    DiscoveryTask,
    DiscoveryTaskRequest,
    FactorHypothesis,
    QueryResult,
    QueryStatus,
)
from china_a_share.discovery.backtester import FactorBacktester
from china_a_share.discovery.evolution_loop import EvolutionLoop
from china_a_share.discovery.search import PAIRING_CANDIDATE_LIMIT, RuleSearchEngine
from china_a_share.tasks import MemoryAnalysisTaskStore


class FakeQueryExecutor:
    """Return deterministic market snapshots for discovery tests."""

    def __init__(self, trade_dates, basics, prices, adjustments=None):
        self._trade_dates = trade_dates
        self._basics = basics
        self._prices = prices
        self._adjustments = adjustments

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
        elif query.operation == "adj_factor":
            trade_date = query.params["trade_date"]
            if self._adjustments is None:
                rows = [
                    {"ts_code": row["ts_code"], "adj_factor": 1.0}
                    for row in self._prices.get(trade_date, [])
                ]
            else:
                rows = self._adjustments.get(trade_date, [])
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


def test_research_dataset_prefers_daily_market_fields_over_daily_basic_duplicates():
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            date: [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": date,
                    "close": 999.0,
                    "pe_ttm": 10.0,
                }
            ]
            for date in ["20260105", "20260106"]
        },
        {
            "20260105": [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260105",
                    "close": 10.0,
                }
            ],
            "20260106": [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260106",
                    "close": 11.0,
                }
            ],
        },
    )

    dataset = FactorBacktester(executor).build_dataset(
        "20260105",
        "20260105",
        forward_days=1,
    )

    assert dataset["close"].tolist() == [10.0]
    assert dataset["forward_return"].tolist() == pytest.approx([0.10])


def test_research_dataset_uses_adjusted_closes_for_corporate_actions():
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            date: [{"ts_code": "000001.SZ", "pe_ttm": 10.0}]
            for date in ["20260105", "20260106"]
        },
        {
            "20260105": [{"ts_code": "000001.SZ", "close": 10.0}],
            "20260106": [{"ts_code": "000001.SZ", "close": 5.0}],
        },
        {
            "20260105": [{"ts_code": "000001.SZ", "adj_factor": 1.0}],
            "20260106": [{"ts_code": "000001.SZ", "adj_factor": 2.0}],
        },
    )

    dataset = FactorBacktester(executor).build_dataset(
        "20260105",
        "20260105",
        forward_days=1,
    )

    assert dataset["forward_return"].tolist() == pytest.approx([0.0])
    assert dataset["adjusted_close"].tolist() == pytest.approx([10.0])


def test_research_dataset_retains_signals_with_missing_future_prices():
    securities = ["000001.SZ", "000002.SZ"]
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            "20260105": [
                {"ts_code": code, "pe_ttm": 10.0}
                for code in securities
            ],
            "20260106": [{"ts_code": "000001.SZ", "pe_ttm": 10.0}],
        },
        {
            "20260105": [
                {"ts_code": code, "close": 10.0}
                for code in securities
            ],
            "20260106": [{"ts_code": "000001.SZ", "close": 11.0}],
        },
    )

    dataset = FactorBacktester(executor).build_dataset(
        "20260105",
        "20260105",
        forward_days=1,
    )

    assert len(dataset) == 2
    assert dataset["forward_return"].notna().sum() == 1
    assert pd.isna(
        dataset.loc[dataset["ts_code"] == "000002.SZ", "forward_return"].iloc[0]
    )


def test_research_dataset_uses_future_prices_without_future_basic_rows():
    securities = ["000001.SZ", "000002.SZ"]
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            "20260105": [
                {"ts_code": code, "pe_ttm": 10.0}
                for code in securities
            ],
            "20260106": [{"ts_code": "000001.SZ", "pe_ttm": 10.0}],
        },
        {
            "20260105": [
                {"ts_code": code, "close": 10.0}
                for code in securities
            ],
            "20260106": [
                {"ts_code": code, "close": 11.0}
                for code in securities
            ],
        },
    )

    dataset = FactorBacktester(executor).build_dataset(
        "20260105",
        "20260105",
        forward_days=1,
    )

    assert len(dataset) == 2
    assert dataset["forward_return"].notna().sum() == 2
    assert dataset["forward_return"].tolist() == pytest.approx([0.10, 0.10])


def test_research_dataset_uses_future_prices_when_future_basic_is_empty():
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            "20260105": [{"ts_code": "000001.SZ", "pe_ttm": 10.0}],
            "20260106": [],
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

    assert dataset["forward_return"].tolist() == pytest.approx([0.10])


def test_research_dataset_rejects_failed_optional_basic_query():
    class FailedBasicExecutor(FakeQueryExecutor):
        def execute(self, query, *, api_route, request_id):
            result = super().execute(
                query,
                api_route=api_route,
                request_id=request_id,
            )
            if (
                query.operation == "daily_basic"
                and query.params["trade_date"] == "20260106"
            ):
                return result.model_copy(update={"status": QueryStatus.ERROR})
            return result

    executor = FailedBasicExecutor(
        ["20260105", "20260106"],
        {
            "20260105": [{"ts_code": "000001.SZ", "pe_ttm": 10.0}],
            "20260106": [],
        },
        {
            "20260105": [{"ts_code": "000001.SZ", "close": 10.0}],
            "20260106": [{"ts_code": "000001.SZ", "close": 11.0}],
        },
    )

    with pytest.raises(ValueError, match="Incomplete daily_basic data"):
        FactorBacktester(executor).build_dataset(
            "20260105",
            "20260105",
            forward_days=1,
        )


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


def test_research_dataset_fails_when_a_required_session_is_missing():
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

    with pytest.raises(ValueError, match="20260106"):
        FactorBacktester(executor).build_dataset(
            "20260105",
            "20260105",
            forward_days=1,
        )


def test_research_dataset_rejects_duplicate_trading_calendar_dates():
    executor = FakeQueryExecutor(
        ["20260105", "20260106", "20260106"],
        {
            date: [{"ts_code": "000001.SZ", "pe_ttm": 10.0}]
            for date in ["20260105", "20260106"]
        },
        {
            date: [{"ts_code": "000001.SZ", "close": 10.0}]
            for date in ["20260105", "20260106"]
        },
    )

    with pytest.raises(ValueError, match="contains duplicate dates"):
        FactorBacktester(executor).build_dataset(
            "20260105",
            "20260105",
            forward_days=1,
        )


def test_research_dataset_rejects_invalid_trading_calendar_dates():
    class InvalidCalendarExecutor(FakeQueryExecutor):
        def execute(self, query, *, api_route, request_id):
            if query.operation == "trade_cal":
                return QueryResult(
                    query_id=query.query_id,
                    provider="fake",
                    operation=query.operation,
                    status=QueryStatus.SUCCESS,
                    columns=["cal_date", "is_open"],
                    rows=[{"cal_date": "20260199", "is_open": "1"}],
                    row_count=1,
                )
            return super().execute(
                query,
                api_route=api_route,
                request_id=request_id,
            )

    executor = InvalidCalendarExecutor([], {}, {})

    with pytest.raises(ValueError, match="Invalid discovery trading date"):
        FactorBacktester(executor).build_dataset(
            "20260105",
            "20260105",
            forward_days=1,
        )


def test_research_dataset_rejects_duplicate_security_rows():
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            date: [{"ts_code": "000001.SZ", "pe_ttm": 10.0}]
            for date in ["20260105", "20260106"]
        },
        {
            "20260105": [
                {"ts_code": "000001.SZ", "close": 10.0},
                {"ts_code": "000001.SZ", "close": 10.0},
            ],
            "20260106": [{"ts_code": "000001.SZ", "close": 11.0}],
        },
    )

    with pytest.raises(ValueError, match="Duplicate daily security rows"):
        FactorBacktester(executor).build_dataset(
            "20260105",
            "20260105",
            forward_days=1,
        )


def test_research_dataset_rejects_partial_adjustment_coverage():
    securities = ["000001.SZ", "000002.SZ"]
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            date: [{"ts_code": code, "pe_ttm": 10.0} for code in securities]
            for date in ["20260105", "20260106"]
        },
        {
            date: [{"ts_code": code, "close": 10.0} for code in securities]
            for date in ["20260105", "20260106"]
        },
        {
            date: [{"ts_code": "000001.SZ", "adj_factor": 1.0}]
            for date in ["20260105", "20260106"]
        },
    )

    with pytest.raises(ValueError, match="Missing adjustment factors for 1"):
        FactorBacktester(executor).build_dataset(
            "20260105",
            "20260105",
            forward_days=1,
        )


def test_research_dataset_rejects_non_finite_prices():
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            date: [{"ts_code": "000001.SZ", "pe_ttm": 10.0}]
            for date in ["20260105", "20260106"]
        },
        {
            "20260105": [{"ts_code": "000001.SZ", "close": float("inf")}],
            "20260106": [{"ts_code": "000001.SZ", "close": 11.0}],
        },
    )

    with pytest.raises(ValueError, match="Invalid close or adjustment factor"):
        FactorBacktester(executor).build_dataset(
            "20260105",
            "20260105",
            forward_days=1,
        )


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
    assert result.return_p05 == pytest.approx(-0.17)
    assert result.max_drawdown == pytest.approx(-0.20)
    assert result.baseline_win_rate == pytest.approx(0.75)
    assert result.baseline_sample_count == 4


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
    assert result.baseline_sample_count == 3


def test_rule_evaluation_preserves_schema_for_an_empty_match():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105", "20260106"],
            "factor": [1.0, 2.0],
            "forward_return": [0.10, -0.10],
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor > 10")

    assert result.sample_count == 0
    assert result.win_rate == 0.0


def test_rule_evaluation_reports_missing_outcome_coverage():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105", "20260106", "20260107"],
            "factor": [1.0, 1.0, 1.0],
            "forward_return": [0.10, None, -0.10],
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")

    assert result.matched_sample_count == 3
    assert result.sample_count == 2
    assert result.missing_outcome_count == 1
    assert result.outcome_coverage_rate == pytest.approx(2 / 3)


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


def test_rule_evaluation_does_not_claim_precision_from_one_trading_day():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105"] * 100,
            "factor": list(range(100)),
            "forward_return": [0.10] * 80 + [-0.10] * 20,
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor >= 0")

    assert result.sample_count == 100
    assert result.trading_day_count == 1
    assert result.confidence_lower == 0.0
    assert result.confidence_upper == 1.0


@pytest.mark.parametrize(
    ("forward_return", "bound"),
    [(0.10, "lower"), (-0.10, "upper")],
)
def test_rule_evaluation_keeps_boundary_probabilities_uncertain(
    forward_return,
    bound,
):
    dataset = pd.DataFrame(
        {
            "trade_date": [f"202601{i:02d}" for i in range(1, 21)],
            "factor": [1.0] * 20,
            "forward_return": [forward_return] * 20,
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")

    if bound == "lower":
        assert 0.80 < result.confidence_lower < 1.0
        assert result.confidence_upper == 1.0
    else:
        assert result.confidence_lower == 0.0
        assert 0.0 < result.confidence_upper < 0.20


def test_rule_evaluation_accounts_for_overlap_when_estimating_lift_uncertainty():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105"] * 2 + ["20260106"] * 2,
            "factor": [1.0, 0.0, 1.0, 0.0],
            "forward_return": [0.10, -0.10, -0.10, -0.10],
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")

    assert result.win_rate_lift == pytest.approx(0.25)
    assert result.lift_standard_error == pytest.approx(0.25)


def test_rule_evaluation_uses_hac_error_for_overlapping_forward_windows():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105", "20260106", "20260107", "20260108"],
            "factor": [1.0] * 4,
            "forward_return": [0.10, 0.10, -0.10, -0.10],
        }
    )

    independent = FactorBacktester.evaluate_rule(dataset, "factor == 1")
    overlapping = FactorBacktester.evaluate_rule(
        dataset,
        "factor == 1",
        dependence_lag_days=1,
    )

    assert overlapping.dependence_lag_days == 1
    assert overlapping.cluster_standard_error > independent.cluster_standard_error


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
    training_scores = [
        candidate.train_result.confidence_lower
        + candidate.train_result.win_rate_lift
        for candidate in candidates
    ]
    assert training_scores == sorted(training_scores, reverse=True)
    assert all(candidate.generalization_gap >= 0 for candidate in candidates)


def test_rule_search_ignores_non_finite_factor_values():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"202601{i:02d}" for i in range(1, 11)],
            "value": [float("-inf"), *range(8), float("inf")],
            "forward_return": [-0.10] * 5 + [0.10] * 5,
        }
    )

    candidates, _ = RuleSearchEngine(min_sample_count=2).search(
        dataset,
        dataset.copy(),
        ["value"],
        max_conditions=1,
        top_n=10,
    )

    assert candidates
    assert all("inf" not in candidate.formula for candidate in candidates)
    assert all(candidate.train_result.sample_count < 10 for candidate in candidates)


def test_rule_search_deduplicates_equivalent_discrete_thresholds():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"202601{i:02d}" for i in range(1, 21)],
            "binary": [0.0] * 10 + [1.0] * 10,
            "forward_return": [-0.10] * 10 + [0.10] * 10,
        }
    )

    conditions = RuleSearchEngine(min_sample_count=2)._build_conditions(
        dataset,
        ["binary"],
    )

    selected_sets = [
        tuple(dataset.query(formula).index)
        for formula, _ in conditions
    ]
    assert len(selected_sets) == len(set(selected_sets))


def test_rule_search_deduplicates_thresholds_after_formula_rounding():
    dataset = pd.DataFrame(
        {
            "value": [1.0 + index * 1e-11 for index in range(100)],
        }
    )

    conditions = RuleSearchEngine(min_sample_count=2)._build_conditions(
        dataset,
        ["value"],
    )

    formulas = [formula for formula, _ in conditions]
    selected_sets = [
        tuple(dataset.query(formula).index)
        for formula in formulas
    ]
    assert len(formulas) == len(set(formulas))
    assert len(selected_sets) == len(set(selected_sets))


def test_rule_search_discovers_a_same_factor_middle_interval():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"2026{i:04d}" for i in range(1, 101)],
            "value": list(range(100)),
            "forward_return": [
                0.10 if 30 <= value < 70 else -0.10
                for value in range(100)
            ],
        }
    )

    candidates, _ = RuleSearchEngine(min_sample_count=10).search(
        dataset,
        dataset.copy(),
        ["value"],
        max_conditions=2,
        top_n=20,
    )

    interval_rules = [
        candidate
        for candidate in candidates
        if candidate.formula.count("value") == 2
    ]
    assert interval_rules
    assert interval_rules[0].train_result.win_rate > 0.70


def test_rule_search_balances_factors_and_directions_in_the_pairing_pool():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"2026{i:04d}" for i in range(1, 101)],
            "first": list(range(100)),
            "second": list(range(100)),
            "forward_return": [-0.10] * 50 + [0.10] * 50,
        }
    )
    search = RuleSearchEngine(min_sample_count=10)
    conditions = search._build_conditions(dataset, ["first", "second"])
    candidates, _ = search._evaluate_training_formulas(conditions, dataset)

    pairing_pool = search._select_pairing_conditions(candidates)
    buckets = [
        (field, formula.split()[1])
        for formula, field in pairing_pool
    ]

    assert set(field for _, field in pairing_pool) == {"first", "second"}
    assert len(buckets) == len(set(buckets)) == 4


def test_rule_search_prioritizes_factor_breadth_in_the_pairing_pool():
    factor_names = [f"factor_{index}" for index in range(8)]
    dataset = pd.DataFrame(
        {
            "trade_date": [f"2026{index:04d}" for index in range(1, 101)],
            "forward_return": [-0.10] * 50 + [0.10] * 50,
            **{factor: list(range(100)) for factor in factor_names},
        }
    )
    search = RuleSearchEngine(min_sample_count=10)
    conditions = search._build_conditions(dataset, factor_names)
    candidates, _ = search._evaluate_training_formulas(conditions, dataset)

    pairing_pool = search._select_pairing_conditions(candidates)

    assert len(pairing_pool) == PAIRING_CANDIDATE_LIMIT
    assert {field for _, field in pairing_pool} == set(factor_names)


def test_rule_search_deduplicates_formulas_selecting_the_same_training_cohort():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"202601{i:02d}" for i in range(1, 41)],
            "first": list(range(40)),
            "duplicate": list(range(40)),
            "forward_return": [-0.10] * 20 + [0.10] * 20,
        }
    )

    candidates, _ = RuleSearchEngine(min_sample_count=5).search(
        dataset,
        dataset.copy(),
        ["first", "duplicate"],
        max_conditions=2,
        top_n=50,
    )

    selected_sets = [
        tuple(dataset.query(candidate.formula).index)
        for candidate in candidates
    ]
    assert candidates
    assert len(selected_sets) == len(set(selected_sets))


def test_rule_search_does_not_use_validation_outcomes_to_choose_the_winner():
    train = pd.DataFrame(
        {
            "trade_date": [f"202601{i:02d}" for i in range(1, 21)],
            "value": list(range(20)),
            "forward_return": [-0.10] * 10 + [0.10] * 10,
        }
    )
    validation = train.assign(forward_return=-train["forward_return"])

    candidates, _ = RuleSearchEngine(min_sample_count=4).search(
        train,
        validation,
        ["value"],
        max_conditions=1,
        top_n=1,
    )

    assert len(candidates) == 1
    assert candidates[0].formula.startswith("value >=")
    assert (
        candidates[0].val_result.win_rate
        < candidates[0].val_result.baseline_win_rate
    )


def test_rule_search_rejects_many_events_concentrated_on_one_day():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105"] * 100,
            "value": list(range(100)),
            "forward_return": [-0.10] * 50 + [0.10] * 50,
        }
    )

    candidates, _ = RuleSearchEngine(
        min_sample_count=10,
        min_trading_day_count=2,
    ).search(
        dataset,
        dataset.copy(),
        ["value"],
        max_conditions=1,
        top_n=10,
    )

    assert candidates == []


def test_rule_search_rejects_low_outcome_coverage():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"202601{i:02d}" for i in range(1, 11)],
            "value": list(range(10)),
            "forward_return": [0.10, None] * 5,
        }
    )

    candidates, _ = RuleSearchEngine(
        min_sample_count=2,
        min_outcome_coverage=0.90,
    ).search(
        dataset,
        dataset.copy(),
        ["value"],
        max_conditions=1,
        top_n=10,
    )

    assert candidates == []


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
    assert all(
        candidate.validation_passed
        == (
            candidate.q_value <= 0.10
            and candidate.train_result.win_rate_lift > 0.0
            and candidate.val_result.win_rate_lift > 0.0
        )
        for candidate in candidates
    )
    ordered_by_p = sorted(candidates, key=lambda candidate: candidate.p_value)
    assert [candidate.q_value for candidate in ordered_by_p] == sorted(
        candidate.q_value for candidate in candidates
    )


def test_false_discovery_rate_counts_ineligible_validation_candidates():
    candidates = [
        FactorHypothesis(
            formula=f"value >= {index}",
            description="Test rule",
            reasoning="Test evidence",
            p_value=p_value,
        )
        for index, p_value in enumerate([0.01, 0.04])
    ]

    RuleSearchEngine._apply_false_discovery_rate(
        candidates,
        family_size=10,
    )

    assert [candidate.q_value for candidate in candidates] == pytest.approx(
        [0.10, 0.20]
    )
    assert all(candidate.fdr_family_size == 10 for candidate in candidates)


def test_validation_rejects_a_rule_with_negative_training_lift():
    candidate = FactorHypothesis(
        formula="value >= 1",
        description="Test rule",
        reasoning="Test evidence",
        train_result=BacktestResult(
            win_rate=0.40,
            mean_return=-0.01,
            max_drawdown=-0.10,
            eval_time_ms=1,
            win_rate_lift=-0.05,
        ),
        val_result=BacktestResult(
            win_rate=0.70,
            mean_return=0.05,
            max_drawdown=-0.05,
            eval_time_ms=1,
            win_rate_lift=0.20,
        ),
        q_value=0.01,
    )

    assert RuleSearchEngine._passes_validation(candidate) is False


def test_clustered_lift_significance_remains_finite_for_large_samples():
    probability = RuleSearchEngine._clustered_lift_tail_probability(0.06, 0.01)

    assert 0.0 <= probability <= 1.0
    assert probability < 0.001


def test_clustered_lift_significance_is_conservative_without_variation():
    probability = RuleSearchEngine._clustered_lift_tail_probability(0.10, 0.0)

    assert probability == 1.0


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


def test_discovery_request_rejects_duplicate_factors():
    with pytest.raises(ValueError, match="Discovery factors must be unique"):
        DiscoveryTaskRequest(
            target_pool="A_SHARE",
            train_start="20250101",
            train_end="20251231",
            val_start="20260101",
            val_end="20260630",
            factors=["pe_ttm", "pe_ttm"],
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
    assert request.max_generations == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_generations", 2), ("max_conditions", 3)],
)
def test_discovery_request_rejects_unimplemented_search_depths(field, value):
    request = {
        "target_pool": "A_SHARE",
        "train_start": "20250101",
        "train_end": "20251231",
        "val_start": "20260101",
        "val_end": "20260630",
        "factors": ["pe_ttm"],
        field: value,
    }

    with pytest.raises(ValueError):
        DiscoveryTaskRequest(**request)


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
            minimum_trading_days=25,
            minimum_outcome_coverage_pct=98,
            max_conditions=2,
        ),
        created_at=now,
        updated_at=now,
    )

    store.put(task)

    loaded = store.get(task.task_id)
    assert loaded.request.forward_days == 20
    assert loaded.request.minimum_samples == 50
    assert loaded.request.minimum_trading_days == 25
    assert loaded.request.minimum_outcome_coverage_pct == 98
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
            minimum_trading_days=5,
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
    assert completed.progress.training_factor_coverage == {"pe_ttm": 1.0}
    assert completed.progress.validation_factor_coverage == {"pe_ttm": 1.0}
    assert completed.progress.leaderboard
    assert all(
        candidate.val_result.dependence_lag_days == 4
        for candidate in completed.progress.leaderboard
    )
    assert backtester.calls == [
        ("20250101", "20251231", 5),
        ("20260101", "20260630", 5),
    ]


def test_evolution_loop_reports_finite_factor_coverage():
    frame = pd.DataFrame(
        {
            "available": [1.0, None, float("inf"), "2.0"],
            "invalid": [None, "not-a-number", float("-inf"), None],
        }
    )

    coverage = EvolutionLoop._factor_coverage(
        frame,
        ["available", "invalid", "absent"],
    )

    assert coverage == {
        "available": 0.5,
        "invalid": 0.0,
        "absent": 0.0,
    }

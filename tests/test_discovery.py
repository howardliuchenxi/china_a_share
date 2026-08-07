import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from china_a_share.core.contracts import (
    AnalysisRequest,
    AnalysisTask,
    AnalysisTaskStatus,
    BacktestResult,
    DISCOVERY_FACTOR_FIELDS,
    DISCOVERY_SEQUENCE_FACTOR_FIELDS,
    DiscoveryTask,
    DiscoveryTaskRequest,
    FactorHypothesis,
    QueryResult,
    QueryStatus,
)
from china_a_share.discovery.backtester import FactorBacktester
from china_a_share.discovery.evolution_loop import EvolutionLoop
from china_a_share.discovery.search import (
    PAIRING_CANDIDATE_LIMIT,
    VALIDATION_CANDIDATE_LIMIT,
    RuleSearchEngine,
)
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


def test_frontend_discovery_catalog_matches_backend_contract_and_safety_sets():
    repository_root = Path(__file__).resolve().parents[1]
    discovery_page = (
        repository_root / "frontend/src/DiscoveryPage.tsx"
    ).read_text(encoding="utf-8")
    factor_block = re.search(
        r"const discoveryFactorFields = new Set\(\[(.*?)\]\);",
        discovery_page,
        re.DOTALL,
    )
    assert factor_block is not None
    frontend_factors = set(
        re.findall(r'"([a-z][a-z0-9_]*)"', factor_block.group(1))
    )
    unsupported_block = re.search(
        r"const unsupportedDirectApplicationFields = new Set\(\[(.*?)\]\);",
        discovery_page,
        re.DOTALL,
    )
    assert unsupported_block is not None
    unsupported_direct_application = set(
        re.findall(r'"([a-z][a-z0-9_]*)"', unsupported_block.group(1))
    )
    dictionary_source = (
        repository_root / "frontend/src/dataDictionary.ts"
    ).read_text(encoding="utf-8")
    dictionary_labels = {
        field: label
        for label, field in re.findall(
            r'\{ label: "([^"]+)", field: "([^"]+)"',
            dictionary_source,
        )
    }

    assert frontend_factors == DISCOVERY_FACTOR_FIELDS
    assert unsupported_direct_application == DISCOVERY_SEQUENCE_FACTOR_FIELDS
    assert DISCOVERY_FACTOR_FIELDS <= dictionary_labels.keys()
    assert all(
        dictionary_labels[field] not in {"", "Y", "N"}
        for field in DISCOVERY_FACTOR_FIELDS
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


def test_research_dataset_derives_point_in_time_sequence_features():
    trade_dates = [f"202601{index:02d}" for index in range(5, 12)]
    executor = FakeQueryExecutor(
        trade_dates,
        {date: [{"ts_code": "000001.SZ"}] for date in trade_dates},
        {
            date: [
                {
                    "ts_code": "000001.SZ",
                    "close": 1.0 if index == 6 else 10.0 + index,
                }
            ]
            for index, date in enumerate(trade_dates)
        },
    )

    dataset = FactorBacktester(executor).build_dataset(
        "20260110",
        "20260110",
        forward_days=1,
    )

    assert dataset["return_5d_pct"].tolist() == pytest.approx([50.0])
    expected_volatility = pd.Series(
        [
            15.0 / 14.0 - 1.0,
            14.0 / 13.0 - 1.0,
            13.0 / 12.0 - 1.0,
            12.0 / 11.0 - 1.0,
            11.0 / 10.0 - 1.0,
        ]
    ).std(ddof=0) * 100.0
    assert dataset["volatility_5d_pct"].tolist() == pytest.approx(
        [expected_volatility]
    )
    assert dataset["max_drawdown_5d_pct"].tolist() == [0.0]
    assert dataset["distance_from_5d_peak_pct"].tolist() == [0.0]
    assert dataset["positive_days_3"].tolist() == [3.0]
    assert dataset["forward_return"].tolist() == pytest.approx([1.0 / 15.0 - 1.0])


def test_research_dataset_derives_point_in_time_max_drawdown():
    trade_dates = [f"202601{index:02d}" for index in range(5, 12)]
    closes = [10.0, 12.0, 9.0, 11.0, 8.0, 10.0, 1000.0]
    executor = FakeQueryExecutor(
        trade_dates,
        {date: [{"ts_code": "000001.SZ"}] for date in trade_dates},
        {
            date: [{"ts_code": "000001.SZ", "close": closes[index]}]
            for index, date in enumerate(trade_dates)
        },
    )

    dataset = FactorBacktester(executor).build_dataset(
        "20260110",
        "20260110",
        forward_days=1,
    )

    assert dataset["max_drawdown_5d_pct"].tolist() == pytest.approx(
        [(8.0 / 12.0 - 1.0) * 100.0]
    )
    assert dataset["distance_from_5d_peak_pct"].tolist() == pytest.approx(
        [(10.0 / 12.0 - 1.0) * 100.0]
    )


def test_sequence_features_reject_non_consecutive_security_history():
    trade_dates = [f"202601{index:02d}" for index in range(5, 12)]
    prices = {}
    for index, date in enumerate(trade_dates):
        rows = [
            {
                "ts_code": "000002.SZ",
                "close": 20.0 + index,
                "pct_chg": 1.0,
            }
        ]
        if date != "20260108":
            rows.append(
                {
                    "ts_code": "000001.SZ",
                    "close": 10.0 + index,
                    "pct_chg": 1.0,
                }
            )
        prices[date] = rows
    executor = FakeQueryExecutor(
        trade_dates,
        {date: [] for date in trade_dates},
        prices,
    )

    dataset = FactorBacktester(executor).build_dataset(
        "20260110",
        "20260110",
        forward_days=1,
    ).set_index("ts_code")

    assert pd.isna(dataset.loc["000001.SZ", "return_5d_pct"])
    assert pd.isna(dataset.loc["000001.SZ", "volatility_5d_pct"])
    assert pd.isna(dataset.loc["000001.SZ", "max_drawdown_5d_pct"])
    assert pd.isna(dataset.loc["000001.SZ", "distance_from_5d_peak_pct"])
    assert pd.isna(dataset.loc["000001.SZ", "positive_days_3"])
    assert dataset.loc["000002.SZ", "positive_days_3"] == 3.0
    assert pd.notna(dataset.loc["000002.SZ", "volatility_5d_pct"])
    assert dataset.loc["000002.SZ", "max_drawdown_5d_pct"] == 0.0
    assert dataset.loc["000002.SZ", "distance_from_5d_peak_pct"] == 0.0


def test_research_dataset_resolves_forward_returns_across_a_long_market_closure():
    executor = FakeQueryExecutor(
        ["20200123", "20200203"],
        {
            "20200123": [{"ts_code": "000001.SZ", "pe_ttm": 10.0}],
            "20200203": [{"ts_code": "000001.SZ", "pe_ttm": 11.0}],
        },
        {
            "20200123": [{"ts_code": "000001.SZ", "close": 10.0}],
            "20200203": [{"ts_code": "000001.SZ", "close": 11.0}],
        },
    )

    dataset = FactorBacktester(executor).build_dataset(
        "20200123",
        "20200123",
        forward_days=1,
    )

    assert dataset["future_trade_date"].tolist() == ["20200203"]
    assert dataset["forward_return"].tolist() == pytest.approx([0.10])


def test_research_dataset_rejects_a_window_without_trading_sessions():
    executor = FakeQueryExecutor([], {}, {})

    with pytest.raises(ValueError, match="contains no trading sessions"):
        FactorBacktester(executor).build_dataset(
            "20260105",
            "20260106",
            forward_days=1,
        )


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


def test_research_dataset_requires_a_signal_date_daily_bar():
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            "20260105": [
                {"ts_code": "000001.SZ", "pe_ttm": 10.0},
                {"ts_code": "000002.SZ", "pe_ttm": 20.0},
            ],
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

    assert dataset["ts_code"].tolist() == ["000001.SZ"]
    assert dataset["forward_return"].tolist() == pytest.approx([0.10])


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


@pytest.mark.parametrize("invalid_date", ["20260199", "2026010"])
def test_research_dataset_rejects_invalid_trading_calendar_dates(invalid_date):
    class InvalidCalendarExecutor(FakeQueryExecutor):
        def execute(self, query, *, api_route, request_id):
            if query.operation == "trade_cal":
                return QueryResult(
                    query_id=query.query_id,
                    provider="fake",
                    operation=query.operation,
                    status=QueryStatus.SUCCESS,
                    columns=["cal_date", "is_open"],
                    rows=[{"cal_date": invalid_date, "is_open": "1"}],
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


def test_research_dataset_rejects_a_failed_trading_calendar_query():
    class FailedCalendarExecutor(FakeQueryExecutor):
        def execute(self, query, *, api_route, request_id):
            result = super().execute(
                query,
                api_route=api_route,
                request_id=request_id,
            )
            if query.operation == "trade_cal":
                return result.model_copy(update={"status": QueryStatus.ERROR})
            return result

    executor = FailedCalendarExecutor([], {}, {})

    with pytest.raises(ValueError, match="calendar could not be loaded"):
        FactorBacktester(executor).build_dataset(
            "20260105",
            "20260105",
            forward_days=1,
        )


def test_research_dataset_rejects_an_incomplete_forward_window():
    executor = FakeQueryExecutor(["20260105"], {}, {})

    with pytest.raises(ValueError, match="extends beyond available sessions"):
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


def test_research_dataset_excludes_b_shares_before_adjustment_validation():
    securities = ["000001.SZ", "200001.SZ", "900901.SH"]
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            date: [
                {"ts_code": code, "pe_ttm": 10.0}
                for code in securities
            ]
            for date in ["20260105", "20260106"]
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
        {
            date: [{"ts_code": "000001.SZ", "adj_factor": 1.0}]
            for date in ["20260105", "20260106"]
        },
    )

    dataset = FactorBacktester(executor).build_dataset(
        "20260105",
        "20260105",
        forward_days=1,
    )

    assert dataset["ts_code"].tolist() == ["000001.SZ"]
    assert dataset["forward_return"].tolist() == pytest.approx([0.10])


def test_research_dataset_rejects_a_session_containing_only_b_shares():
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            date: [{"ts_code": "200001.SZ", "pe_ttm": 10.0}]
            for date in ["20260105", "20260106"]
        },
        {
            date: [{"ts_code": "200001.SZ", "close": 10.0}]
            for date in ["20260105", "20260106"]
        },
        {
            date: [{"ts_code": "200001.SZ", "adj_factor": 1.0}]
            for date in ["20260105", "20260106"]
        },
    )

    with pytest.raises(ValueError, match="No A-share daily data"):
        FactorBacktester(executor).build_dataset(
            "20260105",
            "20260105",
            forward_days=1,
        )


def test_research_dataset_rejects_missing_adjustment_fields():
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            date: [{"ts_code": "000001.SZ", "pe_ttm": 10.0}]
            for date in ["20260105", "20260106"]
        },
        {
            date: [{"ts_code": "000001.SZ", "close": 10.0}]
            for date in ["20260105", "20260106"]
        },
        {
            date: [{"ts_code": "000001.SZ"}]
            for date in ["20260105", "20260106"]
        },
    )

    with pytest.raises(ValueError, match="Missing adj_factor fields"):
        FactorBacktester(executor).build_dataset(
            "20260105",
            "20260105",
            forward_days=1,
        )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("600000.SH", True),
        ("000001.SZ", True),
        ("430001.BJ", True),
        ("900901.SH", False),
        ("200001.SZ", False),
        ("000001.HK", False),
        ("invalid", False),
    ],
)
def test_a_share_code_filter_has_explicit_market_boundaries(code, expected):
    assert FactorBacktester._is_a_share_code(code) is expected


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


def test_rule_evaluation_reports_exact_event_statistics_without_fake_drawdown():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105", "20260106", "20260107", "20260108"],
            "factor": [1.0, 2.0, 3.0, 4.0],
            "forward_return": [0.10, -0.20, 0.30, 0.10],
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor >= 2")

    assert result.sample_count == 3
    assert result.security_count == 3
    assert result.positive_count == 2
    assert result.win_rate == pytest.approx(2 / 3)
    assert result.mean_return == pytest.approx(0.0666666667)
    assert result.median_return == pytest.approx(0.10)
    assert result.return_p05 == pytest.approx(-0.17)
    assert result.max_drawdown is None
    assert result.eligible_sample_count == 4
    assert result.rule_support_rate == pytest.approx(0.75)
    assert result.baseline_win_rate == pytest.approx(0.75)
    assert result.baseline_sample_count == 4


def test_single_rule_backtest_builds_and_evaluates_one_dataset():
    executor = FakeQueryExecutor(
        ["20260105", "20260106"],
        {
            date: [{"ts_code": "000001.SZ", "pe_ttm": 10.0}]
            for date in ["20260105", "20260106"]
        },
        {
            "20260105": [{"ts_code": "000001.SZ", "close": 10.0}],
            "20260106": [{"ts_code": "000001.SZ", "close": 11.0}],
        },
    )

    result = FactorBacktester(executor).run_backtest(
        "pe_ttm <= 10",
        "20260105",
        "20260105",
        forward_days=1,
        target_return=0.05,
    )

    assert result.sample_count == 1
    assert result.win_rate == pytest.approx(1.0)
    assert result.target_return == pytest.approx(0.05)
    assert result.dependence_lag_days == 0
    assert result.eval_time_ms >= 0


def test_rule_evaluation_rejects_missing_outcomes_and_invalid_formulas():
    with pytest.raises(ValueError, match="missing forward_return"):
        FactorBacktester.evaluate_rule(
            pd.DataFrame({"factor": [1.0]}),
            "factor >= 1",
        )

    with pytest.raises(ValueError, match="Invalid discovery rule"):
        FactorBacktester.evaluate_rule(
            pd.DataFrame({"factor": [1.0], "forward_return": [0.10]}),
            "factor >>> 1",
        )


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


def test_rule_evaluation_uses_a_factor_comparable_baseline():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105", "20260106", "20260107", "20260108"],
            "factor": [1.0, 2.0, None, None],
            "forward_return": [0.10, -0.10, 0.10, 0.10],
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor >= 1")

    assert result.win_rate == pytest.approx(0.50)
    assert result.baseline_win_rate == pytest.approx(0.50)
    assert result.baseline_sample_count == 2
    assert result.win_rate_lift == pytest.approx(0.0)
    assert result.eligible_sample_count == 2
    assert result.rule_support_rate == pytest.approx(1.0)


@pytest.mark.parametrize(
    "formula",
    [
        "forward_return > 0",
        "future_adjusted_close > 10",
        "future_trade_date >= '20260106'",
    ],
)
def test_rule_evaluation_rejects_outcome_field_leakage(formula):
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105"],
            "factor": [1.0],
            "forward_return": [0.10],
            "future_adjusted_close": [11.0],
            "future_trade_date": ["20260106"],
        }
    )

    with pytest.raises(ValueError, match="cannot reference outcome fields"):
        FactorBacktester.evaluate_rule(dataset, formula)


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
    assert result.confidence_lower == 0.0
    assert result.confidence_upper == 1.0
    assert result.lift_confidence_lower == -1.0
    assert result.lift_confidence_upper == 1.0
    assert result.event_examples == []


def test_rule_evaluation_keeps_all_missing_outcomes_fully_uncertain():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105", "20260106", "20260107"],
            "factor": [1.0, 1.0, 1.0],
            "forward_return": [None, None, None],
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")

    assert result.matched_sample_count == 3
    assert result.sample_count == 0
    assert result.missing_outcome_count == 3
    assert result.outcome_coverage_rate == 0.0
    assert result.confidence_lower == 0.0
    assert result.confidence_upper == 1.0
    assert result.lift_confidence_lower == -1.0
    assert result.lift_confidence_upper == 1.0


def test_empty_statistical_helpers_preserve_full_uncertainty():
    assert FactorBacktester._event_examples(pd.DataFrame(), []) == []
    assert FactorBacktester._outcome_bounds(0, 0, 0) == (0.0, 1.0)
    assert FactorBacktester._outcome_robust_lift_bounds(
        selected_positive_count=0,
        selected_matched_count=0,
        selected_observed_count=0,
        baseline_positive_count=0,
        baseline_matched_count=0,
        baseline_observed_count=0,
    ) == (-1.0, 1.0)
    assert FactorBacktester._wilson_interval(0.0, 0.0) == (0.0, 1.0)
    assert FactorBacktester._effective_cluster_count(pd.Series(dtype=str)) == 0.0


def test_outcome_robust_lift_rejects_impossible_overlap_counts():
    with pytest.raises(
        ValueError,
        match="Outcome counts violate selected/baseline containment",
    ):
        FactorBacktester._outcome_robust_lift_bounds(
            selected_positive_count=1,
            selected_matched_count=2,
            selected_observed_count=1,
            baseline_positive_count=1,
            baseline_matched_count=3,
            baseline_observed_count=3,
        )


def test_rule_evaluation_retains_bounded_recent_event_examples():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"202601{index:02d}" for index in range(1, 8)],
            "future_trade_date": [f"202602{index:02d}" for index in range(1, 8)],
            "ts_code": [f"{index:06d}.SZ" for index in range(1, 8)],
            "factor": [1.0] * 7,
            "forward_return": [index / 100.0 for index in range(1, 8)],
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")

    assert len(result.event_examples) == 5
    assert [example.trade_date for example in result.event_examples] == [
        "20260107",
        "20260106",
        "20260105",
        "20260104",
        "20260103",
    ]
    assert result.event_examples[0].ts_code == "000007.SZ"
    assert result.event_examples[0].future_trade_date == "20260207"
    assert result.event_examples[0].forward_return == pytest.approx(0.07)
    assert result.event_examples[0].factor_values == {"factor": 1.0}


def test_rule_event_examples_are_stable_when_latest_date_exceeds_the_limit():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260107"] * 7,
            "ts_code": [f"{index:06d}.SZ" for index in range(7, 0, -1)],
            "factor": [1.0] * 7,
            "forward_return": [0.01] * 7,
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")

    assert [example.ts_code for example in result.event_examples] == [
        "000001.SZ",
        "000002.SZ",
        "000003.SZ",
        "000004.SZ",
        "000005.SZ",
    ]


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
    assert result.baseline_outcome_coverage_rate == pytest.approx(2 / 3)


def test_training_screen_rejects_low_comparable_baseline_outcome_coverage():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"2026{index:04d}" for index in range(1, 21)],
            "factor": [1.0] * 10 + [0.0] * 10,
            "forward_return": [0.10] * 10 + [None] * 10,
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")
    candidates, evaluated_count = RuleSearchEngine(
        min_sample_count=5,
        min_outcome_coverage=0.90,
    )._evaluate_training_formulas([("factor == 1", "factor")], dataset)

    assert result.outcome_coverage_rate == 1.0
    assert result.baseline_outcome_coverage_rate == 0.5
    assert evaluated_count == 1
    assert candidates == []


def test_probability_interval_includes_unobserved_outcome_extremes():
    observed_dates = [f"2026{index:04d}" for index in range(1, 101)]
    missing_dates = [f"2027{index:04d}" for index in range(1, 6)]
    dataset = pd.DataFrame(
        {
            "trade_date": observed_dates + missing_dates,
            "factor": [1.0] * 105,
            "forward_return": [0.10] * 100 + [None] * 5,
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")

    assert result.win_rate == 1.0
    assert result.outcome_coverage_rate == pytest.approx(100 / 105)
    assert result.confidence_lower == pytest.approx(100 / 105)
    assert result.confidence_upper == 1.0
    assert result.outcome_robust_lift_lower == pytest.approx(0.0)
    assert result.outcome_robust_lift_upper == pytest.approx(0.0)


def test_outcome_robust_lift_preserves_selected_baseline_overlap():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"2026{index:04d}" for index in range(1, 211)],
            "factor": [1.0] * 100 + [0.0] * 110,
            "forward_return": [0.10] * 100 + [-0.10] * 100 + [None] * 10,
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")

    assert result.win_rate_lift == pytest.approx(0.50)
    assert result.outcome_robust_lift_lower == pytest.approx(1 - 110 / 210)
    assert result.outcome_robust_lift_upper == pytest.approx(1 - 100 / 210)


def test_rule_evaluation_reports_single_security_event_concentration():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"2026{index:04d}" for index in range(1, 101)],
            "ts_code": ["000001.SZ"] * 91
            + [f"{index:06d}.SZ" for index in range(2, 11)],
            "factor": [1.0] * 100,
            "forward_return": [0.10, -0.10] * 50,
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")

    assert result.security_count == 10
    assert result.max_security_event_share == pytest.approx(0.91)
    assert result.effective_security_count == pytest.approx(
        10000 / (91**2 + 9)
    )


def test_training_screen_rejects_low_effective_security_breadth():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"2026{index:04d}" for index in range(1, 101)],
            "ts_code": ["000001.SZ"] * 91
            + [f"{index:06d}.SZ" for index in range(2, 11)],
            "factor": [1.0] * 100,
            "forward_return": [0.10, -0.10] * 50,
        }
    )

    candidates, evaluated_count = RuleSearchEngine(
        min_sample_count=10,
        min_security_count=5,
    )._evaluate_training_formulas([("factor == 1", "factor")], dataset)

    assert evaluated_count == 1
    assert candidates == []


def test_rule_evaluation_reports_single_date_event_concentration():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260101"] * 81
            + [f"202601{index:02d}" for index in range(2, 21)],
            "ts_code": [f"{index:06d}.SZ" for index in range(1, 101)],
            "factor": [1.0] * 100,
            "forward_return": [0.10, -0.10] * 50,
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")

    assert result.trading_day_count == 20
    assert result.max_signal_date_event_share == pytest.approx(0.81)
    assert result.effective_trading_day_count == pytest.approx(
        10000 / (81**2 + 19)
    )


def test_training_screen_rejects_low_effective_trading_day_breadth():
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260101"] * 81
            + [f"202601{index:02d}" for index in range(2, 21)],
            "factor": [1.0] * 100,
            "forward_return": [0.10, -0.10] * 50,
        }
    )

    candidates, evaluated_count = RuleSearchEngine(
        min_sample_count=10,
        min_trading_day_count=5,
    )._evaluate_training_formulas([("factor == 1", "factor")], dataset)

    assert evaluated_count == 1
    assert candidates == []


def test_date_concentration_widens_the_boundary_safe_probability_interval():
    concentrated = pd.DataFrame(
        {
            "trade_date": ["20260101"] * 81
            + [f"202601{index:02d}" for index in range(2, 21)],
            "factor": [1.0] * 100,
            "forward_return": [0.10] * 100,
        }
    )
    balanced = pd.DataFrame(
        {
            "trade_date": [f"202601{index:02d}" for index in range(1, 21)],
            "factor": [1.0] * 20,
            "forward_return": [0.10] * 20,
        }
    )

    concentrated_result = FactorBacktester.evaluate_rule(
        concentrated,
        "factor == 1",
    )
    balanced_result = FactorBacktester.evaluate_rule(balanced, "factor == 1")

    assert concentrated_result.trading_day_count == 20
    assert balanced_result.trading_day_count == 20
    assert concentrated_result.effective_trading_day_count < 2.0
    assert balanced_result.effective_trading_day_count == pytest.approx(20.0)
    assert (
        concentrated_result.confidence_lower
        < balanced_result.confidence_lower
    )


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
    assert result.cluster_standard_error == 0.0
    assert result.lift_confidence_lower == -1.0
    assert result.lift_confidence_upper == 1.0


def test_hac_standard_error_is_zero_for_one_cluster():
    assert FactorBacktester._hac_standard_error(pd.Series([0.10]), 1) == 0.0


def test_clustered_probability_does_not_count_zero_gap_as_effective_cluster():
    frame = pd.DataFrame(
        {
            "trade_date": ["20260105", "20260107"],
            "forward_return": [0.10, -0.10],
        }
    )

    _, _, standard_error, trading_day_count = (
        FactorBacktester._clustered_confidence_interval(
            frame,
            target_return=0.0,
            dependence_lag_days=0,
            signal_dates=pd.Series(["20260105", "20260106", "20260107"]),
        )
    )

    assert trading_day_count == 2
    assert standard_error == pytest.approx(0.5)


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
    assert result.lift_confidence_lower <= result.win_rate_lift
    assert result.lift_confidence_upper >= result.win_rate_lift


def test_lift_interval_stays_uncertain_at_boundary_probabilities():
    dates = [f"202601{index:02d}" for index in range(1, 21)]
    dataset = pd.DataFrame(
        {
            "trade_date": [date for date in dates for _ in range(2)],
            "factor": [value for _ in range(20) for value in (1.0, 0.0)],
            "forward_return": [
                outcome
                for _ in range(20)
                for outcome in (0.10, -0.10)
            ],
        }
    )

    result = FactorBacktester.evaluate_rule(dataset, "factor == 1")

    assert result.win_rate_lift == pytest.approx(0.50)
    assert 0.0 < result.lift_confidence_lower < result.win_rate_lift
    assert result.win_rate_lift < result.lift_confidence_upper < 1.0


def test_lift_uncertainty_preserves_dates_without_comparable_factor_events():
    comparable = pd.DataFrame(
        {
            "trade_date": ["20260105", "20260105", "20260107", "20260107"],
            "forward_return": [0.10, -0.10, -0.10, -0.10],
        }
    )
    signal_dates = pd.Series(
        ["20260105", "20260106", "20260107"],
    )

    standard_error = FactorBacktester._clustered_lift_standard_error(
        comparable,
        pd.Index([0, 2]),
        target_return=0.0,
        dependence_lag_days=1,
        signal_dates=signal_dates,
    )

    expected_influence = pd.Series([0.125, 0.0, -0.125])
    assert standard_error == pytest.approx(
        FactorBacktester._hac_standard_error(
            expected_influence,
            1,
            effective_cluster_count=2,
        )
    )


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
    assert all(candidate.train_result.event_examples for candidate in candidates)
    assert all(candidate.val_result.event_examples for candidate in candidates)
    training_scores = [
        candidate.train_result.lift_confidence_lower
        for candidate in candidates
    ]
    assert training_scores == sorted(training_scores, reverse=True)
    assert all(candidate.generalization_gap >= 0 for candidate in candidates)
    assert all(
        candidate.support_rate_gap
        == pytest.approx(
            abs(
                candidate.train_result.rule_support_rate
                - candidate.val_result.rule_support_rate
            )
        )
        for candidate in candidates
    )
    assert all(
        candidate.support_retention_ratio
        == pytest.approx(
            candidate.val_result.rule_support_rate
            / candidate.train_result.rule_support_rate
        )
        for candidate in candidates
    )


def test_training_rank_penalizes_uncertain_lift():
    precise = FactorHypothesis(
        formula="value >= 1",
        description="Precise rule",
        reasoning="Test evidence",
        train_result=BacktestResult(
            win_rate=0.60,
            mean_return=0.01,
            max_drawdown=0.0,
            eval_time_ms=0,
            win_rate_lift=0.08,
            lift_standard_error=0.01,
            lift_confidence_lower=0.05,
            lift_confidence_upper=0.11,
        ),
    )
    uncertain = FactorHypothesis(
        formula="value >= 2",
        description="Uncertain rule",
        reasoning="Test evidence",
        train_result=BacktestResult(
            win_rate=0.70,
            mean_return=0.02,
            max_drawdown=0.0,
            eval_time_ms=0,
            win_rate_lift=0.12,
            lift_standard_error=0.05,
            lift_confidence_lower=-0.02,
            lift_confidence_upper=0.26,
        ),
    )

    assert RuleSearchEngine._training_rank_key(precise) > (
        RuleSearchEngine._training_rank_key(uncertain)
    )


def test_training_formula_screening_defers_event_example_extraction():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"202601{index:02d}" for index in range(1, 21)],
            "value": list(range(20)),
            "forward_return": [-0.10] * 10 + [0.10] * 10,
        }
    )
    search = RuleSearchEngine(min_sample_count=4)
    conditions = search._build_conditions(dataset, ["value"])

    candidates, _ = search._evaluate_training_formulas(conditions, dataset)

    assert candidates
    assert all(candidate.train_result.event_examples == [] for candidate in candidates)


def test_training_failures_do_not_consume_the_holdout_validation_budget(monkeypatch):
    blocked = [
        FactorHypothesis(
            formula=f"value >= {index}",
            description="Training failure",
            reasoning="Test evidence",
            train_result=BacktestResult(
                win_rate=0.49,
                mean_return=-0.01,
                eval_time_ms=0,
                win_rate_lift=-0.01,
                outcome_coverage_rate=1.0,
                baseline_outcome_coverage_rate=1.0,
                outcome_robust_lift_lower=-0.02,
                lift_confidence_lower=-0.02,
            ),
        )
        for index in range(VALIDATION_CANDIDATE_LIMIT)
    ]
    eligible = FactorHypothesis(
        formula="value >= 100",
        description="Eligible training rule",
        reasoning="Test evidence",
        train_result=BacktestResult(
            win_rate=0.60,
            mean_return=0.01,
            eval_time_ms=0,
            win_rate_lift=0.10,
            outcome_coverage_rate=1.0,
            baseline_outcome_coverage_rate=1.0,
            outcome_robust_lift_lower=0.01,
            lift_confidence_lower=-0.20,
        ),
    )
    validated_formulas = []

    monkeypatch.setattr(
        RuleSearchEngine,
        "_build_conditions",
        lambda self, train, factors: [("value >= 0", "value")],
    )
    monkeypatch.setattr(
        RuleSearchEngine,
        "_evaluate_training_formulas",
        lambda self, formulas, train, **kwargs: (
            [*blocked, eligible],
            len(blocked) + 1,
        ),
    )
    monkeypatch.setattr(
        RuleSearchEngine,
        "_deduplicate_by_training_selection",
        lambda self, candidates, train: list(candidates),
    )

    def capture_validation(self, candidates, validation, *, training=None):
        validated_formulas.extend(candidate.formula for candidate in candidates)
        for candidate in candidates:
            candidate.val_result = BacktestResult(
                win_rate=0.60,
                mean_return=0.01,
                eval_time_ms=0,
            )
        return list(candidates)

    monkeypatch.setattr(
        RuleSearchEngine,
        "_validate_candidates",
        capture_validation,
    )
    dataset = pd.DataFrame(
        {
            "trade_date": ["20260105"],
            "value": [100.0],
            "forward_return": [0.01],
        }
    )

    candidates, evaluated_count = RuleSearchEngine(min_sample_count=1).search(
        dataset,
        dataset.copy(),
        ["value"],
        max_conditions=1,
        top_n=1,
    )

    assert evaluated_count == VALIDATION_CANDIDATE_LIMIT + 1
    assert validated_formulas == [eligible.formula]
    assert [candidate.formula for candidate in candidates] == [eligible.formula]


def test_training_rank_does_not_treat_zero_standard_error_as_certainty():
    degenerate = FactorHypothesis(
        formula="value >= 1",
        description="Degenerate rule",
        reasoning="Test evidence",
        train_result=BacktestResult(
            win_rate=0.80,
            mean_return=0.05,
            eval_time_ms=0,
            win_rate_lift=0.30,
            lift_standard_error=0.0,
            lift_confidence_lower=-0.05,
            lift_confidence_upper=0.65,
        ),
    )
    identifiable = FactorHypothesis(
        formula="value >= 2",
        description="Identifiable rule",
        reasoning="Test evidence",
        train_result=BacktestResult(
            win_rate=0.60,
            mean_return=0.02,
            eval_time_ms=0,
            win_rate_lift=0.10,
            lift_standard_error=0.02,
            lift_confidence_lower=0.04,
            lift_confidence_upper=0.16,
        ),
    )

    assert RuleSearchEngine._training_rank_key(identifiable) > (
        RuleSearchEngine._training_rank_key(degenerate)
    )


def test_training_rank_uses_robust_returns_instead_of_outlier_mean():
    tail_robust = FactorHypothesis(
        formula="value >= 1",
        description="Tail-robust rule",
        reasoning="Test evidence",
        train_result=BacktestResult(
            win_rate=0.60,
            mean_return=0.03,
            median_return=0.02,
            return_p05=-0.05,
            eval_time_ms=0,
            win_rate_lift=0.10,
            lift_standard_error=0.02,
            lift_confidence_lower=0.04,
            lift_confidence_upper=0.16,
        ),
    )
    outlier_driven = FactorHypothesis(
        formula="value >= 2",
        description="Outlier-driven rule",
        reasoning="Test evidence",
        train_result=BacktestResult(
            win_rate=0.60,
            mean_return=0.30,
            median_return=0.01,
            return_p05=-0.20,
            eval_time_ms=0,
            win_rate_lift=0.10,
            lift_standard_error=0.02,
            lift_confidence_lower=0.04,
            lift_confidence_upper=0.16,
        ),
    )

    assert tail_robust.train_result.mean_return < outlier_driven.train_result.mean_return
    assert RuleSearchEngine._training_rank_key(tail_robust) > (
        RuleSearchEngine._training_rank_key(outlier_driven)
    )


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


def test_rule_search_excludes_conditions_that_do_not_filter_the_cohort():
    dataset = pd.DataFrame({"binary": [0.0] * 10 + [1.0] * 10})

    conditions = RuleSearchEngine(min_sample_count=2)._build_conditions(
        dataset,
        ["binary"],
    )

    selected_counts = [len(dataset.query(formula)) for formula, _ in conditions]
    assert selected_counts
    assert all(0 < count < len(dataset) for count in selected_counts)


def test_rule_search_preserves_pairing_budget_for_pure_interactions():
    factor_rows = [
        (first, second)
        for _ in range(25)
        for first, second in ((0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0))
    ]
    dataset = pd.DataFrame(
        {
            "trade_date": [f"2026{index:04d}" for index in range(1, 101)],
            "first": [row[0] for row in factor_rows],
            "second": [row[1] for row in factor_rows],
            "forward_return": [
                0.10 if first == second else -0.10
                for first, second in factor_rows
            ],
        }
    )

    candidates, _ = RuleSearchEngine(min_sample_count=20).search(
        dataset,
        dataset.copy(),
        ["first", "second"],
        max_conditions=2,
        top_n=10,
    )

    interaction_rules = [
        candidate
        for candidate in candidates
        if "first" in candidate.formula and "second" in candidate.formula
    ]
    assert interaction_rules
    assert interaction_rules[0].train_result.win_rate == pytest.approx(1.0)
    assert interaction_rules[0].train_result.win_rate_lift == pytest.approx(0.5)


def test_rule_search_allows_outcome_coverage_to_recover_in_an_interaction():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"202601{index:02d}" for index in range(1, 21)],
            "first": [1.0] * 10 + [0.0] * 10,
            "second": [1.0] * 5 + [None] * 5 + [1.0] * 5 + [0.0] * 5,
            "forward_return": [0.10] * 5 + [None] * 5 + [-0.10] * 10,
        }
    )

    candidates, _ = RuleSearchEngine(
        min_sample_count=4,
        min_outcome_coverage=0.90,
    ).search(
        dataset,
        dataset.copy(),
        ["first", "second"],
        max_conditions=2,
        top_n=20,
    )

    interaction = next(
        candidate
        for candidate in candidates
        if "first >= 1" in candidate.formula
        and "second >= 1" in candidate.formula
    )
    assert interaction.train_result.outcome_coverage_rate == 1.0
    assert interaction.train_result.baseline_outcome_coverage_rate == 1.0
    assert interaction.train_result.win_rate_lift == pytest.approx(2 / 3)
    assert all(
        candidate.formula != "first >= 1"
        for candidate in candidates
    )


def test_rule_search_enumerates_rare_discrete_sequence_states():
    dataset = pd.DataFrame(
        {
            "positive_days_3": [0.0] * 99 + [3.0],
        }
    )

    conditions = RuleSearchEngine(min_sample_count=1)._build_conditions(
        dataset,
        ["positive_days_3"],
    )

    assert "positive_days_3 >= 3" in [formula for formula, _ in conditions]
    assert dataset.query("positive_days_3 >= 3").index.tolist() == [99]


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("continuous >= 10", "quantile"),
        ("discrete >= 3", "observed_value"),
        ("(continuous >= 10) and (discrete >= 3)", "mixed"),
        ("missing >= 1", "unknown"),
    ],
)
def test_rule_search_records_the_actual_threshold_source(formula, expected):
    train = pd.DataFrame(
        {
            "continuous": list(range(20)),
            "discrete": [0.0] * 19 + [3.0],
        }
    )

    assert RuleSearchEngine._threshold_source(formula, train) == expected


def test_rule_search_ignores_missing_and_constant_factors():
    dataset = pd.DataFrame({"constant": [1.0, 1.0]})

    conditions = RuleSearchEngine(min_sample_count=2)._build_conditions(
        dataset,
        ["missing", "constant"],
    )

    assert conditions == []


def test_rule_search_retains_candidates_when_validation_factor_is_absent():
    train = pd.DataFrame(
        {
            "trade_date": [f"2026{index:04d}" for index in range(1, 41)],
            "factor": [0.0] * 20 + [1.0] * 20,
            "forward_return": [-0.10] * 20 + [0.10] * 20,
        }
    )
    validation = pd.DataFrame(
        {
            "trade_date": [f"2027{index:04d}" for index in range(1, 41)],
            "forward_return": [-0.10, 0.10] * 20,
        }
    )

    candidates, _ = RuleSearchEngine(min_sample_count=10).search(
        train,
        validation,
        ["factor"],
        max_conditions=1,
        top_n=10,
    )

    assert candidates
    assert all(candidate.val_result is not None for candidate in candidates)
    assert all(candidate.val_result.sample_count == 0 for candidate in candidates)
    positive_training_candidate = next(
        candidate
        for candidate in candidates
        if candidate.train_result.win_rate_lift > 0.0
    )
    assert (
        positive_training_candidate.validation_reason
        == "insufficient_validation_samples"
    )
    assert all(candidate.validation_passed is False for candidate in candidates)


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
    assert set(buckets) == {
        ("first", "<="),
        ("first", ">="),
        ("second", "<="),
        ("second", ">="),
    }
    assert len(conditions) == 20
    assert len(pairing_pool) == 8
    assert len(pairing_pool) > len(set(buckets))


@pytest.mark.parametrize(
    ("factor_count", "expected_direction_counts"),
    [(10, {2}), (13, {1, 2})],
)
def test_rule_search_fills_pairing_pool_with_alternate_directions(
    factor_count,
    expected_direction_counts,
):
    factor_names = [f"factor_{index}" for index in range(factor_count)]
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
    direction_counts = {
        field: len({
            formula.split()[1]
            for formula, candidate_field in pairing_pool
            if candidate_field == field
        })
        for field in factor_names
    }

    assert len(pairing_pool) == PAIRING_CANDIDATE_LIMIT
    assert set(direction_counts.values()) == expected_direction_counts


def test_rule_search_rejects_same_factor_conditions_in_the_same_direction():
    assert RuleSearchEngine._conditions_are_compatible(
        "value >= 10",
        "value",
        "value >= 20",
        "value",
    ) is False


def test_rule_search_prioritizes_factor_breadth_in_the_pairing_pool():
    factor_names = [
        f"factor_{index}"
        for index in range(len(DISCOVERY_FACTOR_FIELDS))
    ]
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
    assert PAIRING_CANDIDATE_LIMIT == len(DISCOVERY_FACTOR_FIELDS)
    assert {field for _, field in pairing_pool} == set(factor_names)


def test_rule_search_is_invariant_to_requested_factor_order():
    factor_names = [f"factor_{index:02d}" for index in range(3)]
    dataset = pd.DataFrame(
        {
            "trade_date": [f"2026{index:04d}" for index in range(1, 101)],
            "forward_return": [-0.10] * 50 + [0.10] * 50,
            **{factor: list(range(100)) for factor in factor_names},
        }
    )
    search = RuleSearchEngine(min_sample_count=10)

    forward, _ = search.search(
        dataset,
        dataset.copy(),
        factor_names,
        max_conditions=2,
        top_n=20,
    )
    reversed_order, _ = search.search(
        dataset,
        dataset.copy(),
        list(reversed(factor_names)),
        max_conditions=2,
        top_n=20,
    )

    assert [candidate.formula for candidate in forward] == [
        candidate.formula for candidate in reversed_order
    ]


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


def test_generalization_gap_tracks_relative_edge_across_market_regimes():
    train_result = BacktestResult(
        win_rate=0.70,
        mean_return=0.05,
        max_drawdown=-0.10,
        eval_time_ms=1,
        baseline_win_rate=0.50,
        win_rate_lift=0.20,
    )
    validation_result = BacktestResult(
        win_rate=0.50,
        mean_return=0.03,
        max_drawdown=-0.10,
        eval_time_ms=1,
        baseline_win_rate=0.30,
        win_rate_lift=0.20,
    )

    gap = RuleSearchEngine._lift_generalization_gap(
        train_result,
        validation_result,
    )

    assert gap == pytest.approx(0.0)


def test_support_retention_exposes_relative_applicability_collapse():
    validation = pd.DataFrame(
        {
            "trade_date": [f"2026{index:04d}" for index in range(1, 101)],
            "value": list(range(100)),
            "forward_return": [0.10] * 100,
        }
    )
    candidate = FactorHypothesis(
        formula="value >= 99",
        description="Rare validation rule",
        reasoning="Test evidence",
        train_result=BacktestResult(
            win_rate=0.60,
            mean_return=0.03,
            eval_time_ms=1,
            rule_support_rate=0.03,
            win_rate_lift=0.10,
        ),
    )

    validated = RuleSearchEngine(
        min_sample_count=0,
        min_trading_day_count=0,
        min_security_count=0,
        min_outcome_coverage=0.0,
    )._validate_candidates([candidate], validation)

    assert validated[0].support_rate_gap == pytest.approx(0.02)
    assert validated[0].support_retention_ratio == pytest.approx(1.0 / 3.0)


def test_validation_score_penalizes_uncertainty_and_lift_instability():
    validation_result = BacktestResult(
        win_rate=0.60,
        mean_return=0.03,
        max_drawdown=-0.10,
        eval_time_ms=1,
        baseline_win_rate=0.50,
        win_rate_lift=0.10,
        lift_standard_error=0.02,
    )

    score = RuleSearchEngine._conservative_validation_score(
        validation_result,
        generalization_gap=0.03,
    )

    assert score == pytest.approx(
        0.10 - 1.6448536269514722 * 0.02 - 0.03
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


def test_rule_search_rejects_many_events_from_one_security():
    dataset = pd.DataFrame(
        {
            "trade_date": [f"202601{i:02d}" for i in range(1, 21)],
            "ts_code": ["000001.SZ"] * 20,
            "factor": list(range(20)),
            "forward_return": [-0.10] * 10 + [0.10] * 10,
        }
    )

    candidates, evaluated_count = RuleSearchEngine(
        min_sample_count=4,
        min_trading_day_count=4,
        min_security_count=2,
    ).search(
        dataset,
        dataset.copy(),
        ["factor"],
        max_conditions=1,
        top_n=10,
    )

    assert evaluated_count > 0
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
    assert all(
        candidate.validation_passed
        == (candidate.validation_reason == "passed")
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

    harmonic_ten = sum(1.0 / rank for rank in range(1, 11))
    assert [candidate.q_value for candidate in candidates] == pytest.approx(
        [0.10 * harmonic_ten, 0.20 * harmonic_ten]
    )
    assert all(candidate.fdr_family_size == 10 for candidate in candidates)


def test_false_discovery_rate_rejects_an_incomplete_test_family():
    candidates = [
        FactorHypothesis(
            formula=f"value >= {index}",
            description="Test rule",
            reasoning="Test evidence",
            p_value=0.01,
        )
        for index in range(2)
    ]

    with pytest.raises(ValueError, match="FDR family cannot be smaller"):
        RuleSearchEngine._apply_false_discovery_rate(
            candidates,
            family_size=1,
        )


@pytest.mark.parametrize(
    (
        "train_lift",
        "train_outcome_lower",
        "validation_lift",
        "validation_outcome_lower",
        "q_value",
        "expected",
    ),
    [
        (-0.05, -0.05, 0.20, 0.20, 0.01, "training_lift_not_positive"),
        (
            0.05,
            0.0,
            0.20,
            0.20,
            0.01,
            "training_outcome_attrition_not_robust",
        ),
        (0.05, 0.05, -0.01, -0.01, 0.01, "validation_lift_not_positive"),
        (
            0.05,
            0.05,
            0.20,
            -0.01,
            0.01,
            "validation_outcome_attrition_not_robust",
        ),
        (0.05, 0.05, 0.20, 0.20, 0.11, "fdr_not_passed"),
        (0.05, 0.05, 0.20, 0.20, 0.10, "passed"),
    ],
)
def test_validation_reports_the_first_failed_replication_gate(
    train_lift,
    train_outcome_lower,
    validation_lift,
    validation_outcome_lower,
    q_value,
    expected,
):
    candidate = FactorHypothesis(
        formula="value >= 1",
        description="Test rule",
        reasoning="Test evidence",
        train_result=BacktestResult(
            win_rate=0.40,
            mean_return=-0.01,
            max_drawdown=-0.10,
            eval_time_ms=1,
            win_rate_lift=train_lift,
            outcome_robust_lift_lower=train_outcome_lower,
        ),
        val_result=BacktestResult(
            win_rate=0.70,
            mean_return=0.05,
            max_drawdown=-0.05,
            eval_time_ms=1,
            win_rate_lift=validation_lift,
            outcome_robust_lift_lower=validation_outcome_lower,
            trading_day_count=20,
            effective_trading_day_count=20.0,
        ),
        q_value=q_value,
    )

    search = RuleSearchEngine(
        min_sample_count=0,
        min_trading_day_count=0,
        min_security_count=0,
        min_outcome_coverage=0.0,
    )

    assert search._validation_reason(candidate) == expected


@pytest.mark.parametrize(
    ("result_update", "expected"),
    [
        ({"sample_count": 9}, "insufficient_validation_samples"),
        ({"trading_day_count": 4}, "insufficient_validation_days"),
        (
            {"effective_trading_day_count": 4.9},
            "insufficient_validation_effective_days",
        ),
        ({"security_count": 2}, "insufficient_validation_securities"),
        (
            {"effective_security_count": 2.9},
            "insufficient_validation_effective_securities",
        ),
        ({"outcome_coverage_rate": 0.89}, "insufficient_validation_coverage"),
        (
            {"baseline_outcome_coverage_rate": 0.89},
            "insufficient_validation_baseline_coverage",
        ),
    ],
)
def test_validation_reason_identifies_the_failed_evidence_threshold(
    result_update,
    expected,
):
    validation_result = BacktestResult(
        win_rate=0.60,
        mean_return=0.03,
        eval_time_ms=1,
        sample_count=10,
        trading_day_count=20,
        effective_trading_day_count=20.0,
        security_count=5,
        effective_security_count=5.0,
        outcome_coverage_rate=1.0,
        baseline_outcome_coverage_rate=1.0,
        win_rate_lift=0.10,
    ).model_copy(update=result_update)
    candidate = FactorHypothesis(
        formula="value >= 1",
        description="Test rule",
        reasoning="Test evidence",
        train_result=BacktestResult(
            win_rate=0.60,
            mean_return=0.03,
            eval_time_ms=1,
            win_rate_lift=0.10,
            outcome_robust_lift_lower=0.10,
        ),
        val_result=validation_result,
    )
    search = RuleSearchEngine(
        min_sample_count=10,
        min_trading_day_count=5,
        min_security_count=3,
        min_outcome_coverage=0.90,
    )

    assert search._validation_reason(candidate) == expected


def test_validation_keeps_training_ranked_candidates_with_insufficient_evidence():
    validation = pd.DataFrame(
        {
            "trade_date": [f"202601{index:02d}" for index in range(1, 21)],
            "ts_code": [f"{index:06d}.SZ" for index in range(1, 21)],
            "value": list(range(20)),
            "forward_return": [-0.10] * 10 + [0.10] * 10,
        }
    )
    candidates = [
        FactorHypothesis(
            formula="value >= 100",
            description="Training leader",
            reasoning="Test evidence",
            train_result=BacktestResult(
                win_rate=0.70,
                mean_return=0.05,
                eval_time_ms=1,
                sample_count=10,
                win_rate_lift=0.20,
                outcome_robust_lift_lower=0.20,
            ),
        ),
        FactorHypothesis(
            formula="value >= 10",
            description="Training runner-up",
            reasoning="Test evidence",
            train_result=BacktestResult(
                win_rate=0.60,
                mean_return=0.03,
                eval_time_ms=1,
                sample_count=10,
                win_rate_lift=0.10,
                outcome_robust_lift_lower=0.10,
            ),
        ),
    ]
    search = RuleSearchEngine(
        min_sample_count=5,
        min_trading_day_count=5,
        min_security_count=5,
    )

    validated = search._validate_candidates(candidates, validation)
    search._apply_false_discovery_rate(validated, family_size=len(candidates))
    for candidate in validated:
        candidate.validation_reason = search._validation_reason(candidate)

    assert [candidate.formula for candidate in validated] == [
        "value >= 100",
        "value >= 10",
    ]
    assert validated[0].val_result is not None
    assert validated[0].p_value == 1.0
    assert validated[0].validation_reason == "insufficient_validation_samples"


def test_validation_reason_reports_insufficient_effective_significance_days():
    candidate = FactorHypothesis(
        formula="value >= 1",
        description="Test rule",
        reasoning="Test evidence",
        train_result=BacktestResult(
            win_rate=0.60,
            mean_return=0.03,
            eval_time_ms=1,
            win_rate_lift=0.10,
            outcome_robust_lift_lower=0.10,
        ),
        val_result=BacktestResult(
            win_rate=0.60,
            mean_return=0.03,
            eval_time_ms=1,
            win_rate_lift=0.10,
            outcome_robust_lift_lower=0.10,
            trading_day_count=100,
            effective_trading_day_count=19.9,
        ),
        q_value=1.0,
    )

    assert (
        RuleSearchEngine(
            min_sample_count=0,
            min_trading_day_count=0,
            min_security_count=0,
            min_outcome_coverage=0.0,
        )._validation_reason(candidate)
        == "insufficient_significance_days"
    )


def test_validation_reason_reports_an_unevaluated_candidate():
    candidate = FactorHypothesis(
        formula="value >= 1",
        description="Test rule",
        reasoning="Test evidence",
    )

    assert (
        RuleSearchEngine(min_sample_count=0)._validation_reason(candidate)
        == "not_evaluated"
    )


def test_clustered_lift_significance_remains_finite_for_large_samples():
    probability = RuleSearchEngine._clustered_lift_tail_probability(
        0.06,
        0.01,
        100,
    )

    assert 0.0 <= probability <= 1.0
    assert probability < 0.001


def test_clustered_lift_significance_uses_finite_date_degrees_of_freedom():
    probability = RuleSearchEngine._clustered_lift_tail_probability(
        1.7291328115,
        1.0,
        20.9,
    )

    assert probability == pytest.approx(0.05, abs=1e-6)


@pytest.mark.parametrize(
    ("t_score", "degrees_freedom", "expected"),
    [
        (0.0, 5, 0.5),
        (1.0, 1, 0.25),
        (1.0, 2, 0.5 * (1.0 - 1.0 / 3.0**0.5)),
    ],
)
def test_student_t_survival_matches_closed_form_cases(
    t_score,
    degrees_freedom,
    expected,
):
    assert RuleSearchEngine._student_t_survival(
        t_score,
        degrees_freedom,
    ) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("lift", "standard_error"),
    [(0.10, 0.0), (-0.01, 0.01)],
)
def test_clustered_lift_significance_rejects_non_testable_edges(
    lift,
    standard_error,
):
    probability = RuleSearchEngine._clustered_lift_tail_probability(
        lift,
        standard_error,
        100,
    )

    assert probability == 1.0


def test_clustered_lift_significance_is_exploratory_below_twenty_days():
    probability = RuleSearchEngine._clustered_lift_tail_probability(
        0.20,
        0.01,
        19,
    )

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


@pytest.mark.parametrize(
    ("field", "message"),
    [
        (
            "minimum_trading_days",
            "Minimum trading days cannot exceed minimum samples",
        ),
        (
            "minimum_securities",
            "Minimum securities cannot exceed minimum samples",
        ),
    ],
)
def test_discovery_request_rejects_impossible_breadth_thresholds(field, message):
    request = {
        "target_pool": "A_SHARE",
        "train_start": "20250101",
        "train_end": "20251231",
        "val_start": "20260101",
        "val_end": "20260630",
        "factors": ["pe_ttm"],
        "minimum_samples": 5,
        "minimum_trading_days": 5,
        "minimum_securities": 5,
    }
    request[field] = 6

    with pytest.raises(ValueError, match=message):
        DiscoveryTaskRequest(**request)


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


def test_discovery_request_accepts_point_in_time_sequence_factors():
    request = DiscoveryTaskRequest(
        target_pool="A_SHARE",
        train_start="20250101",
        train_end="20251231",
        val_start="20260101",
        val_end="20260630",
        factors=[
            "distance_from_5d_peak_pct",
            "max_drawdown_5d_pct",
            "positive_days_3",
            "return_5d_pct",
            "volatility_5d_pct",
        ],
    )

    assert request.factors == [
        "distance_from_5d_peak_pct",
        "max_drawdown_5d_pct",
        "positive_days_3",
        "return_5d_pct",
        "volatility_5d_pct",
    ]


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
            minimum_securities=12,
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
    assert loaded.request.minimum_securities == 12
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
            minimum_securities=5,
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


def test_evolution_loop_rejects_an_analysis_task_without_mutating_it():
    class UnexpectedBacktester:
        def build_dataset(self, start_date, end_date, **kwargs):
            raise AssertionError("The backtester must not run for an analysis task.")

    store = MemoryAnalysisTaskStore()
    now = datetime.now(timezone.utc)
    task = AnalysisTask(
        task_id="analysis-task",
        status=AnalysisTaskStatus.QUEUED,
        request=AnalysisRequest(prompt="Show recent A-share prices."),
        created_at=now,
        updated_at=now,
    )
    store.put(task)

    with pytest.raises(
        TypeError,
        match="Task is not a discovery task: analysis-task",
    ):
        EvolutionLoop(store, UnexpectedBacktester()).run(task.task_id)

    persisted = store.get(task.task_id)
    assert isinstance(persisted, AnalysisTask)
    assert persisted.status == AnalysisTaskStatus.QUEUED
    assert persisted.error is None


def test_evolution_loop_explains_every_no_candidate_evidence_gate(monkeypatch):
    class EmptySearchBacktester:
        def build_dataset(self, start_date, end_date, **kwargs):
            return pd.DataFrame(
                {
                    "trade_date": ["20250102"],
                    "future_trade_date": ["20250103"],
                    "pe_ttm": [10.0],
                    "forward_return": [0.01],
                }
            )

    monkeypatch.setattr(
        RuleSearchEngine,
        "search",
        lambda self, *args, **kwargs: ([], 1),
    )
    store = MemoryAnalysisTaskStore()
    now = datetime.now(timezone.utc)
    task = DiscoveryTask(
        task_id="empty-discovery-loop",
        status=AnalysisTaskStatus.QUEUED,
        request=DiscoveryTaskRequest(
            target_pool="A_SHARE",
            train_start="20240101",
            train_end="20241231",
            val_start="20250101",
            val_end="20250630",
            factors=["pe_ttm"],
            minimum_samples=5,
            minimum_trading_days=5,
            minimum_securities=5,
        ),
        created_at=now,
        updated_at=now,
    )
    store.put(task)

    EvolutionLoop(store, EmptySearchBacktester()).run(task.task_id)

    failed = store.get(task.task_id)
    assert failed.status == AnalysisTaskStatus.FAILED
    assert "raw/effective trading-day breadth" in failed.error.message
    assert "raw/effective security breadth" in failed.error.message
    assert "selected/comparable-baseline outcome-coverage" in failed.error.message


def test_evolution_loop_persists_a_dataset_failure():
    class FailedBacktester:
        def build_dataset(self, start_date, end_date, **kwargs):
            raise RuntimeError("market snapshot unavailable")

    store = MemoryAnalysisTaskStore()
    now = datetime.now(timezone.utc)
    task = DiscoveryTask(
        task_id="failed-discovery-loop",
        status=AnalysisTaskStatus.QUEUED,
        request=DiscoveryTaskRequest(
            target_pool="A_SHARE",
            train_start="20250101",
            train_end="20251231",
            val_start="20260101",
            val_end="20260630",
            factors=["pe_ttm"],
        ),
        created_at=now,
        updated_at=now,
    )
    store.put(task)

    EvolutionLoop(store, FailedBacktester()).run(task.task_id)

    failed = store.get(task.task_id)
    assert failed.status == AnalysisTaskStatus.FAILED
    assert failed.progress.current_stage == "failed"
    assert failed.error.message == "market snapshot unavailable"


def test_evolution_loop_persists_a_missing_future_trade_date_failure():
    class MissingFutureDateBacktester:
        def build_dataset(self, start_date, end_date, **kwargs):
            return pd.DataFrame(
                {
                    "trade_date": ["20250102"],
                    "pe_ttm": [10.0],
                    "forward_return": [0.01],
                }
            )

    store = MemoryAnalysisTaskStore()
    now = datetime.now(timezone.utc)
    task = DiscoveryTask(
        task_id="missing-future-date-discovery-loop",
        status=AnalysisTaskStatus.QUEUED,
        request=DiscoveryTaskRequest(
            target_pool="A_SHARE",
            train_start="20250101",
            train_end="20251231",
            val_start="20260101",
            val_end="20260630",
            factors=["pe_ttm"],
        ),
        created_at=now,
        updated_at=now,
    )
    store.put(task)

    EvolutionLoop(store, MissingFutureDateBacktester()).run(task.task_id)

    failed = store.get(task.task_id)
    assert failed.status == AnalysisTaskStatus.FAILED
    assert failed.progress.current_stage == "failed"
    assert failed.error.message == "Training dataset is missing future_trade_date."

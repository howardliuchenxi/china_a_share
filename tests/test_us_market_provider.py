from datetime import datetime, timezone

import pandas as pd
import pytest

from china_a_share.core.errors import DataProviderError
from china_a_share.feishu_agent import ResearchToolbox
from china_a_share.providers.composite import CompositeMarketDataProvider
from china_a_share.providers.us_market import (
    FINNHUB_API_BASE_URL,
    MASSIVE_API_BASE_URL,
    USMarketCacheExpirationPolicy,
    USMarketDataProvider,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class SequenceGetSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class PassthroughCache:
    def get_or_fetch(
        self,
        provider,
        operation,
        params,
        fields,
        fetch,
        *,
        api_route,
        request_id,
        query_id,
    ):
        return fetch()


def query(provider, operation, params, fields=()):
    return provider.query(
        operation,
        params,
        fields,
        api_route="/test",
        request_id="request-1",
        query_id="query-1",
    )


def test_single_stock_daily_uses_massive_and_normalizes_rows():
    massive = SequenceGetSession(
        [
            FakeResponse(
                {
                    "status": "OK",
                    "results": [
                        {
                            "T": "AAPL",
                            "t": 1_789_099_200_000,
                            "o": 201.0,
                            "h": 205.0,
                            "l": 200.0,
                            "c": 204.0,
                            "v": 1_000,
                            "vw": 203.0,
                            "n": 100,
                        }
                    ],
                }
            )
        ]
    )
    finnhub = SequenceGetSession([])
    provider = USMarketDataProvider(
        "massive-key",
        "finnhub-key",
        PassthroughCache(),
        massive_session=massive,
        finnhub_session=finnhub,
    )

    frame = query(
        provider,
        "us_stock_daily",
        {"symbol": "aapl", "start_date": "2026-09-09", "end_date": "2026-09-09"},
    )

    assert frame.loc[0, "symbol"] == "AAPL"
    assert frame.loc[0, "close"] == 204.0
    assert frame.loc[0, "data_source"] == "massive"
    assert frame.attrs["provider"] == "massive"
    assert massive.calls[0][0].startswith(MASSIVE_API_BASE_URL)
    assert massive.calls[0][1]["params"]["adjusted"] == "true"
    assert massive.calls[0][1]["params"]["apiKey"] == "massive-key"
    assert finnhub.calls == []


def test_daily_result_preserves_actual_provider_when_source_column_is_not_selected():
    massive = SequenceGetSession(
        [
            FakeResponse(
                {
                    "status": "OK",
                    "results": [{"t": 1_789_099_200_000, "c": 204.0}],
                }
            )
        ]
    )
    provider = USMarketDataProvider(
        "massive-key",
        "",
        PassthroughCache(),
        massive_session=massive,
    )

    frame = query(
        provider,
        "us_stock_daily",
        {"symbol": "AAPL", "start_date": "2026-09-09", "end_date": "2026-09-09"},
        ["symbol", "date", "close"],
    )

    assert list(frame.columns) == ["symbol", "date", "close"]
    assert frame.attrs["provider"] == "massive"


@pytest.mark.parametrize("status_code", [403, 429, 500])
def test_single_stock_daily_falls_back_to_finnhub_on_quota_or_transient_failure(
    status_code,
):
    massive = SequenceGetSession(
        [FakeResponse({"status": "ERROR", "error": "quota"}, status_code=status_code)]
    )
    finnhub = SequenceGetSession(
        [
            FakeResponse(
                {
                    "s": "ok",
                    "t": [1_789_099_200],
                    "o": [201.0],
                    "h": [205.0],
                    "l": [200.0],
                    "c": [204.0],
                    "v": [1_000],
                }
            )
        ]
    )
    provider = USMarketDataProvider(
        "massive-key",
        "finnhub-key",
        PassthroughCache(),
        massive_session=massive,
        finnhub_session=finnhub,
    )

    frame = query(
        provider,
        "us_stock_daily",
        {"symbol": "AAPL", "start_date": "20260909", "end_date": "20260909"},
    )

    assert frame.loc[0, "data_source"] == "finnhub"
    assert frame.loc[0, "vwap"] is None
    assert frame.attrs["provider"] == "finnhub"
    assert finnhub.calls[0][0] == f"{FINNHUB_API_BASE_URL}/stock/candle"
    assert finnhub.calls[0][1]["params"]["resolution"] == "D"


def test_single_stock_daily_does_not_hide_invalid_massive_requests():
    massive = SequenceGetSession(
        [FakeResponse({"status": "ERROR", "error": "bad request"}, status_code=400)]
    )
    finnhub = SequenceGetSession([])
    provider = USMarketDataProvider(
        "massive-key",
        "finnhub-key",
        PassthroughCache(),
        massive_session=massive,
        finnhub_session=finnhub,
    )

    with pytest.raises(DataProviderError) as raised:
        query(
            provider,
            "us_stock_daily",
            {"symbol": "AAPL", "start_date": "20260909", "end_date": "20260909"},
        )

    assert raised.value.http_status == 400
    assert finnhub.calls == []


def test_local_massive_quota_uses_finnhub_without_a_sixth_massive_call():
    massive = SequenceGetSession(
        [FakeResponse({"status": "OK", "results": []}) for _ in range(5)]
    )
    finnhub = SequenceGetSession([FakeResponse({"s": "no_data"})])
    provider = USMarketDataProvider(
        "massive-key",
        "finnhub-key",
        PassthroughCache(),
        massive_session=massive,
        finnhub_session=finnhub,
        monotonic_clock=lambda: 10.0,
    )

    for day in range(1, 7):
        query(
            provider,
            "us_stock_daily",
            {
                "symbol": "AAPL",
                "start_date": f"2026-09-{day:02d}",
                "end_date": f"2026-09-{day:02d}",
            },
        )

    assert len(massive.calls) == 5
    assert len(finnhub.calls) == 1


def test_full_market_daily_never_degrades_to_per_symbol_finnhub_calls():
    massive = SequenceGetSession(
        [
            FakeResponse(
                {
                    "status": "OK",
                    "results": [
                        {"T": "AAPL", "o": 1, "h": 2, "l": 1, "c": 2, "v": 3}
                    ],
                }
            )
        ]
    )
    finnhub = SequenceGetSession([])
    provider = USMarketDataProvider(
        "massive-key",
        "finnhub-key",
        PassthroughCache(),
        massive_session=massive,
        finnhub_session=finnhub,
    )

    frame = query(provider, "us_market_daily", {"date": "2026-09-09"})

    assert frame.to_dict(orient="records")[0]["symbol"] == "AAPL"
    assert "/v2/aggs/grouped/locale/us/market/stocks/2026-09-09" in massive.calls[0][0]
    assert finnhub.calls == []


def test_finnhub_research_operations_are_normalized():
    finnhub = SequenceGetSession(
        [FakeResponse({"metric": {"peTTM": 25.5, "roeTTM": 0.42}})]
    )
    provider = USMarketDataProvider(
        "",
        "finnhub-key",
        PassthroughCache(),
        finnhub_session=finnhub,
    )

    frame = query(provider, "us_financial_metrics", {"symbol": "NVDA"})

    assert frame.to_dict(orient="records") == [
        {
            "symbol": "NVDA",
            "metric": "peTTM",
            "value": 25.5,
            "data_source": "finnhub",
        },
        {
            "symbol": "NVDA",
            "metric": "roeTTM",
            "value": 0.42,
            "data_source": "finnhub",
        },
    ]


def test_provider_rejects_unknown_fields_and_invalid_date_ranges():
    provider = USMarketDataProvider("massive-key", "", PassthroughCache())

    with pytest.raises(ValueError, match="Unsupported fields"):
        provider.validate_query(
            "us_stock_daily",
            {"symbol": "AAPL", "start_date": "2026-09-01", "end_date": "2026-09-02"},
            ["adjusted_close"],
        )
    with pytest.raises(ValueError, match="start_date"):
        provider.validate_query(
            "us_stock_daily",
            {"symbol": "AAPL", "start_date": "2026-09-03", "end_date": "2026-09-02"},
            ["close"],
        )


def test_research_toolbox_exposes_provider_native_operations_without_local_shapes():
    provider = USMarketDataProvider("massive-key", "finnhub-key", PassthroughCache())
    toolbox = ResearchToolbox(provider, "request-1")

    result = toolbox.call(
        "search_market_data",
        {"query": "Apple US stock daily prices"},
        lambda _stage, _message: None,
    )

    operations = {item["name"]: item for item in result["operations"]}
    assert "us_stock_daily" in operations
    assert operations["us_stock_daily"]["query_shapes"] == []


def test_cache_policy_keeps_historical_daily_data_longer_than_news():
    policy = USMarketCacheExpirationPolicy()
    fetched_at = datetime(2026, 9, 17, tzinfo=timezone.utc)

    daily_expiration = policy.resolve(
        "us_stock_daily",
        {"end_date": "2026-09-01"},
        fetched_at,
    )
    news_expiration = policy.resolve(
        "us_company_news",
        {"end_date": "2026-09-17"},
        fetched_at,
    )

    assert daily_expiration - fetched_at > news_expiration - fetched_at


class StubProvider:
    def __init__(self, name, operation):
        self._name = name
        self._operation = operation
        self.queries = []

    @property
    def name(self):
        return self._name

    def search_operations(self, prompt):
        from china_a_share.core.contracts import DataOperation

        return [DataOperation(name=self._operation, description=f"{prompt} daily data")]

    def supports(self, operation):
        return operation == self._operation

    def validate_query(self, operation, params, fields):
        assert operation == self._operation

    def query(self, operation, params, fields, **kwargs):
        self.queries.append((operation, params, fields, kwargs))
        return pd.DataFrame([{"value": 1}])


def test_composite_routes_by_operation_and_rejects_duplicate_ownership():
    first = StubProvider("first", "first_daily")
    second = StubProvider("second", "second_daily")
    provider = CompositeMarketDataProvider((first, second))

    frame = query(provider, "second_daily", {"symbol": "AAPL"}, ["value"])

    assert frame.loc[0, "value"] == 1
    assert first.queries == []
    assert len(second.queries) == 1

    with pytest.raises(ValueError, match="exposed by both"):
        CompositeMarketDataProvider(
            (StubProvider("first", "daily"), StubProvider("second", "daily"))
        )

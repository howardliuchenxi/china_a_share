import pandas as pd
import requests

from china_a_share.client import TushareTransport
from china_a_share.providers.tushare import TushareDataProvider


class FakeProApi:
    def __init__(self) -> None:
        self.calls = []

    def daily(self, **kwargs):
        self.calls.append(("daily", kwargs))
        return pd.DataFrame([{"ts_code": kwargs["ts_code"], "close": 10.0}])

    def stock_basic(self, **kwargs):
        self.calls.append(("stock_basic", kwargs))
        return pd.DataFrame([{"ts_code": "000001.SZ", "name": "Ping An Bank"}])


class FakeResponseCache:
    def __init__(self):
        self.calls = []

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
        self.calls.append(
            (
                provider,
                operation,
                params,
                fields,
                api_route,
                request_id,
                query_id,
            )
        )
        return pd.DataFrame([{"ts_code": "000001.SZ", "close": 10.5}])


class SuccessfulResponse:
    status_code = 200

    def json(self):
        return {
            "code": 0,
            "data": {
                "fields": ["ts_code", "close"],
                "items": [["000001.SZ", 10.5]],
            },
        }


class TransientSession:
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.calls < 3:
            raise requests.ConnectionError("connection interrupted")
        return SuccessfulResponse()


def test_daily_forwards_query_parameters():
    api = FakeProApi()
    client = TushareTransport("test-token", pro_api=api)

    result = client.daily("000001.SZ", "20240101", "20240131")

    assert len(result) == 1
    assert api.calls == [
        (
            "daily",
            {
                "ts_code": "000001.SZ",
                "start_date": "20240101",
                "end_date": "20240131",
            },
        )
    ]


def test_stock_basic_requests_analysis_fields():
    api = FakeProApi()
    client = TushareTransport("test-token", pro_api=api)

    result = client.stock_basic(exchange="SSE")

    assert result.iloc[0]["name"] == "Ping An Bank"
    assert api.calls[0][1]["exchange"] == "SSE"
    assert "industry" in api.calls[0][1]["fields"]


def test_tushare_transport_retries_transient_connection_failures(monkeypatch):
    session = TransientSession()
    monkeypatch.setattr("china_a_share.client.sleep", lambda _: None)
    client = TushareTransport(
        "test-token",
        pro_api=FakeProApi(),
        session=session,
    )

    result = client.query(
        "daily",
        {"trade_date": "20260717"},
        ["ts_code", "close"],
    )

    assert session.calls == 3
    assert result.to_dict(orient="records") == [
        {"ts_code": "000001.SZ", "close": 10.5}
    ]


def test_tushare_provider_delegates_to_provider_aware_cache():
    response_cache = FakeResponseCache()
    provider = TushareDataProvider(
        "test-token",
        response_cache,
        pro_api=FakeProApi(),
    )

    result = provider.query(
        "daily",
        {"trade_date": "20260717"},
        ["ts_code", "close"],
        api_route="/api/analysis",
        request_id="request-1",
        query_id="query-1",
    )

    assert result.iloc[0]["close"] == 10.5
    assert response_cache.calls == [
        (
            "tushare",
            "daily",
            {"trade_date": "20260717"},
            ["ts_code", "close"],
            "/api/analysis",
            "request-1",
            "query-1",
        )
    ]

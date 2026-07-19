import pandas as pd

from china_a_share.client import TushareClient
from china_a_share.config import Settings


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

    def get_or_fetch(self, api_name, params, fields, fetch):
        self.calls.append((api_name, params, fields))
        return pd.DataFrame([{"ts_code": "000001.SZ", "close": 10.5}])


def test_daily_forwards_query_parameters():
    api = FakeProApi()
    client = TushareClient(Settings("test-token"), pro_api=api)

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
    client = TushareClient(Settings("test-token"), pro_api=api)

    result = client.stock_basic(exchange="SSE")

    assert result.iloc[0]["name"] == "Ping An Bank"
    assert api.calls[0][1]["exchange"] == "SSE"
    assert "industry" in api.calls[0][1]["fields"]


def test_generic_query_delegates_to_configured_response_cache():
    response_cache = FakeResponseCache()
    client = TushareClient(
        Settings("test-token"),
        pro_api=FakeProApi(),
        response_cache=response_cache,
    )

    result = client.query(
        "daily",
        {"trade_date": "20260717"},
        ["ts_code", "close"],
    )

    assert result.iloc[0]["close"] == 10.5
    assert response_cache.calls == [
        (
            "daily",
            {"trade_date": "20260717"},
            ["ts_code", "close"],
        )
    ]

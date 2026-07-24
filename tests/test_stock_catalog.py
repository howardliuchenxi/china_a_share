import pandas as pd
import pytest

from china_a_share.application.stock_catalog import (
    STOCK_BASIC_FIELDS,
    StockCatalogService,
)
from china_a_share.client import TushareApiError


class FakeStockProvider:
    def __init__(self, frame=None, error=None):
        self.frame = frame
        self.error = error
        self.calls = []

    def query(
        self,
        operation,
        params,
        fields,
        *,
        api_route,
        request_id,
        query_id,
    ):
        self.calls.append(
            (
                operation,
                params,
                fields,
                api_route,
                request_id,
                query_id,
            )
        )
        if self.error is not None:
            raise self.error
        return self.frame.copy()


def stock_frame():
    return pd.DataFrame(
        [
            {
                "ts_code": "600519.SH",
                "symbol": "600519",
                "name": "Kweichow Moutai",
                "area": "Guizhou",
                "industry": "Beverages",
                "market": "Main Board",
                "exchange": "SSE",
                "list_date": "20010827",
            },
            {
                "ts_code": "000001.SZ",
                "symbol": "000001",
                "name": "Ping An Bank",
                "area": "Shenzhen",
                "industry": "Banking",
                "market": "Main Board",
                "exchange": "SZSE",
                "list_date": "19910403",
            },
            {
                "ts_code": "832982.BJ",
                "symbol": "832982",
                "name": "Jinbo Bio",
                "area": None,
                "industry": "Biotechnology",
                "market": "Beijing Stock Exchange",
                "exchange": "BSE",
                "list_date": "20230720",
            },
        ]
    )


def list_stocks(service, **overrides):
    parameters = {
        "page": 1,
        "page_size": 20,
        "search": "",
        "exchange": "",
        "industry": "",
        "api_route": "/api/stocks",
    }
    parameters.update(overrides)
    return service.list_stocks("request-1", **parameters)


def test_list_stocks_returns_deterministically_sorted_page():
    provider = FakeStockProvider(stock_frame())
    service = StockCatalogService(provider)

    response = list_stocks(service, page_size=2)

    assert response.total == 3
    assert response.total_pages == 2
    assert [item.code for item in response.items] == ["000001.SZ", "600519.SH"]
    assert response.items[0].listed_on.isoformat() == "1991-04-03"
    assert provider.calls == [
        (
            "stock_basic",
            {"list_status": "L"},
            STOCK_BASIC_FIELDS,
            "/api/stocks",
            "request-1",
            "stock-catalog",
        )
    ]


@pytest.mark.parametrize(
    ("filters", "expected_code"),
    [
        ({"search": "bank"}, "000001.SZ"),
        ({"exchange": "BSE"}, "832982.BJ"),
        ({"industry": "Beverages"}, "600519.SH"),
    ],
)
def test_list_stocks_filters_by_search_exchange_and_industry(
    filters,
    expected_code,
):
    service = StockCatalogService(FakeStockProvider(stock_frame()))

    response = list_stocks(service, **filters)

    assert response.total == 1
    assert [item.code for item in response.items] == [expected_code]


def test_list_stocks_returns_all_available_industries():
    service = StockCatalogService(FakeStockProvider(stock_frame()))

    response = list_stocks(service, exchange="BSE")

    assert response.available_industries == [
        "Banking",
        "Beverages",
        "Biotechnology",
    ]


def test_list_stocks_rejects_page_beyond_filtered_results():
    service = StockCatalogService(FakeStockProvider(stock_frame()))

    with pytest.raises(IndexError, match="page 2 exceeds the last available page 1"):
        list_stocks(service, page=2)


def test_list_stocks_preserves_provider_failure():
    provider_error = TushareApiError(
        message="Permission denied.",
        code=40203,
        http_status=200,
        raw_response={"code": 40203, "msg": "Permission denied."},
    )
    service = StockCatalogService(FakeStockProvider(error=provider_error))

    with pytest.raises(TushareApiError, match="Permission denied") as error_info:
        list_stocks(service)

    assert error_info.value.code == 40203
    assert error_info.value.raw_response == {
        "code": 40203,
        "msg": "Permission denied.",
    }

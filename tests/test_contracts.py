from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from china_a_share.core.contracts import (
    AnalysisImage,
    AnalysisRequest,
    AnalysisResponse,
    AnalysisStatus,
    DataCacheRecord,
    DataQuery,
    QueryPlan,
    QueryResult,
    QueryStatus,
    ServiceError,
)


def test_data_cache_record_serializes_successful_response():
    fetched_at = datetime(2026, 7, 17, 9, 10, tzinfo=timezone.utc)
    record = DataCacheRecord(
        provider="tushare",
        operation="daily",
        params={"trade_date": "20260717"},
        fields=["ts_code", "close"],
        fetched_at=fetched_at,
        expires_at=fetched_at + timedelta(hours=1),
        columns=["ts_code", "close"],
        rows=[["000001.SZ", 10.5]],
    )

    payload = record.model_dump(mode="json")

    assert payload["schema_version"] == 4
    assert payload["provider"] == "tushare"
    assert payload["operation"] == "daily"
    assert payload["params"] == {"trade_date": "20260717"}
    assert payload["fields"] == ["ts_code", "close"]
    assert payload["columns"] == ["ts_code", "close"]
    assert payload["rows"] == [["000001.SZ", 10.5]]


def test_data_cache_record_rejects_non_future_expiration():
    fetched_at = datetime(2026, 7, 17, 9, 10, tzinfo=timezone.utc)

    with pytest.raises(ValidationError, match="expires_at must be later"):
        DataCacheRecord(
            provider="tushare",
            operation="daily",
            fetched_at=fetched_at,
            expires_at=fetched_at,
        )


def test_data_cache_record_requires_timezone_aware_instants():
    fetched_at = datetime(2026, 7, 17, 9, 10)

    with pytest.raises(ValidationError):
        DataCacheRecord(
            provider="tushare",
            operation="daily",
            fetched_at=fetched_at,
            expires_at=fetched_at + timedelta(hours=1),
        )


def test_query_plan_is_fixed_to_a_share_market():
    query = DataQuery(
        query_id="daily_prices",
        operation="daily",
        params={"ts_code": "000001.SZ"},
        purpose="Retrieve daily prices.",
    )

    plan = QueryPlan(interpretation="Analyze Ping An Bank.", queries=[query])

    assert plan.market == "A_SHARE"
    with pytest.raises(ValidationError):
        QueryPlan(
            market="US_EQUITY",
            interpretation="Analyze another market.",
            queries=[query],
        )


def test_analysis_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        AnalysisRequest(prompt="Show bank stocks.", market="US")


def test_analysis_request_accepts_one_supported_screenshot():
    request = AnalysisRequest(
        prompt="Identify the security shown in this screenshot.",
        image=AnalysisImage(media_type="image/png", base64_data="c2NyZWVuc2hvdA=="),
    )

    assert request.image is not None
    assert request.image.media_type == "image/png"
    assert request.image.base64_data == "c2NyZWVuc2hvdA=="


def test_analysis_image_rejects_malformed_base64():
    with pytest.raises(ValidationError, match="valid Base64"):
        AnalysisImage(media_type="image/png", base64_data="not-base64")


def test_analysis_image_rejects_unsupported_media_type():
    with pytest.raises(ValidationError):
        AnalysisImage(media_type="image/gif", base64_data="Z2lm")


def test_upstream_error_preserves_safe_raw_response():
    raw_response = {"code": 2002, "msg": "No permission"}

    error = ServiceError(
        source="tushare",
        code=2002,
        message="No permission",
        http_status=200,
        raw_response=raw_response,
    )
    result = QueryResult(
        query_id="financials",
        provider="tushare",
        operation="fina_indicator",
        status=QueryStatus.ERROR,
        error=error,
    )

    assert result.error is not None
    assert result.error.raw_response == raw_response
    assert result.model_dump(mode="json")["error"]["code"] == 2002


def test_analysis_response_serializes_successful_table_result():
    query = DataQuery(
        query_id="daily_prices",
        operation="daily",
        params={"ts_code": "000001.SZ"},
        fields=["ts_code", "trade_date", "close"],
        purpose="Retrieve daily closing prices.",
    )
    response = AnalysisResponse(
        request_id="request-1",
        planner="deepseek",
        data_provider="tushare",
        status=AnalysisStatus.SUCCESS,
        plan=QueryPlan(interpretation="Retrieve closing prices.", queries=[query]),
        results=[
            QueryResult(
                query_id="daily_prices",
                provider="tushare",
                operation="daily",
                status=QueryStatus.SUCCESS,
                columns=["ts_code", "trade_date", "close"],
                rows=[
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260717",
                        "close": 10.5,
                    }
                ],
                row_count=1,
            )
        ],
    )

    payload = response.model_dump(mode="json")

    assert payload["status"] == "success"
    assert payload["plan"]["market"] == "A_SHARE"
    assert payload["results"][0]["rows"][0]["close"] == 10.5

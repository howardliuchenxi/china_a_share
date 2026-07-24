import json
import logging

from china_a_share.observability import StructuredLogFormatter, log_event


def test_structured_formatter_serializes_metric_fields() -> None:
    logger = logging.getLogger("observability-test")
    record = logging.LogRecord(
        name=logger.name,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="cache_lookup_completed",
        args=(),
        exc_info=None,
    )
    record.structured_fields = {
        "event": "cache_lookup_completed",
        "api_route": "/api/analysis",
        "outcome": "hit",
    }

    payload = json.loads(StructuredLogFormatter().format(record))

    assert payload == {
        "severity": "INFO",
        "logger": "observability-test",
        "message": "cache_lookup_completed",
        "event": "cache_lookup_completed",
        "api_route": "/api/analysis",
        "outcome": "hit",
    }


def test_log_event_preserves_fields_without_using_them_as_message(caplog) -> None:
    logger = logging.getLogger("event-test")

    with caplog.at_level(logging.INFO):
        log_event(
            logger,
            logging.INFO,
            "provider_call_completed",
            provider="tushare",
            operation="daily",
            duration_ms=12,
        )

    record = caplog.records[-1]
    assert record.getMessage() == "provider_call_completed"
    assert record.structured_fields["provider"] == "tushare"
    assert record.structured_fields["operation"] == "daily"
    assert record.structured_fields["duration_ms"] == 12

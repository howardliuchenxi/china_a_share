"""Structured application events for Google Cloud Logging."""

import json
import logging
from typing import Dict


STRUCTURED_FIELDS_ATTRIBUTE = "structured_fields"


class StructuredLogFormatter(logging.Formatter):
    """Serialize standard and application event fields as one JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        """Return one Cloud Logging-compatible JSON line for a log record."""
        payload: Dict[str, object] = {
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event_fields = getattr(record, STRUCTURED_FIELDS_ATTRIBUTE, None)
        if event_fields is not None:
            payload.update(event_fields)
        if record.exc_info:
            payload["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc_info: bool = False,
    **fields: object,
) -> None:
    """Emit one structured event through the configured process logger."""
    logger.log(
        level,
        event,
        extra={
            STRUCTURED_FIELDS_ATTRIBUTE: {
                "event": event,
                **fields,
            }
        },
        exc_info=exc_info,
    )

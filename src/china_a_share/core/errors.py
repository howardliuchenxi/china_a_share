"""Provider-neutral upstream failure contracts."""

from typing import Any, Dict, Optional, Union


class ExternalServiceError(RuntimeError):
    """Safe upstream failure raised by a planner, vision, or data provider."""

    def __init__(
        self,
        source: str,
        message: str,
        code: Optional[Union[int, str]] = None,
        http_status: Optional[int] = None,
        raw_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Preserve safe upstream details without storing credentials."""
        super().__init__(message)
        self.source = source
        self.code = code
        self.http_status = http_status
        self.raw_response = raw_response


class PlannerError(ExternalServiceError):
    """Failure raised while converting a user request into a query plan."""


class DataProviderError(ExternalServiceError):
    """Failure raised while retrieving data from a market-data provider."""


class VisionError(ExternalServiceError):
    """Failure raised while converting a screenshot into planning context."""

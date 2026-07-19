"""A-share analysis tools powered by Tushare."""

from .client import TushareClient
from .config import Settings
from .contracts import (
    AnalysisRequest,
    AnalysisResponse,
    AnalysisStatus,
    ConditionalCount,
    QueryPlan,
    QueryResult,
    QueryStatus,
    ServiceError,
    TushareQuery,
)

__all__ = [
    "AnalysisRequest",
    "AnalysisResponse",
    "AnalysisStatus",
    "ConditionalCount",
    "QueryPlan",
    "QueryResult",
    "QueryStatus",
    "ServiceError",
    "Settings",
    "TushareClient",
    "TushareQuery",
]

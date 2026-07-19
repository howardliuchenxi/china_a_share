"""Execution and local aggregation for validated Tushare queries."""

import logging
from typing import Dict

import pandas as pd

from .client import TushareApiError, TushareClient
from .contracts import QueryResult, QueryStatus, ServiceError, TushareQuery


logger = logging.getLogger(__name__)


class TushareQueryExecutor:
    """Execute one validated Tushare stock query and normalize its result."""

    def __init__(self, client: TushareClient) -> None:
        self.client = client

    def execute(self, query: TushareQuery) -> QueryResult:
        """Return a table result or a safe copy of the upstream error."""
        try:
            frame = self.client.query(query.api_name, query.params, query.fields)
            summary = self._aggregate(frame, query)
            # Object dtype is required so missing numeric values become JSON nulls
            # instead of non-standard NaN values in the browser response.
            safe_frame = frame.astype(object).where(pd.notnull(frame), None)
            return QueryResult(
                query_id=query.query_id,
                api_name=query.api_name,
                status=QueryStatus.SUCCESS,
                columns=list(safe_frame.columns),
                rows=safe_frame.to_dict(orient="records"),
                row_count=len(safe_frame),
                summary=summary,
            )
        except TushareApiError as exc:
            logger.warning(
                "query_failed query_id=%s api_name=%s source=tushare code=%s",
                query.query_id,
                query.api_name,
                exc.code,
            )
            return QueryResult(
                query_id=query.query_id,
                api_name=query.api_name,
                status=QueryStatus.ERROR,
                error=ServiceError(
                    source="tushare",
                    code=exc.code,
                    message=str(exc),
                    http_status=exc.http_status,
                    raw_response=exc.raw_response,
                ),
            )
        except Exception as exc:
            logger.exception(
                "query_failed query_id=%s api_name=%s source=system",
                query.query_id,
                query.api_name,
            )
            return QueryResult(
                query_id=query.query_id,
                api_name=query.api_name,
                status=QueryStatus.ERROR,
                error=ServiceError(source="system", message=str(exc)),
            )

    def _aggregate(self, frame: pd.DataFrame, query: TushareQuery) -> Dict[str, int]:
        summary: Dict[str, int] = {}
        operators = {
            "gt": lambda values, threshold: values > threshold,
            "ge": lambda values, threshold: values >= threshold,
            "eq": lambda values, threshold: values == threshold,
            "le": lambda values, threshold: values <= threshold,
            "lt": lambda values, threshold: values < threshold,
        }
        for aggregation in query.aggregations:
            if aggregation.field not in frame.columns:
                raise ValueError(
                    f"Aggregation field is missing from Tushare data: {aggregation.field}"
                )
            values = pd.to_numeric(frame[aggregation.field], errors="coerce")
            mask = operators[aggregation.operator](values, aggregation.value)
            summary[aggregation.label] = int(mask.fillna(False).sum())
        return summary

"""Deterministic execution of validated result transformation pipelines."""

from __future__ import annotations

from typing import Callable, Dict

import pandas as pd

from china_a_share.core.contracts import (
    QueryResult,
    QueryStatus,
    ResultPipeline,
    ResultPipelineStep,
)


COMPARISONS: Dict[str, Callable[[pd.Series, object], pd.Series]] = {
    "gt": lambda series, value: series > value,
    "ge": lambda series, value: series >= value,
    "eq": lambda series, value: series == value,
    "le": lambda series, value: series <= value,
    "lt": lambda series, value: series < value,
}


class ResultPipelineExecutor:
    """Apply a linear allowlisted plan to one in-memory query result."""

    def execute(
        self,
        pipeline: ResultPipeline,
        source: QueryResult,
    ) -> QueryResult:
        """Return one transformed result or fail fast on an invalid field contract."""
        frame = pd.DataFrame(source.rows)
        source_row_count = len(frame)
        for step in pipeline.steps:
            frame = self._execute_step(frame, step)
        rows = (
            frame.astype(object)
            .where(pd.notna(frame), None)
            .to_dict(orient="records")
        )
        return QueryResult(
            query_id=pipeline.output_query_id,
            provider=source.provider,
            operation="result_pipeline",
            status=QueryStatus.SUCCESS,
            columns=list(frame.columns),
            rows=rows,
            row_count=len(rows),
            summary={
                "source_row_count": source_row_count,
                "pipeline_step_count": len(pipeline.steps),
            },
        )

    def _execute_step(
        self,
        frame: pd.DataFrame,
        step: ResultPipelineStep,
    ) -> pd.DataFrame:
        """Execute one validated relational operation."""
        required_fields = set(step.fields + step.group_by)
        required_fields.update(
            field
            for field in (step.field, step.right_field, step.order_by)
            if field
        )
        missing_fields = required_fields.difference(frame.columns)
        if missing_fields:
            raise ValueError(
                f"{step.operation} fields are missing: "
                + ", ".join(sorted(missing_fields))
            )
        if step.operation == "latest_by_group":
            ascending = step.direction == "asc"
            return (
                frame.sort_values(
                    step.order_by,
                    ascending=ascending,
                    kind="mergesort",
                    na_position="last",
                )
                .drop_duplicates(step.group_by, keep="last" if ascending else "first")
                .reset_index(drop=True)
            )
        if step.operation == "derive":
            numeric = pd.to_numeric(frame[step.field], errors="coerce")
            scalar = float(step.value)
            if step.arithmetic_operator == "divide" and scalar == 0:
                raise ValueError("derive cannot divide by zero")
            operations = {
                "add": lambda: numeric + scalar,
                "subtract": lambda: numeric - scalar,
                "multiply": lambda: numeric * scalar,
                "divide": lambda: numeric / scalar,
                "constant_minus": lambda: scalar - numeric,
            }
            result = frame.copy()
            result[step.output_field] = operations[step.arithmetic_operator]()
            return result
        if step.operation == "drop_missing":
            return frame.dropna(subset=step.fields).reset_index(drop=True)
        if step.operation == "filter":
            series = frame[step.field]
            value: object = step.value
            if isinstance(step.value, (int, float)):
                series = pd.to_numeric(series, errors="coerce")
                value = float(step.value)
            return frame.loc[COMPARISONS[step.comparison](series, value)].reset_index(
                drop=True
            )
        if step.operation == "sort":
            return frame.sort_values(
                step.field,
                ascending=step.direction == "asc",
                kind="mergesort",
                na_position="last",
            ).reset_index(drop=True)
        if step.operation == "limit":
            return frame.head(step.count).reset_index(drop=True)
        if step.operation == "quantile_filter":
            numeric = pd.to_numeric(frame[step.field], errors="coerce")
            threshold = numeric.quantile(step.quantile)
            return frame.loc[
                COMPARISONS[step.comparison](numeric, threshold)
            ].reset_index(drop=True)
        if step.operation == "aggregate":
            named_aggregations = {
                aggregation.output_field: pd.NamedAgg(
                    column=aggregation.field,
                    aggfunc=aggregation.function,
                )
                for aggregation in step.aggregations
            }
            return (
                frame.groupby(step.group_by, dropna=False)
                .agg(**named_aggregations)
                .reset_index()
            )
        if step.operation == "rolling_mean":
            ordered = frame.sort_values(
                step.group_by + [step.order_by],
                kind="mergesort",
            ).copy()
            numeric = pd.to_numeric(ordered[step.field], errors="coerce")
            ordered[step.output_field] = (
                numeric.groupby(
                    [ordered[field] for field in step.group_by],
                    sort=False,
                    dropna=False,
                )
                .rolling(
                    window=step.window,
                    min_periods=step.min_periods or step.window,
                )
                .mean()
                .reset_index(level=list(range(len(step.group_by))), drop=True)
            )
            return ordered.reset_index(drop=True)
        if step.operation == "shift":
            ordered = frame.sort_values(
                step.group_by + [step.order_by],
                kind="mergesort",
            ).copy()
            grouped = ordered.groupby(
                step.group_by,
                sort=False,
                dropna=False,
            )
            shifted = grouped[step.field].shift(step.periods)
            if step.require_consecutive:
                order_values = sorted(ordered[step.order_by].dropna().unique())
                order_ranks = ordered[step.order_by].map(
                    {value: rank for rank, value in enumerate(order_values)}
                )
                shifted_ranks = order_ranks.groupby(
                    [ordered[field] for field in step.group_by],
                    sort=False,
                    dropna=False,
                ).shift(step.periods)
                shifted = shifted.where(
                    shifted_ranks - order_ranks == -step.periods
                )
            ordered[step.output_field] = shifted
            return ordered.reset_index(drop=True)
        if step.operation == "compare_fields":
            result = frame.copy()
            left = pd.to_numeric(result[step.field], errors="coerce")
            right = pd.to_numeric(result[step.right_field], errors="coerce")
            result[step.output_field] = COMPARISONS[step.comparison](left, right)
            return result
        if step.operation == "compare_scalar":
            result = frame.copy()
            series = result[step.field]
            value: object = step.value
            if isinstance(step.value, (int, float)):
                series = pd.to_numeric(series, errors="coerce")
                value = float(step.value)
            result[step.output_field] = COMPARISONS[step.comparison](
                series,
                value,
            )
            return result
        if step.operation == "summarize":
            row = {
                aggregation.output_field: getattr(
                    pd.to_numeric(
                        frame[aggregation.field],
                        errors="coerce",
                    ),
                    aggregation.function,
                )()
                for aggregation in step.aggregations
            }
            return pd.DataFrame([row])
        raise ValueError(f"Unsupported result pipeline operation: {step.operation}")

"""Deterministic execution of validated result transformation pipelines."""

from __future__ import annotations

from typing import Callable, Dict, Mapping, Optional

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
        sources: Optional[Mapping[str, QueryResult]] = None,
    ) -> QueryResult:
        """Return one transformed result or fail fast on an invalid field contract."""
        frame = pd.DataFrame(source.rows)
        source_row_count = len(frame)
        source_results = dict(sources or {})
        source_results.setdefault(source.query_id, source)
        for step in pipeline.steps:
            frame = self._execute_step(frame, step, source_results)
        rows = (
            frame.astype(object)
            .where(pd.notna(frame), None)
            .to_dict(orient="records")
        )
        summary = {
            "source_row_count": source_row_count,
            "pipeline_step_count": len(pipeline.steps),
        }
        summarize_step = next(
            (
                step
                for step in reversed(pipeline.steps)
                if step.operation == "summarize"
            ),
            None,
        )
        if summarize_step is not None and len(frame) == 1:
            summary = {
                aggregation.label or aggregation.output_field: (
                    None
                    if pd.isna(frame.iloc[0][aggregation.output_field])
                    else frame.iloc[0][aggregation.output_field]
                )
                for aggregation in summarize_step.aggregations
            }
        return QueryResult(
            query_id=pipeline.output_query_id,
            provider=source.provider,
            operation="result_pipeline",
            status=QueryStatus.SUCCESS,
            columns=list(frame.columns),
            rows=rows,
            row_count=len(rows),
            summary=summary,
        )

    def _execute_step(
        self,
        frame: pd.DataFrame,
        step: ResultPipelineStep,
        sources: Mapping[str, QueryResult],
    ) -> pd.DataFrame:
        """Execute one validated relational operation."""
        required_fields = set(step.fields + step.group_by + step.join_on)
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
            operand = (
                pd.to_numeric(frame[step.right_field], errors="coerce")
                if step.right_field
                else float(step.value)
            )
            if step.arithmetic_operator == "divide":
                if (
                    (operand == 0).any()
                    if isinstance(operand, pd.Series)
                    else operand == 0
                ):
                    raise ValueError("derive cannot divide by zero")
            operations = {
                "add": lambda: numeric + operand,
                "subtract": lambda: numeric - operand,
                "multiply": lambda: numeric * operand,
                "divide": lambda: numeric / operand,
                "constant_minus": lambda: operand - numeric,
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
        if step.operation in {"rolling_mean", "rolling_sum"}:
            ordered = frame.sort_values(
                step.group_by + [step.order_by],
                kind="mergesort",
            ).copy()
            numeric = pd.to_numeric(ordered[step.field], errors="coerce")
            rolling = (
                numeric.groupby(
                    [ordered[field] for field in step.group_by],
                    sort=False,
                    dropna=False,
                )
                .rolling(
                    window=step.window,
                    min_periods=step.min_periods or step.window,
                )
            )
            aggregated = (
                rolling.mean()
                if step.operation == "rolling_mean"
                else rolling.sum()
            )
            ordered[step.output_field] = aggregated.reset_index(
                level=list(range(len(step.group_by))),
                drop=True,
            )
            return ordered.reset_index(drop=True)
        if step.operation == "match_source":
            right_source = sources.get(step.right_source_query_id)
            if right_source is None:
                raise ValueError(
                    "match_source query result is unavailable: "
                    f"{step.right_source_query_id}"
                )
            if right_source.status != QueryStatus.SUCCESS:
                raise ValueError(
                    "match_source query did not succeed: "
                    f"{step.right_source_query_id}"
                )
            right = pd.DataFrame(right_source.rows)
            missing_right = set(step.join_on).difference(right.columns)
            if missing_right:
                raise ValueError(
                    "match_source right fields are missing: "
                    + ", ".join(sorted(missing_right))
                )
            if step.output_field in frame.columns:
                raise ValueError(
                    f"match_source output field already exists: {step.output_field}"
                )
            marker = right[step.join_on].drop_duplicates().copy()
            marker[step.output_field] = True
            matched = frame.merge(
                marker,
                on=step.join_on,
                how="left",
                sort=False,
                validate="many_to_one",
            )
            matched[step.output_field] = matched[step.output_field].notna()
            return matched
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
        if step.operation == "match_at_offset":
            ordered = frame.sort_values(
                step.group_by + [step.order_by],
                kind="mergesort",
            ).copy()
            ordered_dates = pd.to_datetime(
                ordered[step.order_by],
                format="%Y%m%d",
                errors="coerce",
            )
            market_dates = pd.Index(sorted(ordered_dates.dropna().unique()))
            if step.offset_unit == "trading_session":
                market_ranks = ordered_dates.map(
                    {value: index for index, value in enumerate(market_dates)}
                )
                target_ranks = market_ranks + step.offset_value
                ordered["_target_date"] = target_ranks.map(
                    {
                        index: value
                        for index, value in enumerate(market_dates)
                    }
                )
            else:
                offsets = {
                    "day": pd.DateOffset(days=step.offset_value),
                    "week": pd.DateOffset(weeks=step.offset_value),
                    "month": pd.DateOffset(months=step.offset_value),
                    "year": pd.DateOffset(years=step.offset_value),
                }
                requested_dates = ordered_dates + offsets[step.offset_unit]
                ordered["_target_date"] = requested_dates.map(
                    lambda value: next(
                        (candidate for candidate in market_dates if candidate >= value),
                        pd.NaT,
                    )
                )
            ordered[step.output_field] = None
            ordered[step.matched_date_output_field] = None
            for _, indexes in ordered.groupby(
                step.group_by,
                sort=False,
                dropna=False,
            ).groups.items():
                group_indexes = list(indexes)
                group_date_indexes = {
                    value: index
                    for index, value in ordered_dates.loc[group_indexes].items()
                }
                for row_index in group_indexes:
                    target = ordered.at[row_index, "_target_date"]
                    match_index = group_date_indexes.get(target)
                    if match_index is not None:
                        ordered.at[row_index, step.output_field] = ordered.at[
                            match_index,
                            step.field,
                        ]
                        ordered.at[
                            row_index,
                            step.matched_date_output_field,
                        ] = target.strftime("%Y%m%d")
            return ordered.drop(columns=["_target_date"]).reset_index(drop=True)
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

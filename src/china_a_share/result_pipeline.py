"""Deterministic execution of validated result transformation pipelines."""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Set, Tuple

import pandas as pd

from china_a_share.core.contracts import (
    CalculationTraceStep,
    ColumnCalculationMetadata,
    QueryResult,
    QueryStatus,
    ResultPipeline,
    ResultPipelineStep,
    SummaryMetricMetadata,
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
        frame = self._result_frame(source)
        source_row_count = len(frame)
        source_results = dict(sources or {})
        source_results.setdefault(source.query_id, source)
        summary_input_frame: Optional[pd.DataFrame] = None
        for step in pipeline.steps:
            if step.operation == "summarize":
                summary_input_frame = frame.copy()
            frame = self._execute_step(frame, step, source_results)
        self._validate_result_invariants(pipeline, frame)
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
        summary_metadata = {}
        formulas, dependencies = self._build_field_lineage(pipeline)
        trace = [
            self._build_trace_step(step)
            for step in pipeline.steps
            if step.operation != "summarize"
        ]
        if summarize_step is not None:
            summary_metadata = {
                aggregation.label or aggregation.output_field: SummaryMetricMetadata(
                    output_field=aggregation.output_field,
                    source_field=aggregation.field,
                    function=aggregation.function,
                    value_format=self._summary_value_format(aggregation.output_field),
                    formula=(
                        f"{aggregation.function}("
                        f"{formulas.get(aggregation.field, aggregation.field)})"
                    ),
                    source_fields=sorted(
                        dependencies.get(aggregation.field, {aggregation.field})
                    ),
                    calculation_steps=trace,
                    initial_sample_count=source_row_count,
                    valid_sample_count=(
                        int(summary_input_frame[aggregation.field].notna().sum())
                        if summary_input_frame is not None
                        and aggregation.field in summary_input_frame.columns
                        else None
                    ),
                )
                for aggregation in summarize_step.aggregations
            }
        column_metadata = {
            column: ColumnCalculationMetadata(
                formula=formulas[column],
                source_fields=sorted(dependencies.get(column, {column})),
                calculation_steps=self._steps_through_output(trace, column),
                value_format=self._summary_value_format(column),
            )
            for column in frame.columns
            if column in formulas
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
            summary_metadata=summary_metadata,
            column_metadata=column_metadata,
        )

    @staticmethod
    def _result_frame(result: QueryResult) -> pd.DataFrame:
        """Preserve a declared field contract when a result contains no rows."""
        return pd.DataFrame(result.rows, columns=result.columns or None)

    @staticmethod
    def _steps_through_output(
        trace: list[CalculationTraceStep],
        output_field: str,
    ) -> list[CalculationTraceStep]:
        """Return the executed trace through the step that produced one field."""
        producing_index = next(
            (
                index
                for index, step in enumerate(trace)
                if output_field in step.output_fields
            ),
            len(trace) - 1,
        )
        return trace[: producing_index + 1]

    @staticmethod
    def _build_trace_step(step: ResultPipelineStep) -> CalculationTraceStep:
        """Expose only validated parameters from one executed pipeline step."""
        input_fields = [
            field
            for field in [step.field, step.right_field, step.order_by]
            if field
        ]
        input_fields.extend(step.group_by)
        input_fields.extend(step.join_on)
        if isinstance(step.fields, list):
            input_fields.extend(step.fields)
        elif isinstance(step.fields, dict):
            input_fields.extend(step.fields.keys())
        output_fields = [
            field
            for field in [step.output_field, step.matched_date_output_field]
            if field
        ]
        if isinstance(step.fields, dict):
            output_fields.extend(step.fields.values())
        excluded = {
            "operation",
            "field",
            "right_field",
            "order_by",
            "group_by",
            "join_on",
            "fields",
            "output_field",
            "matched_date_output_field",
            "aggregations",
        }
        parameters = {
            key: value
            for key, value in step.model_dump(exclude_none=True).items()
            if key not in excluded and value not in ([], {}, False)
        }
        return CalculationTraceStep(
            operation=step.operation,
            input_fields=list(dict.fromkeys(input_fields)),
            output_fields=list(dict.fromkeys(output_fields)),
            parameters=parameters,
        )

    @staticmethod
    def _build_field_lineage(
        pipeline: ResultPipeline,
    ) -> Tuple[Dict[str, str], Dict[str, Set[str]]]:
        """Build formulas and leaf dependencies from validated pipeline operations."""
        formulas: Dict[str, str] = {}
        dependencies: Dict[str, Set[str]] = {}

        def expression(field: str) -> str:
            return formulas.get(field, field)

        def sources(field: str) -> Set[str]:
            return dependencies.get(field, {field})

        binary_symbols = {
            "add": "+",
            "subtract": "-",
            "multiply": "*",
            "divide": "/",
        }
        comparison_symbols = {
            "gt": ">",
            "ge": ">=",
            "eq": "=",
            "le": "<=",
            "lt": "<",
        }
        for step in pipeline.steps:
            if step.operation == "select_fields":
                selected = set(step.fields)
                formulas = {
                    field: formula
                    for field, formula in formulas.items()
                    if field in selected
                }
                dependencies = {
                    field: field_dependencies
                    for field, field_dependencies in dependencies.items()
                    if field in selected
                }
            elif step.operation == "rename_fields" and isinstance(step.fields, dict):
                prior_formulas = dict(formulas)
                prior_dependencies = dict(dependencies)
                renamed_formulas = {
                    output_field: prior_formulas.get(
                        source_field,
                        source_field,
                    )
                    for source_field, output_field in step.fields.items()
                }
                renamed_dependencies = {
                    output_field: prior_dependencies.get(
                        source_field,
                        {source_field},
                    )
                    for source_field, output_field in step.fields.items()
                }
                for source_field in step.fields:
                    formulas.pop(source_field, None)
                    dependencies.pop(source_field, None)
                formulas.update(renamed_formulas)
                dependencies.update(renamed_dependencies)
            elif step.operation == "derive" and step.output_field and step.field:
                right = expression(step.right_field) if step.right_field else str(step.value)
                left = expression(step.field)
                if step.arithmetic_operator == "constant_minus":
                    formulas[step.output_field] = f"({step.value} - {left})"
                else:
                    formulas[step.output_field] = (
                        f"({left} {binary_symbols[step.arithmetic_operator]} {right})"
                    )
                dependencies[step.output_field] = sources(step.field) | (
                    sources(step.right_field) if step.right_field else set()
                )
            elif step.operation in {"compare_scalar", "compare_fields"} and step.output_field and step.field:
                right = expression(step.right_field) if step.right_field else str(step.value)
                formulas[step.output_field] = (
                    f"({expression(step.field)} {comparison_symbols[step.comparison]} {right})"
                )
                dependencies[step.output_field] = sources(step.field) | (
                    sources(step.right_field) if step.right_field else set()
                )
            elif step.operation in {
                "rolling_mean",
                "rolling_sum",
                "rolling_min",
                "rolling_max",
                "rolling_std",
            } and step.output_field and step.field:
                function = step.operation
                formulas[step.output_field] = f"{function}({expression(step.field)}, {step.window})"
                dependencies[step.output_field] = sources(step.field)
            elif step.operation in {"shift", "diff", "pct_change"} and step.output_field and step.field:
                formulas[step.output_field] = (
                    f"{step.operation}({expression(step.field)}, {step.periods})"
                )
                dependencies[step.output_field] = sources(step.field)
            elif step.operation in {"rank", "dense_rank"} and step.output_field and step.field:
                formulas[step.output_field] = (
                    f"{step.operation}({expression(step.field)})"
                )
                dependencies[step.output_field] = sources(step.field)
            elif step.operation == "match_at_offset" and step.output_field and step.field:
                formulas[step.output_field] = (
                    f"match_at_offset({expression(step.field)}, "
                    f"{step.offset_value} {step.offset_unit})"
                )
                dependencies[step.output_field] = sources(step.field)
                if step.matched_date_output_field:
                    formulas[step.matched_date_output_field] = (
                        f"matched_date({step.offset_value} {step.offset_unit})"
                    )
                    dependencies[step.matched_date_output_field] = {
                        step.order_by
                    }
            elif step.operation in {"match_source", "exists_in_source"} and step.output_field:
                formulas[step.output_field] = (
                    f"{step.operation}({step.right_source_query_id}, "
                    f"{', '.join(step.join_on)})"
                )
                dependencies[step.output_field] = set(step.join_on)
            elif step.operation in {"join_fields", "inner_join"} and isinstance(step.fields, dict):
                for source_field, output_field in step.fields.items():
                    formulas[output_field] = f"{step.right_source_query_id}.{source_field}"
                    dependencies[output_field] = {source_field}
            elif step.operation in {"aggregate", "summarize"}:
                for aggregation in step.aggregations:
                    formulas[aggregation.output_field] = (
                        f"{aggregation.function}("
                        f"{expression(aggregation.field)}"
                        f"{', ' + str(aggregation.quantile) if aggregation.quantile is not None else ''})"
                    )
                    dependencies[aggregation.output_field] = sources(
                        aggregation.field
                    )
        return formulas, dependencies

    @staticmethod
    def _summary_value_format(output_field: str) -> str:
        """Return explicit value semantics for common metric naming contracts."""
        if output_field.endswith("_pct"):
            return "percentage_points"
        if output_field.endswith("_ratio"):
            return "ratio"
        return "number"

    def _validate_result_invariants(
        self,
        pipeline: ResultPipeline,
        frame: pd.DataFrame,
    ) -> None:
        """Enforce strict post-execution invariant verification for rankings and metrics."""
        limit_step = next((s for s in pipeline.steps if s.operation == "limit"), None)
        if limit_step is not None and limit_step.count is not None:
            if len(frame) > limit_step.count:
                raise ValueError(
                    f"Result row count ({len(frame)}) exceeds requested limit ({limit_step.count})."
                )

        sort_indexes = [
            index
            for index, step in enumerate(pipeline.steps)
            if step.operation == "sort"
        ]
        sort_step = None
        if sort_indexes:
            sort_index = sort_indexes[-1]
            if all(
                step.operation == "limit"
                for step in pipeline.steps[sort_index + 1 :]
            ):
                sort_step = pipeline.steps[sort_index]
        if sort_step is not None and sort_step.field is not None:
            field = sort_step.field
            if field in frame.columns and not frame.empty:
                series = pd.to_numeric(frame[field], errors="coerce")
                if series.isna().any():
                    raise ValueError(
                        f"Ranking field '{field}' contains invalid or missing (NaN) values in ranking results."
                    )
                if series.isin([float("inf"), float("-inf")]).any():
                    raise ValueError(
                        f"Ranking field '{field}' contains non-finite values in ranking results."
                    )
                ascending = sort_step.direction == "asc"
                if ascending:
                    if not series.is_monotonic_increasing:
                        raise ValueError(
                            f"Ranking field '{field}' is not sorted in monotonic increasing (asc) order."
                        )
                else:
                    if not series.is_monotonic_decreasing:
                        raise ValueError(
                            f"Ranking field '{field}' is not sorted in monotonic decreasing (desc) order."
                        )
                
                # Prevent silent division/derivation errors where all returns are identical (e.g., all 0.0)
                if len(series) >= 2 and series.nunique() == 1:
                    if "start_close" in frame.columns and "end_close" in frame.columns:
                        if (frame["start_close"] == frame["end_close"]).all():
                            raise ValueError(
                                "Ranking failed: all calculated return values are exactly 0.0 "
                                "because starting close and ending close are identical (derived from same source field)."
                            )

    def _execute_step(
        self,
        frame: pd.DataFrame,
        step: ResultPipelineStep,
        sources: Mapping[str, QueryResult],
    ) -> pd.DataFrame:
        """Execute one validated relational operation."""
        fields_list = (
            []
            if step.operation in {"join_fields", "inner_join", "union_all"}
            else (
                list(step.fields.keys())
                if isinstance(step.fields, dict)
                else list(step.fields)
            )
        )
        required_fields = set(fields_list + step.group_by + step.join_on)
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
        if step.operation == "select_fields":
            return frame.loc[:, list(step.fields)].copy().reset_index(drop=True)
        if step.operation == "rename_fields":
            return frame.rename(columns=step.fields).reset_index(drop=True)
        if step.operation == "distinct":
            return frame.drop_duplicates(
                subset=list(step.fields),
                keep=step.keep,
            ).reset_index(drop=True)
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
        if step.operation == "filter_set":
            series = frame[step.field]
            values: list[Any] = list(step.values)
            if values and all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in values
            ):
                series = pd.to_numeric(series, errors="coerce")
                values = [float(value) for value in values]
            mask = series.isin(values)
            return frame.loc[~mask if step.negate else mask].reset_index(drop=True)
        if step.operation == "filter_range":
            numeric = pd.to_numeric(frame[step.field], errors="coerce")
            mask = numeric.between(step.lower_value, step.upper_value, inclusive="both")
            return frame.loc[~mask if step.negate else mask].reset_index(drop=True)
        if step.operation == "filter_null":
            mask = frame[step.field].isna()
            return frame.loc[~mask if step.negate else mask].reset_index(drop=True)
        if step.operation == "having":
            series = frame[step.field]
            value: object = step.value
            if isinstance(step.value, (int, float)):
                series = pd.to_numeric(series, errors="coerce")
                value = float(step.value)
            mask = COMPARISONS[step.comparison](series, value)
            return frame.loc[mask.fillna(False)].reset_index(drop=True)
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
            named_aggregations = self._named_aggregations(step)
            return (
                frame.groupby(step.group_by, dropna=False)
                .agg(**named_aggregations)
                .reset_index()
            )
        if step.operation in {
            "rolling_mean",
            "rolling_sum",
            "rolling_min",
            "rolling_max",
            "rolling_std",
        }:
            ordered = frame.sort_values(
                step.group_by + [step.order_by],
                kind="mergesort",
            ).reset_index(drop=True)
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
            aggregated = getattr(rolling, step.operation.removeprefix("rolling_"))()
            aggregated = aggregated.reset_index(
                level=list(range(len(step.group_by))),
                drop=True,
            )
            if step.require_consecutive:
                # A complete row window is not necessarily a consecutive market
                # window when one security has a missing or suspended session.
                market_order = sorted(ordered[step.order_by].dropna().unique())
                order_ranks = ordered[step.order_by].map(
                    {value: rank for rank, value in enumerate(market_order)}
                )
                group_keys = [
                    ordered[field] for field in step.group_by
                ]
                first_window_rank = order_ranks.groupby(
                    group_keys,
                    sort=False,
                    dropna=False,
                ).shift(step.window - 1)
                aggregated = aggregated.where(
                    order_ranks - first_window_rank == step.window - 1
                )
            ordered[step.output_field] = aggregated
            return ordered.reset_index(drop=True)
        if step.operation in {"rank", "dense_rank"}:
            result = frame.copy()
            numeric = pd.to_numeric(result[step.field], errors="coerce")
            method = "dense" if step.operation == "dense_rank" else step.rank_method
            if step.group_by:
                result[step.output_field] = numeric.groupby(
                    [result[field] for field in step.group_by],
                    sort=False,
                    dropna=False,
                ).rank(method=method, ascending=step.direction == "asc")
            else:
                result[step.output_field] = numeric.rank(
                    method=method,
                    ascending=step.direction == "asc",
                )
            return result
        if step.operation == "top_k_by_group":
            return (
                frame.sort_values(
                    step.group_by + [step.field],
                    ascending=[True] * len(step.group_by)
                    + [step.direction == "asc"],
                    kind="mergesort",
                    na_position="last",
                )
                .groupby(step.group_by, sort=False, dropna=False)
                .head(step.count)
                .reset_index(drop=True)
            )
        if step.operation in {"match_source", "exists_in_source"}:
            right_source = sources.get(step.right_source_query_id)
            if right_source is None:
                raise ValueError(
                    f"{step.operation} query result is unavailable: "
                    f"{step.right_source_query_id}"
                )
            if right_source.status != QueryStatus.SUCCESS:
                raise ValueError(
                    f"{step.operation} query did not succeed: "
                    f"{step.right_source_query_id}"
                )
            right = self._result_frame(right_source)
            missing_right = set(step.join_on).difference(right.columns)
            if missing_right:
                raise ValueError(
                    f"{step.operation} right fields are missing: "
                    + ", ".join(sorted(missing_right))
                )
            if step.output_field in frame.columns:
                raise ValueError(
                    f"{step.operation} output field already exists: {step.output_field}"
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
        if step.operation in {"semi_join", "anti_join"}:
            right_source = sources.get(step.right_source_query_id)
            if right_source is None or right_source.status != QueryStatus.SUCCESS:
                raise ValueError(f"{step.operation} right source is unavailable")
            right = self._result_frame(right_source)
            missing_right = set(step.join_on).difference(right.columns)
            if missing_right:
                raise ValueError(
                    f"{step.operation} right fields are missing: "
                    + ", ".join(sorted(missing_right))
                )
            keys = right[step.join_on].drop_duplicates()
            matched = frame.merge(
                keys.assign(_source_member=True),
                on=step.join_on,
                how="left",
                sort=False,
                validate="many_to_one",
            )
            mask = matched.pop("_source_member").notna()
            if step.operation == "anti_join":
                mask = ~mask
            return matched.loc[mask].reset_index(drop=True)
        if step.operation == "inner_join":
            right_source = sources.get(step.right_source_query_id)
            if right_source is None or right_source.status != QueryStatus.SUCCESS:
                raise ValueError("inner_join right source is unavailable")
            right = self._result_frame(right_source)
            fields_map = step.fields if isinstance(step.fields, dict) else {}
            missing_right = set(step.join_on).union(fields_map).difference(right.columns)
            if missing_right:
                raise ValueError(
                    "inner_join right fields are missing: "
                    + ", ".join(sorted(missing_right))
                )
            right_subset = right[list(step.join_on) + list(fields_map)].rename(
                columns=fields_map
            )
            collisions = set(fields_map.values()).intersection(frame.columns)
            if collisions:
                raise ValueError(
                    "inner_join output fields already exist: "
                    + ", ".join(sorted(collisions))
                )
            return frame.merge(
                right_subset,
                on=step.join_on,
                how="inner",
                sort=False,
                validate=step.cardinality,
            ).reset_index(drop=True)
        if step.operation == "union_all":
            right_source = sources.get(step.right_source_query_id)
            if right_source is None or right_source.status != QueryStatus.SUCCESS:
                raise ValueError("union_all right source is unavailable")
            right = self._result_frame(right_source)
            if set(right.columns) != set(frame.columns):
                raise ValueError("union_all requires identical field contracts")
            return pd.concat(
                [frame, right.reindex(columns=frame.columns)],
                ignore_index=True,
            )
        if step.operation == "join_fields":
            right_source = sources.get(step.right_source_query_id)
            if right_source is None:
                raise ValueError(
                    f"join_fields query result is unavailable: {step.right_source_query_id}"
                )
            if right_source.status != QueryStatus.SUCCESS:
                raise ValueError(
                    f"join_fields query did not succeed: {step.right_source_query_id}"
                )
            right = self._result_frame(right_source)
            for key in step.join_on:
                if key not in frame.columns:
                    raise ValueError(f"join_fields left key field is missing: {key}")
                if key not in right.columns:
                    raise ValueError(f"join_fields right key field is missing: {key}")
            
            # Strict cardinality checking
            if step.cardinality == "one_to_one":
                if frame.duplicated(subset=step.join_on).any():
                    raise ValueError("join_fields one_to_one cardinality violated: duplicate keys in left frame")
                if right.duplicated(subset=step.join_on).any():
                    raise ValueError("join_fields one_to_one cardinality violated: duplicate keys in right frame")
            elif step.cardinality == "many_to_one":
                if right.duplicated(subset=step.join_on).any():
                    raise ValueError("join_fields many_to_one cardinality violated: duplicate keys in right frame")
                    
            fields_map = step.fields if isinstance(step.fields, dict) else {}
            for col, out_col in fields_map.items():
                if col not in right.columns:
                    raise ValueError(f"join_fields right copy field is missing: {col}")
                if out_col in frame.columns:
                    raise ValueError(f"join_fields output field already exists in left frame: {out_col}")
                    
            right_subset = right[list(step.join_on) + list(fields_map.keys())].rename(columns=fields_map)
            matched = frame.merge(
                right_subset,
                on=step.join_on,
                how="left",
                sort=False,
            )
            return matched
        if step.operation in {"shift", "diff", "pct_change"}:
            ordered = frame.sort_values(
                step.group_by + [step.order_by],
                kind="mergesort",
            ).copy()
            grouped = ordered.groupby(
                step.group_by,
                sort=False,
                dropna=False,
            )
            if step.operation == "shift":
                shifted = grouped[step.field].shift(step.periods)
            elif step.operation == "diff":
                numeric = pd.to_numeric(ordered[step.field], errors="coerce")
                shifted = numeric.groupby(
                    [ordered[field] for field in step.group_by],
                    sort=False,
                    dropna=False,
                ).diff(step.periods)
            else:
                numeric = pd.to_numeric(ordered[step.field], errors="coerce")
                shifted = numeric.groupby(
                    [ordered[field] for field in step.group_by],
                    sort=False,
                    dropna=False,
                ).pct_change(periods=step.periods, fill_method=None)
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
            matched_values = [None] * len(ordered)
            matched_dates = [None] * len(ordered)
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
                        matched_values[row_index] = ordered.at[match_index, step.field]
                        matched_dates[row_index] = target.strftime("%Y%m%d")
            # Assign complete columns once. Repeated scalar writes force pandas to
            # rebuild object blocks and become prohibitively slow on market-wide data.
            ordered[step.output_field] = matched_values
            ordered[step.matched_date_output_field] = matched_dates
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
                aggregation.output_field: self._aggregate_series(
                    frame[aggregation.field],
                    aggregation.function,
                    aggregation.quantile,
                )
                for aggregation in step.aggregations
            }
            return pd.DataFrame([row])
        raise ValueError(f"Unsupported result pipeline operation: {step.operation}")

    @classmethod
    def _named_aggregations(
        cls,
        step: ResultPipelineStep,
    ) -> Dict[str, pd.NamedAgg]:
        """Return pandas named aggregations for the validated aggregate contract."""
        return {
            aggregation.output_field: pd.NamedAgg(
                column=aggregation.field,
                aggfunc=(
                    lambda series, item=aggregation: cls._aggregate_series(
                        series,
                        item.function,
                        item.quantile,
                    )
                ),
            )
            for aggregation in step.aggregations
        }

    @staticmethod
    def _aggregate_series(
        series: pd.Series,
        function: str,
        quantile: Optional[float],
    ) -> Any:
        """Apply one allowlisted aggregation with explicit null semantics."""
        if function == "count_distinct":
            return int(series.nunique(dropna=True))
        if function == "quantile":
            return pd.to_numeric(series, errors="coerce").quantile(quantile)
        if function in {"sum", "mean", "median", "min", "max", "std"}:
            return getattr(pd.to_numeric(series, errors="coerce"), function)()
        if function in {"first", "last"}:
            non_null = series.dropna()
            if non_null.empty:
                return None
            return non_null.iloc[0 if function == "first" else -1]
        return getattr(series, function)()

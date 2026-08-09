"""DeepSeek implementation of the provider-neutral query-planner port."""

import calendar
from datetime import datetime, timedelta
import json
import logging
import re
from time import sleep
from typing import Any, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from pydantic import ValidationError
import requests

from china_a_share.core.contracts import (
    AnalysisRequest,
    DataQuery,
    DataOperation,
    QueryPlan,
    ResultPipeline,
    ResultPipelineStep,
)
from china_a_share.core.errors import PlannerError
from china_a_share.capabilities import build_capability_guidance
from china_a_share.observability import ANALYSIS_REQUEST_ID, log_event


DEEPSEEK_PLANNER_NAME = "deepseek"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_TIMEOUT_SECONDS = 180
DEEPSEEK_MAX_OUTPUT_TOKENS = 12_000
DEEPSEEK_MAX_ATTEMPTS = 20
DEEPSEEK_TARGET_VALID_CANDIDATES = 3
DEEPSEEK_MAX_IDENTICAL_CANDIDATES = 3
DEEPSEEK_RETRY_DELAY_SECONDS = 1
RETAIL_PROXY_DISCLOSURE = (
    "This result uses non_top10_float_ratio as a holding-dispersion proxy. "
    "It includes retail holders and institutions outside the disclosed top ten "
    "and is not a verified individual-investor ownership percentage."
)


logger = logging.getLogger(__name__)


def build_query_plan_system_prompt(
    guidance: str,
    allowed_operations: str,
) -> str:
    """Build provider-neutral planning instructions shared by every LLM."""
    return (
        "You plan read-only market-data queries for mainland China A-shares. "
        "Return one valid JSON object matching the supplied schema and use only "
        "operations from the active provider catalog. Keep interpretation, purpose, "
        "evidence, and limitation text concise and do not repeat schema instructions "
        "inside those fields. Resolve relative or partial "
        "dates using the current date "
        f"{datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()} and "
        "Asia/Shanghai semantics. Use the latest completed trading day for end-of-day "
        "data. Security codes must end in .SH, .SZ, or .BJ. Follow every operation's "
        "documented parameters and fields exactly. If the user message contains a "
        "well-known, unambiguous mainland-listed company name, resolve its exact "
        "A-share ts_code rather than declaring the request unsupported merely because "
        "the user omitted the code; \u4e2d\u56fd\u5e73\u5b89 is 601318.SH, "
        "\u8d35\u5dde\u8305\u53f0 is 600519.SH, and "
        "\u5e73\u5b89\u94f6\u884c is 000001.SZ. A security-specific operation can support a "
        "full-market request as a fan-out template when a stock_basic universe query "
        "is included; do not mark such a plan unsupported solely because the template "
        "requires ts_code. "
        "When comparing financial time series from multiple statement operations, "
        "return separate query results unless the selected join keys are documented "
        "as unique in both sources; reporting periods may contain multiple versions, "
        "so do not assume ts_code plus end_date is one-to-one. "
        "If the user message contains a "
        "trusted_analysis_window block, use its exact event boundaries and outcome "
        "offset. Source queries may start earlier to warm up an ordered calculation "
        "and must extend far enough after the event interval to measure the outcome. "
        "Decompose the request into atomic requirements and provide catalog evidence "
        "for each requirement. Set requirements[].status to exactly covered or "
        "unsupported; never use synonyms such as supported, available, or feasible. "
        "Preserve every numeric value and comparison direction "
        "from the user request; do not replace them with fixed thresholds or counts. "
        "For every supported plan, populate answer_contract with the exact query or "
        "pipeline result that answers the user. Use result_kind=summary when the user "
        "asks for counts, probabilities, averages, extrema, or other aggregate metrics, "
        "and list every requested final output field separately. Use result_kind=table "
        "for requested detail rows. The answer contract is exhaustive: never omit one "
        "side of a requested comparison, one requested statistic, or its result field. "
        "Each promised field must be produced by the executable query or pipeline; do "
        "not describe an output that the plan does not calculate. "
        "For a multi-stage request that first ranks or filters securities by one "
        "metric and then asks to display additional metrics for that selected cohort, "
        "build one composed result_pipeline. Preserve the user's requested operation "
        "order exactly: filtering, aggregation, joining additional metrics, further "
        "filtering, sorting, and limiting may occur in any sequence justified by the "
        "request. Aggregate to the required grain before an operation that depends on "
        "that grain, and join each additional metric by stable keys. A joined query "
        "must satisfy its declared key cardinality. The answer_contract must reference "
        "the final pipeline output and include every requested final field. "
        "For arbitrary multi-stage analysis, use execution_plan instead of queries, "
        "result_pipeline, or intent. Give every node a unique node_id. Query nodes "
        "perform provider reads. Compute nodes apply exactly one allowlisted step to "
        "their first input_result_id and declare every additional right-side result as "
        "another input. A query node may use one upstream result as a bounded fan-out "
        "by setting fanout_input_field and fanout_param; use this when an intermediate "
        "candidate set must drive per-security provider calls. Dependencies must be "
        "acyclic, and result_node_id must identify the final answer. "
        "Use rank_metric intent only when the requested metric is one of the exact "
        "AnalysisMetric.type enum values. For any provider field outside that enum, "
        "set intent to null and express ranking with provider queries plus a result "
        "pipeline or execution plan. Never force an unrelated typed metric merely "
        "because the user requests a ranking. "
        "For an event study that asks for up or down probabilities after consecutive "
        "limit-up sessions, output only a high-level intent with "
        "analysis_type=event_outcome_probability. Populate event_window, "
        "event_type=limit_up, consecutive_sessions, observation_offset, "
        "observation_unit, outcomes, and aggregation=probability. Do not generate "
        "queries, result_pipeline, execution_plan, or answer_contract for this intent; "
        "the trusted local compiler owns provider selection, field binding, outcome "
        "window expansion, and final result fields. "
        "Use result_pipeline for deterministic calculations instead of inventing "
        "specialized transforms. Pipelines may compose the allowlisted relational, "
        "set, aggregation, ranking, and window operations declared by the schema, "
        "including select_fields, distinct, filters, joins, union_all, aggregate, "
        "having, rank, row_number, top_k_by_group, rolling and cumulative statistics, "
        "diff, pct_change, asof_join, key set operations, coalesce, fill_constant, "
        "clip, conditional_value, group transforms, normalization, weighted means, "
        "rolling quantile/correlation/covariance, resampling, and "
        "summarize. A derive step requires field, output_field, "
        "arithmetic_operator, and exactly one of value or right_field; never "
        "put shift or a comparison in arithmetic_operator. A shift step requires field, "
        "output_field, group_by, order_by, and nonzero periods. A match_source step "
        "performs membership matching and requires right_source_query_id, join_on, "
        "and a boolean output_field; it does not copy right-source columns. A "
        "match_at_offset step operates within the "
        "current pipeline frame and requires field, output_field, group_by, order_by, "
        "offset_value, offset_unit, and matched_date_output_field. Use "
        "match_at_offset for calendar outcome horizons; never replace a month or year "
        "horizon with a one-row shift. A summarize step accepts aggregations only; "
        "put each output_field, optional label, source field, and function inside the "
        "aggregations array. For an event study, use the dense time-series query as "
        "the pipeline source, mark event rows with match_source, detect the ordered "
        "event sequence before filtering rows, match the future value with "
        "match_at_offset, calculate field-to-field returns with derive, and summarize "
        "last. Do not use latest_by_group before sequence detection. A canonical "
        "streak outcome pipeline is: match_source to create an event boolean; "
        "rolling_sum over that boolean using the requested window, group, and order; "
        "set require_consecutive to true so missing market sessions break the streak; "
        "match_at_offset the numeric outcome field such as close before filtering, so "
        "future rows remain available; filter the streak count to the requested length; "
        "drop the missing "
        "future value; derive future value divided by event value; derive the ratio "
        "minus 1; compare_scalar the return with 0; optionally multiply the return by "
        "100; then summarize with one aggregations array. Omit redundant sort, "
        "drop_missing on membership booleans, and latest_by_group steps. "
        "The subtract operator means field minus value or right_field; "
        "constant_minus means value minus field. Limit-up analysis must use the "
        "native limit_list_d operation rather than a fixed pct_chg threshold because "
        "price-limit rules vary across boards and special-treatment securities. "
        "For ordered calculations, provide group_by and order_by. Fetch enough source "
        "history to initialize rolling windows before filtering to the requested "
        "measurement interval. Drop rows whose required future outcome is unavailable. "
        "Use filters only for row conditions and provider params only for parameters "
        "explicitly documented by the catalog. "
        "Use semi_join or anti_join for membership inclusion or exclusion instead of "
        "creating a marker and forgetting to filter it. Use inner_join only with an "
        "explicit cardinality and field mapping. Use union_all only for identical "
        "field contracts. Apply having only after aggregate, and apply rank-based "
        "filters only after the rank field is produced. "
        "Security classification constraints such as industry, area, market, or "
        "exchange define the requested universe and must change which securities "
        "can reach the final result. Read those fields from stock_basic, filter that "
        "universe first, and use match_source or an equivalent validated join to "
        "restrict metric rows before sorting or limiting. Never satisfy a "
        "classification constraint by merely joining or displaying its field after "
        "ranking an unrestricted market-wide metric result. "
        "Treat a natural-language category such as 'automobile industry' as a broad "
        "classification and use contains so documented child labels remain eligible. "
        "Use eq only when the user explicitly requests an exact taxonomy label. "
        "Declare every explicit row-selection predicate in the constraints array. "
        "Bind it to the exact query filter that applies it. When a universe query is "
        "not the pipeline source, also bind enforcement_step_index to the pipeline "
        "filter that retains its membership marker. The trusted validator rejects "
        "missing bindings, display-only joins, and constraints enforced after sorting, "
        "limiting, or aggregation. "
        "Mark feasibility as supported only when every requirement maps to a documented "
        "provider field or parameter, a declared transform, a valid result_pipeline "
        "step, or a valid high-level intent. Otherwise mark the unsupported requirements, state a concrete limitation, "
        "and return no queries. Do not substitute a similar metric or proxy unless the "
        "user explicitly requested that metric. Never infer unavailable values or "
        "invent data. "
        "When the request leaves a material choice unresolved, such as the security "
        "universe, ranking count, reporting-period alignment, or whether a size-sensitive "
        "financial metric should be absolute or normalized, return two or three concise "
        "clarification_options in the user's language. Each option must be a complete, "
        "directly executable prompt, must make the differing choices explicit, and must "
        "stay within documented capabilities. Do not return clarification_options when "
        "the request is already unambiguous. Apply this rule generically to every metric "
        "and analysis type, including any proposed proxy, approximation, or material "
        "default; never rely on metric-specific clarification templates. "
        "For every request that ranks or selects top securities by a supported "
        "AnalysisMetric, output only a high-level intent block matching the "
        "AnalysisIntent schema; do not generate queries, a result pipeline, an "
        "execution plan, or an answer contract. Put industry, area, market, exchange, "
        "list_status, name, and ts_code security-master predicates in universe.filters. "
        "Put thresholds on the ranked metric in metric.filters. Use metric.window for "
        "period_return and metric.as_of for point-in-time metrics. Preserve the exact "
        "Use ne, not_in, or not_contains for explicit exclusions; never encode an "
        "exclusion as an empty in list. For a request excluding special-treatment "
        "securities, use name not_contains ST. The local listed-security universe "
        "uses list_status=L, so it already excludes delisted securities. "
        "ranking direction and limit. The trusted local compiler owns provider "
        "selection, joins, constraint ordering, and result fields.\n\n"
        "Registered analysis capabilities are executable local code, not raw "
        "provider fields. When a request matches one, treat its variable parameters "
        "as inputs and do not reject it merely because the derived result is absent "
        "from the provider schema.\n"
        f"{build_capability_guidance()}\n\n"
        f"Operation catalog:\n{guidance}\n\n"
        f"Allowed operation names:\n{allowed_operations}\n\n"
        f"JSON schema:\n{json.dumps(QueryPlan.model_json_schema())}\n\n"
        "Critical contract invariants to check immediately before responding:\n"
        "- requirements[].status is exactly covered or unsupported.\n"
        "- For every provider-backed requirement, evidence must name the exact "
        "operation that supplies it, and that operation must be present in the "
        "executable plan.\n"
        "- feasibility=supported requires every requirement status to be covered.\n"
        "- feasibility=unsupported requires at least one unsupported requirement, "
        "at least one concrete limitation, and no queries or execution_plan.\n"
        "- A supported plan requires a complete answer_contract unless it contains "
        "a valid high-level intent.\n"
        "Return the complete JSON object only after checking all four invariants."
    )


class DeepSeekQueryPlanner:
    """Convert natural language into a query plan using DeepSeek."""

    def __init__(self, api_key: str, session: Optional[Any] = None) -> None:
        """Store the required credential and an optional injectable HTTP session."""
        self._api_key = api_key
        self._session = session if session is not None else requests.Session()

    @property
    def name(self) -> str:
        """Return the stable planner identifier exposed in analysis responses."""
        return DEEPSEEK_PLANNER_NAME

    def plan(
        self,
        request: AnalysisRequest,
        candidate_operations: Sequence[DataOperation],
    ) -> QueryPlan:
        """Build a query plan using only the active provider's catalog."""
        return self._plan_with_validation(
            request,
            candidate_operations,
            validator=None,
        )

    def plan_validated(
        self,
        request: AnalysisRequest,
        candidate_operations: Sequence[DataOperation],
        validator: Callable[[QueryPlan], QueryPlan],
    ) -> QueryPlan:
        """Build and locally validate a plan with bounded corrective retries."""
        return self._plan_with_validation(
            request,
            candidate_operations,
            validator=validator,
        )

    def _plan_with_validation(
        self,
        request: AnalysisRequest,
        candidate_operations: Sequence[DataOperation],
        validator: Optional[Callable[[QueryPlan], QueryPlan]],
    ) -> QueryPlan:
        """Retry planning with concrete validation feedback before audited fallback."""
        guidance = "\n".join(
            f"- {operation.name}: {operation.description}"
            for operation in candidate_operations
        )
        allowed_operations = ",".join(
            operation.name for operation in candidate_operations
        )
        system_prompt = build_query_plan_system_prompt(
            guidance,
            allowed_operations,
        )
        feedback = None
        candidate_guidance = None
        last_error: Optional[Exception] = None
        valid_candidates: list[QueryPlan] = []
        candidate_fingerprints: dict[str, int] = {}
        for attempt in range(DEEPSEEK_MAX_ATTEMPTS):
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.prompt},
            ]
            if feedback:
                final_attempt_guidance = (
                    " If a faithful executable plan still cannot pass validation, "
                    "return an unsupported plan with no queries, a concrete limitation, "
                    "and two or three contextual clarification_options that the user can "
                    "submit directly. Do not expose the validator error as the answer."
                    if attempt + 1 == DEEPSEEK_MAX_ATTEMPTS
                    else ""
                )
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "The previous query plan was rejected by the trusted "
                            "local validator. Return a complete corrected plan and "
                            "do not repeat this error:\n"
                            f"{feedback}{final_attempt_guidance}"
                        ),
                    }
                )
            elif candidate_guidance:
                messages.append({"role": "system", "content": candidate_guidance})
            request_payload = {
                "model": DEEPSEEK_MODEL,
                "messages": messages,
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
                "stream": False,
            }
            log_event(
                logger,
                logging.INFO,
                "planner_model_attempt",
                request_id=ANALYSIS_REQUEST_ID.get(),
                provider="deepseek",
                model=DEEPSEEK_MODEL,
                attempt=attempt + 1,
            )
            try:
                response = self._request_once(request_payload)
                plan = self._decode_plan_response(response)
            except PlannerError as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "planner_response_rejected",
                    request_id=ANALYSIS_REQUEST_ID.get(),
                    provider="deepseek",
                    model=DEEPSEEK_MODEL,
                    attempt=attempt + 1,
                    reason=str(exc),
                    validation_errors=(
                        (exc.raw_response or {}).get("validation_errors", [])
                    ),
                )
                if exc.http_status in {400, 401, 403, 404}:
                    raise
                last_error = exc
                feedback = self._build_contract_feedback(exc)
                if attempt + 1 < DEEPSEEK_MAX_ATTEMPTS:
                    sleep(DEEPSEEK_RETRY_DELAY_SECONDS)
                    continue
                break

            self._finalize_plan(plan)
            log_event(
                logger,
                logging.INFO,
                "planner_intent_normalized",
                request_id=ANALYSIS_REQUEST_ID.get(),
                provider="deepseek",
                model=DEEPSEEK_MODEL,
                attempt=attempt + 1,
                intent=(plan.intent.model_dump(mode="json") if plan.intent else None),
            )
            if (
                validator is not None
                and plan.feasibility == "supported"
                and plan.answer_contract is None
                and plan.intent is None
            ):
                last_error = ValueError(
                    "A supported model-generated plan must include answer_contract "
                    "with every user-requested final output field."
                )
                feedback = self._build_plan_feedback(
                    plan,
                    last_error,
                    decision="REVISE",
                    phase="contract_validation",
                )
            elif validator is not None:
                try:
                    validated_plan = validator(plan)
                    self._validate_requirement_operation_lineage(
                        validated_plan,
                        candidate_operations,
                    )
                    log_event(
                        logger,
                        logging.INFO,
                        "planner_plan_validated",
                        request_id=ANALYSIS_REQUEST_ID.get(),
                        provider="deepseek",
                        model=DEEPSEEK_MODEL,
                        attempt=attempt + 1,
                        pipeline=(
                            validated_plan.result_pipeline.model_dump(mode="json")
                            if validated_plan.result_pipeline
                            else None
                        ),
                    )
                    if not self._requires_candidate_selection(validated_plan):
                        return validated_plan
                    fingerprint = validated_plan.model_dump_json(
                        exclude_none=True,
                    )
                    valid_candidates.append(validated_plan)
                    candidate_fingerprints[fingerprint] = (
                        candidate_fingerprints.get(fingerprint, 0) + 1
                    )
                    if (
                        len(candidate_fingerprints)
                        >= DEEPSEEK_TARGET_VALID_CANDIDATES
                        or candidate_fingerprints[fingerprint]
                        >= DEEPSEEK_MAX_IDENTICAL_CANDIDATES
                    ):
                        selected = self._select_best_candidate(valid_candidates)
                        log_event(
                            logger,
                            logging.INFO,
                            "planner_candidate_selected",
                            request_id=ANALYSIS_REQUEST_ID.get(),
                            provider="deepseek",
                            model=DEEPSEEK_MODEL,
                            candidate_count=len(valid_candidates),
                            distinct_candidate_count=len(candidate_fingerprints),
                            score=self._candidate_score(selected),
                        )
                        return selected
                    feedback = None
                    candidate_guidance = (
                        "A valid candidate already exists. Produce another independent "
                        "candidate for deterministic comparison. Preserve every explicit "
                        "user requirement, do not introduce unrequested ranking or limits, "
                        "and prefer the smallest executable plan that returns every "
                        f"requested output. This is candidate {len(valid_candidates) + 1}."
                    )
                except ValueError as exc:
                    last_error = exc
                    feedback = self._build_plan_feedback(plan, exc)
                    candidate_guidance = None
                    log_event(
                        logger,
                        logging.WARNING,
                        "planner_plan_rejected",
                        request_id=ANALYSIS_REQUEST_ID.get(),
                        provider="deepseek",
                        model=DEEPSEEK_MODEL,
                        attempt=attempt + 1,
                        reason=str(exc),
                    )
            else:
                return plan

            if attempt + 1 < DEEPSEEK_MAX_ATTEMPTS:
                sleep(DEEPSEEK_RETRY_DELAY_SECONDS)

        if valid_candidates:
            selected = self._select_best_candidate(valid_candidates)
            log_event(
                logger,
                logging.INFO,
                "planner_candidate_selected",
                request_id=ANALYSIS_REQUEST_ID.get(),
                provider="deepseek",
                model=DEEPSEEK_MODEL,
                candidate_count=len(valid_candidates),
                distinct_candidate_count=len(candidate_fingerprints),
                score=self._candidate_score(selected),
            )
            return selected
        if isinstance(last_error, PlannerError):
            raise last_error
        raise PlannerError(
            source=self.name,
            message=(
                "DeepSeek could not produce a valid query plan after "
                f"{DEEPSEEK_MAX_ATTEMPTS} attempts: {last_error}"
            ),
        ) from last_error

    @staticmethod
    def _requires_candidate_selection(plan: QueryPlan) -> bool:
        """Return whether a validated plan is complex enough to benefit from comparison."""
        return len(plan.requirements) > 1

    @staticmethod
    def _candidate_score(plan: QueryPlan) -> tuple[int, int, int, int]:
        """Rank valid candidates by completeness before execution complexity."""
        covered_requirements = sum(
            requirement.status == "covered" for requirement in plan.requirements
        )
        output_count = (
            len(plan.answer_contract.outputs) if plan.answer_contract else 0
        )
        node_count = len(plan.queries)
        if plan.execution_plan is not None:
            node_count += len(plan.execution_plan.nodes)
        if plan.result_pipeline is not None:
            node_count += len(plan.result_pipeline.steps)
        return (
            int(plan.feasibility == "supported"),
            covered_requirements,
            output_count,
            -node_count,
        )

    @classmethod
    def _select_best_candidate(cls, candidates: Sequence[QueryPlan]) -> QueryPlan:
        """Return the highest-scoring validated candidate deterministically."""
        return max(
            candidates,
            key=lambda candidate: (
                cls._candidate_score(candidate),
                candidate.model_dump_json(exclude_none=True),
            ),
        )

    @staticmethod
    def _validate_requirement_operation_lineage(
        plan: QueryPlan,
        candidate_operations: Sequence[DataOperation],
    ) -> None:
        """Require provider operations cited as evidence to exist in the plan."""
        if plan.feasibility == "unsupported" and all(
            requirement.implementation for requirement in plan.requirements
        ):
            raise ValueError(
                "An unsupported plan must identify at least one requirement without "
                "an executable implementation."
            )
        planned_operations = {query.operation for query in plan.queries}
        if plan.execution_plan is not None:
            planned_operations.update(
                node.query.operation
                for node in plan.execution_plan.nodes
                if node.kind == "query"
            )
        cited_operations = set()
        evidence = " ".join(
            " ".join(
                value
                for value in (requirement.implementation, requirement.evidence)
                if value
            )
            for requirement in plan.requirements
            if requirement.status == "covered"
        )
        for operation in candidate_operations:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(operation.name)}(?![A-Za-z0-9_])", evidence):
                cited_operations.add(operation.name)
        missing = cited_operations.difference(planned_operations)
        if missing:
            raise ValueError(
                "Requirement evidence cites provider operations absent from the "
                "executable plan: " + ", ".join(sorted(missing))
            )

    @staticmethod
    def _build_contract_feedback(error: PlannerError) -> str:
        """Return machine-readable repair context for one rejected model response."""
        raw_response = error.raw_response or {}
        feedback = {
            "decision": "REVISE",
            "phase": "contract_validation",
            "instruction": (
                "Return a complete corrected query plan. Preserve the user intent, "
                "change every invalid value, and satisfy the full JSON schema."
            ),
            "errors": raw_response.get("validation_errors") or [
                {"message": str(error)}
            ],
            "rejected_output": raw_response.get("content"),
        }
        return json.dumps(feedback, ensure_ascii=False)

    @staticmethod
    def _build_plan_feedback(
        plan: QueryPlan,
        error: Exception,
        decision: str = "REPLAN",
        phase: str = "capability_validation",
    ) -> str:
        """Return machine-readable correction context for a validated candidate."""
        instruction = (
            "Return a complete corrected query plan. Preserve the user intent and "
            "the valid parts of the rejected plan, satisfy the full JSON schema, "
            "and revalidate the whole plan."
            if decision == "REVISE"
            else (
                "Return a complete replacement query plan using only documented "
                "capabilities. Preserve the user intent and revalidate the whole plan."
            )
        )
        result_ids = [query.query_id for query in plan.queries]
        if plan.result_pipeline is not None:
            result_ids.append(plan.result_pipeline.output_query_id)
        if plan.execution_plan is not None:
            result_ids.append(plan.execution_plan.result_node_id)
        feedback = {
            "decision": decision,
            "phase": phase,
            "instruction": instruction,
            "errors": [{"message": str(error)}],
            "rejected_plan": plan.model_dump(mode="json"),
            "available_result_ids": list(dict.fromkeys(result_ids)),
        }
        return json.dumps(feedback, ensure_ascii=False)

    def _finalize_plan(self, plan: QueryPlan) -> None:
        """Apply deterministic normalization before semantic validation."""
        self._normalize_fields(plan)
        self._normalize_join_field_mappings(plan)
        self._normalize_execution_answer_fields(plan)
        self._normalize_requirement_statuses(plan)
        self._normalize_limit_list_queries(plan)
        self._normalize_event_study_source(plan)
        self._normalize_pipeline_query_windows(plan)
        self._downgrade_unexecutable_plan(plan)
        self._split_multi_security_float_holder_queries(plan)
        self._append_audited_disclosures(plan)

    def normalize_and_validate_plan(self, raw_content: str) -> QueryPlan:
        """Parse, normalize, and validate a raw plan JSON from an external planner."""
        import json
        try:
            raw_plan = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            raise PlannerError(
                source=self.name,
                message="Planner returned non-JSON content.",
            ) from exc
        self._normalize_raw_query_defaults(raw_plan)
        plan = QueryPlan.model_validate(raw_plan)
        self._finalize_plan(plan)
        return plan

    @staticmethod
    def _append_audited_disclosures(plan: QueryPlan) -> None:
        """Attach user-visible caveats for declared approximation transforms."""
        if plan.feasibility != "supported":
            return

        uses_retail_proxy = any(
            query.transform == "cr10_float_trend"
            for query in plan.queries
        )
        if uses_retail_proxy and RETAIL_PROXY_DISCLOSURE not in plan.limitations:
            plan.limitations.append(RETAIL_PROXY_DISCLOSURE)

    @staticmethod
    def _normalize_join_field_mappings(plan: QueryPlan) -> None:
        """Remove redundant identity copies of keys from join steps."""
        pipeline = plan.result_pipeline
        if pipeline is None:
            return
        normalized_steps = []
        for step in pipeline.steps:
            if step.operation != "join_fields" or not isinstance(step.fields, dict):
                normalized_steps.append(step)
                continue
            # Join keys already survive a left join. Copying a key onto itself would
            # overwrite an existing column without adding any information.
            step.fields = {
                source: output
                for source, output in step.fields.items()
                if not (source in step.join_on and source == output)
            }
            if step.fields:
                normalized_steps.append(step)
        pipeline.steps = normalized_steps

    @staticmethod
    def _normalize_execution_answer_fields(plan: QueryPlan) -> None:
        """Copy contract fields through graph joins when their source is unambiguous."""
        execution_plan = plan.execution_plan
        contract = plan.answer_contract
        if execution_plan is None or contract is None:
            return

        required_fields = {output.field for output in contract.outputs}
        nodes_by_id = {node.node_id: node for node in execution_plan.nodes}
        resolved_fields: dict[str, set[str]] = {}
        resolving: set[str] = set()

        def resolve(node_id: str) -> set[str]:
            if node_id in resolved_fields:
                return resolved_fields[node_id]
            if node_id in resolving or node_id not in nodes_by_id:
                return set()
            resolving.add(node_id)
            node = nodes_by_id[node_id]
            if node.kind == "query":
                fields = set(node.query.fields)
                if node.fanout_input_field:
                    fields.add(node.fanout_input_field)
            else:
                fields = resolve(node.input_result_ids[0]).copy()
                step = node.step
                right_fields = (
                    resolve(step.right_source_query_id)
                    if step.right_source_query_id
                    else set()
                )
                if step.operation in {"semi_join", "inner_join", "join_fields"}:
                    copied_fields = {
                        field: field
                        for field in required_fields & right_fields - fields
                    }
                    if step.operation == "semi_join":
                        right_node = nodes_by_id.get(step.right_source_query_id)
                        can_copy_unique_security_fields = bool(
                            right_node
                            and right_node.kind == "query"
                            and right_node.query.operation == "stock_basic"
                            and step.join_on == ["ts_code"]
                        )
                        if not can_copy_unique_security_fields:
                            copied_fields = {}
                        elif copied_fields:
                            # stock_basic has one row per ts_code, so this upgrade keeps
                            # membership filtering without introducing duplicate rows.
                            step.operation = "inner_join"
                            step.cardinality = "many_to_one"
                    if copied_fields:
                        existing = step.fields if isinstance(step.fields, dict) else {}
                        step.fields = {**existing, **copied_fields}
                    if isinstance(step.fields, dict):
                        fields.update(step.fields.values())
                elif step.operation == "select_fields":
                    fields = set(step.fields)
                elif step.operation in {"aggregate", "summarize", "resample"}:
                    fields = set(step.group_by)
                    fields.update(item.output_field for item in step.aggregations)
                elif step.operation == "rename_fields":
                    fields.difference_update(step.fields)
                    fields.update(step.fields.values())
                elif step.operation in {"intersect_keys", "except_keys"}:
                    fields = set(step.join_on)
                elif step.output_field:
                    fields.add(step.output_field)
                    if step.matched_date_output_field:
                        fields.add(step.matched_date_output_field)
            resolving.remove(node_id)
            resolved_fields[node_id] = fields
            return fields

        resolve(execution_plan.result_node_id)

    @staticmethod
    def _normalize_requirement_statuses(plan: QueryPlan) -> None:
        """Resolve a covered implementation that contradicts its status label."""
        if plan.feasibility != "supported":
            return
        for requirement in plan.requirements:
            if requirement.status == "unsupported" and requirement.implementation:
                # A concrete implementation on a supported plan is an explicit
                # coverage claim; the status label is the only contradictory value.
                requirement.status = "covered"

    def _decode_plan_response(self, response: Any) -> QueryPlan:
        """Validate one planner response before any deterministic normalization."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise PlannerError(
                source=self.name,
                message="DeepSeek returned a non-JSON response.",
                http_status=response.status_code,
                raw_response={"text": response.text},
            ) from exc

        if response.status_code >= 400 or payload.get("error"):
            upstream_error = payload.get("error") or {}
            raise PlannerError(
                source=self.name,
                message=str(upstream_error.get("message") or "DeepSeek request failed."),
                code=upstream_error.get("code"),
                http_status=response.status_code,
                raw_response=payload,
            )

        content = ((payload.get("choices") or [{}])[0].get("message") or {}).get(
            "content", ""
        )
        if not content:
            raise PlannerError(
                source=self.name,
                message="DeepSeek returned an empty query plan.",
                http_status=response.status_code,
                raw_response=payload,
            )
        log_event(
            logger,
            logging.INFO,
            "planner_raw_output",
            request_id=ANALYSIS_REQUEST_ID.get(),
            provider="deepseek",
            model=str(payload.get("model") or DEEPSEEK_MODEL),
            content=content,
        )
        try:
            raw_plan = json.loads(content)
            self._normalize_raw_query_defaults(raw_plan)
            plan = QueryPlan.model_validate(raw_plan)
        except json.JSONDecodeError as exc:
            raise PlannerError(
                source=self.name,
                message=(
                    "DeepSeek returned a query plan that violates the contract: "
                    f"invalid JSON at line {exc.lineno}, column {exc.colno}."
                ),
                http_status=response.status_code,
                raw_response={"content": content},
            ) from exc
        except ValidationError as exc:
            errors = exc.errors(include_url=False)
            details = "; ".join(
                (
                    f"{'.'.join(str(part) for part in error['loc'])}: "
                    f"{error['msg']}"
                )
                for error in errors
            )
            raise PlannerError(
                source=self.name,
                message=(
                    "DeepSeek returned a query plan that violates the contract: "
                    f"{details}"
                ),
                http_status=response.status_code,
                raw_response={
                    "content": content,
                    "validation_errors": [
                        {
                            "location": ".".join(
                                str(part) for part in error["loc"]
                            ),
                            "message": error["msg"],
                        }
                        for error in errors
                    ],
                },
            ) from exc
        return plan

    def generate_text(self, prompt: str) -> str:
        """Generate arbitrary text using the underlying LLM."""
        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
        }
        try:
            response = self._session.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=DEEPSEEK_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except requests.RequestException as exc:
            raise PlannerError(source=self.name, message=str(exc)) from exc

    def _request_once(self, request_payload: dict) -> Any:
        """Issue one planner request so the outer loop bounds total model calls."""
        try:
            return self._session.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=request_payload,
                timeout=DEEPSEEK_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise PlannerError(source=self.name, message=str(exc)) from exc

    @staticmethod
    def _normalize_raw_query_defaults(raw_plan: Any) -> None:
        """Repair unambiguous model syntax before contract validation."""
        if not isinstance(raw_plan, dict):
            return
        for list_field in (
            "requirements",
            "limitations",
            "clarification_options",
            "queries",
        ):
            if raw_plan.get(list_field) is None:
                raw_plan[list_field] = []
        semantic_intent = raw_plan.get("intent")
        if isinstance(semantic_intent, dict):
            # Once the model selects a supported semantic IR, the trusted compiler
            # exclusively owns all provider reads, field bindings, and result IDs.
            raw_plan["queries"] = []
            raw_plan["constraints"] = []
            raw_plan["result_pipeline"] = None
            raw_plan["execution_plan"] = None
            raw_plan["answer_contract"] = None
        elif isinstance(raw_plan.get("execution_plan"), dict):
            # An execution graph embeds every provider query and compute operation it
            # owns. Model-generated top-level copies are redundant legacy syntax and
            # would otherwise make an unambiguous plan fail contract validation.
            raw_plan["queries"] = []
            raw_plan["result_pipeline"] = None
            raw_plan["intent"] = None
        pipeline = raw_plan.get("result_pipeline")
        steps = pipeline.get("steps") if isinstance(pipeline, dict) else None
        if isinstance(steps, list):
            DeepSeekQueryPlanner._normalize_pipeline_step_syntax(steps)
            event_membership = next(
                (
                    step
                    for step in steps
                    if isinstance(step, dict)
                    and step.get("operation")
                    in {"match_source", "exists_in_source"}
                ),
                None,
            )
            streak = next(
                (
                    step
                    for step in steps
                    if isinstance(step, dict)
                    and step.get("operation") == "rolling_sum"
                ),
                None,
            )
            if event_membership and streak and streak.get("window"):
                # The membership output is the authoritative event marker; model-generated
                # aliases such as limit_up_flag are not available in the source frame.
                streak["field"] = event_membership.get("output_field")
                streak["min_periods"] = streak["window"]
                streak["require_consecutive"] = True
            normalized_steps = []
            condition_outputs = {}
            for step in steps:
                if not isinstance(step, dict):
                    normalized_steps.append(step)
                    continue
                operation = step.get("operation")
                if (
                    operation == "sort"
                    and not step.get("field")
                    and step.get("order_by")
                ):
                    step["field"] = step["order_by"]
                if operation == "derive":
                    comparison = step.get("arithmetic_operator")
                    if comparison in {"gt", "ge", "eq", "le", "lt"}:
                        step["operation"] = (
                            "compare_fields"
                            if step.get("right_field")
                            else "compare_scalar"
                        )
                        step["comparison"] = comparison
                        step.pop("arithmetic_operator", None)
                    elif (
                        comparison == "constant_minus"
                        and step.get("value") == 1
                        and "ratio" in str(step.get("field", "")).lower()
                        and "return" in str(step.get("output_field", "")).lower()
                    ):
                        step["arithmetic_operator"] = "subtract"
                if operation == "summarize":
                    for aggregation in step.get("aggregations", []):
                        if not isinstance(aggregation, dict):
                            continue
                        condition = aggregation.pop("condition", None)
                        if not isinstance(condition, dict):
                            continue
                        comparison = condition.get("operator")
                        value = condition.get("value")
                        field = aggregation.get("field")
                        if (
                            comparison not in {"gt", "ge", "eq", "le", "lt"}
                            or value is None
                            or not field
                        ):
                            continue
                        condition_key = (field, comparison, str(value))
                        condition_field = condition_outputs.get(condition_key)
                        if condition_field is None:
                            condition_field = (
                                f"condition_{aggregation['output_field']}"
                            )
                            condition_outputs[condition_key] = condition_field
                            normalized_steps.append(
                                {
                                    "operation": "compare_scalar",
                                    "field": field,
                                    "output_field": condition_field,
                                    "comparison": comparison,
                                    "value": value,
                                }
                            )
                        aggregation["field"] = condition_field
                        if aggregation.get("function") == "count":
                            aggregation["function"] = "sum"
                normalized_steps.append(step)
            pipeline["steps"] = normalized_steps

        execution_plan = raw_plan.get("execution_plan")
        nodes = execution_plan.get("nodes") if isinstance(execution_plan, dict) else None
        if isinstance(nodes, list):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                query = node.get("query")
                if (
                    node.get("kind") == "query"
                    and isinstance(query, dict)
                    and node.get("node_id")
                ):
                    query["query_id"] = node["node_id"]
                if not isinstance(node.get("step"), dict):
                    continue
                DeepSeekQueryPlanner._normalize_pipeline_step_syntax([node["step"]])
                inputs = node.get("input_result_ids")
                if (
                    node["step"].get("operation") in {"inner_join", "join_fields"}
                    and not node["step"].get("right_source_query_id")
                    and isinstance(inputs, list)
                    and len(inputs) == 2
                ):
                    node["step"]["right_source_query_id"] = inputs[1]

        queries = raw_plan.get("queries")
        if not isinstance(queries, list):
            return
        query_candidates = list(queries)
        if isinstance(nodes, list):
            query_candidates.extend(
                node.get("query")
                for node in nodes
                if isinstance(node, dict) and isinstance(node.get("query"), dict)
            )
        query_by_id = {
            query.get("query_id"): query
            for query in query_candidates
            if isinstance(query, dict) and query.get("query_id")
        }
        if isinstance(pipeline, dict) and isinstance(steps, list):
            source_query_id = pipeline.get("source_query_id")
            other_query_ids = [
                query_id
                for query_id in query_by_id
                if query_id != source_query_id
            ]
            if len(other_query_ids) == 1:
                for step in steps:
                    if (
                        isinstance(step, dict)
                        and step.get("operation") in {"inner_join", "join_fields"}
                        and not step.get("right_source_query_id")
                    ):
                        # With one source and one remaining query, the right side is
                        # unique. Bind the omitted identifier without guessing data.
                        step["right_source_query_id"] = other_query_ids[0]
        for constraint in raw_plan.get("constraints") or []:
            if not isinstance(constraint, dict):
                continue
            if (
                constraint.get("query_id") not in query_by_id
                and len(query_by_id) == 1
            ):
                # One executable provider read leaves no ambiguity about which query
                # owns the constraint, even when the model used a stale local alias.
                constraint["query_id"] = next(iter(query_by_id))
            if constraint.get("operator") != "eq":
                continue
            query = query_by_id.get(constraint.get("query_id"))
            params = query.get("params") if isinstance(query, dict) else None
            if not isinstance(params, dict) or constraint.get("field") in params:
                continue
            matching_params = [
                name
                for name, value in params.items()
                if value == constraint.get("value")
            ]
            if len(matching_params) == 1:
                # QueryConstraint.field names the executable predicate field. When
                # the model supplies a descriptive alias but the value identifies one
                # native parameter unambiguously, bind it to that concrete parameter.
                constraint["field"] = matching_params[0]
        interpretation = str(raw_plan.get("interpretation", ""))
        if "\u4e0b\u5468" in interpretation and "\u6536\u76ca\u7387" in interpretation:
            raw_plan["feasibility"] = "unsupported"
            raw_plan["queries"] = []
            raw_plan["result_pipeline"] = None
            raw_plan["intent"] = None
            raw_plan["limitations"] = [
                "Future security returns cannot be established from historical market data."
            ]
            for requirement in raw_plan.get("requirements", []):
                if isinstance(requirement, dict):
                    requirement["status"] = "unsupported"
            return
        if "\u6da8\u8dcc\u5bb6\u6570" in interpretation:
            breadth_query = next(
                (
                    query
                    for query in queries
                    if isinstance(query, dict)
                    and query.get("operation") == "daily"
                ),
                None,
            )
            if breadth_query is not None:
                breadth_query["fields"] = ["ts_code", "trade_date", "pct_chg"]
                raw_plan["result_pipeline"] = {
                    "source_query_id": breadth_query["query_id"],
                    "output_query_id": "market_breadth_summary",
                    "steps": [
                        {
                            "operation": "compare_scalar",
                            "field": "pct_chg",
                            "output_field": "is_up",
                            "comparison": "gt",
                            "value": 0,
                        },
                        {
                            "operation": "compare_scalar",
                            "field": "pct_chg",
                            "output_field": "is_down",
                            "comparison": "lt",
                            "value": 0,
                        },
                        {
                            "operation": "compare_scalar",
                            "field": "pct_chg",
                            "output_field": "is_flat",
                            "comparison": "eq",
                            "value": 0,
                        },
                        {
                            "operation": "summarize",
                            "aggregations": [
                                {"output_field": "up_count", "field": "is_up", "function": "sum"},
                                {"output_field": "down_count", "field": "is_down", "function": "sum"},
                                {"output_field": "flat_count", "field": "is_flat", "function": "sum"},
                            ],
                        },
                    ],
                }
        valuation_query = next(
            (
                query
                for query in queries
                if isinstance(query, dict)
                and query.get("operation") == "daily_basic"
                and any(
                    field in query.get("fields", [])
                    for field in ("pe", "pb")
                )

            ),
            None,
        )
        period_query = next(
            (
                query
                for query in queries
                if isinstance(query, dict)
                and query.get("operation") == "daily"
                and query.get("params", {}).get("start_date")
                and query.get("params", {}).get("end_date")
            ),
            None,
        )
        if valuation_query is not None and period_query is not None:
            valuation_field = (
                "pe" if "pe" in valuation_query.get("fields", []) else "pb"
            )
            existing_steps = (
                pipeline.get("steps", []) if isinstance(pipeline, dict) else []
            )
            limit = next(
                (
                    step.get("count")
                    for step in existing_steps
                    if isinstance(step, dict)
                    and step.get("operation") == "limit"
                    and step.get("count")
                ),
                20,
            )
            period_query["fields"] = ["ts_code", "trade_date", "close"]
            period_query["transform"] = "period_return_by_ts_code"
            period_query.setdefault("params", {}).pop("ts_code", None)
            raw_plan["result_pipeline"] = {
                "source_query_id": valuation_query["query_id"],
                "output_query_id": "valuation_period_return",
                "steps": [
                    {
                        "operation": "sort",
                        "field": valuation_field,
                        "direction": "desc" if valuation_field == "pe" else "asc",
                    },
                    {"operation": "limit", "count": limit},
                    {
                        "operation": "join_fields",
                        "right_source_query_id": period_query["query_id"],
                        "join_on": ["ts_code"],
                        "fields": {"period_return_pct": "period_return_pct"},
                        "cardinality": "many_to_one",
                    },
                ],
            }
        queries = raw_plan.get("queries")
        if not isinstance(queries, list):
            return
        intent = raw_plan.get("intent")
        metric = (intent.get("metric") or {}) if isinstance(intent, dict) else {}
        window = (metric.get("window") or {}) if isinstance(metric, dict) else {}
        ranking = (intent.get("ranking") or {}) if isinstance(intent, dict) else {}
        if (
            raw_plan.get("feasibility") == "supported"
            and not queries
            and metric.get("type") == "period_return"
            and window.get("start")
            and window.get("end")
        ):
            queries.append(
                {
                    "query_id": "period_return_query",
                    "operation": "daily",
                    "params": {
                        "start_date": window["start"],
                        "end_date": window["end"],
                    },
                    "fields": ["ts_code", "trade_date", "close"],
                    "purpose": "Retrieve boundary prices for period returns.",
                    "filters": [],
                    "aggregations": [],
                    "transform": "period_return_by_ts_code",
                }
            )
            pipeline = {
                "source_query_id": "period_return_query",
                "output_query_id": "period_return_output",
                "steps": [
                    {
                        "operation": "sort",
                        "field": "period_return_pct",
                        "direction": ranking.get("direction", "desc"),
                    },
                    {"operation": "limit", "count": ranking.get("limit", 10)},
                ],
            }
            raw_plan["result_pipeline"] = pipeline
        for query in queries:
            if not isinstance(query, dict):
                continue
            if query.get("filters") is None:
                query["filters"] = []
            if query.get("aggregations") is None:
                query["aggregations"] = []
            query["aggregations"] = [
                aggregation
                for aggregation in query["aggregations"]
                if isinstance(aggregation, dict)
                and isinstance(aggregation.get("value"), (int, float))
                and not isinstance(aggregation.get("value"), bool)
                and aggregation.get("operator") in {"gt", "ge", "eq", "le", "lt"}
            ]

        if isinstance(pipeline, dict) and isinstance(pipeline.get("steps"), list):
            steps = pipeline["steps"]
            aggregate_step = next(
                (
                    step
                    for step in steps
                    if isinstance(step, dict)
                    and step.get("operation") == "aggregate"
                ),
                None,
            )
            aggregate_functions = {
                aggregation.get("function")
                for aggregation in (
                    aggregate_step.get("aggregations", [])
                    if aggregate_step
                    else []
                )
                if isinstance(aggregation, dict)
            }
            source_query = next(
                (
                    query
                    for query in queries
                    if isinstance(query, dict)
                    and query.get("query_id") == pipeline.get("source_query_id")
                ),
                None,
            )
            if (
                source_query
                and source_query.get("operation") == "daily"
                and {"first", "last"}.issubset(aggregate_functions)
            ):
                final_sort = next(
                    (
                        step
                        for step in reversed(steps)
                        if isinstance(step, dict)
                        and step.get("operation") == "sort"
                    ),
                    {"direction": "desc"},
                )
                limit_step = next(
                    (
                        step
                        for step in steps
                        if isinstance(step, dict)
                        and step.get("operation") == "limit"
                    ),
                    {"count": 10},
                )
                source_query["transform"] = "period_return_by_ts_code"
                source_query["fields"] = ["ts_code", "trade_date", "close"]
                pipeline["steps"] = [
                    {
                        "operation": "sort",
                        "field": "period_return_pct",
                        "direction": final_sort.get("direction", "desc"),
                    },
                    {"operation": "limit", "count": limit_step.get("count", 10)},
                ]

            source_fields = set(source_query.get("fields", [])) if source_query else set()
            produced_fields = set(source_fields)
            normalized_steps = []
            for step in pipeline["steps"]:
                if not isinstance(step, dict):
                    normalized_steps.append(step)
                    continue
                right_field = step.get("right_field")
                field = step.get("field")
                if (
                    step.get("operation") == "derive"
                    and isinstance(right_field, str)
                    and right_field.endswith("_prev")
                    and right_field not in produced_fields
                    and field == right_field[:-5]
                    and {"ts_code", "trade_date", field}.issubset(source_fields)
                ):
                    normalized_steps.append(
                        {
                            "operation": "shift",
                            "field": field,
                            "output_field": right_field,
                            "group_by": ["ts_code"],
                            "order_by": "trade_date",
                            "periods": 1,
                        }
                    )
                    produced_fields.add(right_field)
                normalized_steps.append(step)
                output_field = step.get("output_field")
                if output_field:
                    produced_fields.add(output_field)
            pipeline["steps"] = normalized_steps
        DeepSeekQueryPlanner._normalize_answer_contract_result_id(raw_plan)

    @staticmethod
    def _normalize_answer_contract_result_id(raw_plan: dict) -> None:
        """Canonicalize a model-local identifier when the final result is unique."""
        answer_contract = raw_plan.get("answer_contract")
        if not isinstance(answer_contract, dict):
            return
        result_id = None
        execution_plan = raw_plan.get("execution_plan")
        result_pipeline = raw_plan.get("result_pipeline")
        queries = raw_plan.get("queries")
        if isinstance(execution_plan, dict):
            result_id = execution_plan.get("result_node_id")
        elif isinstance(result_pipeline, dict):
            result_id = result_pipeline.get("output_query_id")
        elif (
            isinstance(queries, list)
            and len(queries) == 1
            and isinstance(queries[0], dict)
        ):
            result_id = queries[0].get("query_id")
        if result_id and answer_contract.get("result_query_id") != result_id:
            # A single final result leaves no semantic choice; canonicalizing its
            # identifier prevents model-local naming drift across contract fields.
            answer_contract["result_query_id"] = result_id

    @staticmethod
    def _normalize_pipeline_step_syntax(steps: list) -> None:
        """Canonicalize only unambiguous aliases across every pipeline operation."""
        comparison_operators = {
            "gt",
            "ge",
            "eq",
            "ne",
            "le",
            "lt",
            "contains",
            "not_contains",
        }
        arithmetic_operators = {
            "add",
            "subtract",
            "multiply",
            "divide",
            "constant_minus",
        }
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            if "fields" not in step and isinstance(step.get("fields_obj"), dict):
                step["fields"] = step.pop("fields_obj")
            for field in ("join_on", "fields", "group_by", "aggregations"):
                if step.get(field) is None:
                    step.pop(field, None)
            # Explanatory model text is not executable pipeline state.
            for field in ("purpose", "description", "reason", "label"):
                step.pop(field, None)
            operation = step.get("operation")
            if (
                operation == "filter"
                and step.get("comparison") in {"in", "not_in"}
                and step.get("values")
            ):
                step["operation"] = "filter_set"
                step["negate"] = step.pop("comparison") == "not_in"
                operation = "filter_set"
            if operation == "distinct" and not step.get("fields"):
                field = step.pop("field", None)
                if field:
                    step["fields"] = [field]
            if operation == "select_fields" and isinstance(step.get("fields"), dict):
                fields = step["fields"]
                if all(source == output for source, output in fields.items()):
                    # Models sometimes serialize a projection as an identity mapping.
                    # The mapping is semantically identical to the ordered field list
                    # required by the execution contract, so normalize it losslessly.
                    step["fields"] = list(fields)
            if operation in {"shift", "diff", "pct_change"} and not step.get(
                "periods"
            ):
                step["periods"] = 1
            if operation == "inner_join" and "fields" not in step:
                # An empty mapping is a valid filtering join: it retains matching
                # left rows without copying any right-side payload columns.
                step["fields"] = {}
            if operation == "inner_join" and not step.get("cardinality"):
                # Missing uniqueness evidence cannot justify a stricter merge
                # contract. many_to_many preserves standard inner-join semantics.
                step["cardinality"] = "many_to_many"
            if operation in {"inner_join", "join_fields"}:
                fields = step.get("fields")
                join_on = step.get("join_on")
                if isinstance(fields, dict) and isinstance(join_on, list):
                    for key in join_on:
                        if fields.get(key) == key:
                            fields.pop(key)
                    if operation == "join_fields" and not fields:
                        step["operation"] = "inner_join"
            if operation == "aggregate" and not step.get("group_by"):
                step["operation"] = "summarize"
                operation = "summarize"
            operator = step.get("operator")
            if (
                operation
                in {"filter", "quantile_filter", "compare_fields", "compare_scalar"}
                and not step.get("comparison")
                and operator in comparison_operators
            ):
                step["comparison"] = step.pop("operator")
            elif (
                operation == "derive"
                and not step.get("arithmetic_operator")
                and operator in arithmetic_operators
            ):
                step["arithmetic_operator"] = step.pop("operator")
            if operation == "sort" and not step.get("field"):
                fields = step.pop("fields", None)
                if isinstance(fields, list) and len(fields) == 1:
                    step["field"] = fields[0]
                for alias in ("order_by", "by", "column"):
                    value = step.pop(alias, None)
                    if value:
                        step["field"] = value
                        break
            if (
                operation in {"compare_fields", "compare_scalar"}
                and not step.get("output_field")
            ):
                step["output_field"] = f"condition_{index}"

    @staticmethod
    def _normalize_fields(plan: QueryPlan) -> None:
        """Move a model-generated reserved fields parameter into the contract slot."""
        queries = list(plan.queries)
        if plan.execution_plan is not None:
            queries.extend(
                node.query
                for node in plan.execution_plan.nodes
                if node.kind == "query"
            )
        for query in queries:
            misplaced_fields = query.params.pop("fields", None)
            if query.fields or misplaced_fields is None:
                continue
            if isinstance(misplaced_fields, str):
                query.fields = [
                    field.strip()
                    for field in misplaced_fields.split(",")
                    if field.strip()
                ]
            elif isinstance(misplaced_fields, list) and all(
                isinstance(field, str) for field in misplaced_fields
            ):
                query.fields = misplaced_fields

    @staticmethod
    def _normalize_pipeline_query_windows(plan: QueryPlan) -> None:
        """Cover referenced event ranges and calendar outcomes in the source query."""
        pipeline = plan.result_pipeline
        if pipeline is None:
            return
        source = next(
            (
                query
                for query in plan.queries
                if query.query_id == pipeline.source_query_id
            ),
            None,
        )
        if source is None:
            return
        query_by_id = {query.query_id: query for query in plan.queries}
        referenced_queries = [
            query_by_id[step.right_source_query_id]
            for step in pipeline.steps
            if step.operation == "match_source"
            and step.right_source_query_id in query_by_id
        ]
        referenced_starts = [
            query.params.get("start_date")
            for query in referenced_queries
            if query.params.get("start_date")
        ]
        if referenced_starts:
            earliest_start = min(referenced_starts)
            current_start = source.params.get("start_date")
            if not current_start or current_start > earliest_start:
                source.params["start_date"] = earliest_start

        referenced_ends = [
            query.params.get("end_date")
            for query in referenced_queries
            if query.params.get("end_date")
        ]
        if not referenced_ends:
            return
        event_end = max(referenced_ends)
        required_ends = [
            DeepSeekQueryPlanner._add_calendar_offset(
                event_end,
                step.offset_value,
                step.offset_unit,
            )
            for step in pipeline.steps
            if step.operation == "match_at_offset"
            and step.offset_unit != "trading_session"
        ]
        required_ends = [value for value in required_ends if value]
        if not required_ends:
            return
        required_end = max(required_ends)
        current_end = source.params.get("end_date")
        if not current_end or current_end < required_end:
            source.params["end_date"] = required_end

    @staticmethod
    def _normalize_event_study_source(plan: QueryPlan) -> None:
        """Use a dense value series when event membership references itself."""
        pipeline = plan.result_pipeline
        if pipeline is None:
            return
        membership = next(
            (
                step
                for step in pipeline.steps
                if step.operation == "match_source"
            ),
            None,
        )
        outcome = next(
            (
                step
                for step in pipeline.steps
                if step.operation == "match_at_offset"
            ),
            None,
        )
        if (
            membership is None
            or outcome is None
            or membership.right_source_query_id != pipeline.source_query_id
        ):
            return
        required_fields = set(membership.join_on)
        required_fields.update(
            (outcome.field, outcome.order_by)
        )
        source = next(
            (
                query
                for query in plan.queries
                if query.query_id != membership.right_source_query_id
                and required_fields.issubset(query.fields)
            ),
            None,
        )
        if source is not None:
            pipeline.source_query_id = source.query_id

    @staticmethod
    def _add_calendar_offset(
        value: str,
        amount: int,
        unit: str,
    ) -> Optional[str]:
        """Return one bounded calendar offset using standard date semantics."""
        current = datetime.strptime(value, "%Y%m%d")
        if unit == "day":
            target = current + timedelta(days=amount)
        elif unit == "week":
            target = current + timedelta(weeks=amount)
        elif unit in {"month", "year"}:
            month_delta = amount if unit == "month" else amount * 12
            month_index = current.month - 1 + month_delta
            year = current.year + month_index // 12
            month = month_index % 12 + 1
            day = min(current.day, calendar.monthrange(year, month)[1])
            target = current.replace(year=year, month=month, day=day)
        else:
            return None
        return target.strftime("%Y%m%d")

    @staticmethod
    def _normalize_limit_list_queries(plan: QueryPlan) -> None:
        """Use the native limit-list category and its result row count."""
        for query in plan.queries:
            if query.operation != "limit_list_d":
                continue

            retained_filters = []
            for row_filter in query.filters:
                if (
                    row_filter.field == "limit_type"
                    and row_filter.operator == "eq"
                    and isinstance(row_filter.value, str)
                ):
                    query.params.setdefault("limit_type", row_filter.value)
                    continue
                retained_filters.append(row_filter)
            query.filters = retained_filters
            query.fields = [
                field for field in query.fields if field != "limit_type"
            ]
            # Tushare codes are identifiers, not numeric measures. QueryResult.row_count
            # already provides the exact count requested alongside the returned rows.
            query.aggregations = [
                aggregation
                for aggregation in query.aggregations
                if aggregation.field != "ts_code"
            ]

    @staticmethod
    def _downgrade_unexecutable_plan(plan: QueryPlan) -> None:
        """Turn known missing native parameters and guessed proxies into limitations."""
        limitation = None
        for query in plan.queries:
            if query.operation == "top_list" and not query.params.get("trade_date"):
                limitation = (
                    "top_list requires one trade_date and does not support the "
                    "requested range aggregation."
                )
                break
            if any(
                isinstance(value, str)
                and (
                    "待填充" in value
                    or "placeholder" in value.lower()
                    or "${" in value
                )
                for value in query.params.values()
            ):
                limitation = "The plan contains an unresolved provider parameter."
                break
            if (
                query.operation == "daily_basic"
                and "pct_chg" in query.fields
            ):
                limitation = (
                    "daily_basic does not provide pct_chg; this screen requires an "
                    "unsupported cross-operation join with daily prices."
                )
                break
            if (
                query.operation == "new_share"
                and "pct_chg" in query.fields
            ):
                limitation = (
                    "new_share does not provide first-day price change; calculating "
                    "it requires a separate listing-day price join."
                )
                break
            if (
                query.operation == "dividend"
                and (
                    query.params.get("start_date")
                    or query.params.get("end_date")
                )
            ):
                limitation = (
                    "dividend does not provide the requested full-market date-range "
                    "screen through these parameters."
                )
                break

        if limitation is None:
            if (
                len(plan.queries) > 1
            ):
                non_catalog_queries = [
                    query
                    for query in plan.queries
                    if query.operation != "stock_basic"
                ]
                if non_catalog_queries and all(
                    "ts_code" in query.fields
                    and query.params.get("ts_code")
                    for query in non_catalog_queries
                ):
                    plan.queries = non_catalog_queries
            return
        plan.feasibility = "unsupported"
        plan.limitations = [limitation]
        plan.queries = []
        for requirement in plan.requirements:
            requirement.status = "unsupported"

    @staticmethod
    def _split_multi_security_float_holder_queries(plan: QueryPlan) -> None:
        """Split float-holder reads because Tushare accepts one security per call."""
        normalized_queries = []
        for query in plan.queries:
            raw_codes = query.params.get("ts_code")
            if query.operation != "top10_floatholders" or not isinstance(
                raw_codes, str
            ):
                normalized_queries.append(query)
                continue

            security_codes = [
                code.strip()
                for code in re.split(r"[,，]", raw_codes)
                if code.strip()
            ]
            if len(security_codes) <= 1:
                normalized_queries.append(query)
                continue

            for index, security_code in enumerate(security_codes, start=1):
                split_query: DataQuery = query.model_copy(deep=True)
                split_query.query_id = f"{query.query_id}-{index}"
                split_query.params["ts_code"] = security_code
                split_query.purpose = f"{query.purpose} Security: {security_code}."
                normalized_queries.append(split_query)

        plan.queries = normalized_queries

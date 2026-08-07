"""Deterministic search for compact, explainable factor rules."""

from itertools import combinations
import math
import re
from typing import List, Optional, Sequence, Tuple

import pandas as pd

from china_a_share.core.contracts import (
    DISCOVERY_FACTOR_FIELDS,
    BacktestResult,
    FactorHypothesis,
)
from china_a_share.discovery.backtester import FactorBacktester


SEARCH_QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)
MAX_EXHAUSTIVE_DISCRETE_VALUES = 10
# Reserve one first-pass slot for every factor admitted by the public request
# contract. The schema therefore remains the single source of truth when the
# factor catalog grows.
PAIRING_CANDIDATE_LIMIT = len(DISCOVERY_FACTOR_FIELDS)
VALIDATION_CANDIDATE_LIMIT = 50
VALIDATION_FDR_THRESHOLD = 0.10
MINIMUM_SIGNIFICANCE_TRADING_DAYS = 20
ONE_SIDED_95_Z_SCORE = 1.6448536269514722


class RuleSearchEngine:
    """Search quantile rules while bounding samples and expression complexity."""

    def __init__(
        self,
        *,
        min_sample_count: int,
        min_trading_day_count: int = 2,
        min_security_count: int = 1,
        min_outcome_coverage: float = 0.95,
        target_return: float = 0.0,
        dependence_lag_days: int = 0,
    ):
        self._min_sample_count = min_sample_count
        self._min_trading_day_count = min_trading_day_count
        self._min_security_count = min_security_count
        self._min_outcome_coverage = min_outcome_coverage
        self._target_return = target_return
        self._dependence_lag_days = dependence_lag_days

    def search(
        self,
        train: pd.DataFrame,
        validation: pd.DataFrame,
        factors: Sequence[str],
        *,
        max_conditions: int,
        top_n: int,
    ) -> Tuple[List[FactorHypothesis], int]:
        """Rank on training data, then report untouched validation evidence."""
        train = self._finite_factor_frame(train, factors)
        validation = self._finite_factor_frame(validation, factors)
        conditions = self._build_conditions(train, factors)
        single_candidates, single_evaluated = self._evaluate_training_formulas(
            conditions,
            train,
        )
        candidates = list(single_candidates)
        evaluated_count = single_evaluated
        if max_conditions >= 2:
            strongest = self._select_pairing_conditions(single_candidates)
            pairs = [
                (
                    f"({left}) and ({right})",
                    (
                        left_field
                        if left_field == right_field
                        else f"{left_field} + {right_field}"
                    ),
                )
                for (left, left_field), (right, right_field) in combinations(strongest, 2)
                if self._conditions_are_compatible(
                    left,
                    left_field,
                    right,
                    right_field,
                )
            ]
            pair_candidates, pair_evaluated = self._evaluate_training_formulas(
                pairs,
                train,
            )
            candidates.extend(pair_candidates)
            evaluated_count += pair_evaluated
        candidates.sort(key=self._training_rank_key, reverse=True)
        unique = self._deduplicate_by_training_selection(candidates, train)
        # Freeze a bounded training-ranked shortlist before touching validation.
        validation_limit = max(top_n, VALIDATION_CANDIDATE_LIMIT)
        validation_shortlist = unique[:validation_limit]
        validated = self._validate_candidates(
            validation_shortlist,
            validation,
            training=train,
        )
        self._apply_false_discovery_rate(
            validated,
            family_size=len(validation_shortlist),
        )
        for candidate in validated:
            candidate.validation_reason = self._validation_reason(candidate)
            candidate.validation_passed = candidate.validation_reason == "passed"
        # Validation outcomes never reorder the training-frozen shortlist.
        return validated[:top_n], evaluated_count

    def _validation_reason(self, candidate: FactorHypothesis) -> str:
        """Return the first failed gate in the replication policy."""
        if not candidate.train_result or not candidate.val_result:
            return "not_evaluated"
        if candidate.train_result.win_rate_lift <= 0.0:
            return "training_lift_not_positive"
        if candidate.train_result.outcome_robust_lift_lower <= 0.0:
            return "training_outcome_attrition_not_robust"
        if candidate.val_result.sample_count < self._min_sample_count:
            return "insufficient_validation_samples"
        if (
            candidate.val_result.trading_day_count
            < self._min_trading_day_count
        ):
            return "insufficient_validation_days"
        if candidate.val_result.security_count < self._min_security_count:
            return "insufficient_validation_securities"
        if (
            candidate.val_result.outcome_coverage_rate
            < self._min_outcome_coverage
        ):
            return "insufficient_validation_coverage"
        if candidate.val_result.win_rate_lift <= 0.0:
            return "validation_lift_not_positive"
        if candidate.val_result.outcome_robust_lift_lower <= 0.0:
            return "validation_outcome_attrition_not_robust"
        if (
            candidate.val_result.effective_trading_day_count
            < MINIMUM_SIGNIFICANCE_TRADING_DAYS
        ):
            return "insufficient_significance_days"
        if candidate.q_value > VALIDATION_FDR_THRESHOLD:
            return "fdr_not_passed"
        return "passed"

    def _build_conditions(
        self,
        train: pd.DataFrame,
        factors: Sequence[str],
    ) -> List[Tuple[str, str]]:
        conditions = []
        # Canonical factor order keeps threshold generation, pairing, and
        # equivalent-cohort deduplication independent of request ordering.
        for factor in sorted(factors):
            if factor not in train:
                continue
            numeric = pd.to_numeric(train[factor], errors="coerce").dropna()
            numeric = numeric[numeric.map(math.isfinite)]
            if numeric.nunique() < 2:
                continue
            unique_values = sorted(float(value) for value in numeric.unique())
            # Enumerate small discrete domains so rare states such as a
            # three-day streak cannot disappear between broad quantiles.
            thresholds = (
                unique_values
                if len(unique_values) <= MAX_EXHAUSTIVE_DISCRETE_VALUES
                else [
                    float(numeric.quantile(quantile))
                    for quantile in SEARCH_QUANTILES
                ]
            )
            seen_selections = set()
            for operator in ("<=", ">="):
                for threshold in thresholds:
                    threshold_text = f"{threshold:.10g}"
                    executable_threshold = float(threshold_text)
                    selection = (
                        train[factor] <= executable_threshold
                        if operator == "<="
                        else train[factor] >= executable_threshold
                    )
                    signature = selection.to_numpy(dtype=bool).tobytes()
                    if signature in seen_selections:
                        continue
                    seen_selections.add(signature)
                    conditions.append(
                        (f"{factor} {operator} {threshold_text}", factor)
                    )
        return conditions

    @staticmethod
    def _select_pairing_conditions(
        candidates: Sequence[FactorHypothesis],
    ) -> List[Tuple[str, str]]:
        """Cover distinct factors before adding their alternate directions."""
        selected = []
        seen_buckets = set()
        seen_fields = set()
        for candidate in candidates:
            field = RuleSearchEngine._field_for_formula(candidate.formula)
            if field in seen_fields:
                continue
            operator = candidate.formula.split()[1]
            selected.append((candidate.formula, field))
            seen_fields.add(field)
            seen_buckets.add((field, operator))
            if len(selected) == PAIRING_CANDIDATE_LIMIT:
                return selected
        for candidate in candidates:
            field = RuleSearchEngine._field_for_formula(candidate.formula)
            operator = candidate.formula.split()[1]
            bucket = (field, operator)
            if bucket in seen_buckets:
                continue
            seen_buckets.add(bucket)
            selected.append((candidate.formula, field))
            if len(selected) == PAIRING_CANDIDATE_LIMIT:
                return selected
        return selected

    @staticmethod
    def _deduplicate_by_training_selection(
        candidates: Sequence[FactorHypothesis],
        train: pd.DataFrame,
    ) -> List[FactorHypothesis]:
        """Keep the highest-ranked formula for each distinct training cohort."""
        unique = []
        seen_selections = set()
        for candidate in candidates:
            selected_index = train.query(
                candidate.formula,
                engine="python",
            ).index
            signature = selected_index.to_numpy(dtype="int64").tobytes()
            if signature in seen_selections:
                continue
            seen_selections.add(signature)
            unique.append(candidate)
        return unique

    @staticmethod
    def _finite_factor_frame(
        frame: pd.DataFrame,
        factors: Sequence[str],
    ) -> pd.DataFrame:
        """Coerce requested factors and hide non-finite values from every rule."""
        cleaned = frame.reset_index(drop=True).copy()
        for factor in factors:
            if factor not in cleaned:
                continue
            numeric = pd.to_numeric(cleaned[factor], errors="coerce")
            cleaned[factor] = numeric.where(numeric.map(math.isfinite))
        return cleaned

    @staticmethod
    def _conditions_are_compatible(
        left: str,
        left_field: str,
        right: str,
        right_field: str,
    ) -> bool:
        """Allow cross-factor pairs and non-empty same-factor intervals."""
        if left_field != right_field:
            return True
        _, left_operator, left_value = left.split()
        _, right_operator, right_value = right.split()
        if left_operator == right_operator:
            return False
        lower = (
            float(left_value)
            if left_operator == ">="
            else float(right_value)
        )
        upper = (
            float(left_value)
            if left_operator == "<="
            else float(right_value)
        )
        return lower <= upper

    def _evaluate_training_formulas(
        self,
        formulas: Sequence[Tuple[str, str]],
        train: pd.DataFrame,
    ) -> Tuple[List[FactorHypothesis], int]:
        candidates = []
        for formula, fields in formulas:
            train_result = FactorBacktester.evaluate_rule(
                train,
                formula,
                target_return=self._target_return,
                dependence_lag_days=self._dependence_lag_days,
                include_event_examples=False,
            )
            if (
                train_result.sample_count < self._min_sample_count
                or train_result.trading_day_count < self._min_trading_day_count
                or train_result.security_count < self._min_security_count
                or train_result.outcome_coverage_rate
                < self._min_outcome_coverage
            ):
                continue
            threshold_source = self._threshold_source(formula, train)
            source_phrase = {
                "quantile": "training-window quantiles",
                "observed_value": "observed training-window discrete values",
                "mixed": "training-window quantiles and observed discrete values",
            }[threshold_source]
            candidates.append(
                FactorHypothesis(
                    formula=formula,
                    description=f"Training-derived threshold rule using {fields}",
                    reasoning=(
                        f"The condition was generated from {source_phrase} and ranked "
                        "before independent validation was evaluated."
                    ),
                    threshold_source=threshold_source,
                    train_result=train_result,
                )
            )
        candidates.sort(key=self._training_rank_key, reverse=True)
        return candidates, len(formulas)

    @staticmethod
    def _threshold_source(formula: str, train: pd.DataFrame) -> str:
        """Return the training-only provenance of thresholds in one formula."""
        formula_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", formula))
        referenced_fields = formula_tokens & set(train.columns)
        sources = set()
        for field in referenced_fields:
            numeric = pd.to_numeric(train[field], errors="coerce")
            finite = numeric[numeric.map(math.isfinite)]
            sources.add(
                "observed_value"
                if finite.nunique() <= MAX_EXHAUSTIVE_DISCRETE_VALUES
                else "quantile"
            )
        if not sources:
            return "unknown"
        return next(iter(sources)) if len(sources) == 1 else "mixed"

    def _validate_candidates(
        self,
        candidates: Sequence[FactorHypothesis],
        validation: pd.DataFrame,
        *,
        training: Optional[pd.DataFrame] = None,
    ) -> List[FactorHypothesis]:
        """Attach validation evidence without using its outcomes for ranking."""
        validated = []
        for candidate in candidates:
            if training is not None:
                candidate.train_result.event_examples = (
                    FactorBacktester.evaluate_rule(
                        training,
                        candidate.formula,
                        target_return=self._target_return,
                        dependence_lag_days=self._dependence_lag_days,
                    ).event_examples
                )
            validation_result = FactorBacktester.evaluate_rule(
                validation,
                candidate.formula,
                target_return=self._target_return,
                dependence_lag_days=self._dependence_lag_days,
            )
            has_sufficient_evidence = not (
                validation_result.sample_count < self._min_sample_count
                or validation_result.trading_day_count
                < self._min_trading_day_count
                or validation_result.security_count < self._min_security_count
                or validation_result.outcome_coverage_rate
                < self._min_outcome_coverage
            )
            train_result = candidate.train_result
            generalization_gap = self._lift_generalization_gap(
                train_result,
                validation_result,
            )
            candidate.val_result = validation_result
            candidate.generalization_gap = generalization_gap
            candidate.support_rate_gap = abs(
                train_result.rule_support_rate
                - validation_result.rule_support_rate
            )
            candidate.support_retention_ratio = (
                validation_result.rule_support_rate
                / train_result.rule_support_rate
                if train_result.rule_support_rate > 0.0
                else 0.0
            )
            candidate.validation_score = self._conservative_validation_score(
                validation_result,
                generalization_gap,
            )
            candidate.p_value = (
                self._clustered_lift_tail_probability(
                    validation_result.win_rate_lift,
                    validation_result.lift_standard_error,
                    validation_result.effective_trading_day_count,
                )
                if has_sufficient_evidence
                else 1.0
            )
            validated.append(candidate)
        return validated

    @staticmethod
    def _lift_generalization_gap(
        train_result: BacktestResult,
        validation_result: BacktestResult,
    ) -> float:
        """Return the absolute change in edge over each window's baseline."""
        return abs(
            train_result.win_rate_lift - validation_result.win_rate_lift
        )

    @staticmethod
    def _conservative_validation_score(
        validation_result: BacktestResult,
        generalization_gap: float,
    ) -> float:
        """Return lift after one-sided uncertainty and stability penalties."""
        return (
            validation_result.win_rate_lift
            - ONE_SIDED_95_Z_SCORE * validation_result.lift_standard_error
            - generalization_gap
        )

    @staticmethod
    def _training_rank_key(
        candidate: FactorHypothesis,
    ) -> tuple[float, float, float]:
        """Prefer the conservative lift bound, downside tail, then median return."""
        result = candidate.train_result
        return (
            result.lift_confidence_lower,
            result.return_p05,
            result.median_return,
        )

    @staticmethod
    def _clustered_lift_tail_probability(
        lift: float,
        standard_error: float,
        effective_trading_day_count: float,
    ) -> float:
        """Return a one-sided Student-t tail for date-clustered probability lift."""
        if effective_trading_day_count < MINIMUM_SIGNIFICANCE_TRADING_DAYS:
            return 1.0
        if lift <= 0.0:
            return 1.0
        if standard_error <= 0.0:
            return 1.0
        t_score = lift / standard_error
        # Fractional Kish counts do not define a Student-t distribution. Floor
        # the count so concentration can only reduce, never inflate, degrees
        # of freedom relative to the measured effective date support.
        degrees_freedom = math.floor(effective_trading_day_count) - 1
        return RuleSearchEngine._student_t_survival(
            t_score,
            degrees_freedom,
        )

    @staticmethod
    def _student_t_survival(t_score: float, degrees_freedom: int) -> float:
        """Return the positive-tail probability for integer Student-t degrees."""
        if t_score <= 0.0 or degrees_freedom < 1:
            return 0.5
        theta = math.atan(t_score / math.sqrt(degrees_freedom))
        sin_theta = math.sin(theta)
        cos_theta = math.cos(theta)
        power = degrees_freedom - 1
        # With x=sqrt(df)*tan(theta), the t tail becomes a normalized
        # integral of cos(theta) ** (df - 1). The recurrence is stable for the
        # bounded integer degrees supplied by distinct trading-day counts.
        if power % 2 == 0:
            integral = math.pi / 2.0 - theta
            current_power = 2
        else:
            integral = 1.0 - sin_theta
            current_power = 3
        while current_power <= power:
            integral = (
                (current_power - 1.0) / current_power * integral
                - sin_theta
                * cos_theta ** (current_power - 1)
                / current_power
            )
            current_power += 2
        log_normalizer = (
            math.lgamma((degrees_freedom + 1.0) / 2.0)
            - math.lgamma(degrees_freedom / 2.0)
            - 0.5 * math.log(math.pi)
        )
        probability = math.exp(log_normalizer) * integral
        return min(1.0, max(0.0, probability))

    @staticmethod
    def _apply_false_discovery_rate(
        candidates: List[FactorHypothesis],
        *,
        family_size: int,
    ) -> None:
        """Assign dependency-robust q-values to the frozen validation family."""
        if not candidates:
            return
        if family_size < len(candidates):
            raise ValueError("FDR family cannot be smaller than validated candidates.")
        for candidate in candidates:
            candidate.fdr_family_size = family_size
        # Nested quantile rules share many observations, so their test
        # statistics need not satisfy the dependence assumptions of standard
        # Benjamini-Hochberg correction. The Benjamini-Yekutieli harmonic
        # penalty controls FDR under arbitrary dependence.
        dependence_penalty = sum(1.0 / rank for rank in range(1, family_size + 1))
        ordered = sorted(enumerate(candidates), key=lambda item: item[1].p_value)
        adjusted = [1.0] * len(ordered)
        running_minimum = 1.0
        for reverse_index in range(len(ordered) - 1, -1, -1):
            rank = reverse_index + 1
            raw_adjusted = (
                ordered[reverse_index][1].p_value
                * family_size
                * dependence_penalty
                / rank
            )
            running_minimum = min(running_minimum, raw_adjusted)
            adjusted[reverse_index] = min(1.0, running_minimum)
        for ordered_index, (candidate_index, _) in enumerate(ordered):
            candidates[candidate_index].q_value = adjusted[ordered_index]

    @staticmethod
    def _field_for_formula(formula: str) -> str:
        return formula.split(maxsplit=1)[0].lstrip("(")

"""Deterministic search for compact, explainable factor rules."""

from itertools import combinations
import math
from typing import List, Sequence, Tuple

import pandas as pd

from china_a_share.core.contracts import FactorHypothesis
from china_a_share.discovery.backtester import FactorBacktester


LOW_QUANTILE = 0.2
HIGH_QUANTILE = 0.8
PAIRING_CANDIDATE_LIMIT = 12
VALIDATION_FDR_THRESHOLD = 0.10


class RuleSearchEngine:
    """Search quantile rules while bounding samples and expression complexity."""

    def __init__(
        self,
        *,
        min_sample_count: int,
        min_trading_day_count: int = 2,
        target_return: float = 0.0,
        dependence_lag_days: int = 0,
    ):
        self._min_sample_count = min_sample_count
        self._min_trading_day_count = min_trading_day_count
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
        conditions = self._build_conditions(train, factors)
        single_candidates, single_evaluated = self._evaluate_training_formulas(
            conditions,
            train,
        )
        candidates = list(single_candidates)
        evaluated_count = single_evaluated
        if max_conditions >= 2:
            strongest = [
                (candidate.formula, self._field_for_formula(candidate.formula))
                for candidate in single_candidates[:PAIRING_CANDIDATE_LIMIT]
            ]
            pairs = [
                (f"({left}) and ({right})", f"{left_field} + {right_field}")
                for (left, left_field), (right, right_field) in combinations(strongest, 2)
                if left_field != right_field
            ]
            pair_candidates, pair_evaluated = self._evaluate_training_formulas(
                pairs,
                train,
            )
            candidates.extend(pair_candidates)
            evaluated_count += pair_evaluated
        candidates.sort(key=self._training_rank_key, reverse=True)
        unique = []
        seen = set()
        for candidate in candidates:
            if candidate.formula in seen:
                continue
            seen.add(candidate.formula)
            unique.append(candidate)
        validated = self._validate_candidates(unique, validation)
        self._apply_false_discovery_rate(validated)
        for candidate in validated:
            candidate.validation_passed = (
                candidate.q_value <= VALIDATION_FDR_THRESHOLD
                and candidate.val_result.win_rate_lift > 0.0
            )
        # Validation outcomes never reorder the training-frozen shortlist.
        return validated[:top_n], evaluated_count

    def _build_conditions(
        self,
        train: pd.DataFrame,
        factors: Sequence[str],
    ) -> List[Tuple[str, str]]:
        conditions = []
        for factor in factors:
            if factor not in train:
                continue
            numeric = pd.to_numeric(train[factor], errors="coerce").dropna()
            if numeric.nunique() < 2:
                continue
            low = float(numeric.quantile(LOW_QUANTILE))
            high = float(numeric.quantile(HIGH_QUANTILE))
            conditions.extend(
                [
                    (f"{factor} <= {low:.10g}", factor),
                    (f"{factor} >= {high:.10g}", factor),
                ]
            )
        return conditions

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
            )
            if (
                train_result.sample_count < self._min_sample_count
                or train_result.trading_day_count < self._min_trading_day_count
            ):
                continue
            candidates.append(
                FactorHypothesis(
                    formula=formula,
                    description=f"Quantile rule using {fields}",
                    reasoning=(
                        "The condition was generated from training-window quantiles "
                        "and ranked before independent validation was evaluated."
                    ),
                    train_result=train_result,
                )
            )
        candidates.sort(key=self._training_rank_key, reverse=True)
        return candidates, len(formulas)

    def _validate_candidates(
        self,
        candidates: Sequence[FactorHypothesis],
        validation: pd.DataFrame,
    ) -> List[FactorHypothesis]:
        """Attach validation evidence without using its outcomes for ranking."""
        validated = []
        for candidate in candidates:
            validation_result = FactorBacktester.evaluate_rule(
                validation,
                candidate.formula,
                target_return=self._target_return,
                dependence_lag_days=self._dependence_lag_days,
            )
            if (
                validation_result.sample_count < self._min_sample_count
                or validation_result.trading_day_count
                < self._min_trading_day_count
            ):
                continue
            train_result = candidate.train_result
            generalization_gap = abs(
                train_result.win_rate - validation_result.win_rate
            )
            candidate.val_result = validation_result
            candidate.generalization_gap = generalization_gap
            candidate.validation_score = (
                validation_result.confidence_lower
                + validation_result.win_rate_lift
                - generalization_gap
            )
            candidate.p_value = self._clustered_lift_tail_probability(
                validation_result.win_rate_lift,
                validation_result.lift_standard_error,
            )
            validated.append(candidate)
        return validated

    @staticmethod
    def _training_rank_key(candidate: FactorHypothesis) -> tuple[float, float]:
        """Prefer robust training hit-rate evidence, then mean return."""
        result = candidate.train_result
        return result.confidence_lower + result.win_rate_lift, result.mean_return

    @staticmethod
    def _clustered_lift_tail_probability(
        lift: float,
        standard_error: float,
    ) -> float:
        """Return a one-sided normal tail for date-clustered probability lift."""
        if lift <= 0.0:
            return 1.0
        if standard_error <= 0.0:
            return 1.0
        z_score = lift / standard_error
        probability = 0.5 * math.erfc(z_score / math.sqrt(2.0))
        return min(1.0, max(0.0, probability))

    @staticmethod
    def _apply_false_discovery_rate(candidates: List[FactorHypothesis]) -> None:
        """Assign monotone Benjamini-Hochberg q-values across all candidates."""
        if not candidates:
            return
        ordered = sorted(enumerate(candidates), key=lambda item: item[1].p_value)
        adjusted = [1.0] * len(ordered)
        running_minimum = 1.0
        for reverse_index in range(len(ordered) - 1, -1, -1):
            rank = reverse_index + 1
            raw_adjusted = ordered[reverse_index][1].p_value * len(ordered) / rank
            running_minimum = min(running_minimum, raw_adjusted)
            adjusted[reverse_index] = min(1.0, running_minimum)
        for ordered_index, (candidate_index, _) in enumerate(ordered):
            candidates[candidate_index].q_value = adjusted[ordered_index]

    @staticmethod
    def _field_for_formula(formula: str) -> str:
        return formula.split(maxsplit=1)[0].lstrip("(")

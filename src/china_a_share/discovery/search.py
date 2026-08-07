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


class RuleSearchEngine:
    """Search quantile rules while bounding samples and expression complexity."""

    def __init__(self, *, min_sample_count: int, target_return: float = 0.0):
        self._min_sample_count = min_sample_count
        self._target_return = target_return

    def search(
        self,
        train: pd.DataFrame,
        validation: pd.DataFrame,
        factors: Sequence[str],
        *,
        max_conditions: int,
        top_n: int,
    ) -> Tuple[List[FactorHypothesis], int]:
        """Return FDR-adjusted candidates and the number of evaluated formulas."""
        conditions = self._build_conditions(train, factors)
        single_candidates, single_evaluated = self._evaluate_formulas(
            conditions,
            train,
            validation,
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
            pair_candidates, pair_evaluated = self._evaluate_formulas(
                pairs,
                train,
                validation,
            )
            candidates.extend(pair_candidates)
            evaluated_count += pair_evaluated
        self._apply_false_discovery_rate(candidates)
        candidates.sort(
            key=lambda candidate: (
                candidate.q_value,
                -candidate.validation_score,
                -candidate.val_result.mean_return,
            ),
        )
        unique = []
        seen = set()
        for candidate in candidates:
            if candidate.formula in seen:
                continue
            seen.add(candidate.formula)
            unique.append(candidate)
            if len(unique) == top_n:
                break
        return unique, evaluated_count

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

    def _evaluate_formulas(
        self,
        formulas: Sequence[Tuple[str, str]],
        train: pd.DataFrame,
        validation: pd.DataFrame,
    ) -> Tuple[List[FactorHypothesis], int]:
        candidates = []
        for formula, fields in formulas:
            train_result = FactorBacktester.evaluate_rule(
                train,
                formula,
                target_return=self._target_return,
            )
            validation_result = FactorBacktester.evaluate_rule(
                validation,
                formula,
                target_return=self._target_return,
            )
            if (
                train_result.sample_count < self._min_sample_count
                or validation_result.sample_count < self._min_sample_count
            ):
                continue
            generalization_gap = abs(
                train_result.win_rate - validation_result.win_rate
            )
            validation_score = (
                validation_result.confidence_lower
                + validation_result.win_rate_lift
                - generalization_gap
            )
            p_value = self._binomial_tail_probability(
                validation_result.positive_count,
                validation_result.sample_count,
                validation_result.baseline_win_rate,
            )
            candidates.append(
                FactorHypothesis(
                    formula=formula,
                    description=f"Quantile rule using {fields}",
                    reasoning=(
                        "The condition was generated from training-window quantiles "
                        "and ranked only after independent validation."
                    ),
                    train_result=train_result,
                    val_result=validation_result,
                    validation_score=validation_score,
                    generalization_gap=generalization_gap,
                    p_value=p_value,
                )
            )
        candidates.sort(
            key=lambda candidate: (
                candidate.train_result.mean_return,
                candidate.train_result.win_rate_lift,
            ),
            reverse=True,
        )
        return candidates, len(formulas)

    @staticmethod
    def _binomial_tail_probability(
        successes: int,
        observations: int,
        baseline_probability: float,
    ) -> float:
        """Approximate P(X >= successes) with a stable continuity correction."""
        if observations <= 0:
            return 1.0
        if baseline_probability <= 0.0:
            return 0.0 if successes > 0 else 1.0
        if baseline_probability >= 1.0:
            return 1.0
        standard_deviation = math.sqrt(
            observations * baseline_probability * (1.0 - baseline_probability)
        )
        z_score = (
            successes - 0.5 - observations * baseline_probability
        ) / standard_deviation
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

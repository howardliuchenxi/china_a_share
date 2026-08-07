"""Deterministic search for compact, explainable factor rules."""

from itertools import combinations
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
    ) -> List[FactorHypothesis]:
        """Return candidates ranked by validation return and win-rate lift."""
        conditions = self._build_conditions(train, factors)
        single_candidates = self._evaluate_formulas(
            conditions,
            train,
            validation,
        )
        candidates = list(single_candidates)
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
            candidates.extend(self._evaluate_formulas(pairs, train, validation))
        candidates.sort(
            key=lambda candidate: (
                candidate.validation_score,
                candidate.val_result.mean_return,
                candidate.val_result.sample_count,
            ),
            reverse=True,
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
        return unique

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
    ) -> List[FactorHypothesis]:
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
                )
            )
        candidates.sort(
            key=lambda candidate: (
                candidate.train_result.mean_return,
                candidate.train_result.win_rate_lift,
            ),
            reverse=True,
        )
        return candidates

    @staticmethod
    def _field_for_formula(formula: str) -> str:
        return formula.split(maxsplit=1)[0].lstrip("(")

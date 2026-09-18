"""Composite market-data provider routing by advertised operation ownership."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Sequence

import pandas as pd

from china_a_share.core.contracts import DataOperation
from china_a_share.core.ports import MarketDataProvider


class CompositeMarketDataProvider:
    """Expose multiple disjoint provider catalogs through one agent-facing port."""

    def __init__(self, providers: Sequence[MarketDataProvider]) -> None:
        """Index every operation once and reject ambiguous provider ownership."""
        if not providers:
            raise ValueError("providers must not be empty")
        self._providers = tuple(providers)
        self._operation_owners: Dict[str, MarketDataProvider] = {}
        for provider in self._providers:
            for operation in provider.search_operations("catalog"):
                owner = self._operation_owners.get(operation.name)
                if owner is not None:
                    raise ValueError(
                        f"Operation {operation.name} is exposed by both "
                        f"{owner.name} and {provider.name}."
                    )
                self._operation_owners[operation.name] = provider

    @property
    def name(self) -> str:
        """Return a stable cache-safe identity for the connected provider set."""
        return "composite"

    def search_operations(self, prompt: str) -> Sequence[DataOperation]:
        """Return all connected operations ordered by generic lexical relevance."""
        if not prompt.strip():
            return ()
        operations = [
            operation
            for provider in self._providers
            for operation in provider.search_operations(prompt)
        ]
        return tuple(
            sorted(
                operations,
                key=lambda operation: self._relevance(prompt, operation),
                reverse=True,
            )
        )

    def supports(self, operation: str) -> bool:
        """Return whether exactly one connected provider owns the operation."""
        return operation in self._operation_owners

    def describe_query_shapes(self, operation: str) -> Sequence[Dict[str, Any]]:
        """Delegate agent-facing request metadata to the operation owner."""
        return self._owner(operation).describe_query_shapes(operation)

    def validate_query(
        self,
        operation: str,
        params: Dict[str, Any],
        fields: Sequence[str],
    ) -> None:
        """Delegate request validation to the provider that owns the operation."""
        provider = self._owner(operation)
        provider.validate_query(operation, params, fields)

    def query(
        self,
        operation: str,
        params: Dict[str, Any],
        fields: Sequence[str],
        *,
        api_route: str,
        request_id: str,
        query_id: str,
    ) -> pd.DataFrame:
        """Execute one operation through its single catalog owner."""
        provider = self._owner(operation)
        return provider.query(
            operation,
            params,
            fields,
            api_route=api_route,
            request_id=request_id,
            query_id=query_id,
        )

    def _owner(self, operation: str) -> MarketDataProvider:
        """Resolve one operation owner or fail before any upstream request."""
        provider = self._operation_owners.get(operation)
        if provider is None:
            raise ValueError(f"Unsupported market-data operation: {operation}")
        return provider

    @staticmethod
    def _relevance(prompt: str, operation: DataOperation) -> int:
        """Rank catalogs without market-specific prompt branches."""
        query_tokens = CompositeMarketDataProvider._tokens(prompt)
        searchable = f"{operation.name} {operation.description}"
        operation_tokens = CompositeMarketDataProvider._tokens(searchable)
        exact_name_bonus = 8 if operation.name.casefold() in prompt.casefold() else 0
        return exact_name_bonus + len(query_tokens.intersection(operation_tokens))

    @staticmethod
    def _tokens(value: str) -> set[str]:
        """Extract English words and Chinese bigrams for lightweight ranking."""
        normalized = value.casefold()
        words = set(re.findall(r"[a-z0-9_]+", normalized))
        chinese_runs = re.findall(r"[\u4e00-\u9fff]+", normalized)
        bigrams = {
            run[index : index + 2]
            for run in chinese_runs
            for index in range(max(len(run) - 1, 0))
        }
        return words | bigrams

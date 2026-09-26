"""Machine-readable evidence retained for one conversational research task."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from china_a_share.core.contracts import QueryResult
from china_a_share.registry import data_recency_note, field_unit_notes_for


RESEARCH_MANIFEST_SCHEMA_VERSION = "1"


def _canonical_digest(value: Any) -> str:
    """Return a stable SHA-256 digest for one JSON-compatible value."""
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResearchDatasetEvidence(BaseModel):
    """Deterministic provenance and quality evidence for one retained dataset."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(description="Stable identifier used by research tools.")
    provider: str = Field(description="Provider or local executor that produced the dataset.")
    operation: str = Field(description="Provider operation or deterministic transformation name.")
    source_dataset_ids: List[str] = Field(
        default_factory=list,
        description="Ordered upstream dataset identifiers used to produce this dataset.",
    )
    operation_parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Validated non-secret parameters that controlled the operation.",
    )
    requested_fields: List[str] = Field(
        default_factory=list,
        description="Provider fields explicitly requested before result normalization.",
    )
    operation_fingerprint: str = Field(
        description="SHA-256 digest of the operation, sources, parameters, and fields.",
    )
    row_count: int = Field(ge=0, description="Number of complete retained result rows.")
    columns: List[str] = Field(description="Ordered columns in the retained dataset.")
    missing_value_counts: Dict[str, int] = Field(
        description="Null-value count for every retained result column.",
    )
    missing_value_rates: Dict[str, float] = Field(
        description="Null-value share from zero to one for every retained column.",
    )
    field_units: Dict[str, str] = Field(
        default_factory=dict,
        description="Interface-layer unit and semantic notes keyed by result column.",
    )
    data_recency: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Deterministically derived latest-data date and age evidence.",
    )
    column_calculations: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Formula and ordered calculation metadata for derived columns.",
    )
    completeness: str = Field(
        description="Executor assessment of whether the validated request is complete.",
    )
    completeness_evidence: List[str] = Field(
        default_factory=list,
        description="Machine-generated reasons supporting the completeness assessment.",
    )
    recorded_at: datetime = Field(
        description="UTC time when this evidence was recorded by the executor.",
    )


class ResearchManifest(BaseModel):
    """Bounded evidence graph and terminal fingerprints for one research task."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(
        default=RESEARCH_MANIFEST_SCHEMA_VERSION,
        description="Version of the durable research-evidence contract.",
    )
    task_id: str = Field(description="Task whose evidence and outputs are described.")
    created_at: datetime = Field(description="UTC time when manifest collection started.")
    updated_at: datetime = Field(description="UTC time of the latest manifest change.")
    code_revision: str = Field(
        default="",
        description="Git revision supplied by the deployed runtime when available.",
    )
    request_sha256: str = Field(
        default="",
        description="SHA-256 digest of the original user request without storing it twice.",
    )
    datasets: List[ResearchDatasetEvidence] = Field(
        default_factory=list,
        description="Ordered, de-duplicated evidence for every retained task dataset.",
    )
    final_dataset_id: Optional[str] = Field(
        default=None,
        description="Dataset selected for the terminal workbook or session workspace.",
    )
    answer_sha256: str = Field(
        default="",
        description="SHA-256 digest of the terminal conversational answer.",
    )
    artifact_name: Optional[str] = Field(
        default=None,
        description="Generated workbook file name when the task produced one.",
    )
    artifact_sha256: str = Field(
        default="",
        description="SHA-256 digest of the generated workbook bytes when available.",
    )
    output_fingerprint: str = Field(
        default="",
        description="Stable digest binding the request, evidence graph, answer, and artifact.",
    )


def new_research_manifest(task_id: str) -> ResearchManifest:
    """Create the initial evidence container for one durable task."""
    now = datetime.now(timezone.utc)
    return ResearchManifest(
        task_id=task_id,
        created_at=now,
        updated_at=now,
        code_revision=os.getenv("APP_GIT_SHA", "").strip(),
    )


def research_dataset_evidence(
    result: QueryResult,
    *,
    source_dataset_ids: List[str],
    operation_parameters: Mapping[str, Any],
    requested_fields: List[str],
    as_of: Optional[str] = None,
) -> ResearchDatasetEvidence:
    """Build deterministic dataset provenance without relying on model prose."""
    as_of = as_of or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    operation_contract = {
        "provider": result.provider,
        "operation": result.operation,
        "source_dataset_ids": source_dataset_ids,
        "operation_parameters": dict(operation_parameters),
        "requested_fields": requested_fields,
    }
    missing_value_counts = {
        column: sum(row.get(column) is None for row in result.rows)
        for column in result.columns
    }
    missing_value_rates = {
        column: (
            missing_value_counts[column] / result.row_count
            if result.row_count
            else 0.0
        )
        for column in result.columns
    }
    recency = data_recency_note(result.columns, result.rows, as_of)
    return ResearchDatasetEvidence(
        dataset_id=result.query_id,
        provider=result.provider,
        operation=result.operation,
        source_dataset_ids=source_dataset_ids,
        operation_parameters=dict(operation_parameters),
        requested_fields=requested_fields,
        operation_fingerprint=_canonical_digest(operation_contract),
        row_count=result.row_count,
        columns=list(result.columns),
        missing_value_counts=missing_value_counts,
        missing_value_rates=missing_value_rates,
        field_units=field_unit_notes_for(result.operation, result.columns),
        data_recency=recency,
        column_calculations={
            column: metadata.model_dump(mode="json")
            for column, metadata in result.column_metadata.items()
        },
        completeness=result.completeness,
        completeness_evidence=list(result.completeness_evidence),
        recorded_at=datetime.now(timezone.utc),
    )


def record_dataset_evidence(
    manifest: ResearchManifest,
    evidence: ResearchDatasetEvidence,
) -> ResearchManifest:
    """Return a manifest with one dataset inserted or replaced by identifier."""
    datasets = [
        existing
        for existing in manifest.datasets
        if existing.dataset_id != evidence.dataset_id
    ]
    datasets.append(evidence)
    return manifest.model_copy(
        update={
            "datasets": datasets,
            "updated_at": datetime.now(timezone.utc),
        }
    )


def select_final_dataset(
    manifest: ResearchManifest,
    dataset_id: str,
) -> ResearchManifest:
    """Record the exact dataset selected for terminal delivery."""
    if dataset_id not in {evidence.dataset_id for evidence in manifest.datasets}:
        raise ValueError(f"Research manifest does not contain dataset: {dataset_id}")
    return manifest.model_copy(
        update={
            "final_dataset_id": dataset_id,
            "updated_at": datetime.now(timezone.utc),
        }
    )


def finalize_research_manifest(
    manifest: ResearchManifest,
    *,
    request: str,
    answer: str,
    artifact_path: Optional[Path],
) -> ResearchManifest:
    """Bind terminal outputs to the collected evidence with stable digests."""
    artifact_name = artifact_path.name if artifact_path is not None else None
    artifact_sha256 = ""
    if artifact_path is not None and artifact_path.exists():
        artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    request_sha256 = hashlib.sha256(request.encode("utf-8")).hexdigest()
    answer_sha256 = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    fingerprint_payload = {
        "schema_version": manifest.schema_version,
        "task_id": manifest.task_id,
        "code_revision": manifest.code_revision,
        "request_sha256": request_sha256,
        "datasets": [
            evidence.model_dump(mode="json", exclude={"recorded_at"})
            for evidence in manifest.datasets
        ],
        "final_dataset_id": manifest.final_dataset_id,
        "answer_sha256": answer_sha256,
        "artifact_name": artifact_name,
        "artifact_sha256": artifact_sha256,
    }
    return manifest.model_copy(
        update={
            "updated_at": datetime.now(timezone.utc),
            "request_sha256": request_sha256,
            "answer_sha256": answer_sha256,
            "artifact_name": artifact_name,
            "artifact_sha256": artifact_sha256,
            "output_fingerprint": _canonical_digest(fingerprint_payload),
        }
    )

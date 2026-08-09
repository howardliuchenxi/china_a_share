"""Git-backed live case catalog and durable pending-change workflow."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from google.cloud import storage
from pydantic import BaseModel, Field, field_validator, model_validator
import requests

from china_a_share.feedback import GoogleAdminVerifier


LIVE_CASE_CHANGE_PREFIX = "live-case-changes"
GITHUB_API_ROOT = "https://api.github.com"
GITHUB_ACTIONS_VERSION = "2022-11-28"
GITHUB_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_LIVE_CASES_PATH = Path(
    os.getenv("LIVE_CASES_PATH", str(Path.cwd() / "live_cases.json"))
)
logger = logging.getLogger(__name__)


class LiveCase(BaseModel):
    """One version-controlled end-to-end analysis contract."""

    id: str = Field(description="Stable identifier used by Git and the UI.")
    name: str = Field(description="Short operator-facing scenario name.")
    family: str = Field(description="Regression family used for grouped reporting.")
    prompt: str = Field(description="Exact natural-language request sent to analysis.")
    expected_feasibility: Literal["supported", "unsupported"] = Field(
        description="Planner feasibility required for the case to pass."
    )
    tier: Literal["supported", "approximation", "unsupported"] = Field(
        description="Reviewed support tier for the requested capability."
    )
    operations: List[str] = Field(
        default_factory=list,
        description="Allowed provider operations for matrix assertions.",
    )
    quality_invariants: List[str] = Field(
        default_factory=list,
        description="Stable semantic invariants checked after execution.",
    )
    source: Literal["matrix", "reported_regression"] = Field(
        description="Origin controlling the applicable live assertion family."
    )

    @field_validator("id", "name", "family", "prompt")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        """Reject blank identifiers and user-visible case text."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("Live case text fields cannot be blank.")
        return normalized


class LiveCaseCatalog(BaseModel):
    """Versioned canonical collection stored in the Git repository."""

    version: int = Field(description="Schema version for deterministic migrations.")
    cases: List[LiveCase] = Field(description="Ordered canonical live cases.")

    @model_validator(mode="after")
    def validate_uniqueness(self) -> "LiveCaseCatalog":
        """Prevent ambiguous edits and duplicate paid regression requests."""
        ids = [case.id for case in self.cases]
        prompts = [case.prompt for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("Live case IDs must be unique.")
        if len(prompts) != len(set(prompts)):
            raise ValueError("Live case prompts must be unique.")
        return self


class LiveCaseChangeRequest(BaseModel):
    """One administrator mutation against a known deployed Git revision."""

    operation: Literal["create", "update", "delete"] = Field(
        description="Mutation applied by the deterministic GitHub workflow."
    )
    case: Optional[LiveCase] = Field(
        default=None,
        description="Complete desired case for create and update operations.",
    )
    case_id: Optional[str] = Field(
        default=None,
        description="Stable target identifier required for deletion.",
    )
    base_git_sha: str = Field(
        description="Deployed revision used for optimistic concurrency control."
    )

    @model_validator(mode="after")
    def validate_operation_payload(self) -> "LiveCaseChangeRequest":
        """Require exactly the payload shape needed by each mutation."""
        if self.operation in {"create", "update"} and self.case is None:
            raise ValueError("Create and update operations require a complete case.")
        if self.operation == "delete" and not (self.case_id or "").strip():
            raise ValueError("Delete operations require case_id.")
        return self


class LiveCaseView(LiveCase):
    """Published or pending case returned to the administrator page."""

    publication_status: Literal["published", "pending", "failed"] = Field(
        description="Whether the case is present in the deployed Git revision."
    )
    change_id: Optional[str] = Field(
        default=None,
        description="Pending mutation identifier when publication is incomplete.",
    )


class LiveCaseListResponse(BaseModel):
    """Merged published and pending case state for refresh-safe rendering."""

    git_sha: str = Field(description="Exact deployed Git revision backing the catalog.")
    cases: List[LiveCaseView] = Field(description="Published cases with pending overlays.")
    pending_deletions: List[str] = Field(
        description="Published case identifiers waiting to be removed."
    )


class LiveCaseChangeSubmission(BaseModel):
    """Acknowledgement for one durable Git-backed mutation request."""

    change_id: str = Field(description="Durable identifier for the pending mutation.")
    status: Literal["pending"] = Field(description="Initial publication lifecycle state.")
    actions_url: str = Field(description="GitHub Actions page used to inspect progress.")


def load_live_case_catalog(path: Path = DEFAULT_LIVE_CASES_PATH) -> LiveCaseCatalog:
    """Load and validate the canonical Git-backed case catalog."""
    return LiveCaseCatalog.model_validate_json(path.read_text(encoding="utf-8"))


class CloudStorageLiveCaseChangeStore:
    """Persist pending case mutations in the existing private application bucket."""

    def __init__(
        self,
        bucket_name: str,
        storage_client: Optional[storage.Client] = None,
    ) -> None:
        self._bucket = (storage_client or storage.Client()).bucket(bucket_name)

    def put(self, change_id: str, record: Dict[str, Any]) -> None:
        """Create one durable pending mutation before GitHub dispatch."""
        self._bucket.blob(
            f"{LIVE_CASE_CHANGE_PREFIX}/{change_id}.json"
        ).upload_from_string(
            json.dumps(record, ensure_ascii=False),
            content_type="application/json",
        )

    def list(self) -> List[Dict[str, Any]]:
        """Return pending records in creation order."""
        records = []
        for blob in self._bucket.list_blobs(prefix=f"{LIVE_CASE_CHANGE_PREFIX}/"):
            records.append(json.loads(blob.download_as_text()))
        return sorted(records, key=lambda record: str(record.get("created_at", "")))


class GitHubLiveCaseDispatcher:
    """Dispatch one validated case mutation to the repository workflow."""

    def __init__(
        self,
        repository: str,
        token: str,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._repository = repository
        self._token = token
        self._session = session or requests.Session()

    @property
    def actions_url(self) -> str:
        """Return the repository Actions page for operator inspection."""
        return f"https://github.com/{self._repository}/actions"

    def dispatch(self, payload: Dict[str, Any]) -> None:
        """Trigger the deterministic live-case mutation workflow."""
        response = self._session.post(
            f"{GITHUB_API_ROOT}/repos/{self._repository}/dispatches",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": GITHUB_ACTIONS_VERSION,
            },
            json={
                "event_type": "live_case_change_requested",
                "client_payload": payload,
            },
            timeout=GITHUB_REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 204:
            raise RuntimeError(
                "GitHub live-case dispatch failed with HTTP "
                f"{response.status_code}: {response.text[:500]}"
            )

    def statuses(self, change_ids: List[str]) -> Dict[str, str]:
        """Return terminal workflow failures keyed by pending change identifier."""
        if not change_ids:
            return {}
        response = self._session.get(
            f"{GITHUB_API_ROOT}/repos/{self._repository}/actions/runs",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": GITHUB_ACTIONS_VERSION,
            },
            params={"event": "repository_dispatch", "per_page": 100},
            timeout=GITHUB_REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise RuntimeError(
                "GitHub workflow status query failed with HTTP "
                f"{response.status_code}: {response.text[:500]}"
            )
        failures = {"failure", "cancelled", "timed_out", "action_required"}
        statuses = {}
        for run in response.json().get("workflow_runs", []):
            title = str(run.get("display_title") or "")
            matching_id = next(
                (change_id for change_id in change_ids if f"[{change_id}]" in title),
                None,
            )
            if matching_id and run.get("conclusion") in failures:
                statuses[matching_id] = "failed"
        return statuses


class LiveCaseService:
    """Authorize, merge, persist, and dispatch Git-backed live-case mutations."""

    def __init__(
        self,
        verifier: GoogleAdminVerifier,
        store: CloudStorageLiveCaseChangeStore,
        dispatcher: GitHubLiveCaseDispatcher,
        *,
        git_sha: str,
        catalog_path: Path = DEFAULT_LIVE_CASES_PATH,
    ) -> None:
        self._verifier = verifier
        self._store = store
        self._dispatcher = dispatcher
        self._git_sha = git_sha
        self._catalog_path = catalog_path

    def list_cases(self, bearer_token: str) -> LiveCaseListResponse:
        """Return the administrator catalog with pending changes overlaid."""
        self._verifier.verify(bearer_token)
        published = load_live_case_catalog(self._catalog_path).cases
        cases = {case.id: case for case in published}
        pending_ids: Dict[str, tuple[str, str]] = {}
        pending_deletions: List[str] = []
        records = self._store.list()
        change_ids = [str(record["change_id"]) for record in records]
        try:
            workflow_statuses = self._dispatcher.statuses(change_ids)
        except Exception:
            # Catalog reads remain available during a transient GitHub status outage;
            # structured logs preserve the operational failure for investigation.
            logger.exception("live_case_workflow_status_query_failed")
            workflow_statuses = {}
        for record in records:
            request = LiveCaseChangeRequest.model_validate(record["request"])
            if self._is_published(request, cases):
                continue
            change_id = str(record["change_id"])
            change_status = (
                "failed"
                if record.get("status") == "dispatch_failed"
                or workflow_statuses.get(change_id) == "failed"
                else "pending"
            )
            if request.operation == "delete":
                if change_status != "failed":
                    pending_deletions.append(str(request.case_id))
                continue
            assert request.case is not None
            cases[request.case.id] = request.case
            pending_ids[request.case.id] = (
                change_id,
                change_status,
            )
        return LiveCaseListResponse(
            git_sha=self._git_sha,
            cases=[
                LiveCaseView(
                    **case.model_dump(),
                    publication_status=(
                        pending_ids[case.id][1]
                        if case.id in pending_ids
                        else "published"
                    ),
                    change_id=(
                        pending_ids[case.id][0] if case.id in pending_ids else None
                    ),
                )
                for case in cases.values()
            ],
            pending_deletions=pending_deletions,
        )

    def submit(
        self,
        bearer_token: str,
        request: LiveCaseChangeRequest,
    ) -> LiveCaseChangeSubmission:
        """Persist a mutation before dispatching its deterministic Git change."""
        admin_email = self._verifier.verify(bearer_token)
        if request.base_git_sha != self._git_sha:
            raise ValueError("The deployed live-case revision changed; refresh and retry.")
        catalog = load_live_case_catalog(self._catalog_path)
        by_id = {case.id: case for case in catalog.cases}
        target_id = request.case.id if request.case else str(request.case_id)
        if request.operation == "create" and target_id in by_id:
            raise ValueError("A live case with this ID already exists.")
        if request.operation in {"update", "delete"} and target_id not in by_id:
            raise ValueError("The requested live case does not exist.")
        change_id = uuid4().hex
        payload = {
            "change_id": change_id,
            "base_git_sha": request.base_git_sha,
            "operation": request.operation,
            "case_id": request.case_id or target_id,
            "case": request.case.model_dump(mode="json") if request.case else None,
            "previous_case": (
                by_id[target_id].model_dump(mode="json")
                if target_id in by_id
                else None
            ),
        }
        record = {
            "change_id": change_id,
            "admin_email": admin_email,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
            "request": request.model_dump(mode="json"),
        }
        self._store.put(change_id, record)
        try:
            self._dispatcher.dispatch(payload)
        except Exception:
            # Keep failed dispatches visible after refresh so an administrator can
            # retry instead of assuming the requested mutation reached GitHub.
            record["status"] = "dispatch_failed"
            self._store.put(change_id, record)
            raise
        return LiveCaseChangeSubmission(
            change_id=change_id,
            status="pending",
            actions_url=self._dispatcher.actions_url,
        )

    @staticmethod
    def _is_published(
        request: LiveCaseChangeRequest,
        cases: Dict[str, LiveCase],
    ) -> bool:
        """Recognize mutations already represented by the deployed Git catalog."""
        if request.operation == "delete":
            return request.case_id not in cases
        assert request.case is not None
        return cases.get(request.case.id) == request.case

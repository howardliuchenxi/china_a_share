"""Tests for the canonical live-case catalog and pending publication overlay."""

import json

import pytest

from china_a_share.e2e_cases import (
    LiveCase,
    LiveCaseCatalog,
    LiveCaseChangeRequest,
    LiveCaseService,
    load_live_case_catalog,
)


class FakeVerifier:
    """Record authentication attempts without calling Google."""

    def __init__(self) -> None:
        self.tokens = []

    def verify(self, token):
        """Accept one token and return the configured administrator identity."""
        self.tokens.append(token)
        return "admin@example.com"


class FakeStore:
    """Keep pending mutation records in memory for deterministic tests."""

    def __init__(self) -> None:
        self.records = {}

    def put(self, change_id, record):
        """Persist one record exactly as submitted by the service."""
        self.records[change_id] = dict(record)

    def list(self):
        """Return all records in submission order."""
        return list(self.records.values())


class FakeDispatcher:
    """Capture GitHub dispatch payloads without network access."""

    actions_url = "https://github.com/example/repository/actions"

    def __init__(self, error=None, statuses=None) -> None:
        self.payloads = []
        self.error = error
        self.workflow_statuses = statuses or {}

    def dispatch(self, payload):
        """Record one deterministic workflow request."""
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error

    def statuses(self, change_ids):
        """Return configured workflow states for requested changes."""
        return {
            change_id: self.workflow_statuses[change_id]
            for change_id in change_ids
            if change_id in self.workflow_statuses
        }


def write_catalog(tmp_path, cases):
    """Write one valid catalog fixture and return its path."""
    path = tmp_path / "live_cases.json"
    path.write_text(
        json.dumps({"version": 1, "cases": cases}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def sample_case(case_id="case-1", prompt="Count A-share gainers."):
    """Return one complete case payload suitable for catalog validation."""
    return {
        "id": case_id,
        "name": "Market breadth",
        "family": "market_breadth",
        "prompt": prompt,
        "expected_feasibility": "supported",
        "tier": "supported",
        "operations": ["daily"],
        "quality_invariants": [],
        "source": "matrix",
    }


def test_canonical_catalog_contains_all_live_cases():
    catalog = load_live_case_catalog()

    assert catalog.version == 1
    assert len(catalog.cases) == 105
    assert len({case.id for case in catalog.cases}) == 105
    assert len({case.prompt for case in catalog.cases}) == 105
    assert sum(case.source == "matrix" for case in catalog.cases) == 100
    assert sum(case.source == "reported_regression" for case in catalog.cases) == 5


def test_catalog_rejects_duplicate_prompts():
    duplicate = sample_case("case-2")

    with pytest.raises(ValueError, match="prompts must be unique"):
        LiveCaseCatalog.model_validate(
            {"version": 1, "cases": [sample_case(), duplicate]}
        )


def test_pending_create_survives_refresh_and_is_overlaid(tmp_path):
    verifier = FakeVerifier()
    store = FakeStore()
    dispatcher = FakeDispatcher()
    service = LiveCaseService(
        verifier,
        store,
        dispatcher,
        git_sha="a" * 40,
        catalog_path=write_catalog(tmp_path, [sample_case()]),
    )
    new_case = LiveCase.model_validate(
        sample_case("case-2", "List A-share decliners.")
    )

    submission = service.submit(
        "token",
        LiveCaseChangeRequest(
            operation="create",
            case=new_case,
            base_git_sha="a" * 40,
        ),
    )
    response = service.list_cases("token")

    assert submission.status == "pending"
    assert dispatcher.payloads[0]["operation"] == "create"
    assert dispatcher.payloads[0]["previous_case"] is None
    assert len(response.cases) == 2
    pending = next(case for case in response.cases if case.id == "case-2")
    assert pending.publication_status == "pending"
    assert pending.change_id == submission.change_id
    assert verifier.tokens == ["token", "token"]


def test_pending_delete_keeps_case_visible_until_publication(tmp_path):
    store = FakeStore()
    service = LiveCaseService(
        FakeVerifier(),
        store,
        FakeDispatcher(),
        git_sha="b" * 40,
        catalog_path=write_catalog(tmp_path, [sample_case()]),
    )

    service.submit(
        "token",
        LiveCaseChangeRequest(
            operation="delete",
            case_id="case-1",
            base_git_sha="b" * 40,
        ),
    )
    response = service.list_cases("token")

    assert [case.id for case in response.cases] == ["case-1"]
    assert response.pending_deletions == ["case-1"]


def test_change_rejects_stale_deployed_revision(tmp_path):
    service = LiveCaseService(
        FakeVerifier(),
        FakeStore(),
        FakeDispatcher(),
        git_sha="c" * 40,
        catalog_path=write_catalog(tmp_path, [sample_case()]),
    )

    with pytest.raises(ValueError, match="revision changed"):
        service.submit(
            "token",
            LiveCaseChangeRequest(
                operation="delete",
                case_id="case-1",
                base_git_sha="d" * 40,
            ),
        )


def test_dispatch_failure_remains_visible_after_refresh(tmp_path):
    store = FakeStore()
    service = LiveCaseService(
        FakeVerifier(),
        store,
        FakeDispatcher(RuntimeError("dispatch unavailable")),
        git_sha="e" * 40,
        catalog_path=write_catalog(tmp_path, [sample_case()]),
    )
    new_case = LiveCase.model_validate(
        sample_case("case-2", "List A-share decliners.")
    )

    with pytest.raises(RuntimeError, match="dispatch unavailable"):
        service.submit(
            "token",
            LiveCaseChangeRequest(
                operation="create",
                case=new_case,
                base_git_sha="e" * 40,
            ),
        )

    response = service.list_cases("token")
    failed = next(case for case in response.cases if case.id == "case-2")
    assert failed.publication_status == "failed"

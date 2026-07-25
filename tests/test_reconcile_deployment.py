import json

import pytest

from scripts import reconcile_deployment


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_deployment_action_deploys_only_when_target_is_ahead():
    assert reconcile_deployment.deployment_action("ahead") == "deploy"
    assert reconcile_deployment.deployment_action("identical") == "skip"


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_deployment_action_rejects_unsafe_history(status):
    with pytest.raises(RuntimeError, match=status):
        reconcile_deployment.deployment_action(status)


def test_fetch_comparison_status_uses_deployed_commit_as_base(monkeypatch):
    requested_urls = []

    def fake_urlopen(request, timeout):
        requested_urls.append((request.full_url, timeout))
        return FakeResponse({"status": "ahead"})

    monkeypatch.setattr(reconcile_deployment.urllib.request, "urlopen", fake_urlopen)

    status = reconcile_deployment.fetch_comparison_status(
        "owner/repository",
        "1" * 40,
        "2" * 40,
    )

    assert status == "ahead"
    assert requested_urls == [
        (
            "https://api.github.com/repos/owner/repository/compare/"
            f"{'1' * 40}...{'2' * 40}",
            reconcile_deployment.REQUEST_TIMEOUT_SECONDS,
        )
    ]


def test_validate_inputs_rejects_malformed_values():
    with pytest.raises(ValueError, match="repository"):
        reconcile_deployment.validate_inputs("owner", "1" * 40, "2" * 40)

    with pytest.raises(ValueError, match="deployed SHA"):
        reconcile_deployment.validate_inputs(
            "owner/repository",
            "not-a-sha",
            "2" * 40,
        )

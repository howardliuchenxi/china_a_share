import sys

import pytest

from scripts import validate_release_files


def test_release_validation_rejects_sensitive_paths(monkeypatch):
    monkeypatch.setattr(
        validate_release_files,
        "changed_paths",
        lambda: ["src/app.py", ".env.production"],
    )
    monkeypatch.setattr(sys, "argv", ["validate_release_files.py"])

    with pytest.raises(SystemExit, match="sensitive-looking"):
        validate_release_files.main()


def test_release_validation_accepts_expected_source_changes(monkeypatch):
    monkeypatch.setattr(
        validate_release_files,
        "changed_paths",
        lambda: ["src/app.py", "tests/test_app.py"],
    )
    monkeypatch.setattr(sys, "argv", ["validate_release_files.py"])

    validate_release_files.main()


def test_release_validation_rejects_unexpected_post_deploy_changes(monkeypatch):
    monkeypatch.setattr(
        validate_release_files,
        "changed_paths",
        lambda: ["docs/gcp-resources.md", "src/app.py"],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_release_files.py",
            "--allowed-only",
            "docs/gcp-resources.md",
        ],
    )

    with pytest.raises(SystemExit, match="Unexpected files"):
        validate_release_files.main()

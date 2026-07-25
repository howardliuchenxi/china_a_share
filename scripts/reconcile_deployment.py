"""Decide whether the current main commit should replace production."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request


GITHUB_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
GITHUB_API_ROOT = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 30
NON_DEPLOYING_PATHS = frozenset({"docs/gcp-resources.md"})


def parse_args() -> argparse.Namespace:
    """Parse the immutable source and production revisions to compare."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--target-sha", required=True)
    return parser.parse_args()


def validate_inputs(repository: str, deployed_sha: str, target_sha: str) -> None:
    """Reject malformed identifiers before constructing the GitHub API URL."""
    if not GITHUB_REPOSITORY_PATTERN.fullmatch(repository):
        raise ValueError(f"Invalid GitHub repository: {repository}")
    for label, value in (
        ("deployed SHA", deployed_sha),
        ("target SHA", target_sha),
    ):
        if not GIT_SHA_PATTERN.fullmatch(value):
            raise ValueError(f"Invalid {label}: {value}")


def fetch_comparison_status(
    repository: str,
    deployed_sha: str,
    target_sha: str,
) -> tuple[str, tuple[str, ...]]:
    """Return GitHub's commit relationship and changed paths."""
    url = (
        f"{GITHUB_API_ROOT}/repos/{repository}/compare/"
        f"{deployed_sha}...{target_sha}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "china-a-share-deployment-reconciler",
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"GitHub comparison failed with HTTP {error.code}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"GitHub comparison failed: {error.reason}") from error

    status = payload.get("status")
    if status not in {"ahead", "identical", "behind", "diverged"}:
        raise RuntimeError(f"GitHub returned an unexpected comparison status: {status}")
    files = payload.get("files", [])
    changed_paths = tuple(file["filename"] for file in files)
    return status, changed_paths


def deployment_action(status: str, changed_paths: tuple[str, ...] = ()) -> str:
    """Map a verified commit relationship to a fail-fast deployment action."""
    if status == "ahead":
        if changed_paths and set(changed_paths) <= NON_DEPLOYING_PATHS:
            return "skip"
        return "deploy"
    if status == "identical":
        return "skip"
    raise RuntimeError(
        f"Refusing deployment because target main is {status} relative to production"
    )


def main() -> int:
    """Print the single action consumed by the Cloud Build workflow."""
    args = parse_args()
    try:
        validate_inputs(args.repository, args.deployed_sha, args.target_sha)
        if args.deployed_sha == args.target_sha:
            print("skip")
            return 0
        status, changed_paths = fetch_comparison_status(
            args.repository,
            args.deployed_sha,
            args.target_sha,
        )
        print(deployment_action(status, changed_paths))
        return 0
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

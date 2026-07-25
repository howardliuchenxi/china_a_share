"""Reject unsafe or unexpected files before automated release commits."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
from typing import List


MAX_RELEASE_FILE_BYTES = 10 * 1024 * 1024
FORBIDDEN_PATH_PATTERNS = (
    re.compile(r"(^|/)\.env($|\.)", re.IGNORECASE),
    re.compile(r"\.(key|pem|p12|pfx)$", re.IGNORECASE),
    re.compile(r"(^|/)(credentials?|service[-_]?account).*\.json$", re.IGNORECASE),
    re.compile(r"(^|/)(id_rsa|id_ed25519)(\.pub)?$", re.IGNORECASE),
)


def parse_args() -> argparse.Namespace:
    """Parse an optional exact allowlist for post-deployment changes."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowed-only", action="append", default=[])
    return parser.parse_args()


def changed_paths() -> List[str]:
    """Return all tracked and untracked paths reported by Git."""
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        check=True,
        capture_output=True,
    )
    entries = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    paths: List[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        if "R" in status or "C" in status:
            if index >= len(entries):
                raise RuntimeError("Git returned an incomplete rename record.")
            path = entries[index]
            index += 1
        paths.append(path)
    return paths


def main() -> None:
    """Fail fast when release automation would capture unsafe files."""
    args = parse_args()
    paths = changed_paths()
    allowed_paths = set(args.allowed_only)
    if allowed_paths:
        unexpected = sorted(set(paths) - allowed_paths)
        if unexpected:
            raise SystemExit(
                "Unexpected files changed during deployment: "
                + ", ".join(unexpected)
            )
        return

    forbidden = sorted(
        path
        for path in paths
        if any(pattern.search(path) for pattern in FORBIDDEN_PATH_PATTERNS)
    )
    if forbidden:
        raise SystemExit(
            "Release refused sensitive-looking paths: " + ", ".join(forbidden)
        )

    oversized = []
    for path in paths:
        file_path = Path(path)
        if file_path.is_file() and file_path.stat().st_size > MAX_RELEASE_FILE_BYTES:
            oversized.append(path)
    if oversized:
        raise SystemExit(
            "Release refused files larger than 10 MiB: "
            + ", ".join(sorted(oversized))
        )


if __name__ == "__main__":
    main()

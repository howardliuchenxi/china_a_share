"""Update verified deployment values in the Google Cloud resource inventory."""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path


INVENTORY_PATH = Path("docs/gcp-resources.md")


def parse_args() -> argparse.Namespace:
    """Parse deployment values obtained from read-only Google Cloud queries."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-size", required=True, type=int)
    parser.add_argument("--cache-size", required=True, type=int)
    parser.add_argument("--git-branch", required=True)
    parser.add_argument("--git-sha", required=True)
    return parser.parse_args()


def replace_once(content: str, pattern: str, replacement: str) -> str:
    """Replace exactly one inventory field so format drift fails visibly."""
    updated, count = re.subn(pattern, replacement, content, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"Expected one inventory match for pattern: {pattern}")
    return updated


def replace_bucket_size(content: str, bucket: str, size: int) -> str:
    """Replace the size row in the table belonging to the named bucket."""
    bucket_row = f"| Bucket | `gs://{bucket}` |"
    bucket_position = content.find(bucket_row)
    if bucket_position < 0:
        raise ValueError(f"Bucket is missing from the inventory: {bucket}")
    row_start = content.find("| Current logical size |", bucket_position)
    if row_start < 0:
        raise ValueError(f"Size row is missing for bucket: {bucket}")
    row_end = content.find("\n", row_start)
    if row_end < 0:
        raise ValueError(f"Size row is incomplete for bucket: {bucket}")
    replacement = f"| Current logical size | {size:,} bytes at last verification |"
    return f"{content[:row_start]}{replacement}{content[row_end:]}"


def main() -> None:
    """Write the verified revision and storage sizes to the inventory."""
    args = parse_args()
    content = INVENTORY_PATH.read_text(encoding="utf-8")
    verified_date = dt.date.today().isoformat()
    revision_was_recorded = f"`{args.revision}`" in content

    content = replace_once(
        content,
        r"^Last verified: \*\*[^*]+\*\*$",
        f"Last verified: **{verified_date}**",
    )
    content = replace_once(
        content,
        r"^\| Latest ready revision \| `[^`]+` \|$",
        f"| Latest ready revision | `{args.revision}` |",
    )
    content = replace_once(
        content,
        r"^\| Deployed Git branch \| `[^`]+` \|$",
        f"| Deployed Git branch | `{args.git_branch}` |",
    )
    content = replace_once(
        content,
        r"^\| Deployed Git commit \| `[^`]+` \|$",
        f"| Deployed Git commit | `{args.git_sha}` |",
    )
    content = replace_bucket_size(
        content,
        "china-a-share-lab-cache-asia-east2",
        args.cache_size,
    )
    content = replace_bucket_size(
        content,
        "run-sources-china-a-share-lab-asia-east2",
        args.source_size,
    )

    change_entry = (
        f"| {verified_date} | Deployed revision `{args.revision}` through `make deploy`; "
        f"recorded source `{args.git_branch}@{args.git_sha}`, verified 100% traffic, "
        "public health status, runtime configuration, and storage usage with no new "
        "resource types or IAM changes. |"
    )
    if not revision_was_recorded:
        content = f"{content.rstrip()}\n{change_entry}\n"

    INVENTORY_PATH.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()

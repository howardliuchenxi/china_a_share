"""Send a sanitised loop-iteration summary to a Feishu group bot webhook.

Usage::

    python scripts/loop/notify_feishu.py

The script reads FEISHU_WEBHOOK_URL from the project ``.env`` file.  When the
variable is missing or empty the script exits cleanly without sending anything.

Message format
--------------

The notification uses a Feishu *interactive* card with:

- green / red header (pass / fail);
- loop ID and objective from ``.loop/state.json``;
- duration, file count and latest commit from Git;
- backend-test, frontend-build and E2E status from ``.loop/state.json``;
- next proposed objective (optional).

No secrets, raw upstream responses or interactive buttons are included.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOOP_DIR = PROJECT_ROOT / ".loop"
STATE_FILE = LOOP_DIR / "state.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_git(*args: str) -> str:
    """Run a git command in *PROJECT_ROOT* and return stripped stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        raise FileNotFoundError(f"{STATE_FILE} not found")
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def _format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes > 0:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_commit_short() -> str:
    return _run_git("rev-parse", "--short", "HEAD")


def _git_files_changed() -> int:
    """Count files changed in the latest commit (not counting the commit
    itself)."""
    diff = _run_git("diff", "--name-only", "HEAD~1", "HEAD")
    if not diff:
        return 0
    return len(diff.splitlines())


def _git_commit_message() -> str:
    return _run_git("log", "-1", "--format=%s")


# ---------------------------------------------------------------------------
# Card builder
# ---------------------------------------------------------------------------


def _build_card(
    loop_id: str,
    objective: str,
    duration_seconds: float,
    files_changed: int,
    backend_test: str,
    frontend_build: str,
    e2e_test: str,
    commit: str,
    next_objective: Optional[str],
) -> dict[str, Any]:
    all_pass = (
        backend_test == "passed"
        and frontend_build == "passed"
        and e2e_test in ("passed", "skipped")
    )

    status_icon = "✅" if all_pass else "⚠️"
    header_color = "green" if all_pass else "red"

    # Build the markdown body line by line.
    lines: list[str] = [
        f"**目标：**{objective}",
        f"**耗时：**{_format_duration(duration_seconds)}",
        f"**修改：**{files_changed} 个文件",
    ]

    # Test results
    test_lines: list[str] = []
    test_lines.append(f"后端测试：{'✅ 通过' if backend_test == 'passed' else '❌ 失败'}")
    test_lines.append(
        f"前端构建：{'✅ 通过' if frontend_build == 'passed' else '❌ 失败'}"
    )
    e2e_label = {
        "passed": "✅ 通过",
        "failed": "❌ 失败",
        "skipped": "⏭️ 跳过",
    }.get(e2e_test, f"❓ {e2e_test}")
    test_lines.append(f"E2E：{e2e_label}")
    lines.append(f"**测试：**{'; '.join(test_lines)}")

    lines.append(f"**Commit：**`{commit}`")
    if next_objective:
        lines.append(f"**下一步：**{next_objective}")

    body_md = "\n".join(lines)

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{status_icon} {loop_id} 完成",
                },
                "template": header_color,
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": body_md,
                },
                {
                    "tag": "hr",
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": (
                                "China A-Share Lab · Loop Engineering · "
                                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                            ),
                        }
                    ],
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


def _send_card(webhook_url: str, card: dict[str, Any]) -> bool:
    try:
        resp = requests.post(
            webhook_url,
            json=card,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            print(
                f"Feishu webhook returned error: code={body.get('code')} "
                f"msg={body.get('msg')}",
                file=sys.stderr,
            )
            return False
        return True
    except requests.RequestException as exc:
        print(f"Feishu webhook request failed: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    load_dotenv(dotenv_path=str(PROJECT_ROOT / ".env"), override=False)
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "").strip()

    if not webhook_url:
        print("FEISHU_WEBHOOK_URL is not set — skipping notification.")
        return 0

    try:
        state = _load_state()
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Cannot read loop state: {exc}", file=sys.stderr)
        return 1

    loop_id = state.get("iteration_id", f"L-{state.get('iteration', '?')}")
    objective = state.get("objective") or "(未记录)"
    duration_seconds = 0.0

    started_at = state.get("started_at")
    if started_at:
        try:
            start_dt = datetime.fromisoformat(started_at)
            duration_seconds = (
                datetime.now(timezone.utc).timestamp() - start_dt.timestamp()
            )
        except (ValueError, TypeError):
            pass

    # Validation status — prefer explicit values from state, fall back to
    # "unknown".
    validation = state.get("last_validation", {})
    backend_test = str(validation.get("backend_test", "unknown"))
    frontend_build = str(validation.get("frontend_build", "unknown"))
    e2e_test = str(validation.get("e2e_test", "skipped"))

    # Git info
    try:
        commit = _git_commit_short()
        files_changed = _git_files_changed()
    except RuntimeError as exc:
        print(f"Git error: {exc}", file=sys.stderr)
        commit = "unknown"
        files_changed = 0

    next_objective = state.get("next_objective")

    card = _build_card(
        loop_id=loop_id,
        objective=objective,
        duration_seconds=duration_seconds,
        files_changed=files_changed,
        backend_test=backend_test,
        frontend_build=frontend_build,
        e2e_test=e2e_test,
        commit=commit,
        next_objective=next_objective,
    )

    success = _send_card(webhook_url, card)
    if success:
        print(f"Feishu notification sent for {loop_id}.")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

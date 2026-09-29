"""Feishu group issue-report form, transcription coordination, and result cards."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

FEEDBACK_DESCRIPTION_MAX_LENGTH = 500
# One recent answer longer than this is trimmed inside the report transcript.
FEEDBACK_TURN_CONTENT_MAX_CHARS = 4_000
# Oldest turns are dropped first once the whole transcript exceeds this budget.
FEEDBACK_WINDOW_TOTAL_MAX_CHARS = 16_000
# Shown inside the guide card as the canonical one-shot feedback command.
FEEDBACK_TEXT_COMMAND_EXAMPLE = "反馈 2轮 表格列名看不懂"


class FeedbackTurn(BaseModel):
    """One bounded ask/answer pair supplied to the transcriber."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=1_000)
    answer: str = Field(default="", max_length=12_000)


class FeedbackTranscriptionError(RuntimeError):
    """Raised when transcription fails after the raw record was persisted."""


class FeedbackRecordStore(Protocol):
    """Persist one private JSON feedback record."""

    def put(self, feedback_id: str, record: Dict[str, Any]) -> None:
        """Create or replace one private JSON feedback record."""


class FeedbackTranscriber(Protocol):
    """Turn one report description and transcript into a code-agent brief."""

    def transcribe(self, description: str, turns: List[Dict[str, str]]) -> str:
        """Return the structured report text."""


def select_feedback_turn_window(
    turns: List[FeedbackTurn],
    requested: int,
) -> tuple[List[Dict[str, str]], bool]:
    """Return (transcript rows, truncated) for the most recent ``requested`` turns.

    Rows alternate user/assistant and are newest-last. Overlong answers are
    individually trimmed, and when the whole transcript exceeds the total
    budget the oldest turns are dropped first so the reporter's most recent
    exchanges always survive.
    """
    selected = list(turns)[-max(requested, 0) :]
    rows: List[Dict[str, str]] = []
    total_chars = 0
    truncated = False
    for turn in reversed(selected):
        answer = turn.answer
        if len(answer) > FEEDBACK_TURN_CONTENT_MAX_CHARS:
            answer = answer[:FEEDBACK_TURN_CONTENT_MAX_CHARS] + "…（原文过长已截断）"
            truncated = True
        row_cost = len(turn.prompt) + len(answer)
        if rows and total_chars + row_cost > FEEDBACK_WINDOW_TOTAL_MAX_CHARS:
            truncated = True
            break
        total_chars += row_cost
        # Prepend each newest-first pair so rows stay user→assistant per turn
        # and end with the most recent exchange.
        rows = [
            {"role": "user", "content": turn.prompt},
            {"role": "assistant", "content": answer or "（本轮没有可引用的回答文本）"},
        ] + rows
    return rows, truncated


def build_feedback_guide_card() -> Dict[str, Any]:
    """Return the render-safe issue-report guide card without a form container.

    Old Feishu clients cannot render v1 form containers (input, dropdowns)
    and fall back to an "upgrade your client" placeholder, while markdown
    div and note elements render everywhere. The guide therefore only
    explains the one-shot plain-text command, which runs the same pipeline
    on every client version.
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "反馈问题"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "请直接发送一条消息完成反馈：\n"
                        f"**{FEEDBACK_TEXT_COMMAND_EXAMPLE}**\n"
                        "轮数可选 1/2/3/5，省略则默认最近 1 轮"
                        "（如：反馈 问题描述）。\n"
                        "提交后会自动带上所选轮数的问答原文，整理成排查报告并"
                        "通知管理员。"
                    ),
                },
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "描述最多 500 字；整理结果仅管理员可见。",
                    }
                ],
            },
        ],
    }


def build_feedback_result_card(
    *,
    report: str = "",
    raw_description: str = "",
    admin_open_id: str = "",
    transcription_failed: bool = False,
    window_note: str = "",
) -> Dict[str, Any]:
    """Return the group-facing result card, mentioning the administrator."""
    if transcription_failed:
        header = "反馈已收到（转写失败）"
        template = "orange"
        body_lines = [
            "已收到，感谢反馈！自动整理失败，已保存原始反馈，管理员会人工查看。",
            "",
            f"**原始描述**：{raw_description}",
        ]
    else:
        header = "反馈已收到"
        template = "green"
        body_lines = ["已收到，感谢反馈！以下排查报告已通知管理员。", "", report]
    if window_note:
        body_lines.extend(["", window_note])
    if admin_open_id:
        body_lines.extend(["", f"<at id={admin_open_id}></at> 请查收。"])
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": header},
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(body_lines)},
            }
        ],
    }


class FeishuFeedbackCoordinator:
    """Transcribe and persist one Feishu group issue report."""

    def __init__(
        self,
        transcriber: FeedbackTranscriber,
        record_store: FeedbackRecordStore,
        *,
        admin_open_id: str = "",
    ) -> None:
        self._transcriber = transcriber
        self._record_store = record_store
        self._admin_open_id = admin_open_id.strip()
        if not self._admin_open_id:
            logger.warning(
                "FEISHU_FEEDBACK_ADMIN_OPEN_ID is unset; issue-report result "
                "cards will be sent without mentioning an administrator."
            )

    @property
    def admin_open_id(self) -> str:
        """Return the administrator open id to mention, or empty when unset."""
        return self._admin_open_id

    def handle_submission(
        self,
        *,
        description: str,
        transcript_rows: List[Dict[str, str]],
        turns_requested: int,
        turns_included: int,
        window_truncated: bool,
        chat_id: str,
        operator_open_id: str,
    ) -> str:
        """Persist the record, transcribe it, and return the report text."""
        feedback_id = uuid4().hex
        record = {
            "feedback_id": feedback_id,
            "source": "feishu",
            "description": description,
            "turns_requested": turns_requested,
            "turns_included": turns_included,
            "window_truncated": window_truncated,
            "transcript": transcript_rows,
            "chat_id": chat_id,
            "operator_open_id": operator_open_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "received",
        }
        self._record_store.put(feedback_id, record)
        try:
            report = self._transcriber.transcribe(description, transcript_rows)
        except Exception as exc:
            record["status"] = "transcription_failed"
            record["error"] = str(exc)[:500]
            self._record_store.put(feedback_id, record)
            raise FeedbackTranscriptionError(
                "Feedback transcription failed; the raw report was persisted."
            ) from exc
        record["status"] = "transcribed"
        record["report"] = report
        self._record_store.put(feedback_id, record)
        return report

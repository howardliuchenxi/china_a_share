"""Secure Feishu message ingress for multi-turn A-share research."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
from pathlib import Path
import re
from threading import Lock
from typing import Any, Dict, List, Optional, Protocol, Union

import requests
from Crypto.Cipher import AES
from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage

from pydantic import BaseModel, ConfigDict, Field, model_validator

from china_a_share.core.contracts import (
    AnalysisConversationTurn,
    AnalysisRequest,
    AnalysisResponse,
    AnalysisTask,
    AnalysisTaskStatus,
    AnalysisTaskSubmission,
    DiscoveryTask,
)
from china_a_share.observability import log_event
from china_a_share.feishu_agent import (
    FEISHU_MESSAGE_WITHDRAWN_CODE,
    FeishuAgentConversationTurn,
    FeishuAgentCoordinator,
    FeishuAgentRequest,
    FeishuAgentTask,
    FeishuSourceMessageWithdrawnError,
)
from china_a_share.discovery.strategy_interaction import StrategyInteractionCoordinator


FEISHU_API_BASE_URL = "https://open.feishu.cn/open-apis"
FEISHU_TOKEN_TIMEOUT_SECONDS = 10
FEISHU_MESSAGE_TIMEOUT_SECONDS = 15
MAX_FEISHU_MESSAGE_LENGTH = 3_000
MAX_FEISHU_CONTEXT_TURNS = 12
FEISHU_EVENT_LEASE = timedelta(minutes=4)
FEISHU_EVENT_PROCESSING = "processing"
FEISHU_EVENT_COMPLETED = "completed"
MENTION_PATTERN = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)
TASK_ID_PATTERN = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)
STATUS_COMMAND_PATTERN = re.compile(
    r"^(?:查看进度|查询进度|进度|status)(?:\s|$)", re.IGNORECASE
)
RETRY_COMMAND_PATTERN = re.compile(r"^(?:重试|retry)(?:\s|$)", re.IGNORECASE)
NEW_SESSION_COMMAND_PATTERN = re.compile(
    r"^(?:新建会话|新建对话|新对话|new\s+session)(?:\s+(?P<name>.+))?$",
    re.IGNORECASE,
)
NEW_SESSION_AND_PROMPT_PATTERN = re.compile(
    r"^(?:新建会话|新建对话|新对话)"
    r"(?:\s+(?P<name>[^，,；;：:\n]{1,80}))?"
    r"\s*[，,；;：:]\s*(?:并\s*)?(?P<prompt>.+)$",
    re.IGNORECASE,
)
LIST_SESSIONS_COMMAND_PATTERN = re.compile(
    r"^(?:会话列表|对话列表|list\s+sessions)$",
    re.IGNORECASE,
)
SWITCH_SESSION_COMMAND_PATTERN = re.compile(
    r"^(?:切换会话|切换对话|switch\s+session)\s+(?P<target>.+)$",
    re.IGNORECASE,
)
QUICK_MENU_COMMAND_PATTERN = re.compile(
    r"^(?:帮助|菜单|快捷菜单|help)$",
    re.IGNORECASE,
)
MODEL_COMMAND_PATTERN = re.compile(
    r"^(?:切换模型|当前模型|switch\s+model|current\s+model)"
    r"(?:\s+(?P<target>\S+))?$",
    re.IGNORECASE,
)
MODEL_COMMAND_QUERY_PATTERN = re.compile(
    r"^(?:当前模型|current\s+model)", re.IGNORECASE
)
MAX_FEISHU_RESULT_ROWS = 10
logger = logging.getLogger(__name__)


class FeishuConfigurationError(RuntimeError):
    """Report missing or invalid Feishu application configuration."""


class FeishuEventError(ValueError):
    """Report a rejected Feishu callback without exposing credentials."""


class FeishuConversationTurn(BaseModel):
    """One completed research turn retained for follow-up interpretation."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=1_000)
    interpretation: Optional[str] = Field(default=None, min_length=1, max_length=1_000)
    answer: Optional[str] = Field(default=None, min_length=1, max_length=12_000)

    @model_validator(mode="after")
    def validate_content(self) -> "FeishuConversationTurn":
        """Require either a legacy interpretation or a complete agent answer."""
        if self.interpretation is None and self.answer is None:
            raise ValueError("conversation turn requires interpretation or answer")
        return self


class FeishuTaskCoordinator(Protocol):
    """Submit and inspect durable analysis tasks for Feishu conversations."""

    def submit(
        self,
        request: AnalysisRequest,
        *,
        task_id: Optional[str] = None,
    ) -> AnalysisTaskSubmission:
        """Persist and dispatch one new analysis task."""

    def get(self, task_id: str) -> Optional[Union[AnalysisTask, DiscoveryTask]]:
        """Return the current persisted task state."""


class FeishuTaskRecord(BaseModel):
    """Private linkage between one analysis task and its Feishu conversation."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, description="Stable analysis task identifier.")
    source_event_id: str = Field(
        min_length=1,
        description="Feishu event whose exactly-once command created this task.",
    )
    conversation_id: str = Field(
        min_length=1,
        description="Opaque Feishu conversation identity authorized to inspect the task.",
    )
    prompt: str = Field(
        min_length=1,
        max_length=1_000,
        description="Exact research prompt submitted for the task.",
    )
    retry_of_task_id: Optional[str] = Field(
        default=None,
        description="Failed predecessor task when this task is an explicit retry.",
    )
    context_recorded: bool = Field(
        default=False,
        description="Whether the completed interpretation was added to chat context.",
    )


class FeishuAgentSession(BaseModel):
    """One named research session available within a Feishu chat scope."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, description="Stable opaque session identifier.")
    name: str = Field(min_length=1, max_length=80, description="User-visible session name.")


class FeishuAgentSessionState(BaseModel):
    """Active session pointer and bounded session catalog for one chat scope."""

    model_config = ConfigDict(extra="forbid")

    active_session_id: str = Field(
        default="default",
        description="Session receiving ordinary messages in this chat scope.",
    )
    sessions: list[FeishuAgentSession] = Field(
        default_factory=lambda: [
            FeishuAgentSession(session_id="default", name="默认会话")
        ],
        description="Named sessions available for explicit switching.",
    )


class ConversationStore(Protocol):
    """Persist bounded completed turns for one isolated Feishu conversation."""

    def get(self, conversation_id: str) -> list[FeishuConversationTurn]:
        """Return the completed turns for one conversation."""

    def put(
        self,
        conversation_id: str,
        turns: list[FeishuConversationTurn],
    ) -> None:
        """Replace one conversation with its bounded completed turns."""

    def claim_event(self, event_id: str) -> bool:
        """Return whether this process claimed a previously unseen event."""

    def complete_event(self, event_id: str) -> None:
        """Mark one claimed event complete so later retries remain deduplicated."""

    def get_task(self, task_id: str) -> Optional[FeishuTaskRecord]:
        """Return Feishu linkage for one analysis task."""

    def get_latest_task(self, conversation_id: str) -> Optional[FeishuTaskRecord]:
        """Return the latest task submitted by one conversation."""

    def get_event_task(self, event_id: str) -> Optional[FeishuTaskRecord]:
        """Return the task already created by one retried Feishu event."""

    def put_task(self, task: FeishuTaskRecord) -> None:
        """Persist task linkage and advance the conversation's latest-task pointer."""

    def get_session_state(self, conversation_scope_id: str) -> FeishuAgentSessionState:
        """Return named sessions and the active pointer for one chat scope."""

    def put_session_state(
        self,
        conversation_scope_id: str,
        state: FeishuAgentSessionState,
    ) -> None:
        """Replace the named-session state for one chat scope."""


class MemoryConversationStore:
    """Store isolated Feishu conversations and event claims in memory."""

    def __init__(self) -> None:
        self._conversations: Dict[str, list[FeishuConversationTurn]] = {}
        self._event_ids: set[str] = set()
        self._tasks: Dict[str, FeishuTaskRecord] = {}
        self._latest_tasks: Dict[str, str] = {}
        self._event_tasks: Dict[str, str] = {}
        self._session_states: Dict[str, FeishuAgentSessionState] = {}
        self._lock = Lock()

    def get(self, conversation_id: str) -> list[FeishuConversationTurn]:
        """Return a defensive copy of one bounded conversation."""
        with self._lock:
            return [
                turn.model_copy(deep=True)
                for turn in self._conversations.get(conversation_id, [])
            ]

    def put(
        self,
        conversation_id: str,
        turns: list[FeishuConversationTurn],
    ) -> None:
        """Replace one conversation while enforcing the shared context bound."""
        with self._lock:
            self._conversations[conversation_id] = [
                turn.model_copy(deep=True)
                for turn in turns[-MAX_FEISHU_CONTEXT_TURNS:]
            ]

    def claim_event(self, event_id: str) -> bool:
        """Atomically reject callbacks already processed by this instance."""
        with self._lock:
            if event_id in self._event_ids:
                return False
            self._event_ids.add(event_id)
            return True

    def complete_event(self, event_id: str) -> None:
        """Retain the in-memory claim as the completed-event marker."""

    def get_task(self, task_id: str) -> Optional[FeishuTaskRecord]:
        """Return an isolated task linkage when it exists."""
        with self._lock:
            task = self._tasks.get(task_id)
            return task.model_copy(deep=True) if task else None

    def get_latest_task(self, conversation_id: str) -> Optional[FeishuTaskRecord]:
        """Return the latest task linkage for one conversation."""
        with self._lock:
            task_id = self._latest_tasks.get(conversation_id)
            task = self._tasks.get(task_id) if task_id else None
            return task.model_copy(deep=True) if task else None

    def get_event_task(self, event_id: str) -> Optional[FeishuTaskRecord]:
        """Return the task previously created by one source event."""
        with self._lock:
            task_id = self._event_tasks.get(event_id)
            task = self._tasks.get(task_id) if task_id else None
            return task.model_copy(deep=True) if task else None

    def put_task(self, task: FeishuTaskRecord) -> None:
        """Store an isolated task linkage and latest-task pointer."""
        with self._lock:
            self._tasks[task.task_id] = task.model_copy(deep=True)
            self._latest_tasks[task.conversation_id] = task.task_id
            self._event_tasks[task.source_event_id] = task.task_id

    def get_session_state(self, conversation_scope_id: str) -> FeishuAgentSessionState:
        """Return an isolated named-session state for one chat scope."""
        with self._lock:
            state = self._session_states.get(conversation_scope_id)
            return (
                state.model_copy(deep=True)
                if state is not None
                else FeishuAgentSessionState()
            )

    def put_session_state(
        self,
        conversation_scope_id: str,
        state: FeishuAgentSessionState,
    ) -> None:
        """Replace one named-session state atomically in memory."""
        with self._lock:
            self._session_states[conversation_scope_id] = state.model_copy(deep=True)


class CloudStorageConversationStore:
    """Persist bounded conversations and idempotency claims in Cloud Storage."""

    def __init__(
        self,
        bucket_name: str,
        storage_client: Optional[storage.Client] = None,
    ) -> None:
        if not bucket_name:
            raise FeishuConfigurationError(
                "TUSHARE_CACHE_BUCKET is required for Feishu conversation storage."
            )
        self._bucket = (storage_client or storage.Client()).bucket(bucket_name)

    def get(self, conversation_id: str) -> list[FeishuConversationTurn]:
        """Read one private conversation object when it exists."""
        blob = self._bucket.blob(self._conversation_object(conversation_id))
        if not blob.exists():
            return []
        payload = json.loads(blob.download_as_text())
        return [FeishuConversationTurn.model_validate(item) for item in payload]

    def put(
        self,
        conversation_id: str,
        turns: list[FeishuConversationTurn],
    ) -> None:
        """Replace one bounded conversation as a private JSON object."""
        payload = [
            turn.model_dump(mode="json")
            for turn in turns[-MAX_FEISHU_CONTEXT_TURNS:]
        ]
        blob = self._bucket.blob(self._conversation_object(conversation_id))
        blob.upload_from_string(
            json.dumps(payload, ensure_ascii=False), content_type="application/json"
        )

    def claim_event(self, event_id: str) -> bool:
        """Claim a new event or reclaim an abandoned processing lease."""
        blob = self._bucket.blob(self._event_object(event_id))
        try:
            blob.upload_from_string(
                FEISHU_EVENT_PROCESSING,
                content_type="text/plain",
                if_generation_match=0,
            )
        except PreconditionFailed:
            blob.reload()
            if blob.download_as_text() == FEISHU_EVENT_COMPLETED:
                return False
            updated = blob.updated
            if updated is None or datetime.now(timezone.utc) - updated < FEISHU_EVENT_LEASE:
                return False
            try:
                # The generation precondition lets only one retry take over a
                # lease abandoned by an instance shutdown or hard termination.
                blob.upload_from_string(
                    FEISHU_EVENT_PROCESSING,
                    content_type="text/plain",
                    if_generation_match=blob.generation,
                )
            except PreconditionFailed:
                return False
        return True

    def complete_event(self, event_id: str) -> None:
        """Persist completion after the reply is accepted by Feishu."""
        blob = self._bucket.blob(self._event_object(event_id))
        blob.upload_from_string(FEISHU_EVENT_COMPLETED, content_type="text/plain")

    def get_task(self, task_id: str) -> Optional[FeishuTaskRecord]:
        """Read one private Feishu task linkage when it exists."""
        blob = self._bucket.blob(self._task_object(task_id))
        if not blob.exists():
            return None
        return FeishuTaskRecord.model_validate_json(blob.download_as_text())

    def get_latest_task(self, conversation_id: str) -> Optional[FeishuTaskRecord]:
        """Resolve the latest task pointer for one Feishu conversation."""
        blob = self._bucket.blob(self._latest_task_object(conversation_id))
        if not blob.exists():
            return None
        task_id = blob.download_as_text().strip()
        return self.get_task(task_id) if task_id else None

    def get_event_task(self, event_id: str) -> Optional[FeishuTaskRecord]:
        """Resolve the task pointer created by one retried source event."""
        blob = self._bucket.blob(self._event_task_object(event_id))
        if not blob.exists():
            return None
        task_id = blob.download_as_text().strip()
        return self.get_task(task_id) if task_id else None

    def put_task(self, task: FeishuTaskRecord) -> None:
        """Persist one task linkage and its conversation pointer."""
        self._bucket.blob(self._task_object(task.task_id)).upload_from_string(
            task.model_dump_json(),
            content_type="application/json",
        )
        self._bucket.blob(
            self._latest_task_object(task.conversation_id)
        ).upload_from_string(task.task_id, content_type="text/plain")
        self._bucket.blob(self._event_task_object(task.source_event_id)).upload_from_string(
            task.task_id, content_type="text/plain"
        )

    def get_session_state(self, conversation_scope_id: str) -> FeishuAgentSessionState:
        """Read named-session state or return the default session."""
        blob = self._bucket.blob(self._session_state_object(conversation_scope_id))
        if not blob.exists():
            return FeishuAgentSessionState()
        return FeishuAgentSessionState.model_validate_json(blob.download_as_text())

    def put_session_state(
        self,
        conversation_scope_id: str,
        state: FeishuAgentSessionState,
    ) -> None:
        """Persist the complete named-session state for one chat scope."""
        self._bucket.blob(
            self._session_state_object(conversation_scope_id)
        ).upload_from_string(state.model_dump_json(), content_type="application/json")

    @staticmethod
    def _conversation_object(conversation_id: str) -> str:
        """Hide Feishu identifiers from storage object names."""
        digest = hashlib.sha256(conversation_id.encode()).hexdigest()
        return f"feishu/conversations/{digest}.json"

    @staticmethod
    def _event_object(event_id: str) -> str:
        """Return a stable opaque object name for one event claim."""
        digest = hashlib.sha256(event_id.encode()).hexdigest()
        return f"feishu/events/{digest}.json"

    @staticmethod
    def _task_object(task_id: str) -> str:
        """Return the private object name for one Feishu task linkage."""
        return f"feishu/tasks/{task_id}.json"

    @staticmethod
    def _latest_task_object(conversation_id: str) -> str:
        """Hide the conversation identifier in its latest-task pointer name."""
        digest = hashlib.sha256(conversation_id.encode()).hexdigest()
        return f"feishu/conversation-tasks/{digest}.txt"

    @staticmethod
    def _event_task_object(event_id: str) -> str:
        """Hide the Feishu event identifier in its task pointer name."""
        digest = hashlib.sha256(event_id.encode()).hexdigest()
        return f"feishu/event-tasks/{digest}.txt"

    @staticmethod
    def _session_state_object(conversation_scope_id: str) -> str:
        """Hide the chat scope in the named-session object path."""
        digest = hashlib.sha256(conversation_scope_id.encode()).hexdigest()
        return f"feishu/agent-sessions/{digest}.json"


class FeishuMessageSender(Protocol):
    """Send one reply to the message that initiated an analysis turn."""

    def reply(self, message_id: str, text: str) -> str:
        """Reply with bounded plain text and return the created message ID."""

    def update(self, message_id: str, text: str) -> None:
        """Replace one application-authored text message."""

    def reply_card(self, message_id: str, card: Dict[str, Any]) -> str:
        """Reply with one interactive card and return the created message ID."""

    def reply_file(self, message_id: str, path: "Path") -> None:
        """Upload and reply with one file attachment."""


class FeishuOpenApiClient:
    """Authenticate one Feishu custom app and send message replies."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not app_id or not app_secret:
            raise FeishuConfigurationError(
                "FEISHU_APP_ID and FEISHU_APP_SECRET are required."
            )
        self._app_id = app_id
        self._app_secret = app_secret
        self._session = session or requests.Session()

    def reply(self, message_id: str, text: str) -> str:
        """Reply visibly to the source message through the application identity."""
        token = self._tenant_access_token()
        response = self._session.post(
            f"{FEISHU_API_BASE_URL}/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "msg_type": "text",
                "content": json.dumps(
                    {"text": text[:MAX_FEISHU_MESSAGE_LENGTH]}, ensure_ascii=False
                ),
            },
            timeout=FEISHU_MESSAGE_TIMEOUT_SECONDS,
        )
        self._raise_for_feishu_error(response, "message reply")
        reply_message_id = str(
            ((response.json().get("data") or {}).get("message_id") or "")
        ).strip()
        if not reply_message_id:
            raise RuntimeError("Feishu message reply omitted the message ID.")
        return reply_message_id

    def update(self, message_id: str, text: str) -> None:
        """Replace one text reply so progress does not flood the group chat."""
        token = self._tenant_access_token()
        response = self._session.put(
            f"{FEISHU_API_BASE_URL}/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "msg_type": "text",
                "content": json.dumps(
                    {"text": text[:MAX_FEISHU_MESSAGE_LENGTH]}, ensure_ascii=False
                ),
            },
            timeout=FEISHU_MESSAGE_TIMEOUT_SECONDS,
        )
        self._raise_for_feishu_error(response, "message update")

    def reply_card(self, message_id: str, card: Dict[str, Any]) -> str:
        """Reply with one interactive card through the application identity."""
        token = self._tenant_access_token()
        response = self._session.post(
            f"{FEISHU_API_BASE_URL}/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            timeout=FEISHU_MESSAGE_TIMEOUT_SECONDS,
        )
        self._raise_for_feishu_error(response, "card reply")
        reply_message_id = str(
            ((response.json().get("data") or {}).get("message_id") or "")
        ).strip()
        if not reply_message_id:
            raise RuntimeError("Feishu card reply omitted the message ID.")
        return reply_message_id

    def send_chat_card(self, chat_id: str, card: Dict[str, Any]) -> str:
        """Send one new interactive card to a chat without a source message."""
        token = self._tenant_access_token()
        response = self._session.post(
            f"{FEISHU_API_BASE_URL}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            timeout=FEISHU_MESSAGE_TIMEOUT_SECONDS,
        )
        self._raise_for_feishu_error(response, "chat card send")
        sent_message_id = str(
            ((response.json().get("data") or {}).get("message_id") or "")
        ).strip()
        if not sent_message_id:
            raise RuntimeError("Feishu chat card send omitted the message ID.")
        return sent_message_id

    def reply_file(self, message_id: str, path: "Path") -> None:
        """Upload one generated workbook and reply with the resulting file key."""
        token = self._tenant_access_token()
        with path.open("rb") as file_handle:
            upload_response = self._session.post(
                f"{FEISHU_API_BASE_URL}/im/v1/files",
                headers={"Authorization": f"Bearer {token}"},
                data={"file_type": "xls", "file_name": path.name},
                files={
                    "file": (
                        path.name,
                        file_handle,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
                timeout=FEISHU_MESSAGE_TIMEOUT_SECONDS,
            )
        self._raise_for_feishu_error(upload_response, "file upload")
        file_key = ((upload_response.json().get("data") or {}).get("file_key") or "")
        if not file_key:
            raise RuntimeError("Feishu file upload omitted the file key.")
        response = self._session.post(
            f"{FEISHU_API_BASE_URL}/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}),
            },
            timeout=FEISHU_MESSAGE_TIMEOUT_SECONDS,
        )
        self._raise_for_feishu_error(response, "file reply")

    def _tenant_access_token(self) -> str:
        """Return a fresh tenant token without exposing credentials to callers."""
        token_response = self._session.post(
            f"{FEISHU_API_BASE_URL}/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
            timeout=FEISHU_TOKEN_TIMEOUT_SECONDS,
        )
        self._raise_for_feishu_error(token_response, "tenant token")
        token = token_response.json().get("tenant_access_token", "")
        if not token:
            raise RuntimeError("Feishu tenant token response omitted the access token.")
        return token

    @staticmethod
    def _raise_for_feishu_error(response: requests.Response, operation: str) -> None:
        """Fail fast on HTTP or Feishu application errors."""
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = {}
        code = payload.get("code")
        message = str(payload.get("msg") or "").strip()
        detail = ""
        if code is not None or message:
            detail = f" code={code} message={message[:300]}"
        if code == FEISHU_MESSAGE_WITHDRAWN_CODE:
            # A withdrawn source message is a terminal delivery condition, not a
            # transient failure; callers need to stop replying instead of retrying.
            raise FeishuSourceMessageWithdrawnError(
                f"Feishu {operation} rejected because the source message was "
                f"withdrawn.{detail}"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Feishu {operation} failed with HTTP {response.status_code}.{detail}"
            )
        if payload.get("code", 0) != 0:
            raise RuntimeError(
                f"Feishu {operation} failed with code {payload.get('code')}."
                f" message={message[:300]}"
            )


@dataclass(frozen=True)
class FeishuMessageEvent:
    """Validated fields required to execute one Feishu research turn."""

    event_id: str
    message_id: str
    conversation_id: str
    prompt: str
    sender_open_id: str = ""
    chat_id: str = ""


@dataclass(frozen=True)
class FeishuStrategyCardAction:
    """Validated fields of one strategy card button click."""

    event_id: str
    message_id: str
    chat_id: str
    operator_open_id: str
    action_name: str
    value: Dict[str, Any]


class FeishuResearchBot:
    """Validate callbacks and connect Feishu conversations to analysis."""

    def __init__(
        self,
        task_coordinator: FeishuTaskCoordinator,
        sender: FeishuMessageSender,
        store: ConversationStore,
        *,
        verification_token: str,
        encrypt_key: str,
        allowed_open_ids: Optional[set[str]] = None,
        agent_coordinator: Optional[FeishuAgentCoordinator] = None,
        strategy_interaction: Optional[StrategyInteractionCoordinator] = None,
        llm_switcher: Optional[Any] = None,
    ) -> None:
        if not verification_token or not encrypt_key:
            raise FeishuConfigurationError(
                "FEISHU_VERIFICATION_TOKEN and FEISHU_ENCRYPT_KEY are required."
            )
        self._task_coordinator = task_coordinator
        self._sender = sender
        self._store = store
        self._verification_token = verification_token
        self._encrypt_key = encrypt_key
        self._allowed_open_ids = allowed_open_ids or set()
        self._agent_coordinator = agent_coordinator
        self._strategy_interaction = strategy_interaction
        self._llm_switcher = llm_switcher

    @property
    def strategy_scanner(self):
        """Return the strategy scanner wired into the interaction layer, if any."""
        if self._strategy_interaction is None:
            return None
        return self._strategy_interaction.scanner

    def verify_signature(
        self,
        body: bytes,
        timestamp: str,
        nonce: str,
        signature: str,
        *,
        allow_missing: bool = False,
    ) -> None:
        """Reject callbacks whose Feishu request signature does not match."""
        if allow_missing and not timestamp and not nonce and not signature:
            # Some card-callback security modes authenticate only with the
            # verification token embedded in the V2 event header.
            return
        if not timestamp or not nonce or not signature:
            raise FeishuEventError("Feishu signature headers are required.")
        expected = hashlib.sha256(
            timestamp.encode()
            + nonce.encode()
            + self._encrypt_key.encode()
            + body
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise FeishuEventError("Feishu callback signature is invalid.")

    def decode_payload(self, body: bytes) -> Dict[str, Any]:
        """Decode one plaintext or AES-encrypted Feishu callback body."""
        try:
            envelope = json.loads(body)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FeishuEventError("Feishu callback body is invalid JSON.") from exc
        if not isinstance(envelope, dict):
            raise FeishuEventError("Feishu callback body must be a JSON object.")

        encrypted = envelope.get("encrypt")
        if encrypted is None:
            return envelope
        if not isinstance(encrypted, str) or not encrypted:
            raise FeishuEventError("Feishu callback encryption is invalid.")

        try:
            encrypted_body = base64.b64decode(encrypted, validate=True)
            if (
                len(encrypted_body) < AES.block_size * 2
                or len(encrypted_body) % AES.block_size != 0
            ):
                raise ValueError("invalid ciphertext length")
            key = hashlib.sha256(self._encrypt_key.encode()).digest()
            cipher = AES.new(key, AES.MODE_CBC, encrypted_body[: AES.block_size])
            padded_body = cipher.decrypt(encrypted_body[AES.block_size :])
            padding_size = padded_body[-1]
            if (
                padding_size < 1
                or padding_size > AES.block_size
                or padded_body[-padding_size:] != bytes([padding_size]) * padding_size
            ):
                raise ValueError("invalid PKCS7 padding")
            payload = json.loads(padded_body[:-padding_size].decode("utf-8"))
        except (
            binascii.Error,
            IndexError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise FeishuEventError("Feishu callback encryption is invalid.") from exc
        if not isinstance(payload, dict):
            raise FeishuEventError("Feishu callback body must be a JSON object.")
        return payload

    def parse_event(self, payload: Dict[str, Any]) -> Optional[FeishuMessageEvent]:
        """Validate one decoded v2 Feishu text-message callback."""
        header = payload.get("header") or {}
        if header.get("token") != self._verification_token:
            raise FeishuEventError("Feishu callback verification token is invalid.")
        if header.get("event_type") != "im.message.receive_v1":
            return None
        event = payload.get("event") or {}
        sender_id = ((event.get("sender") or {}).get("sender_id") or {}).get(
            "open_id", ""
        )
        if self._allowed_open_ids and sender_id not in self._allowed_open_ids:
            raise FeishuEventError("This Feishu user is not allowed to run research.")
        message = event.get("message") or {}
        if message.get("message_type") != "text":
            return None
        try:
            text = json.loads(message.get("content", "{}"))["text"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise FeishuEventError("Feishu text message content is invalid.") from exc
        prompt = MENTION_PATTERN.sub("", text)
        for mention in message.get("mentions") or []:
            key = str(mention.get("key") or "").strip()
            if key:
                prompt = prompt.replace(key, " ")
        prompt = prompt.strip() or "快捷菜单"
        chat_id = str(message.get("chat_id", "")).strip()
        message_id = str(message.get("message_id", "")).strip()
        event_id = str(header.get("event_id", "")).strip()
        if not chat_id or not message_id or not event_id or not sender_id:
            raise FeishuEventError("Feishu callback omitted required identifiers.")
        # Topic groups stamp a stable thread_id on every message, so it can safely
        # partition conversations there. root_id only encodes reply linkage in
        # ordinary chats: a user answering the bot's previous message via a quoted
        # reply must stay in the same conversation bucket, or follow-ups such as a
        # bare menu number arrive with no recorded history.
        topic_id = str(message.get("thread_id") or "root")
        conversation_id = ":".join(
            [str(header.get("tenant_key", "")), chat_id, topic_id, sender_id]
        )
        return FeishuMessageEvent(
            event_id,
            message_id,
            conversation_id,
            prompt,
            sender_open_id=sender_id,
            chat_id=chat_id,
        )

    def parse_card_action(
        self, payload: Dict[str, Any]
    ) -> Optional[FeishuMessageEvent]:
        """Validate one card interaction and translate it to an existing command."""
        context = self._extract_card_action_context(payload)
        if context is None:
            return None
        (
            event_id,
            chat_id,
            message_id,
            operator_id,
            action_name,
            form_value,
            selected_option,
            value,
        ) = context

        if action_name == "submit_research":
            prompt = str(
                form_value.get("prompt") or "" if isinstance(form_value, dict) else ""
            ).strip()
            if not prompt:
                raise FeishuEventError("Research prompt is required.")
        elif action_name == "switch_model":
            # A standalone select_static fires immediately with the chosen
            # option; a form-embedded one would deliver it via form_value.
            selected = str(
                selected_option
                or (form_value.get("model") if isinstance(form_value, dict) else "")
                or ""
            ).strip()
            if not selected:
                raise FeishuEventError("Model selection is required.")
            prompt = f"切换模型 {selected}"
        else:
            prompt = {
                "new_session": "新建会话",
                "list_sessions": "会话列表",
                "task_status": "查看进度",
            }.get(action_name, "")
        if not prompt:
            return None

        conversation_id = ":".join(
            [str((payload.get("header") or {}).get("tenant_key") or ""), chat_id, "root", operator_id]
        )
        return FeishuMessageEvent(
            event_id,
            message_id,
            conversation_id,
            prompt,
            sender_open_id=operator_id,
            chat_id=chat_id,
        )

    def parse_strategy_card_action(
        self, payload: Dict[str, Any]
    ) -> Optional[FeishuStrategyCardAction]:
        """Validate one strategy card interaction or return None for other actions."""
        if self._strategy_interaction is None:
            return None
        context = self._extract_card_action_context(payload)
        if context is None:
            return None
        event_id, chat_id, message_id, operator_id, action_name, _form_value, _option, value = (
            context
        )
        if not action_name.startswith("strategy_"):
            return None
        return FeishuStrategyCardAction(
            event_id=event_id,
            message_id=message_id,
            chat_id=chat_id,
            operator_open_id=operator_id,
            action_name=action_name,
            value=value if isinstance(value, dict) else {},
        )

    def _extract_card_action_context(self, payload: Dict[str, Any]):
        """Validate one card callback and return its identifiers and action."""
        header = payload.get("header") or {}
        if header.get("token") != self._verification_token:
            raise FeishuEventError("Feishu callback verification token is invalid.")
        if header.get("event_type") != "card.action.trigger":
            return None
        event = payload.get("event") or {}
        operator_id = str((event.get("operator") or {}).get("open_id") or "").strip()
        if self._allowed_open_ids and operator_id not in self._allowed_open_ids:
            raise FeishuEventError("This Feishu user is not allowed to run research.")
        context = event.get("context") or {}
        chat_id = str(context.get("open_chat_id") or "").strip()
        message_id = str(context.get("open_message_id") or "").strip()
        event_id = str(header.get("event_id") or "").strip()
        if not chat_id or not message_id or not event_id or not operator_id:
            raise FeishuEventError("Feishu card callback omitted required identifiers.")

        action = event.get("action") or {}
        value = action.get("value") or {}
        action_value = value.get("action") if isinstance(value, dict) else None
        action_name = str(action_value or action.get("name") or "").strip()
        form_value = action.get("form_value") or {}
        selected_option = str(action.get("option") or "").strip()
        return (
            event_id,
            chat_id,
            message_id,
            operator_id,
            action_name,
            form_value,
            selected_option,
            value,
        )

    def verify_challenge(self, payload: Dict[str, Any]) -> str:
        """Validate and return one Feishu endpoint-verification challenge."""
        if payload.get("token") != self._verification_token:
            raise FeishuEventError("Feishu callback verification token is invalid.")
        challenge = str(payload.get("challenge", ""))
        if not challenge:
            raise FeishuEventError("Feishu callback challenge is missing.")
        return challenge

    def process(self, event: FeishuMessageEvent) -> None:
        """Submit or inspect one durable research task from a claimed event."""
        if not self._store.claim_event(event.event_id):
            return
        try:
            if self._strategy_interaction is not None and self._strategy_interaction.handles_prompt(event.prompt):
                card = self._strategy_interaction.handle_message(
                    event.sender_open_id,
                    event.chat_id,
                    event.prompt,
                    event.event_id,
                )
                if card is not None:
                    self._sender.reply_card(event.message_id, card)
                reply = None
            elif QUICK_MENU_COMMAND_PATTERN.match(event.prompt):
                model_switcher = self._llm_switcher
                self._sender.reply_card(
                    event.message_id,
                    build_feishu_quick_menu_card(
                        include_strategy=self._strategy_interaction is not None,
                        model_status=(
                            model_switcher.status_line()
                            if model_switcher is not None
                            else ""
                        ),
                        model_options=(
                            model_switcher.dropdown_options()
                            if model_switcher is not None
                            else []
                        ),
                    ),
                )
                reply = None
            else:
                combined_session = (
                    NEW_SESSION_AND_PROMPT_PATTERN.match(event.prompt)
                    if self._agent_coordinator is not None
                    else None
                )
                if combined_session is not None:
                    reply = self._create_session_and_submit(event, combined_session)
                elif self._agent_coordinator is not None and (
                    NEW_SESSION_COMMAND_PATTERN.match(event.prompt)
                    or LIST_SESSIONS_COMMAND_PATTERN.match(event.prompt)
                    or SWITCH_SESSION_COMMAND_PATTERN.match(event.prompt)
                ):
                    reply = self._session_command_reply(event)
                elif STATUS_COMMAND_PATTERN.match(event.prompt):
                    reply = self._status_reply(event)
                elif RETRY_COMMAND_PATTERN.match(event.prompt):
                    reply = self._retry_reply(event)
                elif MODEL_COMMAND_PATTERN.match(event.prompt):
                    reply = self._model_command_reply(event)
                else:
                    reply = self._submit_reply(event)
            if reply is not None:
                self._sender.reply(event.message_id, reply)
            self._store.complete_event(event.event_id)
        except Exception:
            log_event(
                logger,
                logging.ERROR,
                "feishu_research_turn_failed",
                event_id=event.event_id,
                source="system",
                exc_info=True,
            )
            self._sender.reply(
                event.message_id,
                "研究任务操作失败，请稍后重试。若问题持续，请联系管理员并提供"
                f"事件编号 {event.event_id}。",
            )

    def process_strategy_card_action(self, action: FeishuStrategyCardAction) -> None:
        """Execute one claimed strategy card click and reply with its card."""
        if self._strategy_interaction is None:
            return
        if not self._store.claim_event(action.event_id):
            return
        try:
            card = self._strategy_interaction.handle_card_action(
                action.operator_open_id,
                action.chat_id,
                action.action_name,
                action.value,
                action.event_id,
            )
            if card is not None:
                self._sender.reply_card(action.message_id, card)
            self._store.complete_event(action.event_id)
        except Exception:
            log_event(
                logger,
                logging.ERROR,
                "feishu_strategy_action_failed",
                event_id=action.event_id,
                source="system",
                exc_info=True,
            )
            self._sender.reply(
                action.message_id,
                "策略操作失败，请稍后重试。若问题持续，请联系管理员并提供"
                f"事件编号 {action.event_id}。",
            )

    def _submit_reply(self, event: FeishuMessageEvent) -> Optional[str]:
        """Create one durable analysis task and return its tracking commands."""
        if self._agent_coordinator is not None:
            return self._submit_agent_reply(event)
        existing = self._store.get_event_task(event.event_id)
        if existing is not None:
            return self._accepted_task_reply(existing.task_id)
        latest = self._store.get_latest_task(event.conversation_id)
        if latest is not None:
            latest_task = self._task_coordinator.get(latest.task_id)
            if isinstance(latest_task, AnalysisTask) and latest_task.status in {
                AnalysisTaskStatus.QUEUED,
                AnalysisTaskStatus.RUNNING,
            }:
                return (
                    f"上一项研究任务 {latest.task_id} 仍在"
                    f"{_task_status_label(latest_task.status)}。\n"
                    "请先回复“查看进度”，任务完成后再提交下一项研究。"
                )
        prior_turns = self._completed_conversation(event.conversation_id)
        submission = self._task_coordinator.submit(
            AnalysisRequest(
                prompt=event.prompt,
                conversation=[
                    AnalysisConversationTurn(
                        prompt=turn.prompt,
                        interpretation=turn.interpretation,
                    )
                    for turn in prior_turns
                ],
            ),
            task_id=self._task_id_for_event(event.event_id),
        )
        self._store.put_task(
            FeishuTaskRecord(
                task_id=submission.task_id,
                source_event_id=event.event_id,
                conversation_id=event.conversation_id,
                prompt=event.prompt,
            )
        )
        return self._accepted_task_reply(submission.task_id)

    def _submit_agent_reply(self, event: FeishuMessageEvent) -> Optional[str]:
        """Submit one independent agent turn with bounded completed context."""
        existing = self._store.get_event_task(event.event_id)
        if existing is not None:
            return self._accepted_task_reply(existing.task_id)
        active_session = self._active_agent_session(event.conversation_id)
        conversation_id = (
            f"{event.conversation_id}:session:{active_session.session_id}"
        )
        prior_turns = self._completed_agent_conversation(conversation_id)
        task = self._agent_coordinator.submit(
            FeishuAgentRequest(
                prompt=event.prompt,
                conversation_id=conversation_id,
                conversation_name=active_session.name,
                source_message_id=event.message_id,
                conversation=[
                    FeishuAgentConversationTurn(
                        prompt=turn.prompt,
                        answer=turn.answer or turn.interpretation or "",
                    )
                    for turn in prior_turns
                ],
            ),
            task_id=self._task_id_for_event(event.event_id),
        )
        self._store.put_task(
            FeishuTaskRecord(
                task_id=task.task_id,
                source_event_id=event.event_id,
                conversation_id=conversation_id,
                prompt=event.prompt,
            )
        )
        # The worker creates one visible progress reply and edits it in place.
        # Suppressing the acknowledgement avoids a second message per question.
        return None

    @staticmethod
    def _accepted_task_reply(task_id: str) -> str:
        """Return the stable acknowledgement for a newly accepted task."""
        return (
            "研究任务已受理。\n"
            f"任务编号：{task_id}\n"
            "当前状态：排队中\n"
            "回复“查看进度”可查看最新任务；失败后回复“重试”可重新提交。"
        )

    def _status_reply(self, event: FeishuMessageEvent) -> str:
        """Render progress for an explicitly named or latest conversation task."""
        record = self._resolve_task_record(event)
        if record is None:
            return "当前对话中没有可跟踪的研究任务。"
        task = self._task_coordinator.get(record.task_id)
        if isinstance(task, FeishuAgentTask):
            if task.status == AnalysisTaskStatus.SUCCEEDED:
                self._record_completed_agent_context(record, task)
            return format_feishu_agent_task(task)
        if not isinstance(task, AnalysisTask):
            return f"未找到研究任务 {record.task_id}。"
        if task.status == AnalysisTaskStatus.SUCCEEDED:
            self._record_completed_context(record, task)
        return format_analysis_task(task)

    def _model_command_reply(self, event: FeishuMessageEvent) -> str:
        """Answer 切换模型/当前模型 commands against persisted preference."""
        if self._llm_switcher is None:
            return (
                "模型切换不可用：当前部署未配置持久化偏好存储。"
                "请联系管理员配置应用存储桶。"
            )
        if MODEL_COMMAND_QUERY_PATTERN.match(event.prompt):
            return self._llm_switcher.status_reply()
        match = MODEL_COMMAND_PATTERN.match(event.prompt)
        target = (match.groupdict().get("target") or "").strip() if match else ""
        if not target:
            return self._llm_switcher.status_reply()
        return self._llm_switcher.switch(target)

    def _retry_reply(self, event: FeishuMessageEvent) -> Optional[str]:
        """Submit a new attempt from one failed task without changing its request."""
        existing = self._store.get_event_task(event.event_id)
        if existing is not None:
            return self._retried_task_reply(existing)
        record = self._resolve_task_record(event)
        if record is None:
            return "当前对话中没有可重试的研究任务。"
        task = self._task_coordinator.get(record.task_id)
        if isinstance(task, FeishuAgentTask):
            if task.status != AnalysisTaskStatus.FAILED:
                return (
                    f"任务 {task.task_id} 当前状态为{_task_status_label(task.status)}，"
                    "只有失败任务可以重试。"
                )
            retry_request = task.request.model_copy(
                update={"source_message_id": event.message_id}
            )
            retried_task = self._agent_coordinator.submit(
                retry_request,
                task_id=self._task_id_for_event(event.event_id),
            )
            retry_record = FeishuTaskRecord(
                task_id=retried_task.task_id,
                source_event_id=event.event_id,
                conversation_id=record.conversation_id,
                prompt=record.prompt,
                retry_of_task_id=record.task_id,
            )
            self._store.put_task(retry_record)
            return None
        if not isinstance(task, AnalysisTask):
            return f"未找到研究任务 {record.task_id}。"
        if task.status != AnalysisTaskStatus.FAILED:
            return (
                f"任务 {task.task_id} 当前状态为{_task_status_label(task.status)}，"
                "只有失败任务可以重试。"
            )
        submission = self._task_coordinator.submit(
            task.request,
            task_id=self._task_id_for_event(event.event_id),
        )
        retry_record = FeishuTaskRecord(
            task_id=submission.task_id,
            source_event_id=event.event_id,
            conversation_id=event.conversation_id,
            prompt=record.prompt,
            retry_of_task_id=record.task_id,
        )
        self._store.put_task(retry_record)
        return self._retried_task_reply(retry_record)

    @staticmethod
    def _retried_task_reply(record: FeishuTaskRecord) -> str:
        """Return the stable acknowledgement for an explicit retry attempt."""
        return (
            f"已重试失败任务 {record.retry_of_task_id}。\n"
            f"新任务编号：{record.task_id}\n"
            "当前状态：排队中"
        )

    def _resolve_task_record(
        self, event: FeishuMessageEvent
    ) -> Optional[FeishuTaskRecord]:
        """Resolve a task while enforcing conversation-level authorization."""
        task_id_match = TASK_ID_PATTERN.search(event.prompt)
        active_conversation_id = (
            self._active_agent_conversation_id(event.conversation_id)
            if self._agent_coordinator is not None
            else event.conversation_id
        )
        record = (
            self._store.get_task(task_id_match.group(0).lower())
            if task_id_match
            else self._store.get_latest_task(active_conversation_id)
        )
        if record is None or record.conversation_id != active_conversation_id:
            return None
        return record

    def _active_agent_conversation_id(self, conversation_scope_id: str) -> str:
        """Resolve the active named session within one isolated chat scope."""
        session = self._active_agent_session(conversation_scope_id)
        return f"{conversation_scope_id}:session:{session.session_id}"

    def _active_agent_session(
        self,
        conversation_scope_id: str,
    ) -> FeishuAgentSession:
        """Return the active named session or fail on corrupted persisted state."""
        state = self._store.get_session_state(conversation_scope_id)
        for session in state.sessions:
            if session.session_id == state.active_session_id:
                return session
        raise RuntimeError(
            "Active Feishu session is missing from the persisted session catalog."
        )

    def _create_session_and_submit(
        self,
        event: FeishuMessageEvent,
        command: re.Match[str],
    ) -> Optional[str]:
        """Create one backend session and submit the command's research prompt."""
        research_prompt = command.group("prompt").strip()
        explicit_name = (command.group("name") or "").strip()
        session_name = explicit_name or self._session_name_from_prompt(
            research_prompt
        )
        self._create_agent_session(event.conversation_id, event.event_id, session_name)
        research_event = FeishuMessageEvent(
            event_id=event.event_id,
            message_id=event.message_id,
            conversation_id=event.conversation_id,
            prompt=research_prompt,
        )
        return self._submit_agent_reply(research_event)

    def _session_command_reply(self, event: FeishuMessageEvent) -> str:
        """Create, list, or switch named sessions without invoking the model."""
        state = self._store.get_session_state(event.conversation_id)
        new_match = NEW_SESSION_COMMAND_PATTERN.match(event.prompt)
        if new_match:
            name = (new_match.group("name") or f"会话 {len(state.sessions)}").strip()
            session = self._create_agent_session(
                event.conversation_id,
                event.event_id,
                name,
            )
            return f"已新建并切换到会话：{session.name}（{session.session_id}）"
        if LIST_SESSIONS_COMMAND_PATTERN.match(event.prompt):
            lines = ["当前群聊会话："]
            for session in state.sessions:
                marker = "当前" if session.session_id == state.active_session_id else "可切换"
                lines.append(f"- {session.name}（{session.session_id}，{marker}）")
            return "\n".join(lines)
        switch_match = SWITCH_SESSION_COMMAND_PATTERN.match(event.prompt)
        target = switch_match.group("target").strip()
        matches = [
            session
            for session in state.sessions
            if session.session_id == target or session.name == target
        ]
        if len(matches) != 1:
            return "未找到唯一匹配的会话，请发送“会话列表”查看名称和编号。"
        state.active_session_id = matches[0].session_id
        self._store.put_session_state(event.conversation_id, state)
        return f"已切换到会话：{matches[0].name}（{matches[0].session_id}）"

    def _create_agent_session(
        self,
        conversation_scope_id: str,
        event_id: str,
        name: str,
    ) -> FeishuAgentSession:
        """Persist one idempotent named session and make it active."""
        state = self._store.get_session_state(conversation_scope_id)
        session_id = self._session_id_for_event(event_id)
        existing = next(
            (
                session
                for session in state.sessions
                if session.session_id == session_id
            ),
            None,
        )
        if existing is None:
            existing = FeishuAgentSession(session_id=session_id, name=name)
            state.sessions.append(existing)
        state.active_session_id = existing.session_id
        self._store.put_session_state(conversation_scope_id, state)
        return existing

    @staticmethod
    def _session_name_from_prompt(prompt: str) -> str:
        """Derive a concise display alias while keeping the opaque ID authoritative."""
        normalized = re.sub(r"\s+", " ", prompt).strip(" ，,。；;：:")
        return normalized[:80] or "未命名会话"

    @staticmethod
    def _session_id_for_event(event_id: str) -> str:
        """Derive an opaque session identifier that is stable across event retries."""
        return hashlib.sha256(f"feishu-session:{event_id}".encode()).hexdigest()[:8]

    @staticmethod
    def _task_id_for_event(event_id: str) -> str:
        """Derive a stable task identifier so callback retries cannot resubmit."""
        return hashlib.sha256(f"feishu:{event_id}".encode()).hexdigest()[:32]

    def _completed_conversation(
        self, conversation_id: str
    ) -> list[FeishuConversationTurn]:
        """Synchronize a completed latest task before compiling a follow-up."""
        record = self._store.get_latest_task(conversation_id)
        if record is not None and not record.context_recorded:
            task = self._task_coordinator.get(record.task_id)
            if isinstance(task, AnalysisTask) and task.status == AnalysisTaskStatus.SUCCEEDED:
                self._record_completed_context(record, task)
        return self._store.get(conversation_id)

    def _completed_agent_conversation(
        self, conversation_id: str
    ) -> list[FeishuConversationTurn]:
        """Synchronize a successful latest agent answer before one follow-up."""
        record = self._store.get_latest_task(conversation_id)
        if record is not None and not record.context_recorded:
            task = self._agent_coordinator.get(record.task_id)
            if task is not None and task.status == AnalysisTaskStatus.SUCCEEDED:
                self._record_completed_agent_context(record, task)
        return self._store.get(conversation_id)

    def _record_completed_agent_context(
        self,
        record: FeishuTaskRecord,
        task: FeishuAgentTask,
    ) -> None:
        """Persist one complete agent exchange exactly once."""
        if record.context_recorded or not task.answer:
            return
        turns = self._store.get(record.conversation_id)
        turns.append(
            FeishuConversationTurn(prompt=record.prompt, answer=task.answer)
        )
        self._store.put(record.conversation_id, turns)
        record.context_recorded = True
        self._store.put_task(record)

    def _record_completed_context(
        self,
        record: FeishuTaskRecord,
        task: AnalysisTask,
    ) -> None:
        """Persist one successful interpretation exactly once for follow-ups."""
        if record.context_recorded or task.response is None or task.response.plan is None:
            return
        turns = self._store.get(record.conversation_id)
        turns.append(
            FeishuConversationTurn(
                prompt=record.prompt,
                interpretation=task.response.plan.interpretation,
            )
        )
        self._store.put(record.conversation_id, turns)
        record.context_recorded = True
        self._store.put_task(record)


def format_analysis_task(task: AnalysisTask) -> str:
    """Render bounded task progress or terminal analysis evidence for Feishu."""
    heading = f"任务 {task.task_id}\n状态：{_task_status_label(task.status)}"
    if task.status == AnalysisTaskStatus.QUEUED:
        return heading
    if task.status == AnalysisTaskStatus.RUNNING:
        if task.total_items > 0:
            return f"{heading}\n进度：{task.completed_items}/{task.total_items}"
        return f"{heading}\n进度：正在规划和获取数据"
    if task.status == AnalysisTaskStatus.FAILED:
        message = task.error.message if task.error else "工作进程未返回错误详情。"
        return f"{heading}\n失败原因：{message}\n回复“重试”可重新提交。"
    if task.response is None:
        return f"{heading}\n任务已结束，但没有可展示的分析结果。"
    return f"{heading}\n{_format_analysis_response(task.response)}"


def format_feishu_agent_task(task: FeishuAgentTask) -> str:
    """Render one agent lifecycle without exposing internal tool arguments."""
    heading = f"任务 {task.task_id}\n状态：{_task_status_label(task.status)}"
    if task.status in {AnalysisTaskStatus.QUEUED, AnalysisTaskStatus.RUNNING}:
        return f"{heading}\n进度：{task.progress_message}"
    if task.status == AnalysisTaskStatus.FAILED:
        message = task.error.message if task.error else "工作进程未返回错误详情。"
        return f"{heading}\n失败原因：{message}\n回复“重试”可重新提交。"
    answer = task.answer or "任务已完成，但没有可展示的回答。"
    if task.artifact_name:
        answer += f"\n附件：{task.artifact_name}"
    return f"{heading}\n{answer}"


def _format_analysis_response(response: AnalysisResponse) -> str:
    """Render the answer-contract result without exposing internal diagnostics."""
    if response.error is not None:
        return f"分析未形成结果：{response.error.message}"
    result_query_id = (
        response.plan.answer_contract.result_query_id
        if response.plan is not None and response.plan.answer_contract is not None
        else None
    )
    result = next(
        (
            candidate
            for candidate in response.results
            if candidate.query_id == result_query_id
        ),
        response.results[-1] if response.results else None,
    )
    interpretation = response.plan.interpretation if response.plan is not None else ""
    lines = [interpretation] if interpretation else []
    if result is None:
        lines.append("没有可展示的数据行。")
        return "\n".join(lines)
    if result.summary:
        lines.extend(f"{key}：{value}" for key, value in result.summary.items())
        return "\n".join(lines)
    if not result.rows:
        lines.append("查询成功，但结果为空。")
        return "\n".join(lines)
    columns = list(result.rows[0])
    lines.append(" | ".join(columns))
    for row in result.rows[:MAX_FEISHU_RESULT_ROWS]:
        lines.append(" | ".join(_format_cell(row.get(column)) for column in columns))
    if len(result.rows) > MAX_FEISHU_RESULT_ROWS:
        lines.append(f"仅展示前 {MAX_FEISHU_RESULT_ROWS} 行，共 {len(result.rows)} 行。")
    return "\n".join(lines)


def build_feishu_quick_menu_card(
    include_strategy: bool = False,
    model_status: str = "",
    model_options: Optional[List[tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """Return the interactive research form and common command shortcuts.

    The model selector is one dropdown form fed by the chat-model registry,
    so registering an additional model extends the card without layout
    changes. The status line and the check-marked option both reflect the
    preference at render time.
    """
    elements: list[Dict[str, Any]] = []
    if model_status:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**当前研究模型**：{model_status}",
                },
            }
        )
    elements.extend(
        [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "输入研究问题，或者选择一个快捷操作。",
                },
            },
        {
            "tag": "form",
            "name": "research_form",
            "elements": [
                {
                    "tag": "input",
                    "name": "prompt",
                    "required": True,
                    "max_length": 1_000,
                    "placeholder": {
                        "tag": "plain_text",
                        "content": "例如：查询最近5个交易日涨幅最大的10只A股",
                    },
                },
                {
                    "tag": "button",
                    "name": "submit_research",
                    "type": "primary",
                    "action_type": "form_submit",
                    "text": {"tag": "plain_text", "content": "开始研究"},
                    "value": {"action": "submit_research"},
                },
            ],
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "新建会话"},
                    "value": {"action": "new_session"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "会话列表"},
                    "value": {"action": "list_sessions"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看进度"},
                    "value": {"action": "task_status"},
                },
            ],
        },
        ]
    )
    if include_strategy:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "新建策略"},
                        "value": {"action": "strategy_create_draft"},
                    },
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "策略列表"},
                        "value": {"action": "strategy_list"},
                    },
                ],
            }
        )
    if model_options:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        # A standalone select_static inside an action container
                        # fires immediately on selection (callback carries the
                        # chosen option) and renders on older Feishu clients,
                        # unlike form containers with input components.
                        "tag": "select_static",
                        "name": "model",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "选择研究模型…",
                        },
                        "value": {"action": "switch_model"},
                        "options": [
                            {
                                "text": {
                                    "tag": "plain_text",
                                    "content": label,
                                },
                                "value": provider,
                            }
                            for provider, label in model_options
                        ],
                    }
                ],
            }
        )
    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "也可以继续直接 @A股研究助手 并输入问题。",
                }
            ],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "A股研究助手"},
        },
        "elements": elements,
    }


def _format_cell(value: object) -> str:
    """Return one compact table cell for a plain-text Feishu reply."""
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _task_status_label(status: AnalysisTaskStatus) -> str:
    """Translate the persisted lifecycle state for conversational display."""
    return {
        AnalysisTaskStatus.QUEUED: "排队中",
        AnalysisTaskStatus.RUNNING: "运行中",
        AnalysisTaskStatus.SUCCEEDED: "已完成",
        AnalysisTaskStatus.FAILED: "失败",
    }[status]

"""Secure Feishu message ingress for multi-turn A-share research."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import re
from threading import Lock
from typing import Any, Dict, Optional, Protocol
from uuid import uuid4

import requests
from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage

from pydantic import BaseModel, ConfigDict, Field

from china_a_share.observability import log_event


FEISHU_API_BASE_URL = "https://open.feishu.cn/open-apis"
FEISHU_TOKEN_TIMEOUT_SECONDS = 10
FEISHU_MESSAGE_TIMEOUT_SECONDS = 15
MAX_FEISHU_MESSAGE_LENGTH = 3_000
MAX_FEISHU_CONTEXT_TURNS = 3
MENTION_PATTERN = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)
logger = logging.getLogger(__name__)


class FeishuConfigurationError(RuntimeError):
    """Report missing or invalid Feishu application configuration."""


class FeishuEventError(ValueError):
    """Report a rejected Feishu callback without exposing credentials."""


class FeishuConversationTurn(BaseModel):
    """One completed research turn retained for follow-up interpretation."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=1_000)
    interpretation: str = Field(min_length=1, max_length=1_000)


class ResearchConversationService(Protocol):
    """Answer one research question through registered deterministic tools."""

    def answer(
        self,
        request_id: str,
        prompt: str,
        conversation: list[FeishuConversationTurn],
    ) -> tuple[str, str]:
        """Return the user reply and normalized interpretation."""


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


class MemoryConversationStore:
    """Store isolated Feishu conversations and event claims in memory."""

    def __init__(self) -> None:
        self._conversations: Dict[str, list[FeishuConversationTurn]] = {}
        self._event_ids: set[str] = set()
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
        """Create one event marker atomically so Feishu retries remain idempotent."""
        blob = self._bucket.blob(self._event_object(event_id))
        try:
            blob.upload_from_string(
                "{}", content_type="application/json", if_generation_match=0
            )
        except PreconditionFailed:
            return False
        return True

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


class FeishuMessageSender(Protocol):
    """Send one reply to the message that initiated an analysis turn."""

    def reply(self, message_id: str, text: str) -> None:
        """Reply with bounded plain text to one Feishu message."""


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

    def reply(self, message_id: str, text: str) -> None:
        """Reply to one source message through the tenant application identity."""
        token_response = self._session.post(
            f"{FEISHU_API_BASE_URL}/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
            timeout=FEISHU_TOKEN_TIMEOUT_SECONDS,
        )
        self._raise_for_feishu_error(token_response, "tenant token")
        token = token_response.json().get("tenant_access_token", "")
        if not token:
            raise RuntimeError("Feishu tenant token response omitted the access token.")
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

    @staticmethod
    def _raise_for_feishu_error(response: requests.Response, operation: str) -> None:
        """Fail fast on HTTP or Feishu application errors."""
        if response.status_code >= 400:
            raise RuntimeError(
                f"Feishu {operation} failed with HTTP {response.status_code}."
            )
        payload = response.json()
        if payload.get("code", 0) != 0:
            raise RuntimeError(
                f"Feishu {operation} failed with code {payload.get('code')}."
            )


@dataclass(frozen=True)
class FeishuMessageEvent:
    """Validated fields required to execute one Feishu research turn."""

    event_id: str
    message_id: str
    conversation_id: str
    prompt: str


class FeishuResearchBot:
    """Validate callbacks and connect Feishu conversations to analysis."""

    def __init__(
        self,
        research_service: ResearchConversationService,
        sender: FeishuMessageSender,
        store: ConversationStore,
        *,
        verification_token: str,
        encrypt_key: str,
        allowed_open_ids: Optional[set[str]] = None,
    ) -> None:
        if not verification_token or not encrypt_key:
            raise FeishuConfigurationError(
                "FEISHU_VERIFICATION_TOKEN and FEISHU_ENCRYPT_KEY are required."
            )
        self._research_service = research_service
        self._sender = sender
        self._store = store
        self._verification_token = verification_token
        self._encrypt_key = encrypt_key
        self._allowed_open_ids = allowed_open_ids or set()

    def verify_signature(
        self,
        body: bytes,
        timestamp: str,
        nonce: str,
        signature: str,
    ) -> None:
        """Reject callbacks whose Feishu request signature does not match."""
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

    def parse_event(self, payload: Dict[str, Any]) -> Optional[FeishuMessageEvent]:
        """Validate one unencrypted v2 Feishu text-message callback."""
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
        prompt = MENTION_PATTERN.sub("", text).strip()
        if not prompt:
            return None
        chat_id = str(message.get("chat_id", "")).strip()
        message_id = str(message.get("message_id", "")).strip()
        event_id = str(header.get("event_id", "")).strip()
        if not chat_id or not message_id or not event_id or not sender_id:
            raise FeishuEventError("Feishu callback omitted required identifiers.")
        thread_id = str(message.get("thread_id") or message.get("root_id") or "root")
        conversation_id = ":".join(
            [str(header.get("tenant_key", "")), chat_id, thread_id, sender_id]
        )
        return FeishuMessageEvent(event_id, message_id, conversation_id, prompt)

    def verify_challenge(self, payload: Dict[str, Any]) -> str:
        """Validate and return one Feishu endpoint-verification challenge."""
        if payload.get("token") != self._verification_token:
            raise FeishuEventError("Feishu callback verification token is invalid.")
        challenge = str(payload.get("challenge", ""))
        if not challenge:
            raise FeishuEventError("Feishu callback challenge is missing.")
        return challenge

    def process(self, event: FeishuMessageEvent) -> None:
        """Execute one claimed research turn and persist only completed context."""
        if not self._store.claim_event(event.event_id):
            return
        prior_turns = self._store.get(event.conversation_id)
        request_id = f"feishu-{uuid4().hex}"
        try:
            reply, interpretation = self._research_service.answer(
                request_id,
                event.prompt,
                [turn.model_copy(deep=True) for turn in prior_turns],
            )
            self._sender.reply(event.message_id, reply)
            prior_turns.append(
                FeishuConversationTurn(
                    prompt=event.prompt,
                    interpretation=interpretation,
                )
            )
            self._store.put(event.conversation_id, prior_turns)
        except Exception:
            log_event(
                logger,
                logging.ERROR,
                "feishu_research_turn_failed",
                request_id=request_id,
                source="system",
                exc_info=True,
            )
            self._sender.reply(
                event.message_id,
                f"研究任务执行失败。请使用请求编号 {request_id} 联系管理员。",
            )

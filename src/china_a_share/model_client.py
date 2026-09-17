"""Provider-neutral chat-model transport for OpenAI-compatible APIs."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Protocol, Sequence

import requests


DEFAULT_MODEL_TIMEOUT_SECONDS = 180


class ChatModel(Protocol):
    """Return assistant messages through a replaceable model transport."""

    @property
    def model(self) -> str:
        """Return the configured model identifier."""

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Return one OpenAI-compatible assistant message."""


class OpenAICompatibleChatModel:
    """Call one configured OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        api_secret: str = "",
        session: Optional[requests.Session] = None,
        timeout_seconds: int = DEFAULT_MODEL_TIMEOUT_SECONDS,
    ) -> None:
        """Store transport configuration without embedding provider behavior."""
        normalized_base_url = base_url.strip().rstrip("/")
        if not normalized_base_url:
            raise ValueError("Model API base URL is required.")
        if not model.strip():
            raise ValueError("Model identifier is required.")
        if not api_key.strip():
            raise ValueError("Model API key is required.")
        if timeout_seconds <= 0:
            raise ValueError("Model timeout must be positive.")
        self._endpoint = f"{normalized_base_url}/chat/completions"
        self._model = model.strip()
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()
        self._session = session or requests.Session()
        self._timeout_seconds = timeout_seconds

    @property
    def model(self) -> str:
        """Return the configured model identifier."""
        return self._model

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Return one validated assistant message from the configured API."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._api_secret:
            # Compatible gateways may require a second credential in addition to
            # the bearer key. Standard bearer-only APIs omit this header.
            headers["X-API-Secret"] = self._api_secret
        body: Dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "temperature": 0,
            "max_tokens": 8_000,
        }
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = "auto"
        response = self._session.post(
            self._endpoint,
            headers=headers,
            json=body,
            timeout=self._timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Model API returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        try:
            payload = response.json()
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("Model API returned an invalid response contract.") from exc
        if not isinstance(message, dict):
            raise RuntimeError("Model API returned a non-object assistant message.")
        return dict(message)

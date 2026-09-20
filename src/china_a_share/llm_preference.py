"""Runtime-selectable planning-provider preference shared across processes.

The webhook service and the worker job are separate processes, so a model
switch issued in a Feishu conversation must persist outside process memory.
The preference lives as one small JSON object in the application bucket and
overrides the LLM_PROVIDER environment default for chat-driven runs.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Protocol

from google.cloud import storage

from china_a_share.config import Settings
from china_a_share.observability import log_event


logger = logging.getLogger(__name__)

ALLOWED_PROVIDERS = ("deepseek", "glm")
_PROVIDER_LABELS = {
    "deepseek": "DeepSeek（API 按量计费）",
    "glm": "GLM（智谱编码套餐额度）",
}
_SWITCH_USAGE = "用法：切换模型 glm 或 切换模型 deepseek；查看当前用「当前模型」。"


class LlmPreferenceStore(Protocol):
    """Persist the operator-selected planning provider across processes."""

    def get(self) -> Optional[str]:
        """Return the persisted provider name, or None when unset or invalid."""
        ...

    def set(self, provider: str) -> None:
        """Persist one validated provider name."""
        ...


class MemoryLlmPreferenceStore:
    """In-memory preference store for tests and single-process deployments."""

    def __init__(self) -> None:
        self._provider: Optional[str] = None

    def get(self) -> Optional[str]:
        return self._provider

    def set(self, provider: str) -> None:
        _validate_provider(provider)
        self._provider = provider


class CloudStorageLlmPreferenceStore:
    """Persist the provider preference as one JSON object in the app bucket."""

    OBJECT_NAME = "llm-provider-preference.json"

    def __init__(
        self,
        bucket_name: str,
        storage_client: Optional[storage.Client] = None,
    ) -> None:
        if not bucket_name:
            raise ValueError(
                "A bucket name is required for the LLM preference store."
            )
        self._bucket = (storage_client or storage.Client()).bucket(bucket_name)

    def get(self) -> Optional[str]:
        """Read the persisted provider, ignoring corrupted or unknown values."""
        blob = self._bucket.blob(self.OBJECT_NAME)
        try:
            if not blob.exists():
                return None
            payload = json.loads(blob.download_as_text())
        except (ValueError, OSError, json.JSONDecodeError):
            return None
        provider = str((payload or {}).get("provider") or "")
        return provider if provider in ALLOWED_PROVIDERS else None

    def set(self, provider: str) -> None:
        """Replace the persisted provider after validation."""
        _validate_provider(provider)
        blob = self._bucket.blob(self.OBJECT_NAME)
        blob.upload_from_string(
            json.dumps({"provider": provider}, ensure_ascii=False),
            content_type="application/json",
        )


def _validate_provider(provider: str) -> None:
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError(
            f"Unknown planning provider '{provider}'. "
            f"Allowed providers: {', '.join(ALLOWED_PROVIDERS)}."
        )


def provider_label(provider: str) -> str:
    """Return the user-facing label for one provider name."""
    return _PROVIDER_LABELS.get(provider, provider)


def resolve_active_provider(
    settings: Settings,
    preference: Optional[str],
) -> str:
    """Return the provider a chat-driven run should use right now.

    A persisted preference wins only when the required API key for that
    provider is present in this environment; otherwise the LLM_PROVIDER
    environment default applies.
    """
    if preference == "glm" and settings.zai_api_key:
        return "glm"
    if preference == "deepseek" and settings.deepseek_api_key:
        return "deepseek"
    return settings.llm_provider


class LlmPreferenceController:
    """Answer Feishu model-switch commands against keys and persistence."""

    def __init__(self, settings: Settings, store: LlmPreferenceStore) -> None:
        self._settings = settings
        self._store = store

    def current(self) -> str:
        """Return the provider that would be used for the next chat run."""
        return resolve_active_provider(self._settings, self._store.get())

    def status_reply(self) -> str:
        """Compose the「当前模型」answer."""
        return (
            f"当前模型：{provider_label(self.current())}\n"
            "可切换：glm（智谱编码套餐额度）/ deepseek（API 按量计费）\n"
            f"{_SWITCH_USAGE}"
        )

    def switch(self, target: str) -> str:
        """Validate, persist, and confirm one provider switch."""
        target = target.strip().lower()
        if target not in ALLOWED_PROVIDERS:
            return (
                f"未知模型「{target}」。{_SWITCH_USAGE}\n当前模型："
                f"{provider_label(self.current())}"
            )
        if target == "glm" and not self._settings.zai_api_key:
            return (
                "无法切换到 GLM：当前环境未配置 ZAI_API_KEY。"
                "请先在部署环境中配置该密钥。"
            )
        if target == "deepseek" and not self._settings.deepseek_api_key:
            return (
                "无法切换到 DeepSeek：当前环境未配置 DEEPSEEK_API_KEY。"
                "请先在部署环境中配置该密钥。"
            )
        self._store.set(target)
        return (
            f"已切换到 {provider_label(target)}，下一次研究任务生效。\n"
            "GLM 使用智谱编码套餐额度（与编码工具共享 5 小时/每周上限）；"
            "DeepSeek 按量计费。"
        )


def read_llm_preference(settings: Settings) -> Optional[str]:
    """Read the persisted preference defensively for one worker run."""
    if not settings.tushare_cache_bucket:
        return None
    try:
        return CloudStorageLlmPreferenceStore(
            settings.tushare_cache_bucket
        ).get()
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "llm_preference_read_failed",
            reason=str(exc),
        )
        return None

"""Runtime-selectable chat-research model shared across processes.

The Feishu webhook and the worker job are separate processes, so a model
switch issued in a conversation must persist outside process memory. The
preference lives as one small JSON object in the application bucket and
selects which research runtime the worker assembles. Every user-facing
surface (quick-menu dropdown, text-command validation, runtime resolution)
is driven by one registry, so adding a model means adding one registry
entry plus its runtime mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Optional, Protocol, Sequence

from google.cloud import storage

from china_a_share.config import Settings
from china_a_share.observability import log_event


logger = logging.getLogger(__name__)

GLM_AGENT_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"
GLM_AGENT_DEFAULT_MODEL = "glm-5.3"
_SWITCH_USAGE = "用法：切换模型 <模型名>；查看当前用「当前模型」。"


@dataclass(frozen=True)
class ChatModelOption:
    """One selectable chat-research model published to every surface."""

    provider: str
    label: str
    note: str
    required_settings_field: str = ""

    def available_for(self, settings: Settings) -> bool:
        """Return whether this deployment can actually use the model."""
        if not self.required_settings_field:
            return True
        return bool(getattr(settings, self.required_settings_field, ""))


# The single source of truth for selectable chat-research models. Adding a
# third model means appending one entry here and wiring its runtime in
# bootstrap.create_feishu_agent_runtime; the dropdown, command validation,
# and status replies pick it up automatically.
CHAT_MODEL_REGISTRY: tuple[ChatModelOption, ...] = (
    ChatModelOption(
        provider="deepseek",
        label="DeepSeek",
        note="默认 · Codex 研究代理 · API 按量计费",
    ),
    ChatModelOption(
        provider="glm",
        label="GLM（智谱）",
        note="编码套餐额度 · 轻量研究循环",
        required_settings_field="zai_api_key",
    ),
)


def chat_model_options(
    settings: Optional[Settings] = None,
) -> Sequence[ChatModelOption]:
    """Return the registered models, optionally with availability flags."""
    return CHAT_MODEL_REGISTRY


def chat_model_option(provider: str) -> Optional[ChatModelOption]:
    """Return one registered model option by provider name."""
    return next(
        (option for option in CHAT_MODEL_REGISTRY if option.provider == provider),
        None,
    )


def available_providers(settings: Settings) -> tuple[str, ...]:
    """Return provider names this deployment can switch between."""
    return tuple(
        option.provider
        for option in CHAT_MODEL_REGISTRY
        if option.available_for(settings)
    )


ALLOWED_PROVIDERS = tuple(option.provider for option in CHAT_MODEL_REGISTRY)


def provider_label(provider: str) -> str:
    """Return the user-facing label for one provider name."""
    option = chat_model_option(provider)
    if option is None:
        return provider
    return f"{option.label}（{option.note}）"


def _available_hint(settings: Settings) -> str:
    names = " / ".join(available_providers(settings)) or "（无）"
    return f"当前可用模型：{names}"


class LlmPreferenceStore(Protocol):
    """Persist the operator-selected chat-research model across processes."""

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
    """Persist the model preference as one JSON object in the app bucket."""

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
    if chat_model_option(provider) is None:
        raise ValueError(
            f"Unknown chat-research provider '{provider}'. "
            f"Allowed providers: {', '.join(ALLOWED_PROVIDERS)}."
        )


def resolve_active_provider(
    settings: Settings,
    preference: Optional[str],
) -> str:
    """Return the research runtime provider for the next chat run.

    A persisted preference applies only when the model's required key is
    present in this environment; otherwise the deployed DeepSeek default
    stays active.
    """
    option = chat_model_option(preference) if preference else None
    if option is not None and option.available_for(settings):
        return option.provider
    return "deepseek"


def glm_agent_base_url(settings: Settings) -> str:
    """Return the configured GLM chat endpoint for the research runtime."""
    return settings.glm_agent_base_url.strip() or GLM_AGENT_DEFAULT_BASE_URL


def glm_agent_model(settings: Settings) -> str:
    """Return the configured GLM model identifier for the research runtime."""
    return settings.glm_agent_model.strip() or GLM_AGENT_DEFAULT_MODEL


class LlmPreferenceController:
    """Answer Feishu model-switch commands against keys and persistence."""

    def __init__(self, settings: Settings, store: LlmPreferenceStore) -> None:
        self._settings = settings
        self._store = store

    def current(self) -> str:
        """Return the provider that would be used for the next chat run."""
        return resolve_active_provider(self._settings, self._store.get())

    def status_line(self) -> str:
        """Return the one-line status rendered on the quick-menu card."""
        return provider_label(self.current())

    def dropdown_options(self) -> list[tuple[str, str]]:
        """Return (provider, label) pairs for the quick-menu selector."""
        current = self.current()
        return [
            (
                option.provider,
                ("✅ " if option.provider == current else "") + option.label,
            )
            for option in chat_model_options(self._settings)
        ]

    def status_reply(self) -> str:
        """Compose the「当前模型」answer."""
        notes = "\n".join(
            f"- {option.label}：{option.note}"
            + ("（当前）" if option.provider == self.current() else "")
            for option in chat_model_options(self._settings)
        )
        return (
            f"当前研究模型：{provider_label(self.current())}\n"
            f"{notes}\n"
            f"{_SWITCH_USAGE}\n{_available_hint(self._settings)}"
        )

    def switch(self, target: str) -> str:
        """Validate, persist, and confirm one provider switch."""
        target = target.strip().lower()
        option = chat_model_option(target)
        if option is None:
            return (
                f"未知模型「{target}」。{_SWITCH_USAGE}\n"
                f"{_available_hint(self._settings)}\n当前研究模型："
                f"{provider_label(self.current())}"
            )
        if not option.available_for(self._settings):
            missing = option.required_settings_field or "所需密钥"
            return (
                f"无法切换到 {option.label}：当前环境未配置 {missing}。"
                "请先在部署环境中配置该密钥。"
            )
        self._store.set(target)
        return (
            f"已切换研究模型到 {provider_label(target)}，下一次研究任务生效。"
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

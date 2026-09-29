from __future__ import annotations

from typing import Any

from backend.config import Settings
from backend.state import ConversationStore
from clients.llm_providers import LlmProviderConfig, nvidia_model_status

GLOBAL_USER_ID = "__global__"
ENGINE_FLAG = "ai_engine"


def selected_engine(settings: Settings, store: ConversationStore) -> str:
    value = (store.get_user_flag(GLOBAL_USER_ID, ENGINE_FLAG) or "").strip().lower()
    return value if value in {"configured", "nvidia"} else "configured"


def engine_state(settings: Settings, store: ConversationStore) -> dict[str, Any]:
    selected = selected_engine(settings, store)
    nvidia_config = LlmProviderConfig(
        provider="nvidia",
        model=settings.effective_llm_model,
        timeout_seconds=float(max(30, int(settings.effective_llm_request_timeout_seconds))),
        nvidia_api_key=settings.nvidia_api_key,
        nvidia_catalog_path=settings.nvidia_catalog_path,
        nvidia_state_get=lambda key: store.get_user_flag(GLOBAL_USER_ID, key),
    )
    return {
        "selected": selected,
        "configured": {"available": True, "provider": settings.llm_provider, "model": settings.effective_llm_model},
        "nvidia": {
            "available": bool((settings.nvidia_api_key or "").strip()),
            "models": nvidia_model_status(nvidia_config),
        },
    }


def set_engine(settings: Settings, store: ConversationStore, value: str) -> dict[str, Any]:
    value = value.strip().lower()
    if value not in {"configured", "nvidia"}:
        raise ValueError("engine must be 'configured' or 'nvidia'")
    if value == "nvidia" and not (settings.nvidia_api_key or "").strip():
        raise ValueError("NVIDIA is not configured")
    store.set_user_flag(GLOBAL_USER_ID, ENGINE_FLAG, value)
    return engine_state(settings, store)

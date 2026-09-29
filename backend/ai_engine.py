from __future__ import annotations

from typing import Any

import httpx

from backend.config import Settings
from backend.state import ConversationStore
from clients.llm_providers import LlmProviderConfig, nvidia_model_status

GLOBAL_USER_ID = "__global__"
ENGINE_FLAG = "ai_engine"
LITELLM_MODEL_FLAG = "litellm_model"


def selected_engine(settings: Settings, store: ConversationStore) -> str:
    value = (store.get_user_flag(GLOBAL_USER_ID, ENGINE_FLAG) or "").strip().lower()
    return value if value in {"configured", "nvidia", "litellm"} else "configured"


def selected_litellm_model(settings: Settings, store: ConversationStore) -> str:
    return (
        store.get_user_flag(GLOBAL_USER_ID, LITELLM_MODEL_FLAG)
        or settings.litellm_model
        or ""
    ).strip()


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
        "litellm": {
            "available": bool(settings.effective_litellm_base_url),
            "selected_model": selected_litellm_model(settings, store),
        },
    }


def set_engine(
    settings: Settings, store: ConversationStore, value: str, model: str | None = None
) -> dict[str, Any]:
    value = value.strip().lower()
    if value not in {"configured", "nvidia", "litellm"}:
        raise ValueError("engine must be 'configured', 'nvidia', or 'litellm'")
    if value == "nvidia" and not (settings.nvidia_api_key or "").strip():
        raise ValueError("NVIDIA is not configured")
    if value == "litellm":
        if not settings.effective_litellm_base_url:
            raise ValueError("LiteLLM is not configured")
        selected_model = (model or selected_litellm_model(settings, store)).strip()
        if not selected_model:
            raise ValueError("Select a LiteLLM model first")
        store.set_user_flag(GLOBAL_USER_ID, LITELLM_MODEL_FLAG, selected_model)
    store.set_user_flag(GLOBAL_USER_ID, ENGINE_FLAG, value)
    return engine_state(settings, store)


async def litellm_models(settings: Settings) -> tuple[list[str], str | None]:
    base_url = settings.effective_litellm_base_url
    if not base_url:
        return [], "not_configured"
    headers = {}
    api_key = settings.effective_litellm_api_key
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{base_url}/models", headers=headers)
            response.raise_for_status()
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        models = sorted({str(item["id"]).strip() for item in data if isinstance(item, dict) and str(item.get("id") or "").strip()})
        return models, None
    except httpx.HTTPStatusError as exc:
        return [], f"http_{exc.response.status_code}"
    except (httpx.HTTPError, ValueError, TypeError):
        return [], "unavailable"

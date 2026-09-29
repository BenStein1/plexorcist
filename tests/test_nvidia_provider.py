from __future__ import annotations

import json

import httpx
import pytest

from clients.llm_providers import (
    LlmProviderConfig,
    NvidiaProviderClient,
    ToolSchema,
    build_llm_client,
    nvidia_model_status,
)
from clients.nvidia_catalog import PREFERRED_MODELS, nvidia_models


def _response(payload, status=200, headers=None):
    return httpx.Response(status, json=payload, headers=headers or {}, request=httpx.Request("POST", "https://example.invalid"))


def test_nvidia_config_and_preferred_order():
    client = build_llm_client(LlmProviderConfig(provider="nvidia", model="gpt-5-mini", timeout_seconds=1, nvidia_api_key="k"))
    assert isinstance(client, NvidiaProviderClient)
    assert client._models()[: len(PREFERRED_MODELS)] == PREFERRED_MODELS


def test_missing_catalog_keeps_preferred_models(tmp_path):
    assert nvidia_models(str(tmp_path / "missing.json")) == PREFERRED_MODELS


def test_catalog_filters_and_orders_fallbacks(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": [
        {"id": "small", "specs": {"parameter_count": 31_000_000_000, "description": "general purpose reasoning", "input_modalities_list": ["text"], "output_modalities_list": ["text"]}, "api_registry": {"listed": True}, "availability": {"free_endpoint": "available"}, "integration": {"openai_compatible": True}},
        {"id": "large", "specs": {"parameter_count": 90_000_000_000, "description": "general purpose reasoning", "input_modalities_list": ["text"], "output_modalities_list": ["text"]}, "api_registry": {"listed": True}, "availability": {"free_endpoint": "available"}, "integration": {"openai_compatible": True}},
        {"id": "embed", "specs": {"parameter_count": 100_000_000_000, "description": "embedding model"}, "api_registry": {"listed": True}, "availability": {"free_endpoint": "available"}, "integration": {"openai_compatible": True}},
    ]}), encoding="utf-8")
    models = nvidia_models(str(path))
    assert models[-2:] == ("large", "small")
    assert "embed" not in models


@pytest.mark.asyncio
async def test_tool_round_trip_usage_and_no_read_deadline(monkeypatch):
    captured = {}
    events = []

    async def post(self, url, json=None):
        captured["json"] = json
        captured["timeout"] = self.timeout
        return _response({"model": "nvidia/nemotron-3-super-120b-a12b", "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [{"type": "function", "id": "call-1", "function": {"name": "search", "arguments": '{"q":"x"}'}}]}}], "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    client = NvidiaProviderClient("key", PREFERRED_MODELS[0], usage_recorder=events.append)
    result = await client.generate_response(
        instructions="system", conversation=[], tools=[ToolSchema("search", "find", {"type": "object"})], usage_context={"user_id": "u"}
    )
    assert result.tool_calls[0].call_id == "call-1"
    assert captured["json"]["tools"][0]["function"]["name"] == "search"
    assert captured["timeout"].read is None
    assert events[0]["model"] == PREFERRED_MODELS[0]
    assert events[0]["user_id"] == "u"


@pytest.mark.asyncio
async def test_auth_is_fatal_and_429_tries_one_alternate(monkeypatch):
    state = {}
    calls = []

    async def post(self, url, json=None):
        model = json["model"]
        calls.append(model)
        if len(calls) == 1:
            return _response({}, status=429, headers={"Retry-After": "120"})
        return _response({"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    client = NvidiaProviderClient("key", "ignored", state_get=state.get, state_set=state.__setitem__)
    result = await client.generate_response(instructions="", conversation=[], tools=[])
    assert result.text == "ok"
    assert calls == list(PREFERRED_MODELS[:2])
    assert json.loads(state["nvidia.cooldown." + PREFERRED_MODELS[0]])["status"] == 429
    assert json.loads(state["nvidia.endpoint_cooldown"])["status"] == 429

    async def auth_post(self, url, json=None):
        return _response({}, status=401)
    monkeypatch.setattr(httpx.AsyncClient, "post", auth_post)
    with pytest.raises(Exception) as caught:
        await NvidiaProviderClient("key", PREFERRED_MODELS[0]).generate_response(instructions="", conversation=[], tools=[])
    assert getattr(caught.value, "status_code", None) == 401


def test_model_status_is_sanitized():
    state = {"nvidia.cooldown." + PREFERRED_MODELS[0]: json.dumps({"until": 9_999_999_999, "status": 500, "reason": "secret body"})}
    statuses = nvidia_model_status(LlmProviderConfig(provider="nvidia", model="x", timeout_seconds=1, nvidia_state_get=state.get))
    assert statuses[0]["status"] == "cooldown"
    assert "reason" not in statuses[0]

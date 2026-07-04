"""Tests for the provider-agnostic LLM layer (clients/llm_providers.py).

No network calls: SDK clients are constructed normally (construction alone
never hits the network) and their `.create` coroutine is monkeypatched per
test. Ollama has no SDK, so its httpx.AsyncClient.post is monkeypatched
instead.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import httpx
import pytest

from backend.state import ConversationStore
from clients.llm_providers import (
    AnthropicProviderClient,
    AssistantTurn,
    ChatTurn,
    LlmConfigurationError,
    LlmProviderConfig,
    OllamaProviderClient,
    OpenAIProviderClient,
    ToolCall,
    ToolResult,
    ToolResultsTurn,
    ToolSchema,
    build_llm_client,
    supported_llm_providers,
)


class _Obj:
    """Minimal stand-in for SDK response/content objects: plain attribute
    access plus a pydantic-ish model_dump()."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def model_dump(self, exclude_none: bool = False) -> dict:
        data = dict(self.__dict__)
        if exclude_none:
            data = {k: v for k, v in data.items() if v is not None}
        return data


# --- 1. ToolCall.parse_arguments ---------------------------------------------


def test_parse_arguments_valid_json():
    call = ToolCall(call_id="c1", name="search", arguments_json='{"query": "matrix"}')
    args, err = call.parse_arguments()
    assert err is None
    assert args == {"query": "matrix"}


def test_parse_arguments_empty_string():
    call = ToolCall(call_id="c1", name="search", arguments_json="")
    args, err = call.parse_arguments()
    assert err is None
    assert args == {}


def test_parse_arguments_malformed_json():
    call = ToolCall(call_id="c1", name="search", arguments_json="{not json")
    args, err = call.parse_arguments()
    assert args is None
    assert isinstance(err, str)
    assert "search" in err


def test_parse_arguments_non_dict_json():
    call = ToolCall(call_id="c1", name="search", arguments_json="[1, 2, 3]")
    args, err = call.parse_arguments()
    assert args is None
    assert isinstance(err, str)
    assert "object" in err.lower()


# --- 2. build_llm_client ------------------------------------------------------


def test_build_llm_client_unknown_provider_raises():
    with pytest.raises(LlmConfigurationError) as excinfo:
        build_llm_client(LlmProviderConfig(provider="bogus", model="x", timeout_seconds=30))
    message = str(excinfo.value)
    for provider in supported_llm_providers():
        assert provider in message


def test_build_llm_client_missing_openai_key_returns_none():
    client = build_llm_client(
        LlmProviderConfig(provider="openai", model="gpt-4o-mini", timeout_seconds=30, openai_api_key=None)
    )
    assert client is None


def test_build_llm_client_missing_anthropic_key_returns_none():
    client = build_llm_client(
        LlmProviderConfig(provider="anthropic", model="claude-3-5", timeout_seconds=30, anthropic_api_key=None)
    )
    assert client is None


def test_build_llm_client_blank_model_raises():
    with pytest.raises(LlmConfigurationError):
        build_llm_client(LlmProviderConfig(provider="openai", model="   ", timeout_seconds=30, openai_api_key="k"))


def test_build_llm_client_ollama_needs_no_key():
    client = build_llm_client(LlmProviderConfig(provider="ollama", model="llama3", timeout_seconds=30))
    assert isinstance(client, OllamaProviderClient)


# --- 3. Anthropic adapter message building -----------------------------------


def test_anthropic_build_messages_merges_tool_results_and_extracts_system():
    client = AnthropicProviderClient(api_key="test-key", model="claude-3-5", max_output_tokens=1024)
    conversation = [
        ChatTurn(role="system", text="Keep it terse."),
        ChatTurn(role="user", text="Find the matrix"),
        AssistantTurn(
            native=[
                {"type": "text", "text": "Looking it up."},
                {"type": "tool_use", "id": "call_1", "name": "search_media", "input": {"query": "matrix"}},
            ]
        ),
        ToolResultsTurn(
            results=[
                ToolResult(call_id="call_1", name="search_media", content={"found": True}, is_error=False),
                ToolResult(call_id="call_2", name="search_media", content="not found", is_error=True),
            ]
        ),
    ]

    system, messages = client._build_messages("You are the concierge.", conversation)

    assert "You are the concierge." in system
    assert "Keep it terse." in system

    assert messages[0] == {"role": "user", "content": "Find the matrix"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == conversation[2].native

    # Both tool_result blocks land in a single user message.
    assert messages[2]["role"] == "user"
    tool_result_blocks = messages[2]["content"]
    assert len(tool_result_blocks) == 2
    assert all(block["type"] == "tool_result" for block in tool_result_blocks)

    success_block, error_block = tool_result_blocks
    assert success_block["tool_use_id"] == "call_1"
    assert "is_error" not in success_block
    assert error_block["tool_use_id"] == "call_2"
    assert error_block["is_error"] is True

    # No stray fourth message; system ChatTurn never became a message.
    assert len(messages) == 3
    assert all(msg["role"] != "system" for msg in messages)


@pytest.mark.asyncio
async def test_anthropic_generate_response_records_usage(monkeypatch):
    events: list[dict] = []
    client = AnthropicProviderClient(
        api_key="test-key", model="claude-3-5", max_output_tokens=2048, usage_recorder=events.append
    )

    fake_response = _Obj(
        content=[
            _Obj(type="text", text="Sure thing"),
            _Obj(type="tool_use", id="call_9", name="search_media", input={"query": "matrix"}),
        ],
        model="claude-3-5-resolved",
        usage=_Obj(input_tokens=40, output_tokens=15, cache_read_input_tokens=5, cache_creation_input_tokens=2),
    )

    async def fake_create(**kwargs):
        return fake_response

    monkeypatch.setattr(client._client.messages, "create", fake_create)

    result = await client.generate_response(
        instructions="Be helpful.",
        conversation=[ChatTurn(role="user", text="Find the matrix")],
        tools=[ToolSchema(name="search_media", description="search", parameters={"type": "object"})],
        usage_context={"user_id": "u1"},
    )

    assert result.text == "Sure thing"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].call_id == "call_9"
    assert result.native_turn[0]["type"] == "text"
    assert result.native_turn[1]["type"] == "tool_use"

    assert len(events) == 1
    event = events[0]
    assert event["provider"] == "anthropic"
    assert event["model"] == "claude-3-5"
    assert event["resolved_model"] == "claude-3-5-resolved"
    assert event["input_tokens"] == 40 + 5 + 2
    assert event["cached_input_tokens"] == 5
    assert event["output_tokens"] == 15
    assert event["total_tokens"] == 40 + 5 + 2 + 15
    assert event["user_id"] == "u1"
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
        assert isinstance(event[key], int)


# --- 4. OpenAI adapter --------------------------------------------------------


def test_openai_build_input_tool_result_error_serializes_with_marker():
    client = OpenAIProviderClient(api_key="test-key", model="gpt-4o-mini")
    conversation = [
        ToolResultsTurn(
            results=[ToolResult(call_id="call_1", name="search_media", content="boom", is_error=True)]
        )
    ]
    items = client._build_input(conversation)
    assert len(items) == 1
    assert items[0]["type"] == "function_call_output"
    assert items[0]["call_id"] == "call_1"
    output = json.loads(items[0]["output"])
    assert output == {"ok": False, "is_error": True, "error": "boom"}


def test_openai_build_input_system_role_becomes_developer():
    client = OpenAIProviderClient(api_key="test-key", model="gpt-4o-mini")
    items = client._build_input([ChatTurn(role="system", text="Be terse.")])
    assert items == [{"role": "developer", "content": "Be terse."}]


def test_openai_build_response_extracts_tool_calls_and_text():
    client = OpenAIProviderClient(api_key="test-key", model="gpt-4o-mini")
    message_item = _Obj(type="message", role="assistant", content=[_Obj(type="output_text", text="Hello there")])
    function_call_item = _Obj(type="function_call", call_id="call_1", name="search_media", arguments='{"query": "matrix"}')
    reasoning_item = _Obj(type="reasoning", summary=[])
    unrelated_item = _Obj(type="web_search_call", id="ws_1")

    fake_response = _Obj(
        output=[message_item, function_call_item, reasoning_item, unrelated_item],
        output_text=None,
        model="gpt-4o-mini-2024",
        usage=None,
    )

    result = client._build_response(fake_response)

    assert result.text == "Hello there"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].call_id == "call_1"
    assert result.tool_calls[0].name == "search_media"
    args, err = result.tool_calls[0].parse_arguments()
    assert err is None
    assert args == {"query": "matrix"}

    native_types = {item["type"] for item in result.native_turn}
    assert native_types == {"message", "function_call", "reasoning"}


@pytest.mark.asyncio
async def test_openai_generate_response_records_usage(monkeypatch):
    events: list[dict] = []
    client = OpenAIProviderClient(api_key="test-key", model="gpt-4o-mini", usage_recorder=events.append)

    fake_response = _Obj(
        output=[_Obj(type="message", role="assistant", content=[_Obj(type="output_text", text="hi")])],
        output_text="hi",
        model="gpt-4o-mini-2024-07-18",
        usage=_Obj(input_tokens=100, output_tokens=20, total_tokens=120, input_tokens_details=_Obj(cached_tokens=30)),
    )

    async def fake_create(**kwargs):
        return fake_response

    monkeypatch.setattr(client._client.responses, "create", fake_create)

    result = await client.generate_response(
        instructions="be helpful",
        conversation=[ChatTurn(role="user", text="hello")],
        tools=[],
        usage_context={"user_id": "u1"},
    )

    assert result.text == "hi"
    assert len(events) == 1
    event = events[0]
    assert event["provider"] == "openai"
    assert event["model"] == "gpt-4o-mini"
    assert event["resolved_model"] == "gpt-4o-mini-2024-07-18"
    assert event["input_tokens"] == 100
    assert event["cached_input_tokens"] == 30
    assert event["output_tokens"] == 20
    assert event["total_tokens"] == 120
    assert event["user_id"] == "u1"
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
        assert isinstance(event[key], int)


# --- 5. Ollama adapter --------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_generate_response_builds_payload_and_records_usage(monkeypatch):
    captured: dict = {}
    events: list[dict] = []

    async def fake_post(self, url, json=None, **kwargs):
        captured["url"] = url
        captured["payload"] = json
        return httpx.Response(
            200,
            json={
                "model": "llama3",
                "message": {
                    "role": "assistant",
                    "content": "sure",
                    "tool_calls": [{"function": {"name": "search_media", "arguments": {"query": "matrix"}}}],
                },
                "prompt_eval_count": 12,
                "eval_count": 4,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = OllamaProviderClient(base_url="localhost:11434", model="llama3", usage_recorder=events.append)
    conversation = [
        ChatTurn(role="system", text="Be terse."),
        ChatTurn(role="user", text="Find the matrix"),
    ]
    result = await client.generate_response(
        instructions="You are the concierge.",
        conversation=conversation,
        tools=[ToolSchema(name="search_media", description="search", parameters={"type": "object"})],
        usage_context={"conversation_id": "conv-1"},
    )

    # Payload building: base URL normalized, system merged, tool schema wrapped.
    assert captured["url"] == "http://localhost:11434/api/chat"
    payload = captured["payload"]
    assert payload["messages"][0] == {
        "role": "system",
        "content": "You are the concierge.\n\nBe terse.",
    }
    assert payload["messages"][1] == {"role": "user", "content": "Find the matrix"}
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["function"]["name"] == "search_media"

    # Response parsing.
    assert result.text == "sure"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "search_media"
    args, err = result.tool_calls[0].parse_arguments()
    assert err is None
    assert args == {"query": "matrix"}
    assert result.tool_calls[0].call_id  # generated uuid-based id

    # Usage event mapping.
    assert len(events) == 1
    event = events[0]
    assert event["provider"] == "ollama"
    assert event["model"] == "llama3"
    assert event["resolved_model"] == "llama3"
    assert event["input_tokens"] == 12
    assert event["cached_input_tokens"] == 0
    assert event["output_tokens"] == 4
    assert event["total_tokens"] == 16
    assert event["conversation_id"] == "conv-1"
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
        assert isinstance(event[key], int)


@pytest.mark.asyncio
async def test_ollama_tool_result_message_encodes_error(monkeypatch):
    captured: dict = {}

    async def fake_post(self, url, json=None, **kwargs):
        captured["payload"] = json
        return httpx.Response(
            200,
            json={"model": "llama3", "message": {"role": "assistant", "content": "ok"}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = OllamaProviderClient(base_url="localhost:11434", model="llama3")
    await client.generate_response(
        instructions="",
        conversation=[
            ToolResultsTurn(results=[ToolResult(call_id="call_1", name="x", content="boom", is_error=True)])
        ],
        tools=[],
    )

    tool_message = [m for m in captured["payload"]["messages"] if m.get("role") == "tool"][0]
    assert tool_message["tool_call_id"] == "call_1"
    decoded = json.loads(tool_message["content"])
    assert decoded == {"ok": False, "is_error": True, "error": "boom"}


@pytest.mark.asyncio
async def test_ollama_skips_usage_recording_when_no_counts(monkeypatch):
    events: list[dict] = []

    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={"model": "llama3", "message": {"role": "assistant", "content": "ok"}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = OllamaProviderClient(base_url="localhost:11434", model="llama3", usage_recorder=events.append)
    await client.generate_response(instructions="", conversation=[], tools=[])
    assert events == []


# --- 7. backend.state provider column ----------------------------------------


def test_record_openai_token_usage_stores_provider_column(tmp_path):
    db_path = tmp_path / "usage.db"
    store = ConversationStore(f"sqlite:///{db_path}")

    store.record_openai_token_usage(
        {
            "provider": "anthropic",
            "model": "claude-3-5",
            "resolved_model": "claude-3-5-resolved",
            "input_tokens": 10,
            "cached_input_tokens": 2,
            "output_tokens": 3,
            "total_tokens": 13,
            "user_id": "u1",
            "username": "ben",
            "conversation_id": "conv-1",
            "source": "chat",
        }
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT provider, model, resolved_model, input_tokens, output_tokens FROM openai_token_usage"
        ).fetchone()

    assert row == ("anthropic", "claude-3-5", "claude-3-5-resolved", 10, 3)


def test_summarize_openai_token_usage_unaffected_by_provider_column(tmp_path):
    db_path = tmp_path / "usage2.db"
    store = ConversationStore(f"sqlite:///{db_path}")
    store.record_openai_token_usage(
        {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "input_tokens": 5,
            "cached_input_tokens": 1,
            "output_tokens": 2,
            "total_tokens": 7,
        }
    )
    since = datetime.utcnow() - timedelta(days=1)
    summary = store.summarize_openai_token_usage_since(model="gpt-4o-mini", since=since)
    assert summary["call_count"] == 1
    assert summary["input_tokens"] == 5
    assert summary["output_tokens"] == 2

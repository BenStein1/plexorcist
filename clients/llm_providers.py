"""Provider-agnostic LLM layer on official SDKs, with a neutral internal format.

This is the Phase 2 replacement for the OpenAI-Responses-shaped internal
format in clients/llm.py. It talks to OpenAI and Anthropic through their
official SDKs (clients/llm.py used raw httpx) and to Ollama through httpx
(no official SDK exists). The neutral `ConversationItem` shapes let the agent
build one conversation and hand it to whichever provider is configured,
without knowing that provider's wire format.

clients/llm.py, clients/openai_client.py, backend/agent.py, and
backend/main.py are untouched in this phase — the running app still uses the
old layer. A later phase swaps the agent onto this module and deletes the old
files.
"""

from __future__ import annotations

import json
import logging
import math
import time
from email.utils import parsedate_to_datetime
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI


logger = logging.getLogger(__name__)


UsageRecorder = Callable[[dict[str, Any]], None]


class LlmConfigurationError(ValueError):
    pass


class NvidiaProviderError(httpx.HTTPError):
    """Provider failure with a safe status code for the agent's error path."""

    def __init__(self, message: str, *, status_code: int | None = None, reason: str = "provider_error"):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


# --- Neutral tool shapes ------------------------------------------------------


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments_json: str
    raw: Any = None

    def parse_arguments(self) -> tuple[dict[str, Any] | None, str | None]:
        """Returns (args, None) or (None, human-readable parse error).

        Empty string -> ({}, None). Non-dict JSON -> error.
        """
        raw = self.arguments_json
        if raw == "":
            return {}, None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"Could not parse arguments for tool {self.name!r}: {exc}"
        if not isinstance(parsed, dict):
            return None, (
                f"Arguments for tool {self.name!r} must be a JSON object, "
                f"got {type(parsed).__name__}."
            )
        return parsed, None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: dict[str, Any] | str
    is_error: bool = False


# --- Neutral response shape ---------------------------------------------------


@dataclass(frozen=True)
class LlmResponse:
    text: str
    tool_calls: list[ToolCall]
    native_turn: Any
    raw: Any = None


# --- Neutral conversation shapes ----------------------------------------------


@dataclass(frozen=True)
class ChatTurn:
    role: str
    text: str


@dataclass(frozen=True)
class AssistantTurn:
    native: Any


@dataclass(frozen=True)
class ToolResultsTurn:
    results: list[ToolResult]


ConversationItem = ChatTurn | AssistantTurn | ToolResultsTurn


class LlmClient(Protocol):
    async def generate_response(
        self,
        *,
        instructions: str,
        conversation: list[ConversationItem],
        tools: list[ToolSchema],
        usage_context: dict[str, Any] | None = None,
        tool_choice: str | None = None,
    ) -> LlmResponse:
        ...


@dataclass(frozen=True)
class LlmProviderConfig:
    provider: str
    model: str
    timeout_seconds: float
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    ollama_base_url: str | None = None
    max_output_tokens: int = 4096
    nvidia_api_key: str | None = None
    nvidia_catalog_path: str | None = None
    nvidia_state_get: Callable[[str], str | None] | None = None
    nvidia_state_set: Callable[[str, str], None] | None = None


ProviderBuilder = Callable[[LlmProviderConfig, UsageRecorder | None], LlmClient | None]


def _normalize_provider(provider: str | None) -> str:
    return (provider or "openai").strip().lower() or "openai"


def _tool_result_payload(result: ToolResult) -> str:
    """Serialize a ToolResult's content the same way across adapters that
    encode errors as a JSON envelope (OpenAI, Ollama)."""
    if result.is_error:
        return json.dumps({"ok": False, "is_error": True, "error": result.content}, ensure_ascii=False)
    if isinstance(result.content, str):
        return result.content
    return json.dumps(result.content, ensure_ascii=False)


def _arguments_to_json(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    if arguments is None:
        return "{}"
    try:
        return json.dumps(arguments, ensure_ascii=False)
    except TypeError:
        return "{}"


def _generated_call_id(provider: str, index: int) -> str:
    return f"{provider}_call_{uuid4().hex}_{index}"


# --- OpenAI adapter (Responses API) -------------------------------------------


_OPENAI_ECHOABLE_OUTPUT_TYPES = {"message", "function_call", "reasoning"}


class OpenAIProviderClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 120.0,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self.model = model
        self.usage_recorder = usage_recorder
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=2)

    async def generate_response(
        self,
        *,
        instructions: str,
        conversation: list[ConversationItem],
        tools: list[ToolSchema],
        usage_context: dict[str, Any] | None = None,
        tool_choice: str | None = None,
    ) -> LlmResponse:
        input_items = self._build_input(conversation)
        tool_payloads = [self._tool_payload(tool) for tool in tools]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": input_items,
            "tools": tool_payloads,
        }
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        response = await self._client.responses.create(
            **kwargs,
        )
        self._record_usage(response, usage_context or {})
        return self._build_response(response)

    def _tool_payload(self, tool: ToolSchema) -> dict[str, Any]:
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }

    def _build_input(self, conversation: list[ConversationItem]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for turn in conversation:
            if isinstance(turn, ChatTurn):
                role = "developer" if turn.role == "system" else turn.role
                items.append({"role": role, "content": turn.text})
            elif isinstance(turn, AssistantTurn):
                native = turn.native if isinstance(turn.native, list) else []
                items.extend(native)
            elif isinstance(turn, ToolResultsTurn):
                for result in turn.results:
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": result.call_id,
                            "output": _tool_result_payload(result),
                        }
                    )
        return items

    def _build_response(self, response: Any) -> LlmResponse:
        output = list(getattr(response, "output", None) or [])
        tool_calls: list[ToolCall] = []
        for item in output:
            if getattr(item, "type", None) != "function_call":
                continue
            tool_calls.append(
                ToolCall(
                    call_id=str(getattr(item, "call_id", "") or ""),
                    name=str(getattr(item, "name", "") or ""),
                    arguments_json=str(getattr(item, "arguments", "") or ""),
                    raw=item,
                )
            )
        native_turn = [
            dumped
            for item in output
            if (dumped := item.model_dump(exclude_none=True)).get("type") in _OPENAI_ECHOABLE_OUTPUT_TYPES
        ]
        return LlmResponse(
            text=self._extract_text(response, output),
            tool_calls=tool_calls,
            native_turn=native_turn,
            raw=response,
        )

    def _extract_text(self, response: Any, output: list[Any]) -> str:
        direct = getattr(response, "output_text", None)
        if direct:
            return str(direct)

        chunks: list[str] = []
        for item in output:
            if getattr(item, "type", None) != "message":
                continue
            for content in getattr(item, "content", None) or []:
                content_type = getattr(content, "type", None)
                content_text = getattr(content, "text", None)
                if content_type in {"output_text", "text"} and content_text:
                    chunks.append(str(content_text))
        return "\n".join(chunk for chunk in chunks if chunk).strip()

    def _record_usage(self, response: Any, usage_context: dict[str, Any]) -> None:
        if self.usage_recorder is None:
            return
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        input_details = getattr(usage, "input_tokens_details", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", 0) or (input_tokens + output_tokens))
        event = {
            "provider": "openai",
            "model": self.model,
            "resolved_model": str(getattr(response, "model", None) or self.model),
            "input_tokens": input_tokens,
            "cached_input_tokens": int(getattr(input_details, "cached_tokens", 0) or 0) if input_details is not None else 0,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            **usage_context,
        }
        try:
            self.usage_recorder(event)
        except Exception:
            logger.exception("Failed to record OpenAI token usage")


# --- Anthropic adapter (Messages API) -----------------------------------------


class AnthropicProviderClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 120.0,
        max_output_tokens: int = 4096,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.usage_recorder = usage_recorder
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_seconds, max_retries=2)

    async def generate_response(
        self,
        *,
        instructions: str,
        conversation: list[ConversationItem],
        tools: list[ToolSchema],
        usage_context: dict[str, Any] | None = None,
        tool_choice: str | None = None,
    ) -> LlmResponse:
        system, messages = self._build_messages(instructions, conversation)
        tool_payloads = [self._tool_payload(tool) for tool in tools]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "system": system,
            "messages": messages,
        }
        if tool_payloads:
            kwargs["tools"] = tool_payloads
        if tool_choice == "required":
            kwargs["tool_choice"] = {"type": "any"}
        response = await self._client.messages.create(**kwargs)
        self._record_usage(response, usage_context or {})
        return self._build_response(response)

    def _tool_payload(self, tool: ToolSchema) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }

    def _append_message(self, messages: list[dict[str, Any]], role: str, content: Any) -> None:
        if messages and messages[-1].get("role") == role:
            prev = messages[-1]
            prev_content = prev["content"]
            if isinstance(prev_content, str):
                prev_content = [{"type": "text", "text": prev_content}]
            new_content = content
            if isinstance(new_content, str):
                new_content = [{"type": "text", "text": new_content}]
            prev["content"] = [*prev_content, *new_content]
            return
        messages.append({"role": role, "content": content})

    def _build_messages(
        self,
        instructions: str,
        conversation: list[ConversationItem],
    ) -> tuple[str, list[dict[str, Any]]]:
        system_parts = [instructions.strip()] if instructions.strip() else []
        messages: list[dict[str, Any]] = []

        for turn in conversation:
            if isinstance(turn, ChatTurn):
                if turn.role == "system":
                    if turn.text:
                        system_parts.append(turn.text)
                    continue
                self._append_message(messages, turn.role, turn.text)
            elif isinstance(turn, AssistantTurn):
                native = turn.native if isinstance(turn.native, list) else []
                self._append_message(messages, "assistant", native)
            elif isinstance(turn, ToolResultsTurn):
                blocks = [self._tool_result_block(result) for result in turn.results]
                self._append_message(messages, "user", blocks)

        return "\n\n".join(part for part in system_parts if part), messages

    def _tool_result_block(self, result: ToolResult) -> dict[str, Any]:
        content = result.content if isinstance(result.content, str) else json.dumps(result.content, ensure_ascii=False)
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": result.call_id,
            "content": content,
        }
        if result.is_error:
            block["is_error"] = True
        return block

    def _build_response(self, response: Any) -> LlmResponse:
        text_chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        content = getattr(response, "content", None) or []
        for block in content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text = getattr(block, "text", "") or ""
                if text:
                    text_chunks.append(text)
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        call_id=str(getattr(block, "id", "") or ""),
                        name=str(getattr(block, "name", "") or ""),
                        arguments_json=json.dumps(getattr(block, "input", None) or {}, ensure_ascii=False),
                        raw=block,
                    )
                )
        native_turn = [block.model_dump(exclude_none=True) for block in content]
        return LlmResponse(
            text="\n".join(chunk for chunk in text_chunks if chunk).strip(),
            tool_calls=tool_calls,
            native_turn=native_turn,
            raw=response,
        )

    def _record_usage(self, response: Any, usage_context: dict[str, Any]) -> None:
        if self.usage_recorder is None:
            return
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        cache_read_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        total_input_tokens = input_tokens + cache_read_tokens + cache_creation_tokens
        event = {
            "provider": "anthropic",
            "model": self.model,
            "resolved_model": str(getattr(response, "model", None) or self.model),
            "input_tokens": total_input_tokens,
            "cached_input_tokens": cache_read_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_input_tokens + output_tokens,
            **usage_context,
        }
        try:
            self.usage_recorder(event)
        except Exception:
            logger.exception("Failed to record Anthropic token usage")


# --- Ollama adapter (httpx, /api/chat) ----------------------------------------


class OllamaProviderClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = 120.0,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.usage_recorder = usage_recorder

    def _normalize_base_url(self, base_url: str) -> str:
        normalized = (base_url or "http://localhost:11434").strip()
        if not normalized:
            normalized = "http://localhost:11434"
        if "://" not in normalized:
            normalized = f"http://{normalized}"
        return normalized.rstrip("/")

    async def generate_response(
        self,
        *,
        instructions: str,
        conversation: list[ConversationItem],
        tools: list[ToolSchema],
        usage_context: dict[str, Any] | None = None,
        tool_choice: str | None = None,
    ) -> LlmResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(instructions, conversation),
            "stream": False,
        }
        tool_payloads = [self._tool_payload(tool) for tool in tools]
        if tool_payloads:
            payload["tools"] = tool_payloads
        if tool_choice:
            payload["tool_choice"] = tool_choice

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.base_url}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        self._record_usage(data, usage_context or {})
        return self._build_response(data)

    def _tool_payload(self, tool: ToolSchema) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    def _append_text_message(self, messages: list[dict[str, Any]], role: str, text: str) -> None:
        if not text:
            return
        if messages and messages[-1].get("role") == role and "tool_calls" not in messages[-1]:
            existing = str(messages[-1].get("content") or "")
            messages[-1]["content"] = f"{existing}\n{text}" if existing else text
            return
        messages.append({"role": role, "content": text})

    def _tool_result_message(self, result: ToolResult) -> dict[str, Any]:
        return {
            "role": "tool",
            "content": _tool_result_payload(result),
            "tool_call_id": result.call_id,
        }

    def _build_messages(self, instructions: str, conversation: list[ConversationItem]) -> list[dict[str, Any]]:
        system_parts = [instructions.strip()] if instructions.strip() else []
        messages: list[dict[str, Any]] = []

        for turn in conversation:
            if isinstance(turn, ChatTurn):
                if turn.role == "system":
                    if turn.text:
                        system_parts.append(turn.text)
                    continue
                self._append_text_message(messages, turn.role, turn.text)
            elif isinstance(turn, AssistantTurn):
                native = turn.native if isinstance(turn.native, dict) else {}
                messages.append(native)
            elif isinstance(turn, ToolResultsTurn):
                for result in turn.results:
                    messages.append(self._tool_result_message(result))

        if system_parts:
            messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})
        return messages

    def _build_response(self, payload: dict[str, Any]) -> LlmResponse:
        message = payload.get("message")
        if not isinstance(message, dict):
            message = {}

        text = str(message.get("content") or "").strip()
        tool_calls: list[ToolCall] = []
        raw_tool_calls = message.get("tool_calls")
        if not isinstance(raw_tool_calls, list):
            raw_tool_calls = []

        for index, raw_call in enumerate(raw_tool_calls, start=1):
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict):
                function = {}
            name = str(function.get("name") or raw_call.get("name") or "")
            arguments = _arguments_to_json(function.get("arguments", raw_call.get("arguments")))
            call_id = str(raw_call.get("id") or raw_call.get("call_id") or _generated_call_id("ollama", index))
            tool_calls.append(ToolCall(call_id=call_id, name=name, arguments_json=arguments, raw=raw_call))

        return LlmResponse(text=text, tool_calls=tool_calls, native_turn=message, raw=payload)

    def _record_usage(self, payload: dict[str, Any], usage_context: dict[str, Any]) -> None:
        if self.usage_recorder is None:
            return
        prompt_eval_count = payload.get("prompt_eval_count")
        eval_count = payload.get("eval_count")
        if prompt_eval_count is None and eval_count is None:
            return
        input_tokens = int(prompt_eval_count or 0)
        output_tokens = int(eval_count or 0)
        event = {
            "provider": "ollama",
            "model": self.model,
            "resolved_model": str(payload.get("model") or self.model),
            "input_tokens": input_tokens,
            "cached_input_tokens": 0,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            **usage_context,
        }
        try:
            self.usage_recorder(event)
        except Exception:
            logger.exception("Failed to record Ollama token usage")


# --- NVIDIA adapter (OpenAI-compatible chat completions) --------------------


NVIDIA_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_COOLDOWN_SECONDS = 900.0
NVIDIA_ENDPOINT_COOLDOWN_MAX = 300.0
_NVIDIA_PROFILES: dict[str, dict[str, Any]] = {
    "moonshotai/kimi-k3": {"temperature": 1.0, "max_tokens": 65536, "reasoning_effort": "max"},
    "nvidia/nemotron-3-ultra-550b-a55b": {
        "temperature": 1.0, "top_p": 0.95, "max_tokens": 32768,
        "chat_template_kwargs": {"enable_thinking": True, "force_nonempty_content": True},
    },
    "nvidia/nemotron-3-super-120b-a12b": {
        "temperature": 1.0, "top_p": 0.95, "max_tokens": 32768,
        "chat_template_kwargs": {"enable_thinking": True, "force_nonempty_content": True},
    },
    "z-ai/glm-5.3": {
        "temperature": 0.5, "max_tokens": 32768, "reasoning_effort": "max",
        "chat_template_kwargs": {"clear_thinking": True},
    },
    "z-ai/glm-5.3-flash": {"temperature": 1.0, "max_tokens": 32768, "reasoning_effort": "max"},
}


def _nvidia_retry_after(value: str | None) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0.0, parsedate_to_datetime(str(value)).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 30.0


class NvidiaProviderClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        catalog_path: str | None = None,
        state_get: Callable[[str], str | None] | None = None,
        state_set: Callable[[str, str], None] | None = None,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.catalog_path = catalog_path
        self.state_get = state_get
        self.state_set = state_set
        self.usage_recorder = usage_recorder
        self._active_model = model

    def _state(self, key: str) -> dict[str, Any]:
        if self.state_get is None:
            return {}
        try:
            value = json.loads(self.state_get(key) or "{}")
            if not isinstance(value, dict):
                return {}
            until = value.get("until")
            if until is not None and (not isinstance(until, (int, float)) or not math.isfinite(until)):
                return {}
            return value
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _set_state(self, key: str, value: dict[str, Any]) -> None:
        if self.state_set is not None:
            self.state_set(key, json.dumps(value, separators=(",", ":"), sort_keys=True))

    def _cooldown_until(self, model: str) -> float:
        return float(self._state(f"nvidia.cooldown.{model}").get("until") or 0)

    def _mark_failure(self, model: str, status: int | None, reason: str, *, delay: float = NVIDIA_COOLDOWN_SECONDS) -> None:
        self._set_state(
            f"nvidia.cooldown.{model}",
            {"until": time.time() + delay, "status": status, "reason": reason[:80]},
        )

    def _endpoint_cooldown(self) -> float:
        return float(self._state("nvidia.endpoint_cooldown").get("until") or 0)

    def _mark_endpoint_cooldown(self, status: int | None, reason: str, delay: float) -> None:
        self._set_state(
            "nvidia.endpoint_cooldown",
            {"until": time.time() + max(0.0, delay), "status": status, "reason": reason[:80]},
        )

    def _models(self) -> tuple[str, ...]:
        from clients.nvidia_catalog import nvidia_models

        models = list(nvidia_models(self.catalog_path))
        return tuple(models)

    def _tool_payload(self, tool: ToolSchema) -> dict[str, Any]:
        return {"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters}}

    def _build_messages(self, instructions: str, conversation: list[ConversationItem]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if instructions.strip():
            messages.append({"role": "system", "content": instructions})
        for turn in conversation:
            if isinstance(turn, ChatTurn):
                messages.append({"role": turn.role, "content": turn.text})
            elif isinstance(turn, AssistantTurn):
                native = turn.native
                if isinstance(native, dict):
                    messages.append(native)
            elif isinstance(turn, ToolResultsTurn):
                for result in turn.results:
                    messages.append({"role": "tool", "tool_call_id": result.call_id, "content": _tool_result_payload(result)})
        return messages

    def _payload(self, model: str, instructions: str, conversation: list[ConversationItem], tools: list[ToolSchema]) -> dict[str, Any]:
        profile = _NVIDIA_PROFILES.get(model, {"temperature": 0.3, "max_tokens": 32768})
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(instructions, conversation),
            "tools": [self._tool_payload(tool) for tool in tools],
            "stream": False,
            **profile,
        }
        if not tools:
            payload.pop("tools")
        return payload

    def _parse_response(self, data: Any, tools: list[ToolSchema], usage_context: dict[str, Any]) -> LlmResponse:
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
            raise NvidiaProviderError("NVIDIA returned an invalid response", reason="invalid_response")
        choice = data["choices"][0]
        if not isinstance(choice, dict):
            raise NvidiaProviderError("NVIDIA returned an invalid response", reason="invalid_response")
        message = choice.get("message")
        finish = choice.get("finish_reason")
        if not isinstance(message, dict) or message.get("role") != "assistant" or finish not in {"stop", "tool_calls"}:
            raise NvidiaProviderError("NVIDIA returned an invalid response", reason="invalid_response")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise NvidiaProviderError("NVIDIA returned invalid message content", reason="invalid_response")
        schemas = {tool.name: tool.parameters for tool in tools}
        calls: list[ToolCall] = []
        seen: set[str] = set()
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list) or finish == "tool_calls" and not raw_calls:
            raise NvidiaProviderError("NVIDIA returned invalid tool calls", reason="invalid_response")
        for index, raw_call in enumerate(raw_calls):
            function = raw_call.get("function") if isinstance(raw_call, dict) else None
            call_id = raw_call.get("id") if isinstance(raw_call, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            arguments = function.get("arguments") if isinstance(function, dict) else None
            if not isinstance(raw_call, dict) or not isinstance(call_id, str) or not call_id or call_id in seen or raw_call.get("type") != "function":
                raise NvidiaProviderError("NVIDIA returned invalid tool calls", reason="invalid_response")
            if not isinstance(name, str) or name not in schemas:
                raise NvidiaProviderError("NVIDIA returned an unknown tool", reason="invalid_response")
            if arguments is None:
                arguments_json = "{}"
            elif isinstance(arguments, (str, dict)):
                arguments_json = _arguments_to_json(arguments)
            else:
                raise NvidiaProviderError("NVIDIA returned invalid tool arguments", reason="invalid_response")
            try:
                parsed = json.loads(arguments_json or "{}", parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            except (json.JSONDecodeError, ValueError) as exc:
                raise NvidiaProviderError("NVIDIA returned invalid tool arguments", reason="invalid_response") from exc
            if not isinstance(parsed, dict):
                raise NvidiaProviderError("NVIDIA returned invalid tool arguments", reason="invalid_response")
            seen.add(call_id)
            calls.append(ToolCall(call_id=call_id, name=name, arguments_json=arguments_json, raw=raw_call))
        if not calls and not (content or "").strip():
            raise NvidiaProviderError("NVIDIA returned an empty response", reason="invalid_response")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        self._record_usage(data, usage, usage_context)
        return LlmResponse(text=(content or "").strip(), tool_calls=calls, native_turn=message, raw=data)

    def _record_usage(self, data: dict[str, Any], usage: dict[str, Any], usage_context: dict[str, Any]) -> None:
        if self.usage_recorder is None or not usage:
            return
        input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        try:
            self.usage_recorder({
            "provider": "nvidia", "model": self._active_model,
            "resolved_model": str(data.get("model") or self._active_model),
            "input_tokens": input_tokens, "cached_input_tokens": 0,
            "output_tokens": output_tokens, "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
            **usage_context,
            })
        except Exception:
            logger.exception("Failed to record NVIDIA token usage")

    async def generate_response(self, *, instructions: str, conversation: list[ConversationItem], tools: list[ToolSchema], usage_context: dict[str, Any] | None = None, tool_choice: str | None = None) -> LlmResponse:
        if not self.api_key:
            raise LlmConfigurationError("NVIDIA_API_KEY is required for the NVIDIA provider.")
        if self._endpoint_cooldown() > time.time():
            raise NvidiaProviderError("NVIDIA is temporarily unavailable", reason="endpoint_cooldown")
        last_error: NvidiaProviderError | None = None
        rate_limited = False
        alternate_after_rate_limit = False
        timeout = httpx.Timeout(connect=15.0, write=30.0, read=None, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout, headers={"Authorization": f"Bearer {self.api_key}"}) as client:
            for model in self._models():
                if self._cooldown_until(model) > time.time():
                    continue
                payload = self._payload(model, instructions, conversation, tools)
                self._active_model = model
                if tool_choice:
                    payload["tool_choice"] = tool_choice
                status: int | None = None
                try:
                    response = await client.post(NVIDIA_CHAT_URL, json=payload)
                    status = response.status_code
                    if status in {401, 403}:
                        raise NvidiaProviderError("NVIDIA authentication rejected", status_code=status, reason="authentication")
                    if status == 429:
                        raise NvidiaProviderError("NVIDIA rate limit", status_code=status, reason="rate_limited")
                    response.raise_for_status()
                    return self._parse_response(response.json(), tools, usage_context or {})
                except NvidiaProviderError as exc:
                    last_error = exc
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    last_error = NvidiaProviderError("NVIDIA request failed", status_code=status, reason="transport_or_response")
                if last_error.status_code in {401, 403}:
                    raise last_error
                self._mark_failure(model, last_error.status_code, last_error.reason)
                if last_error.status_code == 429:
                    retry_after = response.headers.get("Retry-After") if "response" in locals() else None
                    self._mark_endpoint_cooldown(429, "rate_limited", _nvidia_retry_after(retry_after))
                    if rate_limited:
                        break
                    rate_limited = True
                    alternate_after_rate_limit = True
                elif alternate_after_rate_limit:
                    break
        raise last_error or NvidiaProviderError("No NVIDIA model is currently available", reason="cooldown")


# --- Registry / factory --------------------------------------------------------


def _build_openai_client(config: LlmProviderConfig, usage_recorder: UsageRecorder | None) -> LlmClient | None:
    if not config.openai_api_key:
        return None
    return OpenAIProviderClient(
        config.openai_api_key,
        config.model,
        timeout_seconds=config.timeout_seconds,
        usage_recorder=usage_recorder,
    )


def _build_anthropic_client(config: LlmProviderConfig, usage_recorder: UsageRecorder | None) -> LlmClient | None:
    if not config.anthropic_api_key:
        return None
    return AnthropicProviderClient(
        config.anthropic_api_key,
        config.model,
        timeout_seconds=config.timeout_seconds,
        max_output_tokens=config.max_output_tokens,
        usage_recorder=usage_recorder,
    )


def _build_ollama_client(config: LlmProviderConfig, usage_recorder: UsageRecorder | None) -> LlmClient | None:
    return OllamaProviderClient(
        config.ollama_base_url or "http://localhost:11434",
        config.model,
        timeout_seconds=config.timeout_seconds,
        usage_recorder=usage_recorder,
    )


def _build_nvidia_client(config: LlmProviderConfig, usage_recorder: UsageRecorder | None) -> LlmClient | None:
    if not config.nvidia_api_key:
        return None
    return NvidiaProviderClient(
        config.nvidia_api_key,
        config.model,
        catalog_path=config.nvidia_catalog_path,
        state_get=config.nvidia_state_get,
        state_set=config.nvidia_state_set,
        usage_recorder=usage_recorder,
    )


_LLM_PROVIDERS: dict[str, ProviderBuilder] = {
    "anthropic": _build_anthropic_client,
    "nvidia": _build_nvidia_client,
    "ollama": _build_ollama_client,
    "openai": _build_openai_client,
}


def supported_llm_providers() -> tuple[str, ...]:
    return tuple(sorted(_LLM_PROVIDERS))


def build_llm_client(
    config: LlmProviderConfig,
    *,
    usage_recorder: UsageRecorder | None = None,
) -> LlmClient | None:
    provider = _normalize_provider(config.provider)
    builder = _LLM_PROVIDERS.get(provider)
    if builder is None:
        supported = ", ".join(supported_llm_providers())
        raise LlmConfigurationError(
            f"Unsupported LLM_PROVIDER {config.provider!r}. Supported providers: {supported}. "
            "Add a provider adapter before selecting this provider."
        )

    model = config.model.strip()
    if not model:
        raise LlmConfigurationError("LLM_MODEL or OPENAI_MODEL must be configured.")
    if config.timeout_seconds <= 0:
        raise LlmConfigurationError("LLM request timeout must be greater than zero.")

    normalized_config = LlmProviderConfig(
        provider=provider,
        model=model,
        timeout_seconds=float(config.timeout_seconds),
        openai_api_key=config.openai_api_key,
        anthropic_api_key=config.anthropic_api_key,
        ollama_base_url=config.ollama_base_url,
        max_output_tokens=config.max_output_tokens,
        nvidia_api_key=config.nvidia_api_key,
        nvidia_catalog_path=config.nvidia_catalog_path,
        nvidia_state_get=config.nvidia_state_get,
        nvidia_state_set=config.nvidia_state_set,
    )
    return builder(normalized_config, usage_recorder)


def nvidia_model_status(config: LlmProviderConfig) -> list[dict[str, Any]]:
    """Return sanitized ordered model state for an authenticated admin surface."""
    from clients.nvidia_catalog import nvidia_models

    def cooldown_timestamp(value: Any) -> float:
        try:
            timestamp = float(value or 0)
            return timestamp if math.isfinite(timestamp) else 0.0
        except (TypeError, ValueError, OverflowError):
            return 0.0

    now = time.time()
    endpoint = {}
    if config.nvidia_state_get is not None:
        try:
            endpoint = json.loads(config.nvidia_state_get("nvidia.endpoint_cooldown") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            endpoint = {}
    endpoint_until = cooldown_timestamp(endpoint.get("until")) if isinstance(endpoint, dict) else 0
    result: list[dict[str, Any]] = []
    for model in nvidia_models(config.nvidia_catalog_path):
        state: dict[str, Any] = {}
        if config.nvidia_state_get is not None:
            try:
                raw = json.loads(config.nvidia_state_get(f"nvidia.cooldown.{model}") or "{}")
                state = raw if isinstance(raw, dict) else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                state = {}
        until = cooldown_timestamp(state.get("until"))
        result.append({
            "model": model,
            "status": "cooldown" if until > now or endpoint_until > now else "available",
            "cooldown_until": max(until, endpoint_until) if max(until, endpoint_until) > now else None,
        })
    return result

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


_LLM_PROVIDERS: dict[str, ProviderBuilder] = {
    "anthropic": _build_anthropic_client,
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
    )
    return builder(normalized_config, usage_recorder)

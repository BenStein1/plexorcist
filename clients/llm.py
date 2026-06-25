from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

import httpx


UsageRecorder = Callable[[dict[str, Any]], None]


class LlmConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: str
    call_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class LlmResponse:
    raw: dict[str, Any]
    output_items: list[Any]
    tool_calls: list[ToolCall]
    text: str


@dataclass(frozen=True)
class LlmProviderConfig:
    provider: str
    model: str
    timeout_seconds: float
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    ollama_base_url: str | None = None


class LlmClient(Protocol):
    async def generate_response(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        usage_context: dict[str, Any] | None = None,
    ) -> LlmResponse:
        ...


ProviderBuilder = Callable[[LlmProviderConfig, UsageRecorder | None], LlmClient | None]


def _normalize_provider(provider: str | None) -> str:
    return (provider or "openai").strip().lower() or "openai"


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if text and item.get("type") in {None, "text", "input_text", "output_text"}:
            chunks.append(str(text))
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def _arguments_to_dict(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _arguments_to_json(arguments: Any) -> str:
    return json.dumps(_arguments_to_dict(arguments), ensure_ascii=False)


def _message_text_item(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _function_call_item(
    *,
    call_id: str,
    name: str,
    arguments: str,
    raw: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "raw": raw,
    }


def _generated_call_id(provider: str, index: int) -> str:
    return f"{provider}_call_{uuid4().hex}_{index}"


def _openai_function_name(tool_schema: dict[str, Any]) -> str:
    name = tool_schema.get("name")
    if name:
        return str(name)
    function = tool_schema.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return ""


def _openai_function_description(tool_schema: dict[str, Any]) -> str:
    description = tool_schema.get("description")
    if description is not None:
        return str(description)
    function = tool_schema.get("function")
    if isinstance(function, dict) and function.get("description") is not None:
        return str(function["description"])
    return ""


def _openai_function_parameters(tool_schema: dict[str, Any]) -> dict[str, Any]:
    parameters = tool_schema.get("parameters")
    if isinstance(parameters, dict):
        return parameters
    function = tool_schema.get("function")
    if isinstance(function, dict) and isinstance(function.get("parameters"), dict):
        return function["parameters"]
    return {"type": "object", "properties": {}}


class AnthropicMessagesClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def generate_response(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        usage_context: dict[str, Any] | None = None,
    ) -> LlmResponse:
        del usage_context
        system, messages = self._build_messages(instructions, input_items)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
        }
        tools = self._build_tools(tool_schemas)
        if tools:
            payload["tools"] = tools

        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": self.api_key,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return self._build_response(response.json())

    def _build_tools(self, tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for tool_schema in tool_schemas:
            name = _openai_function_name(tool_schema)
            if not name:
                continue
            tools.append(
                {
                    "name": name,
                    "description": _openai_function_description(tool_schema),
                    "input_schema": _openai_function_parameters(tool_schema),
                }
            )
        return tools

    def _build_messages(
        self,
        instructions: str,
        input_items: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        system_parts = [instructions.strip()] if instructions.strip() else []
        messages: list[dict[str, Any]] = []

        def append_message(role: str, content_blocks: list[dict[str, Any]]) -> None:
            if not content_blocks:
                return
            if messages and messages[-1].get("role") == role and isinstance(messages[-1].get("content"), list):
                messages[-1]["content"].extend(content_blocks)
                return
            messages.append({"role": role, "content": content_blocks})

        for item in input_items:
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            role = str(item.get("role") or "")
            if role in {"developer", "system"}:
                text = _text_from_content(item.get("content"))
                if text:
                    system_parts.append(text)
                continue

            if role in {"user", "assistant"}:
                text = _text_from_content(item.get("content"))
                if text:
                    append_message(role, [{"type": "text", "text": text}])
                continue

            if item_type == "message":
                text = _text_from_content(item.get("content"))
                if text:
                    append_message("assistant", [{"type": "text", "text": text}])
                continue

            if item_type == "function_call":
                call_id = str(item.get("call_id") or "")
                name = str(item.get("name") or "")
                if not call_id or not name:
                    continue
                append_message(
                    "assistant",
                    [
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": name,
                            "input": _arguments_to_dict(item.get("arguments")),
                        }
                    ],
                )
                continue

            if item_type == "function_call_output":
                call_id = str(item.get("call_id") or "")
                if not call_id:
                    continue
                output = item.get("output")
                append_message(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
                        }
                    ],
                )

        return "\n\n".join(system_parts), messages

    def _build_response(self, payload: dict[str, Any]) -> LlmResponse:
        output_items: list[Any] = []
        tool_calls: list[ToolCall] = []
        text_chunks: list[str] = []
        content = payload.get("content")
        if not isinstance(content, list):
            content = []

        generated_call_count = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text") or "")
                if text:
                    text_chunks.append(text)
                    output_items.append(_message_text_item(text))
                continue
            if block_type != "tool_use":
                continue

            generated_call_count += 1
            call_id = str(block.get("id") or _generated_call_id("anthropic", generated_call_count))
            name = str(block.get("name") or "")
            arguments = _arguments_to_json(block.get("input"))
            output_item = _function_call_item(
                call_id=call_id,
                name=name,
                arguments=arguments,
                raw=block,
            )
            output_items.append(output_item)
            tool_calls.append(
                ToolCall(
                    name=name,
                    arguments=arguments,
                    call_id=call_id,
                    raw=block,
                )
            )

        return LlmResponse(
            raw=payload,
            output_items=output_items,
            tool_calls=tool_calls,
            text="\n".join(chunk for chunk in text_chunks if chunk).strip(),
        )


class OllamaChatClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def generate_response(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        usage_context: dict[str, Any] | None = None,
    ) -> LlmResponse:
        del usage_context
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(instructions, input_items),
            "stream": False,
        }
        tools = self._build_tools(tool_schemas)
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.base_url}/api/chat", json=payload)
            response.raise_for_status()
            return self._build_response(response.json())

    def _normalize_base_url(self, base_url: str) -> str:
        normalized = (base_url or "http://localhost:11434").strip()
        if not normalized:
            normalized = "http://localhost:11434"
        if "://" not in normalized:
            normalized = f"http://{normalized}"
        return normalized.rstrip("/")

    def _build_tools(self, tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for tool_schema in tool_schemas:
            name = _openai_function_name(tool_schema)
            if not name:
                continue
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": _openai_function_description(tool_schema),
                        "parameters": _openai_function_parameters(tool_schema),
                    },
                }
            )
        return tools

    def _build_messages(self, instructions: str, input_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        system_parts = [instructions.strip()] if instructions.strip() else []
        messages: list[dict[str, Any]] = []
        call_names: dict[str, str] = {}

        def append_text_message(role: str, text: str) -> None:
            if not text:
                return
            if messages and messages[-1].get("role") == role and "tool_calls" not in messages[-1]:
                existing = str(messages[-1].get("content") or "")
                messages[-1]["content"] = f"{existing}\n{text}" if existing else text
                return
            messages.append({"role": role, "content": text})

        def append_tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> None:
            if not call_id or not name:
                return
            call_names[call_id] = name
            tool_call = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments,
                },
            }
            if messages and messages[-1].get("role") == "assistant":
                messages[-1].setdefault("content", "")
                messages[-1].setdefault("tool_calls", [])
                if isinstance(messages[-1]["tool_calls"], list):
                    messages[-1]["tool_calls"].append(tool_call)
                    return
            messages.append({"role": "assistant", "content": "", "tool_calls": [tool_call]})

        for item in input_items:
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            role = str(item.get("role") or "")
            if role in {"developer", "system"}:
                text = _text_from_content(item.get("content"))
                if text:
                    system_parts.append(text)
                continue

            if role in {"user", "assistant"}:
                append_text_message(role, _text_from_content(item.get("content")))
                continue

            if item_type == "message":
                append_text_message("assistant", _text_from_content(item.get("content")))
                continue

            if item_type == "function_call":
                call_id = str(item.get("call_id") or "")
                name = str(item.get("name") or "")
                append_tool_call(call_id, name, _arguments_to_dict(item.get("arguments")))
                continue

            if item_type == "function_call_output":
                call_id = str(item.get("call_id") or "")
                output = item.get("output")
                message: dict[str, Any] = {
                    "role": "tool",
                    "content": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
                }
                if call_id:
                    message["tool_call_id"] = call_id
                tool_name = call_names.get(call_id)
                if tool_name:
                    message["tool_name"] = tool_name
                messages.append(message)

        if system_parts:
            messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})
        return messages

    def _build_response(self, payload: dict[str, Any]) -> LlmResponse:
        message = payload.get("message")
        if not isinstance(message, dict):
            message = {}

        text = str(message.get("content") or "").strip()
        output_items: list[Any] = []
        if text:
            output_items.append(_message_text_item(text))

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
            output_item = _function_call_item(
                call_id=call_id,
                name=name,
                arguments=arguments,
                raw=raw_call,
            )
            output_items.append(output_item)
            tool_calls.append(
                ToolCall(
                    name=name,
                    arguments=arguments,
                    call_id=call_id,
                    raw=raw_call,
                )
            )

        return LlmResponse(
            raw=payload,
            output_items=output_items,
            tool_calls=tool_calls,
            text=text,
        )


def _build_openai_client(config: LlmProviderConfig, usage_recorder: UsageRecorder | None) -> LlmClient | None:
    if not config.openai_api_key:
        return None
    from clients.openai_client import OpenAIResponsesClient

    return OpenAIResponsesClient(
        config.openai_api_key,
        config.model,
        timeout_seconds=config.timeout_seconds,
        usage_recorder=usage_recorder,
    )


def _build_anthropic_client(config: LlmProviderConfig, usage_recorder: UsageRecorder | None) -> LlmClient | None:
    del usage_recorder
    if not config.anthropic_api_key:
        return None
    return AnthropicMessagesClient(
        config.anthropic_api_key,
        config.model,
        timeout_seconds=config.timeout_seconds,
    )


def _build_ollama_client(config: LlmProviderConfig, usage_recorder: UsageRecorder | None) -> LlmClient | None:
    del usage_recorder
    return OllamaChatClient(
        config.ollama_base_url or "http://localhost:11434",
        config.model,
        timeout_seconds=config.timeout_seconds,
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
    )
    return builder(normalized_config, usage_recorder)

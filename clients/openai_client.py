from __future__ import annotations

import logging
from typing import Any

import httpx

from clients.llm import LlmResponse, ToolCall, UsageRecorder


logger = logging.getLogger(__name__)


class OpenAIResponsesClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 120.0,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.usage_recorder = usage_recorder

    async def generate_response(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        usage_context: dict[str, Any] | None = None,
    ) -> LlmResponse:
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": input_items,
            "tools": tool_schemas,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            payload = response.json()
            self._record_usage(payload, usage_context or {})
            return self._build_response(payload)

    def _build_response(self, payload: dict[str, Any]) -> LlmResponse:
        output = payload.get("output", [])
        output_items = list(output) if isinstance(output, list) else []
        tool_calls: list[ToolCall] = []
        for item in output_items:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            tool_calls.append(
                ToolCall(
                    name=str(item.get("name") or ""),
                    arguments=str(item.get("arguments") or "{}"),
                    call_id=str(item.get("call_id") or ""),
                    raw=item,
                )
            )
        return LlmResponse(
            raw=payload,
            output_items=output_items,
            tool_calls=tool_calls,
            text=self._extract_text(payload),
        )

    def _extract_text(self, payload: dict[str, Any]) -> str:
        direct = payload.get("output_text")
        if direct:
            return str(direct)

        chunks: list[str] = []
        output = payload.get("output", [])
        if not isinstance(output, list):
            return ""
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content_items = item.get("content", [])
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                if not isinstance(content, dict):
                    continue
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    chunks.append(str(content["text"]))
        return "\n".join(chunk for chunk in chunks if chunk).strip()

    def _record_usage(self, payload: dict[str, Any], usage_context: dict[str, Any]) -> None:
        if self.usage_recorder is None:
            return
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return
        input_details = usage.get("input_tokens_details")
        if not isinstance(input_details, dict):
            input_details = {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
        event = {
            "model": self.model,
            "resolved_model": str(payload.get("model") or self.model),
            "input_tokens": input_tokens,
            "cached_input_tokens": int(input_details.get("cached_tokens") or 0),
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            **usage_context,
        }
        try:
            self.usage_recorder(event)
        except Exception:
            logger.exception("Failed to record OpenAI token usage")

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx


logger = logging.getLogger(__name__)
UsageRecorder = Callable[[dict[str, Any]], None]


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

    async def create_response(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        usage_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": input_items,
            "tools": tools,
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
            return payload

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

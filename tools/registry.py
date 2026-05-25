from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend.models import ToolCallRecord


ToolHandler = Callable[..., Awaitable[dict[str, Any]]]
AfterToolCall = Callable[[ToolCallRecord], Awaitable[None]]


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler


class ToolRegistry:
    def __init__(self, after_call: AfterToolCall | None = None) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._after_call = after_call

    def register(
        self,
        name: str,
        handler: ToolHandler,
        description: str,
        parameters: dict[str, Any],
    ) -> None:
        self._definitions[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
        )

    async def call(self, name: str, **kwargs: Any) -> ToolCallRecord:
        if name not in self._definitions:
            raise ValueError(f"Unknown tool: {name}")
        result = await self._definitions[name].handler(**kwargs)
        record = ToolCallRecord(name=name, arguments=kwargs, result=result)
        if self._after_call is not None:
            await self._after_call(record)
        return record

    def openai_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for definition in self._definitions.values():
            tools.append(
                {
                    "type": "function",
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.parameters,
                }
            )
        return tools

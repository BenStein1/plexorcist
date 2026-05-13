from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend.models import ToolCallRecord


ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

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
        return ToolCallRecord(name=name, arguments=kwargs, result=result)

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

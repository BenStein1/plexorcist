"""Adapts the tool catalog to the neutral LLM tool-call contract.

Every tool call the model makes gets an answer, never a swallowed exception:
unknown tool names, malformed arguments JSON, pydantic validation errors, and
handler exceptions all become an `is_error` ToolResult the model can see and
react to, instead of silently becoming `{}` or a dropped turn.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from backend.config import Settings
from backend.models import ToolCallRecord, UserContext
from backend.state import ConversationStore
from clients.llm_providers import ToolCall, ToolResult, ToolSchema
from tools.catalog import Toolkit, ToolSpec, build_toolkit, visible_specs

logger = logging.getLogger(__name__)

AfterToolCall = Callable[[ToolCallRecord], Awaitable[None]]


class ToolBridge:
    def __init__(
        self,
        toolkit: Toolkit,
        specs: list[ToolSpec],
        after_call: AfterToolCall | None = None,
    ) -> None:
        self.toolkit = toolkit
        self._specs_by_name = {spec.name: spec for spec in specs}
        self._after_call = after_call

    def tool_schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(name=spec.name, description=spec.description, parameters=spec.json_schema())
            for spec in self._specs_by_name.values()
        ]

    async def call(self, tool_call: ToolCall) -> ToolResult:
        spec = self._specs_by_name.get(tool_call.name)
        if spec is None:
            available = ", ".join(sorted(self._specs_by_name)) or "none"
            return ToolResult(
                call_id=tool_call.call_id,
                name=tool_call.name,
                content=f"Unknown tool {tool_call.name!r}. Available tools: {available}.",
                is_error=True,
            )

        arguments, parse_error = tool_call.parse_arguments()
        if parse_error is not None:
            return ToolResult(call_id=tool_call.call_id, name=tool_call.name, content=parse_error, is_error=True)

        try:
            parsed_input = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            lines = []
            for error in exc.errors():
                field = ".".join(str(part) for part in error.get("loc", ())) or "input"
                lines.append(f"{field}: {error.get('msg', 'invalid value')}")
            message = f"Invalid arguments for tool {tool_call.name!r}:\n" + "\n".join(lines)
            return ToolResult(call_id=tool_call.call_id, name=tool_call.name, content=message, is_error=True)

        handler_kwargs = parsed_input.model_dump(exclude_unset=True)
        handler = spec.resolve(self.toolkit)
        try:
            result = await handler(**handler_kwargs)
        except Exception as exc:  # noqa: BLE001 - handler failures must reach the model, not crash the loop
            content = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return ToolResult(call_id=tool_call.call_id, name=tool_call.name, content=content, is_error=True)

        if self._after_call is not None:
            record = ToolCallRecord(name=tool_call.name, arguments=handler_kwargs, result=result)
            try:
                await self._after_call(record)
            except Exception:
                logger.exception("after_call hook failed for tool %s", tool_call.name)

        return ToolResult(call_id=tool_call.call_id, name=tool_call.name, content=result)


def build_bridge(
    settings: Settings,
    store: ConversationStore,
    user: UserContext | None,
    after_call: AfterToolCall | None = None,
) -> ToolBridge:
    toolkit = build_toolkit(settings, store, user)
    specs = visible_specs(user, settings)
    return ToolBridge(toolkit, specs, after_call=after_call)

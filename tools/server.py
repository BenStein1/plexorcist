"""FastMCP server exposing the concierge tool catalog.

The server is built per principal: the toolkit is bound to the authenticated
user, and only the tools that principal may see (tools/catalog.py gating) are
registered. The in-process agent uses the catalog directly; this server is the
same catalog spoken over MCP for external clients (Claude Code, other agents).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import Tool
from pydantic import ConfigDict

from backend.config import Settings
from backend.models import UserContext
from backend.state import ConversationStore
from tools import schemas
from tools.catalog import Toolkit, ToolSpec, build_toolkit, visible_specs


class CatalogTool(Tool):
    """MCP tool backed by a catalog spec: validate with the spec's pydantic
    model, then call the toolkit-bound handler."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    input_model: type[schemas.ToolInput]
    handler: Any

    async def run(self, arguments: dict[str, Any]):
        parsed = self.input_model.model_validate(arguments or {})
        result = await self.handler(**parsed.model_dump(exclude_unset=True))
        return self.convert_result(result)

    @classmethod
    def from_spec(cls, spec: ToolSpec, toolkit: Toolkit) -> "CatalogTool":
        return cls(
            name=spec.name,
            description=spec.description,
            parameters=spec.json_schema(),
            tags=set(spec.tags),
            input_model=spec.input_model,
            handler=spec.resolve(toolkit),
        )


def build_mcp_server(
    settings: Settings,
    store: ConversationStore,
    user: UserContext | None,
) -> FastMCP:
    """Build an MCP server whose toolset is bound and gated to `user`."""
    toolkit = build_toolkit(settings, store, user)
    mcp = FastMCP("Plexorcist Concierge")
    for spec in visible_specs(user, settings):
        mcp.add_tool(CatalogTool.from_spec(spec, toolkit))
    return mcp

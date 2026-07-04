"""In-memory MCP round-trip against the FastMCP server built from the catalog."""

import pytest
from fastmcp import Client

from backend.config import Settings
from backend.models import UserContext
from backend.state import ConversationStore
from tools.server import build_mcp_server


def _server(is_admin: bool = False, monkeypatched_handler=None):
    settings = Settings()
    store = ConversationStore(settings.database_url)
    user = UserContext(user_id="u1", username="ben", display_name="Ben", is_admin=is_admin)
    return build_mcp_server(settings, store, user)


@pytest.mark.asyncio
async def test_list_tools_gated_by_role():
    async with Client(_server(is_admin=False)) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools}
    assert "search_media" in names
    assert "repair_requested_show" in names
    assert "get_admin_task_summary" not in names
    assert "broad_jackett_movie_search" not in names  # direct-source flag off by default


@pytest.mark.asyncio
async def test_admin_sees_full_toolset():
    async with Client(_server(is_admin=True)) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools}
    assert "get_admin_task_summary" in names
    assert "set_admin_motd" in names


@pytest.mark.asyncio
async def test_tool_schemas_carry_descriptions():
    async with Client(_server()) as client:
        tools = await client.list_tools()
    by_name = {tool.name: tool for tool in tools}
    repair = by_name["repair_requested_show"]
    assert "PRIMARY TV troubleshooting tool" in (repair.description or "")
    assert repair.inputSchema["additionalProperties"] is False
    assert "query" in repair.inputSchema["required"]


@pytest.mark.asyncio
async def test_call_tool_validation_error_is_reported_not_swallowed():
    async with Client(_server()) as client:
        result = await client.call_tool(
            "request_movie_for_user", {"title": "Heat"}, raise_on_error=False
        )
    assert result.is_error
    text = "".join(block.text for block in result.content if hasattr(block, "text"))
    assert "year" in text  # the model is told exactly what's missing

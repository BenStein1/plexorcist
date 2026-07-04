"""End-to-end tests for the external /mcp endpoint.

Exercises the real ASGI mount + bearer-token auth + FastMCP streamable-HTTP
lifespan wiring added in Phase 4 -- not just tools.server.build_mcp_server in
isolation (see tests/test_mcp_server.py for that). Each test builds a small,
isolated FastAPI app using backend.main's real mounting helpers
(build_mcp_router / mount_mcp / McpTokenRouter) so different token
configurations can be exercised without fighting get_settings()'s
process-wide lru_cache on the shared backend.main.app singleton.
"""

from __future__ import annotations

import contextlib

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from fastmcp.exceptions import ToolError

from backend.config import Settings
from backend.main import McpTokenRouter, mount_mcp
from backend.state import ConversationStore
from tools.media_search import MediaSearchTools


def _settings(**overrides) -> Settings:
    base = {"mcp_auth_token": None, "mcp_admin_token": None}
    base.update(overrides)
    return Settings(database_url="sqlite:///./test_plexorcist.db", **base)


def _build_app(settings: Settings) -> tuple[FastAPI, McpTokenRouter | None]:
    store = ConversationStore(settings.database_url)
    holder: dict[str, McpTokenRouter | None] = {"router": None}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        router = holder["router"]
        if router is not None:
            async with router.lifespan_context():
                yield
        else:
            yield

    app = FastAPI(lifespan=lifespan)
    holder["router"] = mount_mcp(app, settings, store)
    return app, holder["router"]


def _httpx_client_factory(app: FastAPI):
    def factory(**kwargs):
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver", **kwargs)

    return factory


def _mcp_client(app: FastAPI, token: str | None) -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return Client(
        StreamableHttpTransport(
            url="http://testserver/mcp",
            headers=headers,
            httpx_client_factory=_httpx_client_factory(app),
        )
    )


def test_endpoint_absent_when_no_tokens_configured():
    settings = _settings()
    app, router = _build_app(settings)
    assert router is None

    with TestClient(app) as tc:
        # No mount at all: not even a 401 masquerading as available.
        assert tc.get("/mcp").status_code == 404
        assert tc.get("/mcp", follow_redirects=True).status_code == 404
        assert tc.get("/mcp", headers={"Authorization": "Bearer anything"}).status_code == 404


def test_auth_token_only_grants_non_admin_access_and_gates_wrong_tokens():
    settings = _settings(mcp_auth_token="dev-token")
    app, router = _build_app(settings)
    assert router is not None

    with TestClient(app) as tc:

        async def _list(token):
            async with _mcp_client(app, token) as client:
                return await client.list_tools()

        tools = tc.portal.call(_list, "dev-token")
        names = {t.name for t in tools}
        assert "search_media" in names
        assert "repair_requested_show" in names
        assert "get_admin_task_summary" not in names  # no admin principal configured at all

        for bad_token in (None, "wrong-token", "admin-token"):
            with pytest.raises(httpx.HTTPStatusError) as excinfo:
                tc.portal.call(_list, bad_token)
            assert excinfo.value.response.status_code == 401

        # A raw unauthenticated GET must 401 (route exists, just gated) not 404.
        assert tc.get("/mcp").status_code in (401, 307)
        assert tc.get("/mcp", follow_redirects=True).status_code == 401


def test_both_tokens_grant_tiered_access():
    settings = _settings(mcp_auth_token="dev-token", mcp_admin_token="admin-token")
    app, router = _build_app(settings)
    assert router is not None

    with TestClient(app) as tc:

        async def _list(token):
            async with _mcp_client(app, token) as client:
                return await client.list_tools()

        admin_names = {t.name for t in tc.portal.call(_list, "admin-token")}
        assert "get_admin_task_summary" in admin_names
        assert "set_admin_motd" in admin_names
        assert "search_media" in admin_names

        nonadmin_names = {t.name for t in tc.portal.call(_list, "dev-token")}
        assert "get_admin_task_summary" not in nonadmin_names
        assert "search_media" in nonadmin_names

        # Admin-only tool isn't registered on the non-admin principal's server
        # at all, so calling it directly by name errors rather than running.
        async def _call_admin_tool_as_nonadmin():
            async with _mcp_client(app, "dev-token") as client:
                return await client.call_tool("get_admin_task_summary", {})

        with pytest.raises(ToolError, match="Unknown tool"):
            tc.portal.call(_call_admin_tool_as_nonadmin)


def test_successful_tool_call_end_to_end(monkeypatch):
    async def _fake_search_media(self, query: str) -> dict:
        return {"query": query, "candidate": None, "candidates": [], "plex": {"available": False}}

    # Patch before building the app: the catalog binds handlers (tk.media.search_media)
    # eagerly at server-construction time, so the patch must land first.
    monkeypatch.setattr(MediaSearchTools, "search_media", _fake_search_media)

    settings = _settings(mcp_auth_token="dev-token")
    app, router = _build_app(settings)

    with TestClient(app) as tc:

        async def _call():
            async with _mcp_client(app, "dev-token") as client:
                return await client.call_tool("search_media", {"query": "Heat"})

        result = tc.portal.call(_call)
        assert not result.is_error
        assert result.data["query"] == "Heat"
        assert result.data["plex"] == {"available": False}

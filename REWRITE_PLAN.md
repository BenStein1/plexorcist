# AI-Layer Modernization — Plan & Progress

Approved by Ben 2026-07-03. If you are a fresh session picking this up: read this
whole file, check `git log --oneline -8` against the progress table, and continue
from the first unfinished phase. Implementation coding is dispatched to Sonnet 5
subagents (Ben's request, to conserve Fable limits); the supervising session
reviews each diff and dispatches the next phase.

## Why

The concierge LLM got confused about when/how to use its 29 tools. Root causes
(verified in code before the rewrite):

1. Tool handler exceptions were swallowed (`backend/agent.py` caught and
   `continue`d) — the model never saw errors, couldn't self-correct, and the
   dangling call could corrupt the next turn.
2. Malformed tool arguments silently became `{}` (`_parse_arguments`).
3. All when-to-use knowledge lived in a ~355-line prose system prompt
   (`_build_instructions`) instead of with the tools.
4. All 29 tools were exposed to every user every turn (admin tools included).
5. A synthesis layer could overwrite the model's final reply.

## Decisions (settled with Ben — do not relitigate)

- **Real MCP server** (FastMCP 3.x) exposing the tool catalog; external clients
  (Claude Code, Overlord) can connect too. In-process agent consumes the same
  catalog directly (no protocol overhead), so the two surfaces can't drift.
- **AI-layer-only rewrite**: `tools/` registration layer, `backend/agent.py`,
  `clients/llm*.py`. Service adapters in `clients/` (ombi, plex, sickchill,
  radarr, tautulli, jackett, transmission, prowl), FastAPI routes,
  `backend/state.py`, and the UI stay.
- **Official SDKs**: `openai` + `anthropic`; Ollama stays raw httpx.
- Provider-agnosticism preserved: neutral internal message format, adapters per
  provider, `LLM_PROVIDER` env selects.

## Deploy reality (IMPORTANT)

This repo is deployed to 10.0.0.93. Local commits are NOT live until the deploy
script in this folder runs AND `supervisorctl restart plexorcist` runs on
10.0.0.93 as root. Never claim a change is live after only committing. Deploy is
Ben's explicit call.

## Progress

| Phase | Status | Commit |
|---|---|---|
| 0 — pydantic v2 + pytest harness | DONE | 196fbd2 |
| 1 — MCP tool catalog + FastMCP server | DONE | 161842c |
| 2 — provider layer on official SDKs | DONE (Sonnet 5, reviewed) | 06ba4c9 |
| 3 — agent loop rewrite + prompt shrink | DONE (Sonnet 5, reviewed) | cea3d7c (main change 8562764) |
| 4 — external /mcp endpoint + docs | DONE (Sonnet 5, reviewed) | pending |
| 5 — final verification (live chat flows per provider) | pending | — |

## What exists after Phases 0-1

- `tests/` — pytest suite (24 tests green at Phase 1 commit). Run:
  `.venv/bin/python -m pytest tests/ -q`
- `tools/schemas.py` — typed pydantic v2 input models for all 29 tools, with
  cross-field preconditions raising correctable ValidationErrors (e.g.
  request_movie needs tmdb_id OR title+year; repair scope needs its targets).
- `tools/catalog.py` — single-source `ToolSpec` catalog: when-to-use
  descriptions (migrated from the old prompt's per-tool rules), tags
  (`readonly/request/repair/admin/escalation/direct_source_gated`),
  `Toolkit`/`build_toolkit()` per-user binding, `visible_specs(user, settings)`
  gating (admin tools hidden from non-admins; jackett-movie/transmission tools
  hidden unless `movie_direct_source_enabled`).
- `tools/server.py` — `build_mcp_server(settings, store, user)`: FastMCP server
  from the catalog, gated per principal; `CatalogTool` validates args with the
  spec's model before calling the handler.
- `backend/usage_report.py` — token usage report extracted from main.py.
- Old path still live: `tools/registry.py` + `backend/main.py` registration
  block (`build_agent`, ~lines 394-802) + `clients/llm.py` +
  `clients/openai_client.py` + old `backend/agent.py` loop. Phase 3 removes it.

## Phase 2 spec (dispatched)

New module `clients/llm_providers.py`, neutral shapes:
`ToolSchema`, `ToolCall(call_id, name, arguments_json, parse_arguments() ->
(args|None, error|None))`, `ToolResult(call_id, name, content, is_error)`,
`LlmResponse(text, tool_calls, native_turn, raw)`, conversation items
`ChatTurn(role, text) | AssistantTurn(native) | ToolResultsTurn(results)`,
`LlmClient.generate_response(instructions, conversation, tools, usage_context)`,
`build_llm_client(LlmProviderConfig)` factory.

- OpenAI: SDK `AsyncOpenAI`, Responses API; system ChatTurn -> "developer" role;
  tool errors encoded in function_call_output JSON (`{"ok": false, "is_error":
  true, "error": ...}`).
- Anthropic: SDK `AsyncAnthropic`, Messages API; tools use `input_schema`;
  parallel tool results in ONE user message as `tool_result` blocks with
  `is_error: true` on failures; system turns join the system string;
  `max_output_tokens` config (default 4096).
- Ollama: httpx `/api/chat` port of the old adapter; errors encoded in tool
  message JSON.
- Usage recording for ALL providers into the existing sqlite table with a new
  `provider` column (added in `_ensure_openai_token_usage_columns`); event shape
  matches the old OpenAI recorder plus `provider`.
- Tests in `tests/test_llm_providers.py` (no network; fakes/monkeypatch).
- Old files untouched this phase; local commit when green.

## Phase 3 spec (next to dispatch)

Rewrite `backend/agent.py` `ConciergeAgent.respond()` on the new layer:

- **ToolBridge** (new `tools/bridge.py`): takes `visible_specs(user, settings)` +
  `build_toolkit(...)`; exposes `tool_schemas() -> list[ToolSchema]` and
  `async call(name, arguments_json) -> ToolResult`. Behavior:
  - unknown tool -> ToolResult(is_error, "unknown tool X; available: ...")
  - arguments_json parse error -> ToolResult(is_error, the parse error)
  - pydantic ValidationError -> ToolResult(is_error, field-level message)
  - handler exception -> ToolResult(is_error, sanitized message)
  - success -> ToolResult(content=handler dict)
  - after-call hook: admin alerts (`AdminAlertReporter.report_tool_call`) — port
    the alert plumbing from the old loop (`_build_admin_alert`,
    `_should_send_admin_alert`, alert dedupe by key, marking
    `admin_alert_sent` in results).
- Loop: max turns from env `AGENT_MAX_TURNS` (default 6). Every model tool_call
  gets a ToolResult appended (never dropped). Model's final text is
  authoritative; `_plain_support_reply_from_tool_calls` /
  `_plain_tv_repair_reply` only fire when the model produced NO text or the API
  errored (keep `_fallback_reply_from_tool_calls` for API-error paths).
- Preserve: `_refresh_active_media_context` / `_prime_active_media_context`
  state tracking, NILBOG easter-egg blocks and memory modes, admin identity
  shortcut, httpx/SDK error fallbacks (map SDK exceptions: openai.APIError,
  anthropic.APIError, httpx errors).
- **Prompt shrink**: `_build_instructions` keeps persona/voice/calibration,
  admin identity, visibility rules, reply-style rules, NILBOG, global behavior
  ("every chain ends with a user-facing reply", anti-bluffing, Plex-vs-Ombi
  truth split, admin-notified phrasing) — but ALL per-tool when/how rules are
  deleted (they live in catalog descriptions now). Target <= ~100 lines.
- Delete: `tools/registry.py`, `clients/llm.py`, `clients/openai_client.py`,
  the registration block + per-user closures in `backend/main.py` (build_agent
  slims down to: store + ToolBridge + ConciergeAgent + llm client from
  `clients/llm_providers.build_llm_client`).
- Also port the memory summarizer call in `backend/main.py` (~line 1041,
  `_compact_conversation_memory` area): it calls the OLD
  `generate_response(instructions, input_items, tool_schemas=[])` — convert its
  developer/user input_items to ChatTurn("system"/"user", ...) on the new
  interface.
- Tests: fake LlmClient driving the loop — scripted tool calls verifying error
  round-trip (bad JSON args, validation error, handler raise -> model sees
  is_error result and loop continues), max-turns fallback, no reply overwrite
  when model text exists, admin alert hook fires, gating (non-admin bridge has
  no admin tools).

## Phase 4 spec

- Mount FastMCP streamable-HTTP app in `backend/main.py` under `/mcp`.
  fastmcp: `mcp.http_app()` -> mount into FastAPI (mind lifespan wiring — pass
  the mcp app's lifespan to FastAPI or combine).
- Auth: `MCP_AUTH_TOKEN` env (unset = endpoint disabled/404). Token maps to the
  dev/non-admin principal; `MCP_ADMIN_TOKEN` maps to admin principal (reuse
  `build_mcp_server` per principal; simplest: two server instances mounted or
  token check middleware choosing the server). Bearer token via
  `Authorization: Bearer`.
- README section: what the MCP endpoint is + Claude Code `.mcp.json` snippet.

### Phase 4 — what was built

- `backend/config.py`: `mcp_auth_token`/`mcp_admin_token` (`MCP_AUTH_TOKEN` /
  `MCP_ADMIN_TOKEN`, default `None`).
- `backend/main.py`: single mount point `/mcp`. `McpTokenRouter` is a plain
  ASGI callable mounted there; it reads `Authorization: Bearer <token>`,
  matches it against whichever of the two tokens are configured, and forwards
  the request to that principal's FastMCP `http_app()` (built via
  `tools.server.build_mcp_server`), or returns 401 on a missing/wrong token.
  If neither token is set, `build_mcp_router`/`mount_mcp` return `None` and
  `/mcp` is never mounted at all (404). Both principals share one
  `ConversationStore` built once at import time (`_MCP_STORE`), matching the
  chat path's state.
  - Lifespan: confirmed empirically (see `tests/test_mcp_endpoint.py`) that
    Starlette does **not** propagate the ASGI `lifespan` scope into mounted
    sub-apps — `Router.app` intercepts `scope["type"] == "lifespan"` before
    ever consulting routes/mounts. FastMCP's `http_app()` needs its own
    lifespan entered to start its streamable-HTTP session manager, so
    `backend/main.py`'s old `@app.on_event("startup"/"shutdown")` handlers
    were replaced with a single `@contextlib.asynccontextmanager _lifespan`
    passed to `FastAPI(..., lifespan=_lifespan)`. It does what the old
    handlers did (start/cancel the memory-sweeper task) and additionally
    enters `McpTokenRouter.lifespan_context()` (an `AsyncExitStack` over each
    mounted sub-app's `sub_app.lifespan(sub_app)`) so the FastMCP session
    manager(s) actually run for the life of the process.
- `tests/test_mcp_endpoint.py`: builds small isolated `FastAPI()` instances
  per test (via `backend.main.mount_mcp`/`build_mcp_router`, the same
  production code the real `app` uses) rather than reloading the
  `backend.main` singleton — `get_settings()` is process-wide `lru_cache`'d,
  so reloading the shared module to test multiple token configurations would
  leak mutated global state into other test files. Drives real requests
  through `httpx.ASGITransport` + `fastmcp.Client(StreamableHttpTransport(...,
  httpx_client_factory=...))`, executed via `TestClient(app).portal.call(...)`
  so the FastMCP session manager's task group and the actual HTTP calls run
  on the same event loop (the `TestClient`'s background portal loop).
  Additionally hand-verified (not just the isolated-app tests) against the
  real `backend.main.app`/`_MCP_ROUTER` singleton in a clean subprocess with
  `MCP_AUTH_TOKEN`/`MCP_ADMIN_TOKEN` set before import: lifespan enters both
  sub-app session managers, no-auth `/mcp` gets 401, and `list_tools()`
  through the actual mounted endpoint returns the correct per-principal
  toolset (21 tools non-admin, 27 admin).

## Phase 5 verification

- Full pytest suite green.
- `claude mcp add` against live `/mcp` and call `search_media` from Claude Code.
- Run app locally with each configured provider (`LLM_PROVIDER=openai|anthropic|
  ollama` as keys allow), drive real chat: movie request flow, vague repair
  complaint ("the heat download is busted, wrong language i think"), bad-args
  recovery (episode request without identifying the show — model should recover
  from the validation error, not stall).
- Confirm usage rows (with provider column) land in plexorcist.db.
- Deploy + `supervisorctl restart plexorcist` on 10.0.0.93 = Ben's call only.

# Plexorcist Concierge

Plexorcist Concierge is a FastAPI chat front door for a Plex request stack, currently backed by OpenAI through a provider-aware LLM client. It lets users ask for movies, shows, recommendations, missing-episode checks, and repair actions in normal language while keeping privileged media operations behind bounded server-side tools.

The assistant talks to real service adapters for Ombi, Plex, SickChill/SickRage, Radarr, Tautulli, Jackett, Transmission, and Prowl. The model cannot make arbitrary API calls; it can only invoke the registered tools exposed by the backend.

## Current Capabilities

- Plex OAuth login with server-side session identity.
- Successful Plex logins persist for 180 days on the device; reauthentication renews the period.
- Ombi login gate: users must exist in Ombi before entering Plexorcist.
- Friendly-name/admin identity handling for better chat responses.
- Movie and TV search/request flows through Ombi.
- Movie requests by TMDB ID or exact title plus year.
- TV request scope handling for first season, individual seasons, full series, and single episodes.
- Plex/Ombi side-by-side library inventory checks.
- SickChill repair tools for missing, ignored, wanted, or stuck requested episodes.
- Repair-only SickChill show add path for Ombi-to-SickChill handoff failures.
- Radarr-backed movie repair/retry lane for requested movies that are still missing.
- Tautulli watch-context and server-popularity reporting.
- Admin-only OpenAI token odometer with MTD/YTD estimated cost.
- Prowl notifications for login events and meaningful corrective actions.
- Tiered long-term user memory with background conversation compaction.
- SQLite-backed conversation, session, memory, token, and audit state.
- Mobile-friendly single-page chat UI with starter chips and static artwork.

## Architecture

`backend/`

- `main.py`: FastAPI routes, app startup, agent/tool-bridge construction, auth callback, static UI, memory sweeper, `/mcp` mount.
- `agent.py`: LLM response orchestration, prompt assembly, tool-call handling, admin alert behavior.
- `auth_context.py`: user context providers for development, Plex OAuth, Ombi session, and header passthrough modes.
- `auth_store.py`: Plex OAuth session persistence.
- `config.py`: environment-backed settings.
- `logging.py`: structured audit logging.
- `models.py`: shared request, response, user, and conversation models.
- `policy.py`: request/repair policy helpers.
- `state.py`: SQLite storage for conversations, memory, token usage, and support state.

`clients/`

- Service adapters for Ombi, Plex auth, Plex, SickChill/SickRage, Radarr, Tautulli, Jackett, Transmission, and Prowl.
- `llm_providers.py`: the provider-agnostic LLM layer (official `openai`/`anthropic` SDKs, `httpx` for Ollama) behind a neutral internal tool-call format.

`tools/`

- `catalog.py`/`schemas.py`: the single source of truth for every tool — typed pydantic input models, when-to-use descriptions, and role/flag gating (admin-only, escalation-only).
- `bridge.py`: adapts the catalog to the LLM tool-call contract for the in-process chat agent (every call gets a result, including validation/handler errors).
- `server.py`: builds the same catalog as a real MCP (FastMCP) server for the `/mcp` endpoint and external clients.

`static/`

- Public UI assets such as icons and starter-card artwork.

## Tooling Model

The assistant only sees registered tool schemas. It does not receive API keys, internal credentials, or raw service access. User-scoped actions are bound to the authenticated session by the backend, not by model-supplied usernames or user IDs.

Normal request flow:

- Check Plex/Ombi state.
- Request movies/shows through Ombi.
- Use exact movie title plus year or TMDB ID for movie requests.
- Use scoped TV requests to avoid accidentally requesting entire large shows.

Support/repair flow:

- Use Ombi as request truth.
- Use Plex as current library truth.
- Use SickChill for TV acquisition repair.
- Use Radarr for movie acquisition repair.
- Notify the admin through Prowl for meaningful corrective actions or repair failures.

## MCP Endpoint

The same tool catalog (`tools/catalog.py`) that powers the in-process chat agent is
also exposed over the network as a real MCP (Model Context Protocol) server, so
external MCP clients — Claude Code, Overlord, other agents — can call it
directly. This is a separate surface from the chat path; it does not change how
in-app chat works.

It's mounted at `/mcp` (FastMCP streamable-HTTP transport) and is gated by two
static bearer tokens, both optional:

- `MCP_AUTH_TOKEN` — non-admin principal. Grants the same tools a regular signed-in
  user sees (search, requests, repairs, recommendations); admin-only and
  direct-source-gated tools are hidden.
- `MCP_ADMIN_TOKEN` — admin principal. Grants the full catalog, including
  admin-only tools (token usage, admin task summary, admin messages/MOTD).

If neither variable is set, `/mcp` is not mounted at all (a plain 404, not a
401) — the endpoint doesn't exist until you opt in. If only one token is set,
only that principal works; requests with a missing, wrong, or mismatched-tier
token get 401. Each configured token maps to its own gated FastMCP server built
from `tools/server.py:build_mcp_server`, sharing one real `ConversationStore` so
tool calls made over MCP persist state the same way chat does.

To connect Claude Code to a running instance, add to `.mcp.json`:

```json
{
  "mcpServers": {
    "plexorcist": {
      "type": "streamable-http",
      "url": "http://<host>:<port>/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_AUTH_TOKEN or MCP_ADMIN_TOKEN value>"
      }
    }
  }
}
```

## Shabbos Mode

> "I don't roll on Shabbos."

A per-account, deterministic, **AI-free** interface for users who don't want to
interact with a language model. They get a strict slash-command version of the
full non-admin surface — search, request, status, repair, issue reporting — with
**zero model involvement at runtime**: not in the request path, not in a
background job, not after the fact.

It works by being a *third consumer* of the same `tools/catalog.py` that powers
the chat agent and the MCP server, so it reuses the real tools, the real
permission gate, and the real service adapters — no duplicated request logic.

The guarantee is structural: the fork in `/api/chat` sits upstream of
`build_agent()`, so the Shabbos path is never handed an `LlmClient` at all. The
background memory compactor is guarded too, so a Shabbos conversation never
reaches a model even later.

Admin-only. Enable it by asking the assistant: *"turn on Shabbos Mode for
Richard."* Verify it with *"show me the Shabbos diagnostics"*, which reads the
real audit log rather than asserting a constant.

**See [`SHABBOS_MODE.md`](SHABBOS_MODE.md)** for the command reference, the
architectural guarantee, and the checklist for adding a command.

## Memory

Plexorcist has tiered memory (and Shabbos Mode users are permanently excluded
from it — see above):

- Tier 1: recent user notes, preferences, corrections, and open tasks.
- Tier 2: compacted per-conversation snapshots.
- Tier 3: older snapshots compacted into longer-term summary storage.

A background sweeper compacts stale conversations after `MEMORY_INACTIVITY_MINUTES`. The current conversation is left alone, and the compacted memory is injected into future chats as user-specific context. This gives the assistant continuity across browser sessions without preserving every raw browser chat as active context.

Memory is not a proactive reminder scheduler. If a user asks to be reminded next time, that note can appear in future chat context, but the app does not independently message the user later unless a tool path explicitly sends a notification.

## Token Usage

OpenAI token usage is recorded in SQLite by model, resolved model, input tokens, cached input tokens, output tokens, total tokens, user, conversation, and source. Admins can ask the assistant for current MTD/YTD usage and estimated raw cost for the configured OpenAI model.

The active provider/model is configured with:

```bash
LLM_PROVIDER=openai
LLM_MODEL=gpt-5-mini
```

`openai` remains the default configured provider. Supported configured values are `openai`, `anthropic`, and `ollama`. An administrator can save a global engine choice of the configured provider or `nvidia` from the admin controls; the choice is stored in SQLite and applies to new chat turns. The ordinary user UI does not gain provider, model, or cooldown controls.

NVIDIA uses the hosted chat-completions endpoint at `https://integrate.api.nvidia.com/v1/chat/completions`. It tries the approved preferred models in order, then falls back among eligible NVIDIA catalog models only for actual HTTP, transport, or invalid-response failures. A model that fails enters a shared 15-minute cooldown. NVIDIA inference has no read or total inference timeout, while connection and write timeouts remain bounded. It never automatically crosses back to the configured provider or replays executed tools.

Anthropic and Ollama translate between their chat/tool formats and the same internal response shape so the backend tool loop stays unchanged.

Provider-specific settings:

- OpenAI: `OPENAI_API_KEY`, optional `OPENAI_MODEL` fallback when `LLM_MODEL` is unset.
- Anthropic: `ANTHROPIC_API_KEY`, with `LLM_MODEL` set to a Messages API model.
- Ollama: `OLLAMA_BASE_URL` or `OLLAMA_HOST`, with `LLM_MODEL` set to a locally available chat/tool-capable model.
- NVIDIA: `NVIDIA_API_KEY`; optional `NVIDIA_MODEL_CATALOG` points to a local JSON catalog in the same format as the RosterOps catalog. No catalog snapshot is bundled or fetched automatically; if the file is absent or malformed, the approved preferred models remain available.

## Run Locally

```bash
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -e .
uvicorn backend.main:app --reload
```

Then open `http://localhost:8000`.

For local development, `AUTH_MODE=dev_impersonate` uses the development identity from `.env`. For production, use `AUTH_MODE=plex_oauth`.

## Configuration

Settings are environment-driven. Start from `.env.example` and provide real values locally or on the server.

Important groups:

- App/auth: `AUTH_MODE`, `SESSION_SECRET_KEY`, `PLEXORCIST_BASE_URL`, `ADMIN_USER_ID`, `ADMIN_DISPLAY_NAME`
- Plex OAuth: `PLEX_CLIENT_IDENTIFIER`, `PLEX_AUTH_PRODUCT_NAME`
- Ombi: `OMBI_BASE_URL`, `OMBI_CONTINUE_URL`, `OMBI_API_KEY`
- Media services: `PLEX_BASE_URL`, `SICKCHILL_BASE_URL`, `RADARR_BASE_URL`, `TAUTULLI_BASE_URL`, `JACKETT_BASE_URL`, `TRANSMISSION_HOST`, `TRANSMISSION_MAINTENANCE_VERIFY_WAIT_SECONDS`
- Notifications: `PROWL_API_KEY`, `LOGIN_NOTIFY_ENABLED`, `LOGIN_NOTIFY_SCOPE`
- LLM provider: `LLM_PROVIDER`, `LLM_MODEL`, `LLM_REQUEST_TIMEOUT_SECONDS`
- OpenAI compatibility: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_REQUEST_TIMEOUT_SECONDS`
- Anthropic: `ANTHROPIC_API_KEY`
- Ollama: `OLLAMA_BASE_URL`, `OLLAMA_HOST`
- NVIDIA: `NVIDIA_API_KEY`, `NVIDIA_MODEL_CATALOG`
- Memory: `MEMORY_INACTIVITY_MINUTES`, `MEMORY_COMPACTION_TIMEOUT_SECONDS`, `MEMORY_RECENT_NOTES_LIMIT`, `MEMORY_TIER1_KEEP`
- MCP endpoint: `MCP_AUTH_TOKEN`, `MCP_ADMIN_TOKEN` (see [MCP Endpoint](#mcp-endpoint); unset = `/mcp` disabled)

Secrets, local DBs, logs, friendly-name data, deployment helpers, and scratch artifacts are intentionally ignored by Git.

## Production

The project includes:

- `run_gunicorn.sh` for starting Gunicorn with the project config.
- `gunicorn.conf.py` for the ASGI worker, bind, worker count, and timeouts.
- `deploy.example.sh` as a template for private deployment scripts.

Local/private deployment uses `deploy.local.sh`, which is intentionally ignored by Git because it contains machine-specific paths.

The deploy helper should sync the repo root with `.rsync-filter` exclusions, not a hand-maintained allowlist. If you add local-only files or folders, put them in [`.rsync-filter`](/home/ben/Projects/plexorcist/.rsync-filter) instead of editing the rsync command.

If the service fails to start after deploy, check that any new runtime module was actually included in the sync and then read the server-side `plexorcist.log` before assuming the database is at fault.

**See [`DEPLOY_NOTES.md`](DEPLOY_NOTES.md)** for the server venv/pip gotchas and
the FreeBSD Rust-toolchain workaround needed for some dependencies — required
reading before running `pip install` on the production host.

## Status

The original scaffold goals are mostly complete:

- Real Ombi request/search paths are implemented.
- Plex OAuth session handling is implemented.
- Conversation state now tracks media context, pending actions, memory, and support context.
- Service clients are implemented for the active media stack.
- The LLM tool-calling loop operates against bounded real tools, with OpenAI, Anthropic, and Ollama provider adapters implemented behind `clients/llm_providers.py`.

Remaining work is mostly refinement rather than foundational plumbing: more diagnostics, better ranking/disambiguation, UI polish, and continued tuning of assistant behavior.

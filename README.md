# Plexorcist Concierge

Plexorcist Concierge is a FastAPI/OpenAI chat front door for a Plex request stack. It lets users ask for movies, shows, recommendations, missing-episode checks, and repair actions in normal language while keeping privileged media operations behind bounded server-side tools.

The assistant talks to real service adapters for Ombi, Plex, SickChill/SickRage, Radarr, Tautulli, Jackett, Transmission, and Prowl. The model cannot make arbitrary API calls; it can only invoke the registered tools exposed by the backend.

## Current Capabilities

- Plex OAuth login with server-side session identity.
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

- `main.py`: FastAPI routes, app startup, tool registration, auth callback, static UI, memory sweeper.
- `agent.py`: OpenAI Responses API orchestration, prompt assembly, tool-call handling, admin alert behavior.
- `auth_context.py`: user context providers for development, Plex OAuth, Ombi session, and header passthrough modes.
- `auth_store.py`: Plex OAuth session persistence.
- `config.py`: environment-backed settings.
- `logging.py`: structured audit logging.
- `models.py`: shared request, response, user, and conversation models.
- `policy.py`: request/repair policy helpers.
- `state.py`: SQLite storage for conversations, memory, token usage, and support state.

`clients/`

- Service adapters for Ombi, Plex auth, Plex, SickChill/SickRage, Radarr, Tautulli, Jackett, Transmission, Prowl, and OpenAI.

`tools/`

- Narrow backend operations exposed to the assistant, grouped around media search, requests, repairs, recommendations, and escalation.

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

## Memory

Plexorcist has tiered memory:

- Tier 1: recent user notes, preferences, corrections, and open tasks.
- Tier 2: compacted per-conversation snapshots.
- Tier 3: older snapshots compacted into longer-term summary storage.

A background sweeper compacts stale conversations after `MEMORY_INACTIVITY_MINUTES`. The current conversation is left alone, and the compacted memory is injected into future chats as user-specific context. This gives the assistant continuity across browser sessions without preserving every raw browser chat as active context.

Memory is not a proactive reminder scheduler. If a user asks to be reminded next time, that note can appear in future chat context, but the app does not independently message the user later unless a tool path explicitly sends a notification.

## Token Usage

OpenAI token usage is recorded in SQLite by model, resolved model, input tokens, cached input tokens, output tokens, total tokens, user, conversation, and source. Admins can ask the assistant for current MTD/YTD usage and estimated raw cost for the configured model.

The model is configured with:

```bash
OPENAI_MODEL=gpt-5.4-mini
```

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
- Media services: `PLEX_BASE_URL`, `SICKCHILL_BASE_URL`, `RADARR_BASE_URL`, `TAUTULLI_BASE_URL`, `JACKETT_BASE_URL`, `TRANSMISSION_HOST`
- Notifications: `PROWL_API_KEY`, `LOGIN_NOTIFY_ENABLED`, `LOGIN_NOTIFY_SCOPE`
- OpenAI: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_REQUEST_TIMEOUT_SECONDS`
- Memory: `MEMORY_INACTIVITY_MINUTES`, `MEMORY_COMPACTION_TIMEOUT_SECONDS`, `MEMORY_RECENT_NOTES_LIMIT`, `MEMORY_TIER1_KEEP`

Secrets, local DBs, logs, friendly-name data, deployment helpers, and scratch artifacts are intentionally ignored by Git.

## Production

The project includes:

- `run_gunicorn.sh` for starting Gunicorn with the project config.
- `gunicorn.conf.py` for the ASGI worker, bind, worker count, and timeouts.
- `deploy.example.sh` as a template for private deployment scripts.

Local/private deployment uses `deploy.local.sh`, which is intentionally ignored by Git because it contains machine-specific paths.

## Status

The original scaffold goals are mostly complete:

- Real Ombi request/search paths are implemented.
- Plex OAuth session handling is implemented.
- Conversation state now tracks media context, pending actions, memory, and support context.
- Service clients are implemented for the active media stack.
- The OpenAI tool-calling loop operates against bounded real tools.

Remaining work is mostly refinement rather than foundational plumbing: more diagnostics, better ranking/disambiguation, UI polish, and continued tuning of assistant behavior.

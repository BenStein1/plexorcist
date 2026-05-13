# Plexorcist Concierge

Thin conversational concierge in front of Ombi and the existing Plex request stack.

## Current state

This repository is scaffolded around the project brief:

- FastAPI backend with a minimal mobile-friendly chat page
- switchable auth-context provider with a single-user development shim
- bounded tool registry instead of arbitrary API access
- OpenAI Responses API conversation loop with function calling
- conversation state persisted in SQLite
- policy layer for TV scope decisions
- stub client wrappers for Ombi, SickChill, Plex, Tautulli, Jackett, Transmission, and Prowl
- structured audit logging for user turns and tool actions

## Run

```bash
cp .env.example .env
source .venv/bin/activate
pip install -e .
uvicorn backend.main:app --reload
```

Then open `http://localhost:8000`.

In `AUTH_MODE=dev_impersonate`, the app impersonates one fixed development user from `.env`.

Real deployment should switch to `AUTH_MODE=plex_oauth` and use the Plex sign-in flow. The home page will show a Plex login button until the session is established.


## Architecture

`backend/`

- `main.py`: app entrypoint and route wiring
- `agent.py`: conversational orchestration using only bounded tools
- `auth_context.py`: trusted user identity placeholder
- `policy.py`: request and escalation policy logic
- `state.py`: SQLite-backed conversation state
- `logging.py`: structured audit logging

`clients/`

- one stub client per upstream system

`tools/`

- narrow safe wrappers exposed to the agent

## Design notes

The important rule from the brief is already encoded in the structure: the agent cannot make arbitrary API calls. It only sees purpose-built tool functions such as `search_media`, `request_show_scope_for_user`, and `check_episode_status`.

The conversation layer is now intended to run through an OpenAI model via the Responses API, with your local tools exposed as function calls. That gives you a normal chat-style bot while keeping all privileged operations behind bounded server-side wrappers.

The request path is intentionally split from the support path:

- normal movie/show requests write through Ombi
- troubleshooting checks SickChill and related state
- missing movies check Ombi first, then escalate privately through Jackett and Transmission only if the movie already exists as a request but is still absent
- manual SickChill searches are support-only retries
- broader Jackett and Transmission escalation remains private and policy-driven

That means the next implementation passes should focus on:

1. replacing each stub client with real service adapters
2. hardening auth so user identity is derived server-side
3. expanding the policy engine before making the agent smarter
4. adding real recommendation and missing-episode diagnostics

## Immediate next steps

1. Implement real Ombi search/request endpoints and request attribution.
2. Replace the demo auth headers with Plex OAuth session handling.
3. Expand `ConversationState` to track pending media disambiguation and repeated issue timing for Tier 2 and Tier 3 eligibility.
4. Replace the remaining stub service clients so the OpenAI tool-calling loop can operate on real Ombi, SickRage, PlexPy, Jackett, and Transmission data.

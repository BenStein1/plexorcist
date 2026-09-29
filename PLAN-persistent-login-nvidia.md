# Persistent Plex login and selectable NVIDIA engine

## Approved outcome

Remember each device for 180 days after a successful Plex login. Preserve the
existing AI provider and add NVIDIA with RosterOps' preferred model list and
catalog fallback policy. Only administrators see new controls, model details,
or cooldown information. Ordinary users keep the existing interface, transcript,
loading indicator, and error presentation; persistence and transport changes
operate underneath it. Shabbos remains entirely AI-free.

Implement with native Luna agents at medium reasoning, not bridge dispatch.
Test and commit locally. Do not push, deploy, restart services, access live data,
or modify RosterOps. Preserve pre-existing HANDOFF.md, PROJECT.md and archive/
changes. Deployment is a separate owner-approved step using deploy.local.sh.

## Authentication

- Retain the HMAC-signed opaque cookie and SQLite sessions. Give the authenticated
  cookie Max-Age/Expires for 180 days, HttpOnly, SameSite=Lax, path /, and Secure
  when the configured public URL is HTTPS. Pending PIN cookies remain temporary.
- Enforce matching server expiry using updated_at (last successful login), not
  created_at. Reauthentication renews the period; normal traffic does not.
- Upgrade a valid existing browser-session cookie using its remaining lifetime.
  Preserve logout and blocklist enforcement. Do not delete expired identity rows
  merely for expiry because admin lookup tools use this table.
- Preserve the configured signing secret across workers and restarts.

## Provider and controls

- Extend clients/llm_providers.py through its existing neutral LlmResponse and
  ToolCall contract; preserve OpenAI Responses and the backend agent/tool loop.
- NVIDIA preferred order and exact request profiles come from current passive
  RosterOps source: moonshotai/kimi-k3,
  nvidia/nemotron-3-ultra-550b-a55b, nvidia/nemotron-3-super-120b-a12b,
  z-ai/glm-5.3, z-ai/glm-5.3-flash.
- Adapt RosterOps catalog filtering locally: free/listed general text reasoning
  models above 30B parameters, compatible chat/tool capability, specialist
  exclusions, descending size, deduplication. A missing/malformed catalog leaves
  preferred models available. Support a configurable catalog path and a packaged
  snapshot when the authorized source is available. Never import RosterOps code.
- Use server-side NVIDIA_API_KEY and the hosted NVIDIA chat-completions endpoint.
  Preserve native tool turns, tool IDs/results, and actual model usage reporting.
- Add an admin-only panel and GET/PUT /api/admin/ai-engine. Require authenticated
  admin identity on both endpoints and same-origin JSON on writes. Choices are
  configured (existing environment engine) and nvidia (automatic fallback).
- Store the global choice in existing SQLite user_flags under __global__.
  Default to configured. Reject invalid or unconfigured selections without
  replacing the saved choice. Return sanitized availability/order/cooldown
  details only to admins; omit new controls and their JS from ordinary HTML.
- Snapshot selection once per chat turn or summary, before choosing timeout
  behavior. In-flight work retains its provider; new requests see saved changes
  across both application workers without a restart.

## LiteLLM model selection

- Add LiteLLM Proxy as a third admin-selectable engine without changing the
  configured provider or NVIDIA fallback. Use the existing OpenAI-compatible
  client path with a configurable LiteLLM base URL and server-side virtual key.
- Load available model IDs from the configured proxy for admins only; let the
  admin select and persist one model globally in existing user flags. Keep the
  key out of SQLite, HTML, API responses, logs, and Git.
- Require a configured proxy URL before enabling this engine. Treat a missing
  key as unauthenticated proxy access; never guess the URL or expose model
  metadata to ordinary users.
- Validate catalog retrieval, model selection, and client routing with focused
  tests. Keep Shabbos on its existing no-model path.

## Waiting and recovery

- User explicitly overrides RosterOps' 900-second attempt deadline: NVIDIA
  inference must take as long as needed. No read/total inference deadline or
  NVIDIA summary outer deadline. Retain bounded connection/write timeouts and
  existing deadlines for other providers. Cancellation is not model failure.
- Fail over only on actual HTTP/transport/invalid-response failures. Persist
  per-model 15-minute cooldowns in separate SQLite flags shared across workers.
  Preserve configured quality order, bounded error backoff, and Retry-After.
  401/403 are terminal credential errors. A 429 permits one alternate before
  returning temporary unavailability and respecting endpoint cooldown.
- Never automatically cross to the original provider or replay executed tools.
- Keep POST /api/chat JSON-compatible; browser opts into NDJSON. Send immediate
  and 15-second invisible heartbeats, then a final ChatResponse or sanitized
  error. Preserve ordinary rendering/spinner behavior, with no model details.
  Disable response buffering/cache where response headers can do so.
- Run one strongly referenced task using existing background-task tracking.
  Browser disconnect does not resend/cancel inference or duplicate tools; allow
  the task to finish and persist. Shutdown cancellation still propagates.

## Verification and delivery

- Isolated auth tests: Set-Cookie, restored cookie/new client, fixed expiry,
  renewal, existing-cookie upgrade, tampering, logout, blocklisting.
- Admin tests: both endpoints reject non-admins; ordinary HTML/JSON contains no
  new provider controls/details; selection survives new store instances and
  affects subsequent requests only; invalid saves preserve prior selection.
- Provider tests: config, preferred/catalog order/filtering, tool round trip,
  response validation, usage attribution, shared cooldown, 401/403, 429,
  exhaustion, missing catalog, and unchanged OpenAI/Shabbos behavior.
- Transport/summary tests: multiple heartbeats during slow inference without
  failover, correct final/error rendering, disconnect completion exactly once,
  and engine-specific summary deadline snapshot behavior.
- Run focused and relevant regression tests against temporary databases with
  mocked services and notifications disabled, plus git diff --check.
- Commit only task changes. Report commit IDs and evidence; distinguish local
  tests from unverified deployed browser/proxy/NVIDIA behavior. No deployment.

## Implementation ownership

- NVIDIA agent: provider implementation, local catalog adapter, provider tests.
- Auth/UI agent: auth, config, main route/UI wiring, engine storage helpers,
  keepalives, summary selection, and matching integration tests.
- Coordinator: save this plan first, agree provider interfaces, integrate local
  commits, review and run isolated checks. Agents commit before reporting done.

## Tool-call continuation repair

- Fix the shared provider-neutral tool-call continuation path used by LiteLLM
  Responses and NVIDIA Chat Completions. Preserve assistant native tool calls,
  IDs, JSON arguments, and structured tool results when building the next
  provider request; do not alter the existing configured-provider path.
- Reproduce the `TypeError: sequence item 0: expected str instance, NoneType
  found` from the live LiteLLM movie-request turn, identify and fix its source,
  and retain enough traceback context for any future unexpected chat failure.
- Add a focused adapter/agent round-trip regression check for request-tool
  calls on both LiteLLM and NVIDIA shapes. Do not run a live movie mutation as
  a test; confirm the prior request state before retrying it.
- Run focused provider and agent tests plus `git diff --check`. Commit the
  implementation locally; deployment/push remains a separate explicit action.

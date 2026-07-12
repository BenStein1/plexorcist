# Plexorcist HANDOFF

## 2026-07-10 — Onboarding rework + friends-only auth (DEPLOYED, awaiting restart)

### Status: DONE + deployed to webroot. Ben restarts the service himself.
Commits on `main`: `6dac10d`, `12435f9`, `71c2ca0`, `8d97cfa` (clean tree apart
from this file). `./deploy.local.sh` was run — files are synced to
`/mnt/NALA/.../apps/plexorcist`. **Next concrete step: Ben runs
`supervisorctl restart plexorcist` on 10.0.0.93 as root.** Nothing else pending.

### What changed and why
Problem: a new Plex user (e.g. `phillipconner642`, Apple-ID login) with no Ombi
account got punted to Ombi's broken self-registration and could never use
Plexorcist. Reworked the whole first-login flow.

- **No more Ombi punt.** When a shared user has no Ombi account, we let them in
  instantly and fire Ombi's Plex User Importer in the background
  (`OmbiClient.trigger_plex_user_importer`, fire-and-forget). Login never waits
  on Ombi. (`backend/main.py` plex_auth_callback.)
- **Friends-only, fail-closed gate.** Login gated on **live Plex sharing** via
  `PlexAuthClient.list_shared_users` (plex.tv `shared_servers`, bound to the
  admin's *owned* server, ~60s cache w/ forced refresh on miss). Matches on Plex
  user_id (robust for Apple-ID) or username/email. Depends on `PLEX_TOKEN` in
  prod `.env` (present, verified).
- **Unauthorized = inert 404 hole** (`_hole()`): non-shared OR blocked users get
  an identical identity-less 404, cookies cleared. No disclosure. The branded
  login landing page is intentionally LEFT UNCHANGED (Ben's call).
- **Admin = exactly one account.** Plex `/api/v2/user` returns no admin field;
  `is_admin` is purely `is_admin_identity(user_id)` == `ADMIN_USER_ID`. Ben's
  login exercises the full auth path; the shared-lookup branch is short-circuited
  for him (owner isn't in own shared list) and is covered by any friend login.
- **First-time welcome tour.** First-ever message (flag `onboarding_tour_seen`
  AND `store.has_conversation_history()` false) injects `extra_instructions`
  (`_build_welcome_tour_instructions`) — tour of requesting out-now/upcoming
  movies + TV, the repair/fix feature, relaying a message to admin, plus a strong
  nudge to set a friendly name via `set_my_friendly_name`. `/api/welcome` returns
  `first_time`; starter card shows a "Show me around" chip.
- **Request safety net.** `RequestTools._account_not_ready` returns a friendly
  "still finishing setup" (and re-nudges importer) if a brand-new user requests
  before the import lands; fail-open on check error.

### Verification
76/76 tests pass. Live-verified: plex.tv membership (phillip in, rando out),
importer (phillip already imported to Ombi), tour content, request safety-net,
inert 404. NOT driven end-to-end locally: the real auth callback + first tour
turn — **test post-restart by having phillip log in** (not Ben; admin skips the
shared check).

See memory `plexorcist-auth-model` and `plexorcist-deploy`.

## 2026-07-11 — Shabbos Mode (COMMITTED on branch `shabbos-mode`, NOT deployed)

### Status: feature complete, 128/128 tests pass. Ben is smoke-testing locally.
Branch `shabbos-mode`, commit `43be9c4`. `main` untouched, nothing pushed,
nothing deployed. Deploy stays Ben's explicit call — and the onboarding-rework
restart from the entry above is STILL outstanding.

### What it is
A per-account, deterministic, AI-free interface for users who won't use AI
(prompted by Ben's friend Richard, but it's for anyone). Strict slash commands
covering the full non-admin surface. **Read `SHABBOS_MODE.md` first.**

Key design: it is a **THIRD consumer of `tools/catalog.py`**, alongside
`tools/bridge.py` (LLM) and `tools/server.py` (MCP) — so it reuses the real
tools, the real pydantic validation, and the real `visible_specs` permission
gate. No duplicated Ombi/Radarr/SickChill logic.

- Flag: `user_flags.shabbos_mode` (no migration). Admin-only toggle via the new
  `set_shabbos_mode` catalog tool → *"turn on Shabbos Mode for Richard."*
- **The guarantee is structural**: the fork in `/api/chat` sits upstream of
  `build_agent()` (which constructs the LlmClient), so that path is never handed
  a model client. Enforced server-side; the UI is cosmetic.
- **The non-obvious leak, guarded**: `_compact_conversation_once` (memory
  sweeper) fed stored conversations to the LLM. Shabbos conversations are now
  skipped permanently, even if the user later leaves the mode.
- Admin tasks still work: the router writes `user_memory_notes` at command time,
  so these users still appear in `get_admin_task_summary`.

### Verified
128/128 pytest. Live run with a deliberately garbage `OPENAI_API_KEY`: every
Shabbos command worked; flipping the flag OFF in the same process made the
normal path fail with a 401 — proving the key really was invalid and that no
model was touched.

### INCIDENT (fixed) — the test suite sent real Prowl pushes to Ben's phone
A test patched `ProwlClient.notify` — a method that DOES NOT EXIST — with
`raising=False`, so the patch silently no-opped; that test's `Settings()` also
loaded the real `PROWL_API_KEY` from `.env`. Result: ~5-6 live pushes reading
`Richard (richard): [playback-error] ...`, which Ben mistook for the real Richard
and replied to. It also wrote `"richard": "Richard"` into the live
`friendlynames.json` (removed; 64 real entries intact).
**Fixed durably**: `tests/conftest.py` hard-blocks `ProwlClient.send_notice` (no
`raising=False`), forces `PROWL_API_KEY=""` and a throwaway `FRIENDLY_NAMES_PATH`;
`tests/test_no_live_side_effects.py` fails loudly if that method is ever renamed.
**Never patch a client method without checking it exists, and never let a test
read real `.env` secrets.**

### Next step
Ben is smoke-testing in a browser locally against a COPY of the DB with dev
impersonation. Note: the real Ombi (`https://ombi.benco.guru`) does not verify
from Ben's desktop (cert chain), so local runs use a stub Ombi/Plex; the real
stack only resolves from the prod host. After he's happy: merging to `main` and
deploying are his call.

### IN FLIGHT (2026-07-11 19:35) — two UX changes from Ben's smoke test
Ben tested at localhost:8000 and asked for exactly two things. Both are UX only;
the AI-isolation architecture is unchanged.

1. **Drop the confirmation CODE.** `/fix ...` should say "Confirm with:
   `/confirm`" — a bare `/confirm`, no token to type. Keep the two-step safety:
   the pending action is still stored server-side, still bound to the user, still
   single-use, still expires (5 min). Just consume it by user_id instead of by
   token. Touches `backend/shabbos/confirm.py` (`mint`/`consume` drop the token),
   `router.py` (`_run_confirm`, and the "Confirm with:" text), the confirm tests
   in `tests/test_shabbos_commands.py`, `/confirm` usage in `commands.py`, and the
   command reference in `SHABBOS_MODE.md`.
2. **Up-arrow command history**, like a real CLI. Up/Down cycles previously sent
   commands in the composer. Shabbos-only — it belongs in
   `_SHABBOS_STARTER_CARD_JS` / the composer keydown handler in `backend/main.py`
   (the Shabbos branch of `index()`), NOT in the concierge UI.

### Next step
Implement those two, re-run `.venv/bin/python -m pytest tests/ -q` (128 green
before this change), and have Ben re-test at localhost:8000. Then merging to
`main` + deploy are his call.

### How to bring the local smoke-test env back up
Real app, real router/tools/renderers; only Ombi/Plex are stubbed (the real Ombi
is https and its cert will NOT verify from Ben's desktop — `curl` fails too, so
don't chase it). Scratchpad dir `SP` holds `fake_ombi.py`, `dev_richard.json`,
`smoke.db` (a COPY of the live DB, test user richard/9001 with the flag ON).

```bash
SP=/tmp/claude-1000/-home-ben/d28eb116-9931-40d6-b944-ace78dbb3abc/scratchpad
.venv/bin/uvicorn --app-dir $SP fake_ombi:app --port 8898 --log-level error &
AUTH_MODE=dev_impersonate DEV_IMPERSONATION_STORE=$SP/dev_richard.json \
DATABASE_URL=sqlite:///$SP/smoke.db \
OMBI_BASE_URL=http://127.0.0.1:8898 PLEX_BASE_URL=http://127.0.0.1:8898 \
PROWL_API_KEY= OPENAI_API_KEY=sk-GARBAGE-ON-PURPOSE \
.venv/bin/uvicorn backend.main:app --port 8000 &
```
`PROWL_API_KEY=` empty and the garbage OpenAI key are deliberate: no phantom
pushes, and any model call would 401 loudly. If the scratchpad is gone, recreate
the stub from this recipe rather than pointing at real Ombi.

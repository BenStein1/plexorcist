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

## Session checkpoint (auto: session (5-hour) usage at 94.0%) — 2026-08-03 13:43 MST
The session (5-hour) usage cap is at 94.0% and resets in ~3h 56m. When it hits
100%, the current turn is cut off. cwd: /home/ben/Projects/plexorcist.
ADMIN -> USER MESSAGING: FIXED, DONE, VERIFIED. Committed locally, NOT pushed,
NOT deployed. Nothing is in flight — the only remaining action is Ben's deploy.

Origin: Ben sent an admin message to rmk1900 who never saw it. Two prod bugs
(diagnosed against the live DB/log, see memory plexorcist-admin-message-delivery):
delivery only ever fired on a chat turn, and mark-read was unconditional while
"delivery" was just a soft prompt asking the model to relay. Four commits on
top of e0a1189:

- c562b69  deterministic verbatim delivery in code; killed the LLM soft-prompt
           relay in agent.py:_build_admin_notices_text and the unconditional
           mark-read. Worker Johnny (sonnet).
- 26ced07  two more tests. Johnny.
- e1cdb6f  fixes round 1's blind spot. Johnny.
- 3e97693  the two residual side effects of e1cdb6f. Me (opus), at Ben's
           "just fix it with your big brain".

THE DURABLE LESSON (round 1 was green-tested and still broken): the web UI is an
inline JS string inside backend/main.py. /api/welcome (~1802) renders
`data.message`, but /api/chat (~1919-1923) renders `data.state.messages` and
NEVER reads `data.reply`. Round 1 appended the block to `reply` after
store.save(state), so both chat paths showed the user nothing while still
marking the note read — the original burn one layer down — and all 7 tests were
green because every assertion was on response.reply. Before believing any
delivery fix here, check which field the CLIENT actually renders.

THE INVARIANT everything now holds to: a note is only marked read once its text
is inside a SAVED state.messages. The block is appended as its own ChatMessage,
never written into the agent's trailing message — NILBOG's
nilbog_redacted_message_index (main.py:2360 + agent.py:883) would swap that
content out on later turns.

What 3e97693 changed: /api/welcome now attaches the block to the user's newest
conversation via _persist_admin_block_at_welcome() instead of creating a
standalone one (prune_user_conversations(keep=2) deleted the standalone first,
since it was never written to again); and has_conversation_history() now
requires a USER-role turn, so an admin message pushed at a brand-new user can't
cost them the "Show me around" tour.

VERIFICATION: full suite 141 passed. Both new tests proven to bite — stashed
backend/main.py + backend/state.py back to e1cdb6f with the tests in place and
both failed. Earlier, at 26ced07, reverting main.py+agent.py to e0a1189 gave 5
of 7 failed. Tree clean at 3e97693.

NEXT STEP — Ben's, not an agent's: deploy manually (deploy.local.sh +
supervisorctl restart plexorcist as root, via NALA jexec 5 — memory
plexorcist-deploy). Prod notes 484 (Jeff) and 517 (rmk1900) are deliberately
still unread and deliver on their next login after that. The other 6 were
dismissed at Ben's instruction and marked read with dismissed_by_admin metadata;
prod DB backed up first at prod-backup-20260803-130824.db (gitignored),
integrity_check ok. Do NOT touch the prod DB again — that authorization was for
that one write only.

UNRELATED, STILL OPEN, DO NOT ACT ALONE: overlord-bridge.service has been
inactive AND disabled since 2026-07-30 19:29 with 14 stale AutoResume dispatch
files queued (4 Overlord_v2 Jul 30, 10 migraine-log-agent Jul 31). Restarting it
fires all 14 at once. Ben has not answered whether the stop was a deliberate
kill switch or an unnoticed failure, or whether to clear the queue. Ask first.

AUTO-RESUME ARMED: overlord-resume-autoresume-dd06be3a.timer (fires ~5 min after the session (5-hour) cap resets,
continues the work in /home/ben/Projects/plexorcist from this handoff).
Cancel with: systemctl --user disable --now overlord-resume-autoresume-dd06be3a.timer
If the work in flight lives somewhere else, add a line:  RESUME-FOLDER: /abs/path

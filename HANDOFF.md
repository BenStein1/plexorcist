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

## Session checkpoint (auto: session (5-hour) usage at 98.0%) — 2026-08-03 14:18 MST
The session (5-hour) usage cap is at 98.0% and resets in ~3h 21m. When it hits
100%, the current turn is cut off. cwd: /home/ben/Projects/plexorcist.
DEPLOYED AND RESTARTED 2026-08-03 14:19 MST, at Ben's explicit "deploy and
restart it please". The admin->user messaging fix is now LIVE in prod. Nothing
is in flight. See the checkpoint directly above for the full technical story
(4 commits c562b69 / 26ced07 / e1cdb6f / 3e97693, the invariant, and the
UI-renders-state.messages-not-reply trap that made round 1 a green-tested
no-op). Do not re-derive any of it.

What was done this turn:
- ./deploy.local.sh rsynced the repo to
  /mnt/NALA/.../apps_webroot/apps/plexorcist (backend/main.py, backend/state.py,
  backend/agent.py, tests). Local rsync over the NFS mount; no ssh needed.
- net_ssh to root@10.0.0.12 (NALA), jexec 5 (jail sandbox_1),
  `supervisorctl restart plexorcist`: pid 94541 (11d uptime) -> 94915. Session
  CLOSED afterwards. NOTE: that ssh clearance was ONE TIME for that request and
  does NOT carry forward — ask Ben again before any future connection.
- Verified live, not assumed: new code present on the prod host (3 hits for
  _persist_admin_block_at_welcome in backend/main.py, 2 for json_each in
  backend/state.py), gunicorn workers 94920/94921 up and stable on 0.0.0.0:5500,
  app answering HTTP (unauthenticated request returns the inert 404, which is
  the auth gate behaving correctly), supervisor RUNNING, no crash loop.

NEXT STEP: nothing to build or deploy. Prod notes 484 (Jeff) and 517 (rmk1900)
are still unread by design and will now be delivered on those users' next login.
If Ben wants confirmation it worked, the check is whether those two notes flip
to read with a real delivery behind them — but do NOT write to the prod DB; that
earlier authorization covered one specific write and is spent.

STILL OPEN, UNANSWERED, DO NOT ACT ALONE: overlord-bridge.service has been
inactive AND disabled since 2026-07-30 19:29 with 14 stale AutoResume dispatch
files queued (4 Overlord_v2 Jul 30, 10 migraine-log-agent Jul 31). Restarting it
fires all 14 at once. Ben has not said whether the stop was a deliberate kill
switch or an unnoticed failure. Ask before touching it. (His "restart it" this
turn meant plexorcist — it was bound to "deploy" — not the bridge.)

AUTO-RESUME ARMED: overlord-resume-autoresume-7bf3bbbf.timer (fires ~5 min after the session (5-hour) cap resets,
continues the work in /home/ben/Projects/plexorcist from this handoff).
Cancel with: systemctl --user disable --now overlord-resume-autoresume-7bf3bbbf.timer
If the work in flight lives somewhere else, add a line:  RESUME-FOLDER: /abs/path

## Session checkpoint (auto: session (5-hour) usage at 100.0%) — 2026-08-03 14:22 MST
The session (5-hour) usage cap is at 100.0% and resets in ~3h 17m. When it hits
100%, the current turn is cut off. cwd: /home/ben/Projects/plexorcist.
NOTHING IN FLIGHT. Plexorcist admin->user messaging is fixed, committed,
deployed and restarted — see the two checkpoints directly above for the full
story and the verification. Do not redo any of it.

The only thing that happened this turn: Ben told me, with feeling, that he has
no idea what "the bridge" is and to stop bringing it up. He is right — the
overlord-bridge Telegram relay is MY plumbing, not his work, and I raised it
three turns running while he was trying to ship a fix. Dropped. Do NOT restart
overlord-bridge.service or clear its 14 stale AutoResume dispatches: "just do
the things" from someone who just said they don't know what the thing is, is
not consent to fire 14 days-old workers at his phone. If it matters later,
raise it once, in plain words, when he is not mid-task.

LESSON WORTH KEEPING: don't end a delivery report with an unrelated internal
question. Report the thing he asked for, then stop.

NEXT STEP: none. Wait for Ben.

AUTO-RESUME ARMED: overlord-resume-autoresume-8e6a6ee2.timer (fires ~5 min after the session (5-hour) cap resets,
continues the work in /home/ben/Projects/plexorcist from this handoff).
Cancel with: systemctl --user disable --now overlord-resume-autoresume-8e6a6ee2.timer
If the work in flight lives somewhere else, add a line:  RESUME-FOLDER: /abs/path

## 2026-08-03 — Admin user lookup can only see people who have LOGGED IN (root cause found, fix in progress)

Ben: "send an admin message to Mike Young" -> "I couldn't find a user matching
Mike Young." Then "take a look at the friendly names for Mike" -> also empty.

ROOT CAUSE (measured against the prod DB, read-only copy):
`AdminTools._load_user_labels()` (tools/admin_tools.py:610) builds the ENTIRE
user universe from `plex_auth_sessions` alone — i.e. only people who have
actually logged into Plexorcist. Prod has **15 distinct users** there. But
`friendlynames.json` has **63 people**, including `mwco8` -> "Mike" and
`Baldguy` -> "Mike and Nicole". Zero mike/young rows exist in
plex_auth_sessions. So Mike is invisible to every admin lookup even though Ben
has a friendly name on file for him. This is not a matching bug — 48 of Ben's
63 known people cannot be found at all.

SECOND, SMALLER BUG: `_resolve_user_query()` (line 645) matches exact-equal,
then plain substring `query in value`. "Mike Young" as one substring can never
match a value of "Mike". Needs token matching.

THE FIX:
1. `_load_user_labels()` — union of plex_auth_sessions AND the friendly-names
   ledger (keyed by username, case-insensitive). Ledger-only people get no
   user_id and a `has_account: False` marker.
2. `_resolve_user_query()` — add a token pass (all query tokens found across the
   candidate's values), and when the single match has `has_account: False`,
   return reason `user_not_registered` with a summary that SAYS SO
   ("Mike (mwco8) is in your friendly-names list but has never logged into
   Plexorcist, so there's no account to attach a message to") instead of the
   misleading "I could not find a user matching Mike Young."
3. Tests for both.

NOTE: send_admin_message writes user_memory_notes keyed by user_id, so a
ledger-only person genuinely cannot receive a stored message yet — the win is
an honest, actionable answer instead of a false "no such user."

Prod DB was READ ONLY (copied to a scratch file). Do not write to it.

## 2026-08-03 — Admin user lookup FIXED and DEPLOYED (f45d008 … 2238b44).

Both halves of Ben's transcript now work. Full suite 153 green (was 141).

**f45d008 — resolution.**
- `_load_user_labels()` is now the UNION of `plex_auth_sessions` and the
  friendly-names ledger. Ledger-only people carry `has_account: False` and an
  empty `user_id`, keyed by a synthetic `"ledger:<username>"` dict key that is
  deliberately neither searchable text nor stampable onto a result (the old
  `{**label, "user_id": user_id}` would have written that key into
  `user_memory_notes` as a real recipient id — a note rotting against nobody
  while Ben is told it was sent).
- `_resolve_user_query()` gained two passes after exact/substring: all-tokens
  (auto-select) and any-token (offered as "Closest I have: …", never
  auto-selected — one shared token is a guess).
- New kwarg `require_account=True` (fail closed). Callers that key durable
  state by user_id can never key to `""`; they get `reason=user_not_registered`
  with a summary that says the person has never logged in.
  `set_user_friendly_name` passes `require_account=False` — it keys off
  username, and renaming is how a ledger-only entry gets a searchable name.
- `_format_user_label` no longer renders `"Mike (mwco8, )"` for an empty id.
- New: `FriendlyNameDirectory.all_names()` in backend/auth_context.py.

**4f66b6f — new admin tool `find_users(query, limit)`.** There was no way to
BROWSE the roster at all, only to resolve one person or fail — which is why
"take a look at the friendly names for Mike" had nothing to call. Wired at all
four points (AdminTools method, ToolKit handler + is_admin guard in
tools/catalog.py, ToolSpec, `schemas.FindUsersInput`). Verified visible to an
admin (34 tools) and hidden from a normal user (23).

Measured against the real ledger:
- `"Mike Young"` -> "No match. Closest I have: Mike and Nicole (Baldguy) [no
  account yet], Mike (mwco8) [no account yet]. Which one?"
- `"Mike"` -> "Mike (mwco8) is in your friendly-names list but has never logged
  into Plexorcist, so there's no account to attach this to."
- `find_users("Mike")` -> both Mikes.

**b1ecd7e — the prompt, or none of the above ever runs.** Both halves of the
transcript were tool-SELECTION failures, not only resolution failures: nothing
told the model to widen a not-found lookup, and nothing told it that "take a
look at the friendly names for Mike" is a tool call — so it answered from
memory ("I poked the name ledger and came up empty"), which reads authoritative
and is not. Three lines added to the `Admin messaging:` block in
backend/agent.py (~line 1384): call `find_users` before reporting nobody, treat
"who do I have on file" as a tool call, and report a ledger-only person as
"[no account yet]" rather than unknown. Its test renders the real admin system
prompt via `build_agent`, so it fails if the block is edited away.

16 new tests in tests/test_admin_user_lookup.py; 9 of them proven to fail
against the pre-fix code (revert-and-run, restored). Full suite 157 passed.

Also checked, no change needed: the other three `_load_user_labels()` callers
(get_admin_task_summary, resolve_admin_task, the task_query recipient lookup)
index it by a real `user_id` from task rows, so the new synthetic
`ledger:<username>` keys can never be hit there.

**855a6f8 + 2238b44 — say "never logged in", don't ask an unanswerable
question.** Ben's real recipient was "Mike and Nicole" (`Baldguy`), ledger-only.
Ending a candidate list with "Which one?" invites him to pick someone who gets
refused on the very next turn, so he has to ask twice to learn the actual state.
New `_pick_or_dead_end()` in tools/admin_tools.py: when NOTHING in the shown list
has an account and the caller requires one, the closing sentence is the truth
instead of a question. Applied on both the suggestion and the ambiguity paths.
2238b44 fixes the singular arm, which read "They have ever logged into
Plexorcist" — not English, and it asserts the opposite of what it means.

Now measured against the real 64-entry ledger + a read-only prod DB copy:
- `"Mike Young"` -> "No match. Closest I have: Mike and Nicole (Baldguy) [no
  account yet], Mike (mwco8) [no account yet]. **Neither has ever logged into
  Plexorcist, so there's no account to send this to.**"
- `"Mike and Nicole"` / `"Baldguy"` -> the same answer, singular.

Claim verified before shipping, since it asserts something about real people:
dumped all 15 distinct `plex_auth_sessions` usernames and cross-checked them
against the ledger. None of them is Mike or Nicole (nearest neighbours are
`rmk1900` -> "Kat and Ryan", `DirtyCopper` -> "Chris"), so "never logged in" is
true and not a dedupe-key artifact.

DEPLOYED 2026-08-03. `./deploy.local.sh` (note: `.rsync-filter` excludes
friendlynames.json and *.db, so prod's live ledger is never overwritten by the
stale repo copy), then `jexec 5 supervisorctl restart plexorcist` on NALA.
Proof, not just a status line: pid 94915 -> 11397, new strings grep-confirmed in
the jail at /mnt/webroot/apps/plexorcist, three workers listening on :5500,
`curl http://127.0.0.1:5500/` -> 200. net_ssh session closed. That clearance is
now spent.

KNOWN GAP, deliberately not built — Ben was asked and said "That's fine": a
ledger-only person still cannot RECEIVE a message (`send_admin_message` keys
`user_memory_notes` by user_id). Making that work means keying by username and
reconciling on first login — a real feature. The lookup now just says so
plainly, which is all he wanted.

ALSO LOGGED THIS TURN (not started, Ben said "as a todo"): SickChill never
finishes adding a show — Neuromancer added days ago is still a "Loading..."
placeholder row, as is "Stuart Fails to Save the Universe". Written up in
~/Projects/overlord-bridge/OVERLORD_BACKLOG.md.

## Session checkpoint (auto: session (5-hour) usage at 87.0%) — 2026-08-03 19:44 MST
The session (5-hour) usage cap is at 87.0% and resets in ~3h 45m. When it hits
100%, the current turn is cut off. cwd: /home/ben/Projects/plexorcist.
RESOLVED — SickChill work is COMPLETE as of 2026-08-04. Nothing is in flight.
Plexorcist admin-message work was already done/committed (2238b44, 0b07bd2).

All SickChill fixes are live on **sickchill-ub / 10.0.0.94**, daemon restarted
and verified, net_ssh session closed (that clearance is spent — ask Ben before
reconnecting). Five faults fixed, fully written up ON THAT BOX at
`/opt/sickchill/lib/python3.10/site-packages/sickchill/show/indexers/PATCHES-20260803.md`
— read that file first if this comes back:
  1. TheTVDB retired `/search/series?name=` (404 for everything) -> slug lookup.
  2. `tvdbsimple` sent every request with no timeout; one dead socket wedged the
     whole show queue since 2026-07-28 -> `_TimeoutRequests` shim.
  3. `sb.searchindexers` returned HTTP 500 on zero matches -> dedented return.
  4. Fuzzy title search restored via TVmaze (name->tvdb id only; all data still
     from TheTVDB), and null-`seriesName` rows filtered out.
  5. SickChill's `[. -_]` separator class is a RANGE (0x20-0x5F) that ate every
     capital and digit, so "Bear" searched as "ear" -> fixed to `[.\- _]`, and
     `set()` swapped for `dict.fromkeys()` so variant order is deterministic.

Verified live: "Bear" -> The Bear (403294) top of 8 real rows, no null row;
junk title -> 0 rows / HTTP 200; exact checkbox still strict. Both originally
stuck shows exist with 10 episodes each (435047, 465664), 840 shows total.

ONLY OPEN RISK: `auto_update = 1` on that box will silently revert every patch
on a SickChill upgrade. Symptoms: adds stop finding titles, partial titles stop
matching, `null` rows return, or the show queue goes quiet. Re-apply from the
PATCHES file. Known-not-fixed: `^t?t?\d{7,8}$` is tested before `^\d{6}$`, so a
7-digit *TVDB* id is misrouted to the imdbId branch.

AUTO-RESUME ARMED: overlord-resume-autoresume-d9f484f5.timer (fires ~5 min after the session (5-hour) cap resets,
continues the work in /home/ben/Projects/plexorcist from this handoff).
Cancel with: systemctl --user disable --now overlord-resume-autoresume-d9f484f5.timer
If the work in flight lives somewhere else, add a line:  RESUME-FOLDER: /abs/path

## Session checkpoint (auto: session (5-hour) usage at 91.0%) — 2026-08-03 19:46 MST
The session (5-hour) usage cap is at 91.0% and resets in ~3h 43m. When it hits
100%, the current turn is cut off. cwd: /home/ben/Projects/plexorcist.
<!-- TODO: model should replace this line with what's actually in flight, -->
<!-- what was just decided, and the concrete next step, before continuing. -->
AUTO-RESUME ARMED: overlord-resume-autoresume-6c0eac99.timer (fires ~5 min after the session (5-hour) cap resets,
continues the work in /home/ben/Projects/plexorcist from this handoff).
Cancel with: systemctl --user disable --now overlord-resume-autoresume-6c0eac99.timer
If the work in flight lives somewhere else, add a line:  RESUME-FOLDER: /abs/path

---

## 2026-08-04 — SickChill: DONE & VERIFIED. Ombi->Plexorcist: two new bugs found.

### SickChill (10.0.0.94) — COMPLETE, verified in Ben's real workflow

Seven faults found and fixed across two rounds; full technical write-up lives on
that box at
`/opt/sickchill/lib/python3.10/site-packages/sickchill/show/indexers/PATCHES-20260803.md`
(251 lines). Summary of round 2:

- Fuzzy/partial title search restored via TVmaze name->id bridge (TheTVDB
  retired `?name=`; slugs are exact-only). "altered carb" -> Altered Carbon works.
- `search()` no longer breaks on a slug hit — slug + fuzzy results are merged and
  deduped, so "Altered" returns both *Altered* (425966) and *Altered Carbon*
  (332331).
- Dropped TVDB records with a null `seriesName` (the unpickable `null` row).
- Fixed `[. -_]` — that is a character RANGE 0x2E-0x5F, so searching "Bear" was
  literally searching "ear".

**The Ombi question is answered: it was never a separate bug.** Ombi adds by TVDB
id, which goes `show.addnew?tvdbid=N -> CMDSickChillSearchIndexers (id branch) ->
get_series_by_id -> /series/{id}` and never touches the broken title search.
`add_show()` is fire-and-forget, so `addnew` returned SUCCESS the instant the item
was queued — Ombi reported "added" and moved on, then the serialized show queue,
wedged on a timeout-less TVDB socket, never built the record. Ben confirmed
Neuromancer and Stuart were both added through Ombi. Same root cause as the manual
"Loading..." symptom, one fix.

Verified live 2026-08-04: `SHOWQUEUE-ADD` started 01:11:07, episodes set 01:11:11,
backlog auto-started 01:11:17. Final record matches TheTVDB exactly — S0:2, S1:10,
S2:8, total 20. First attempt, no intervention.

**Standing trap:** `auto_update = 1` silently reverts every SickChill patch on
upgrade. If adds hang again or titles stop matching, check that first.

### FIXED — Plexorcist reported a working Ombi request as a failure

Branch `ombi-false-failure-fix`. Committed locally, **not deployed** (see the
deploy note above — commits are not live until the deploy script runs on jail 5).

Ben's Concierge transcript, adding Altered Carbon:

> "the request came back grumpy and didn't go through. Nothing was added."
> then, on full series: "Ombi threw a 500 and nothing was added. Also: the backend
> came back with a weird mismatch and seems to have looked at **Cinta 7 Susun**
> instead of Altered Carbon."

Both claims were false. The request **worked**. Evidence from Ombi's own log on
the prod box: `POST /api/v1/Request/tv` answered

```json
{"result": false, "isError": false, "errorMessage": null, "requestId": 332331}
```

and SickChill built the show seconds later (`SHOWQUEUE-ADD` 01:11:07, 20/20
episodes by 01:11:17). So `result: false` with no error attached is **not** a
failure signal — Ombi's `RequestEngineResult` derives `isError` from
`ErrorMessage` being non-empty, and there was no error message.

**Fault 1 — `result: false` treated as failure unconditionally.** THE user-visible
bug. `_normalize_request_engine_result()` had
`if payload.get("isError") or payload.get("result") is False:` and mapped the
result through `_map_request_error_status()`, landing on `"error"`. Now an error
requires one of `isError` / a non-empty `errorMessage` / an `errorCode`. A bare
`result: false` becomes status `"unconfirmed"`, which triggers reconciliation
instead of an assertion that nothing happened.

**Fault 2 — reconcile matched by title search, not by the id requested.**
`_reconcile_show_request_failure()` called `check_show_request_status(query=title)`
which took `shows[0]` — the first ranked title hit — and never compared it to the
requested `tvdb_id`. That is the "Cinta 7 Susun" goblin, and it could equally have
produced a false *success*. Now it reads Ombi's authoritative request list
(`get_tv_request_detail(tvdb_id=...)` -> `/api/v1/Request/tv`, matched on
`tvDbId`), and a title-search hit is only accepted when its id matches.

Note the earlier plan of "reconcile via `get_tv_detail(tvdb_id)`" would NOT have
worked: that endpoint returns HTTP 204 for Altered Carbon, i.e. `{}` -> status
`missing` -> still reported as a failure. The request *list* is the right source.

State on a `/api/v1/Request/tv` record lives on `childRequests[]`, not the parent,
so `_extract_tv_request_record_status()` reads it there; presence in that list
already means "requested", so the floor is `requested`, not `missing`.

**Fault 3 — `get_tv_detail()` resolved TVDB ids through a TMDB-keyed endpoint.**
It tried `/api/v2/Search/tv/moviedb/{id}` first — explicitly TheMovieDb-keyed —
before falling back to `/api/v2/Search/tv/{id}`. Feeding a TVDB id to the moviedb
route resolves to whatever unrelated show owns that number on TMDB; 53243 came
back as "Cinta 7 Susun", complete with `available`/`requested` flags for a show
nobody asked about. Order is now unprefixed-first; a moviedb answer is tagged
`_id_namespace_mismatch`, dropped by `_split_detail_namespace()`, and surfaced as
`unresolved_tvdb_id` + `unverified_title`, so it can no longer gate a request or
be named to the user as though it were the show requested.

Scope of that claim, measured on prod: **both** routes answered `204` for 332331,
a TVDB id `POST /api/v1/Request/tv` happily resolves to Altered Carbon. So
`get_tv_detail()` mostly returns `{}` on this path (the production `ombi_detail`
was `{}` and `title` was `null`), the pre-flight gate does not fire, and Ombi
enforces already-requested / already-available itself on the POST. **Unverified:**
whether `/api/v2/Search/tv/{id}` is TVDB-keyed at all or just a second TMDB-keyed
alias. If it is an alias, it will answer `53243` with "Cinta 7 Susun" *unflagged*
and that hole is still open. One curl on the box settles it:
`/api/v2/Search/tv/53243`.

Also worth recording, because it changes what this fault explains: in the
"Cinta 7 Susun" turn the agent called
`request_show_scope_for_user(tvdb_id=53243, scope="full_series")` with **no search
call in that turn at all** — it supplied the wrong id itself. (Same shape as
JBrahs' `tvdb_id: 0` → 500 `Value cannot be null. (Parameter 'source')`; that one
is now blocked by the `safe_tvdb_id <= 0` guard.) The client-side fixes stop
Plexorcist from *vouching* for a bogus id; they don't stop the agent producing one.

**Fault 4 — `first_season` could pick the specials.** `min(seasonNumber)` is
season 0 whenever specials exist, so it now skips season 0 unless that is
genuinely all there is. **Latent only — this never fired in production.**
`_build_request_seasons()` returns `[]` before reaching that branch whenever
`seasonRequests` is empty, which is every observed case; the real payload was
`firstSeason: true, seasons: []` and Ombi chose the season server-side. The two
tests for it hand-feed `seasonRequests`, a shape this path has not been seen to
produce. Keep the guard, don't credit it with a fix.

**Fault 5 — reconciled results had their title overwritten.** The old
`reconciled.update({... "title": detail.get("title") ...})` clobbered a title we
had just confirmed from Ombi with the (usually empty) search detail's `None`.
`_merge_request_context()` keeps the confirmed value.

**DISPROVED — the `episodes or None` NRE theory.** Earlier notes blamed
`{"episodes": episodes or None}` serializing as JSON `null` and NRE-ing Ombi's C#.
It does not hold: `/api/v2/Search/tv/moviedb/{id}` returns `seasonRequests: []`
even for known shows, so `seasons` is always `[]` and no season object is ever
emitted. That line is untouched — do not re-chase it. The 500 on the full-series
retry remains an Ombi-side fault; the fix here is that Plexorcist now reconciles
before declaring failure rather than guessing at the payload.

**Fault 6 — the failure story was written for someone with Ombi access, and
nobody was told when a request really failed.** Ben, on reading the fixed prose:

> "in plexorcist the user doesnt KNOW about ombi. They just ask the machine to get
> it. They cant/wont go to ombi. The AI needs to pass along any api error, self fix
> or resolve the error, or just work. If it fails. I need to be notified that there
> was a real issue."

Three changes:

1. **No backend names in user-facing prose.** `user_summary` on every failed /
   unconfirmed / unidentified request is now service-free, as is
   `backend/shabbos/render.py::_render_request` (including the success line, which
   used to end "Ombi has it"). The system prompt's "report the exact `service` and
   `http_status`" rule is now **admin-only**; non-admin users get an explicit ban on
   naming Ombi/SickChill/Radarr/Tautulli/Prowl/Jackett/Transmission, quoting HTTP
   codes, or being sent to another system to check. The "Ombi search can be picky"
   phrasing examples were teaching the model to name it — now "Title search".
2. **Request failures alert Ben.** All three request tools were in *neither* alert
   whitelist, so a request that never landed reached nobody. `tools/request_tools.py`
   now stamps failures with the `{service, operation, failure_type}` shape the alert
   path keys on, and `build_request_alert()` (shared by `backend/agent.py` and
   `tools/admin_alerts.py`, so they cannot drift) emits **Request Unconfirmed** /
   **Request Failed** / **Request Denied** at priority 1 and **Request Blocked**
   (no usable id) at 0. Benign outcomes — requested, already requested, already
   available, account still provisioning — stay quiet. Titles are usually absent on
   these paths, so the subject and the cooldown key fall back to `TVDB <id>`:
   "Unknown show" would have made every title-less failure share one key and
   silently swallow the next show's alert for the cooldown window.
   `_send_admin_alert()` now reports whether the notice actually went out, so
   `admin_alert_sent` (which the reply quotes to the user) can no longer be stamped
   on a Prowl that is missing, erroring, or unconfigured.
3. **Shabbos Mode pages the admin too.** It has no model to decide to escalate, so
   `ShabbosRouter._execute()` sends the same alert itself on a request failure and
   appends "The admin has been notified." only when the notice succeeded.

Self-fix, per Ben's "self fix or resolve the error": the prompt now tells the model
to resolve `missing_show_identifier` itself (search the title, retry with the real
id, never ask the user for an id) and to treat `unresolved_tvdb_id: true` as "the id
is wrong" rather than naming `unverified_title` — the "Cinta 7 Susun" failure mode.
One corrective retry per turn, never a repeat of the identical call.

**Tests:** `tests/test_ombi_request_results.py`, 17 cases, all fixtures shaped like
real Ombi responses — including the verbatim production payload above asserting
`ok is True`. `tests/test_request_failure_notification.py`, 14 cases over Fault 6:
service-free prose, alert shape, alert content/keys, "notified" meaning notified,
and the Shabbos path end to end. There were previously zero tests over this path.

### Fault 6b (`6d25feb`) — the ban lost to the prompt's own specific rules

The "never name a backend service" rule from Fault 6 was **not enough on its own**.
The prompt already said "hide backend machinery" when the model told a user "Ombi
threw a 500", because five *more specific* lines further down told it to do exactly
that, and specific beats general:

- `request_gate_soft_failed` → "explain that Ombi lookup failed but the tool
  continued through SickChill anyway"
- `backend_connected: false` → "tell the user you cannot reach SickChill right now"
- two Radarr `Unknown Movie` lines → "say Radarr found a candidate but rejected it"
- "If Plex and Ombi disagree, say they disagree"
- an example reply: `Assistant: "Here are the episodes Ombi still shows as not
  fully available: ..."` — the leak, modelled verbatim for the model to copy

All five are now built per role (`soft_gate_line`, `backend_unreachable_line`,
`rejected_candidate_line`, `unknown_movie_line`, `system_disagreement_line` in
`_build_instructions`). Admins keep every name; non-admins get the same instruction
in plain words. Internal routing lines ("Ombi is the source of truth", "Keep Ombi
state and SickChill state separate") are deliberately untouched — the ban is on what
to **say**, not on how to think, and the model still has to route.

`tests/test_prompt_service_names.py` (6 cases) assembles the real non-admin prompt
and fails on any clause that puts a service name after a speech verb (`say`, `tell
the user`, `explain`, `describe`, `report`), skipping prohibitions and lines where
the *user* is the speaker, plus any example reply that names one. It asserts the
admin prompt still names services, and self-checks that the detector still catches
the original bug so it cannot pass vacuously. **Read that test before adding a
prompt line about errors** — it is the thing that stops this regressing a third time.

Two smaller fixes in the same commit:

- A Prowl send that **fails** no longer starts the 10-minute cooldown.
  `_should_send_admin_alert()` stamped the key before the attempt, so one failed
  notice silenced the next ten minutes of that alert — the exact opposite of
  "notified means notified". `_clear_admin_alert_cooldown()` rolls it back.
- `missing_show_identifier` alerts drop from priority 0 to **-2** (Prowl's
  quietest). The prompt tells the model to resolve the id and retry in the same
  turn, so that alert fires even on runs that then succeed. It should be a log
  entry, not a phone buzz.
- `_plain_tv_repair_reply()` no longer says "SickChill did not have the show" —
  that reply only ever goes to a non-admin.

### Fault 6c — Shabbos Mode, where no prompt rule can reach

Shabbos Mode has **no model in the loop**, so a renderer's string *is* the final
text the user reads. The prompt fix above cannot filter it. Four renderers in
`backend/shabbos/render.py` printed backend names straight to a non-admin:

- `"In Ombi (requestable):"` → `"Not in Plex yet (can be requested):"`
- `'Nothing found in Ombi for "X".'` (twice) → `'Nothing found for "X".'`
- `'No {kind} request exists in Ombi for "X".'` → `'No {kind} request exists for "X".'`
- `"SickChill was unreachable, so its state is unknown."` →
  `"Its download status could not be checked right now, so this may be out of date."`

"Plex" stays everywhere — users have Plex accounts and that name is theirs.

`test_no_renderer_names_a_backend_service_to_a_user` in
`tests/test_shabbos_commands.py` parametrizes over **every** entry in
`render.RENDERERS` × nine result shapes (empty, each failure flag,
`backend_connected: false`, `exists_in_ombi: false`, unconfirmed, success) and
runs the output through `assert_user_safe`. It also fails if `render()` swallowed
a renderer exception, so a crashing shape cannot pass silently. A new renderer is
covered the moment it is added to the dict.

Two more surfaces in the same pass, neither of which render.py touches:

- **The confirmation preview.** `/fix` used to ask "This will ask Radarr to
  re-fetch X" / "This will run the SickChill repair loop on X" — written by the
  command builder, shown before the user says yes. Now "This will try to re-fetch
  X" / "This will run the repair loop on X", covered by
  `test_confirmation_preview_never_names_a_backend_service`.
- **`user_summary`, which is the real one.** The renderers *prefer* the tool's
  `user_summary` over their own prose (`_failure`, `_render_repair`,
  `_render_request`), so most real failures never reach the strings above —
  they print whatever the tool wrote. `user_error_summary()` was writing
  "Movie repair for X failed while talking to **Ombi** during multi search: **HTTP
  500** Internal Server Error", and the repair tools were writing "X is requested
  in Ombi, but SickChill did not have the show", "Radarr already has a download
  working for X", "SickChill repair did not start". All reworded. **The admin
  loses nothing**: every one of those results still spreads the classified error
  dict (`service`, `operation`, `http_status`, `http_reason`, `error_message`)
  alongside, and the admin prompt is told to report those exact fields.

`test_no_tool_writes_a_backend_name_into_user_summary` (in
`tests/test_request_failure_notification.py`) walks the AST of `request_tools.py`,
`repair_tools.py` and `movie_repair_tools.py` — whole module, not just the tool
entry points, because the worst offender lived in a private helper — and fails on
any `user_summary`/`change_status` literal naming a backend. 17 literals guarded.
It first asserts those modules hold **no admin-only tool**, so if one is ever added
the test fails and demands re-scoping rather than quietly over-asserting.

Full suite: 216 pass.

## Session checkpoint (auto: weekly (7-day) usage at 88.0%) — 2026-08-04 10:03 MST
The weekly (7-day) usage cap is at 88.0% and resets in ~23h 56m. When it hits
100%, the current turn is cut off. cwd: /home/ben/Projects/plexorcist.

**In flight: nothing. The work is finished and committed.** Branch
`ombi-false-failure-fix`, commits `75087af` (landed request no longer reported as a
failure), `c747c11` (Fault 6: no backend names in user prose, request failures page
Ben, Shabbos alerts too, self-fix prompt rules) and `6d25feb` (Fault 6b: the five
prompt lines that were still ordering the leak, plus the failed-send cooldown and
the priority-2 downgrade), `e7e6b91` + this one (Fault 6c: the Shabbos renderers,
the confirmation preview and every `user_summary` — the surfaces no prompt rule can
reach). Clean tree apart from this file. **Not pushed. Not deployed. Not merged to
`main`.** 216 tests pass via `.venv/bin/python -m pytest -q` (system python has no
pytest).

Just decided, from Ben's own words — "the user doesnt KNOW about ombi… If it
fails. I need to be notified that there was a real issue":

- End-user prose never names a backend service or sends the user to one. The
  "report the exact `service`/`http_status`" prompt rule is now admin-only, and so
  are the five specific lines that used to override it. A test reads the assembled
  non-admin prompt and fails on any new one.
- A request that does not land now alerts Ben (Prowl), in the LLM path and in
  Shabbos Mode. `admin_alert_sent` is only stamped when the notice really sent, and
  a failed send no longer sits on a cooldown.
- The model is told to self-resolve a bad/missing show id (search, retry once)
  instead of reporting a failure or asking the user for an id. That one alerts at
  priority -2 (quiet) because it fires even when the retry works.

**Concrete next steps, in order:**

1. **Deploy is Ben's call and needs him.** `./deploy.local.sh` rsyncs the working
   tree to `/mnt/NALA/…/apps/plexorcist` — but `/mnt/NALA` was **not mounted** as
   of this session, and the restart needs root on NALA (`jexec 5`,
   `supervisorctl restart plexorcist`; 10.0.0.93 has no direct sshd). Ask before
   any SSH — clearance is never standing.
2. **One unanswered question, same trip:** `curl /api/v2/Search/tv/53243` on the
   box settles whether the unprefixed route is TVDB-keyed or a second TMDB alias.
   If it is an alias it will answer with "Cinta 7 Susun" *unflagged*, and that
   hole is still open (see Fault 3).
3. **Deliberately left alone — tell Ben rather than silently fixing:**
   `_fallback_reply_from_tool_calls()` / `_format_tool_error()` still name Ombi,
   Radarr and SickChill on *repair* paths, to any user. Same class of leak as
   Fault 6, but those two are also the admin's diagnostics, so role-splitting them
   is a real change rather than a rewording. (`_plain_tv_repair_reply()` and the
   Shabbos renderers are done — see Faults 6b/6c.)
4. **Ben's call, one line of judgement:** priority -2 on `missing_show_identifier`
   collapses two cases — the model resolving the id and retrying successfully
   (noise, correctly silenced) and the model *failing* to resolve it (a real
   failure he asked to hear about, now a quiet log entry). They are not
   distinguishable at tool-result level, so this was a deliberate trade, not an
   oversight. Raise it back to 0 if he would rather have the false buzzes.

AUTO-RESUME ARMED: overlord-resume-autoresume-6b26f9ed.timer (fires ~5 min after the weekly (7-day) cap resets,
continues the work in /home/ben/Projects/plexorcist from this handoff).
Cancel with: systemctl --user disable --now overlord-resume-autoresume-6b26f9ed.timer
If the work in flight lives somewhere else, add a line:  RESUME-FOLDER: /abs/path

## Session checkpoint (auto: weekly (7-day) usage at 92.0%) — 2026-08-04 10:34 MST
The weekly (7-day) usage cap is at 92.0% and resets in ~23h 25m. When it hits
100%, the current turn is cut off. cwd: /home/ben/Projects/plexorcist.

**DEPLOYED AND LIVE — 2026-08-04 10:35 MST.** Nothing is in flight. Branch
`ombi-false-failure-fix` (7 commits, tree clean, 216 tests pass) was rsynced to
prod via `bash deploy.local.sh` and the service restarted:
`ssh root@10.0.0.12` → `jexec 5 supervisorctl restart plexorcist`. Verified, not
assumed: gunicorn pid 68964 booted clean, both workers reached "Application
startup complete", no ImportError, and the deployed `tools/error_helpers.py` on
the box contains the new "did not go through" wording. `pyproject.toml` was
unchanged, so no `pip install -e .` was needed. SSH was one-shot per command;
nothing left held open.

Not done, and each is a separate decision for Ben:
- **Not pushed to `origin`, not merged to `main`.** Prod runs the branch code;
  GitHub does not have it. Ben's "push" meant deploy, so this is still open.
- **Shabbos alert gap (found, deliberately not fixed).** In
  `backend/shabbos/router.py` (~line 200) only `REQUEST_TOOL_NAMES` failures call
  `_alert_admin_request_failure`. The `else` branch — `/fix`, `/status`,
  `/search`, `/seasons`, `/episode` — and the bare `except Exception` below it
  never page Ben. Before this work that branch at least *printed* "failed while
  talking to Ombi: HTTP 500", so a human could relay it; de-naming it (correctly)
  closed the only channel it had. `build_request_alert()` in `tools/admin_alerts.py`
  is request-shaped (keys on `status`/`tvdb_id`), so this needs a sibling builder
  taking a classified-error dict, reusing `_should_send_admin_alert` /
  `_clear_admin_alert_cooldown`, with the alert key including tool + target so a
  user hammering `/fix` cannot spam him. Only append "The admin has been
  notified." to the user text when the send actually returns true. ~20 min, one
  commit.
- Items 2/3/4 in the section above still stand unchanged.

AUTO-RESUME ARMED: overlord-resume-autoresume-25c83ebb.timer (fires ~5 min after the weekly (7-day) cap resets,
continues the work in /home/ben/Projects/plexorcist from this handoff).
Cancel with: systemctl --user disable --now overlord-resume-autoresume-25c83ebb.timer
If the work in flight lives somewhere else, add a line:  RESUME-FOLDER: /abs/path

## Session checkpoint (auto: weekly (7-day) usage at 93.0%) — 2026-08-04 10:37 MST
The weekly (7-day) usage cap is at 93.0% and resets in ~23h 22m. When it hits
100%, the current turn is cut off. cwd: /home/ben/Projects/plexorcist.

**ALERT GAP CLOSED, DEPLOYED AND LIVE — 2026-08-04 10:41 MST. Nothing in flight.**
`77b3b8d` on branch `ombi-false-failure-fix`; tree clean; **220 tests pass**
(216 + 4 new). Rsynced and restarted (gunicorn pid 69372, both workers reached
"Application startup complete", deployed `router.py` confirmed to contain
`build_command_failure_alert`).

What it does: `/fix`, `/status`, `/search`, `/seasons`, `/episode` failing
against a backend now page Ben with a **Command Failed** alert carrying the
service, the HTTP status and the *tool* — everything the de-named user reply no
longer says. The bare `except` around the handler alerts too (that reply says
nothing actionable, so the alert carries all of it). New pieces:
`build_command_failure_alert()` + module-level `should_send_admin_alert` /
`clear_admin_alert_cooldown` / `reset_admin_alert_cooldowns` / `service_label`
in `tools/admin_alerts.py` (the `AdminAlertReporter` methods now delegate, one
implementation); `ShabbosRouter._send_admin_alert()` is the single send path.
15-min cooldown keyed on **tool + target + problem**, so hammering `/fix` on one
title cannot spam him or bury a different failure; a send that did not land
clears its own cooldown and never appends "The admin has been notified."
`tool` is carried separately from `command` because the repair lane arrives as
`/confirm`, and `target` prefers `invocation.note_text` over `invocation.target`
(on that path `target` is the whole UI preview sentence). An autouse fixture in
`tests/conftest.py` resets the process-wide cooldown dict per test — without it,
"the admin was paged" assertions silently depend on test order.

Still open, both Ben's call:
- **Not pushed to `origin`, not merged to `main`.** Prod runs the branch code;
  GitHub does not have it. Every deploy so far has been rsync + restart only.
- Items 2/3/4 from the section above stand unchanged (the `/api/v2/Search/tv/{id}`
  route question; `_fallback_reply_from_tool_calls()` / `_format_tool_error()`
  still naming backends on repair paths, which are also the admin's diagnostics;
  and the priority `-2` trade on `missing_show_identifier`).
AUTO-RESUME ARMED: overlord-resume-autoresume-d3b955de.timer (fires ~5 min after the weekly (7-day) cap resets,
continues the work in /home/ben/Projects/plexorcist from this handoff).
Cancel with: systemctl --user disable --now overlord-resume-autoresume-d3b955de.timer
If the work in flight lives somewhere else, add a line:  RESUME-FOLDER: /abs/path

---

## 2026-08-10 — Movie requests reported as failures (c40d863, e8695cd)

Ben's report: Kat and Ryan (rmk1900) requested The Commitments. It worked — the
request landed, the movie downloaded, it is in Plex. He was paged anyway:

    User Kat and Ryan (rmk1900) requested TheCommitments; the request failed
    against Ombi: The Commitments (1991) has been successfully added!

and the user was told "I could not put in the request — nothing was added."

**Root cause (c40d863).** Ombi's `RequestEngineResult` has one human-readable
field, `Message`, and it carries both outcomes. The TV engine leaves it null;
the movie engine fills it on success with `"<Title> (<year>) has been
successfully added!"`. `_normalize_request_engine_result` read
`errorMessage or message` and then used that string as part of the
is-this-an-error test, so the success sentence tripped the error branch, matched
no pattern in `_map_request_error_status`, and came out `status: "error"`. The
rest is mechanical: `_stamp_request_failure` sets `error_message` to it and
`build_request_alert` prints it after "the request failed against Ombi:".

**It is a regression from 75087af** — the Altered Carbon fix. That commit hoisted
`error_message` out of the `if isError or result is False` guard and into the
guard itself. Before it, `message` was only read once something else had already
said "error", so a movie success could never reach it. TV was unaffected because
its engine sends no Message.

`message` now speaks only for a payload that is not `result: true`. It was
deliberately *not* dropped from error detection outright: `_map_request_error_status`
classifies already_requested / already_available / permission_denied off that
text, and if Ombi ever sends one without duplicating it into `errorMessage`, a
permission refusal would land as "unconfirmed" — "it may not exist" — for a
request that was deliberately denied and **cannot self-heal through the
reconcile**, because no request was ever created. The already_* pair would
self-heal; permission_denied is the one that would not.

**Second defect, same family (e8695cd).** 75087af's message claims "the movie
path shares the normalizer and now reconciles the same way". Only the normalizer
half was true. `_reconcile_movie_request_failure` still took
`check_movie_request_status`'s `results[0]` with no id comparison — so an
ambiguous POST for The Commitments could be confirmed by a hit for *Commitment*
and reported to the user as landed. `check_movie_request_status` now takes an
optional `tmdb_id` and scans the **whole** list for it (id-checking only
`results[0]` would make a request that ranked second "unconfirmed" — the same
lie pointing the other way). Reconciled movie results carry `title`, `tmdb_id`,
`reconciled_by` and go through `_merge_request_context`, which now protects
`tmdb_id` too. `check_existing_media_status` passes the id it already has.

Because the full list is scanned before the guard fires, a rejection means the
id is genuinely absent, not merely outranked. **Known trade:** a request record
with no `theMovieDbId` cannot match, falls to `title_rank`, and is dropped —
intended (a hit we cannot identify does not vouch), but it is the shape to look
at first if a landed movie request ever starts alerting as unconfirmed.

5 tests; the 4 regression ones were each run against the pre-fix client and fail
there. The classification test passes either way by design — it exists to stop a
future simplification from eating the already-requested/permission wording.

**Third defect, found by checking the record shape instead of trusting the
fixture (888396d).** The e8695cd claim above — that the movie path now
reconciles — was still only half true. `check_movie_request_status` read hits
from `/api/v1/Request/movie/search` with `_extract_request_status`, the
extractor for *search-index* hits, whose floor is `"missing"`. A real movie
request record (verbatim from `plexorcist.log`) has **no `requested` key at
all**:

```
{"theMovieDbId": 11393, "approved": true, "available": false, "denied": false,
 "requestStatus": "Common.ProcessingRequest", "requestedDate": "...", ...}
```

So a request that had landed but was still awaiting approval fell through to
`"Common.ProcessingRequest"` — in no landed state — and the reconcile called it
unconfirmed.

**This half is defensive, not load-bearing — Ben confirmed Ombi auto-approves
everything.** Every request record reads `approved: true` → `"approved"` → a
landed state, under the old extractor too, so the pending branch is unreachable
today. Measured, not assumed: every `check_movie_request_status` result in
`plexorcist.log` that found a record is `"approved"`, none pending. Kept because
it costs nothing and changes no current behavior (an unapproved record is the
only input whose reading differs), and it is what would break quietly if
auto-approve were ever off for one user or missing from a new friend's role —
the failure being a *false* "unconfirmed" page for a request that worked. TV
already had `_extract_tv_request_record_status` with the same `"requested"`
floor from 75087af; movies now have `_extract_movie_request_record_status`. **The old fixture invented
`requested: True`, which is what hid it — it is now copied from the real
payload.** Denied movie records also now surface as `"denied"` rather than
silently reading as no-request.

Same confusion one layer up — and **this half is live under auto-approve**, in
fact auto-approve is precisely when it fires: `check_existing_media_status` called
`check_movie_request_status` and then passed the whole **envelope**
(`{query, username, exists_in_ombi, status, raw, …}`) to a record extractor,
which matched none of its flag keys and returned `"missing"` unconditionally —
the HTTP call was paid for and its answer thrown away, so an already-requested
movie read as never requested. It now reads the lookup's own verdict, and only
when `matched_by == "tmdb_id"`, since the search behind it is title-ranked.

Full suite **228 pass**; the 3 new tests each verified failing pre-fix.

**Residual, pre-existing, not introduced here.** The model-facing tool
(`tools/catalog.py`) calls `check_movie_request_status` with a query only — no
tmdb id — so `matched_by` is `"title_rank"` and the hit may be a different film.
The `"requested"` floor makes the status honest *for the record that matched*;
it does not make the match right. Tolerable because `_render_request_status`
prints the matched record's own title, so a stranger announces itself
("Commitment — request status: requested.") rather than vouching for what was
asked. Checked: nothing downstream keys on the old `"missing"` /
`"Common.ProcessingRequest"` strings. If it ever needs tightening, give the tool
a tmdb id the way the reconcile has one.

**Unverified, left open on purpose.** Both movie reconcile call sites query by
`detail.get("title") or str(tmdb_id)`. For TV that degrades badly because
`get_tv_detail` 204s on prod, but `get_movie_detail` asks
`/api/v2/Search/movie/{tmdb_id}` — a TMDB id against a TMDB-keyed route, the
right namespace — so it should populate. Not confirmed against prod. Worth a
glance if a movie reconcile ever misses: if the title is empty the query becomes
a bare id string and the title search cannot match. Note Ben's alert said
"TheCommitments" while Ombi said "The Commitments (1991)", so that subject line
did not come from Ombi's detail.

**NOT DEPLOYED** at the time this was written. Superseded — see the deploy
checkpoint below.

## 2026-08-10 23:33 MST — Ombi false-failure fix DEPLOYED AND RESTARTED

Done at Ben's explicit "run the plexorcist deploy script and ssh to the jail and
restart plexorcist". Branch `ombi-false-failure-fix` at `7610442`, clean tree.
Not merged to `main`, not pushed — deploying the branch working tree is the
established pattern here, and both remain Ben's call.

Context: the session that did this work (`d99bca4a`) died mid-instruction on an
`API Error: Unable to connect to API (ENOTFOUND)` before it could deploy. Ben's
two deploy instructions to that session were never executed. This closes that
gap; nothing else was left in flight.

What shipped — the rsync delta was exactly the Ombi fix, everything else on the
branch was already live:

- `clients/ombi_client.py` (c40d863 success-message-read-as-error, e8695cd
  title-ranked hit vouching for the wrong movie, 888396d the movie-request-record
  extractor)
- `tests/test_ombi_request_results.py`
- `HANDOFF.md` (one commit stale on the server — this section is not synced)

Verified, not assumed:

- Pre-flight `pytest tests/test_ombi_request_results.py
  tests/test_request_failure_notification.py` — 46 passed.
- `rsync -avn` dry run first, to see the delta before writing to prod.
- `./deploy.local.sh` over the NFS mount (autofs → `10.0.0.12:/mnt/STORAGEpool`).
  No ssh needed for this half.
- **Jail number checked, not assumed** — Ben flagged it might have moved. `jls -v`
  on NALA: JID 5 is still `sandbox_1` (`/mnt/NASpool/jails/sandbox_1/root`), and
  `jexec 5 supervisorctl status` confirmed plexorcist is the service living
  there before anything was restarted. Other JIDs shown: 1 plexmediaserver_3,
  2 tautulli_1, 4 radarr_2, 6 unifi_3, 7 urbackup_1.
- New code visible from *inside* the jail at the path gunicorn actually loads:
  `jexec 5 md5 /mnt/webroot/apps/plexorcist/clients/ombi_client.py` ==
  `2bd026a889ae7ed92cf27ceb0cb0b431`, identical to the local file.
- `jexec 5 supervisorctl restart plexorcist`: pid 69372 (6d 12h uptime) → 31916,
  workers 31919/31920, all three bound to `*:5500`.
- App serving: the `memory_sweep_run` background job is still ticking at its
  120s cadence, which is the proof the event loop is alive and not just a bound
  socket. Zero tracebacks / ImportError / SyntaxError in the last 200 log lines.
  Stable pid, no crash loop.
- **Weak spot, flagged not papered over.** The HTTP probe on 127.0.0.1:5500
  returned a full `<!doctype html>` page, not the inert 404 that the 2026-08-03
  checkpoint records as the correct unauthenticated response — and the probe's
  exit status was piped into `head`, so `probe-exit=0` was head's status and
  checked nothing. So that probe is not evidence either way. **Untouched by this
  deploy**: the rsync delta was `clients/ombi_client.py`, its test, and this
  file — no auth code moved, so whatever bare-`/` does now, it did before.
  Worth one clean `fetch -s` next time someone is legitimately in the box.
- net_ssh session to root@10.0.0.12 **CLOSED** afterwards. That clearance was
  ONE TIME for that request and does NOT carry forward — ask Ben again before
  any future connection.

NEXT STEP: nothing to build or deploy. The residual and the unverified item in
the section directly above are both still open and both still pre-existing —
neither was touched by this deploy.

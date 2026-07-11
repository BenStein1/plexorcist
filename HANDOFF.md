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

# Shabbos Mode

> "I don't roll on Shabbos."

A per-account, deterministic, **AI-free** interface to Plexorcist. For users who
do not want to interact with a language model — commands only, no conversational
interpretation, no model calls anywhere at runtime.

The explanation to give a user:

> Shabbos Mode uses fixed commands and ordinary application code. Your messages
> do not go to a local or cloud language model. If a command cannot be handled
> deterministically, it fails instead of falling back to AI.

## What it does

- A strict slash-command interface covering the **entire non-admin surface** of
  Plexorcist: search, request, status, repair, episode checks, watch history,
  reporting a problem to the admin, and setting your name. You lose the
  conversation, not the capability.
- Calls the **same tools, the same service adapters and the same permission gate**
  as the normal interface (`tools/catalog.py`). No duplicated request logic.
- Records what you did, deterministically, so you still appear in the admin's
  task list.

## What it does NOT do

- No language model, at any point: not in the request path, not in a background
  job, not after the fact. (There are no embeddings or vector stores anywhere in
  Plexorcist, so there is nothing of that kind to avoid.)
- No fuzzy intent inference, spelling correction, or "did you mean". A command
  that does not parse is an error, never a guess.
- No conversational memory. A Shabbos conversation is **permanently** excluded
  from the memory compactor — including retroactively, if the account later
  leaves the mode. Content created under the promise stays out of a model.
- `/cancel` is not supported (Ombi exposes no cancel tool). It fails closed.

## How an administrator enables it

Admin-only. Just ask Plexorcist in normal chat:

> "Turn on Shabbos Mode for Richard."

That routes to the `set_shabbos_mode` admin tool. It is also available to the
admin token over `/mcp`. To check that it is genuinely AI-free, ask:

> "Show me the Shabbos diagnostics."

`get_shabbos_diagnostics` reads the **real audit log** and reports how many
commands ran and how many invoked a model (which must be zero). It reports what
actually happened rather than asserting a constant.

The setting lives in the existing `user_flags` table (`shabbos_mode = "true"`).
No migration. Rollback is clearing the flag.

## Commands

| Command | What it does |
|---|---|
| `/help [command]` | List commands, or detail one. Generated from the registry. |
| `/search [movie\|show] <title>` | Numbered results with stable ids. |
| `/request <n>` | Request result *n* from the last `/search`. |
| `/request tmdb:<id>` | Request a movie by id. |
| `/request tvdb:<id> [--first\|--latest\|--all]` | Request a show. **Defaults to the first season**, never the whole series. |
| `/request tvdb:<id> s01e02` | Request one episode. |
| `/status movie\|show <title>` | Is it requested, and where is it? |
| `/seasons <show> [--season N]` | Episode-by-episode status. |
| `/library <title>` | Is it already in the library? |
| `/inventory <actor\|director\|franchise>` | What we have vs. what's requestable. |
| `/episode <show> s01e02` | Status of one episode. |
| `/fix movie <title> [--year Y]` | Ask Radarr to re-fetch. **Requires `/confirm`.** |
| `/fix show <title> [--season N \| --episode s01e02]` | Run the SickChill repair loop. **Requires `/confirm`.** |
| `/watching` | Your recent watch history. |
| `/issue <type> <note>` | Report a problem to the admin. |
| `/name <name>` | Change what you're called. |
| `/confirm <CODE>` | Confirm a pending action. |
| `/whoami`, `/logout` | Session. |

Issue types are fixed tokens: `missing`, `wrong-version`, `bad-audio`,
`bad-video`, `no-subtitles`, `wrong-language`, `playback-error`, `incomplete`,
`other`. The note is passed through **verbatim** — never classified, summarized,
or interpreted.

Use `/issue` (not `/fix`) for wrong language, audio or subtitles: policy sends
those to the admin rather than running an automated movie repair.

## Architectural guarantee

`tools/catalog.py` is the single source of truth for every tool. It already had
two consumers — `tools/bridge.py` (the LLM agent) and `tools/server.py` (the MCP
server). Shabbos Mode is simply a **third**:

```
POST /api/chat
    |
    v
get_user_context                    (unchanged: Plex OAuth + blocklist)
    |
    +-- shabbos_mode? --> ShabbosRouter        <-- never built an LlmClient
    |                         |
    |                         v
    |                     visible_specs() + build_toolkit()
    |                         |
    |                         v
    |                     the same Ombi / Plex / Radarr / SickChill adapters
    |
    +-- otherwise -------> build_agent() -> ConciergeAgent   (unchanged)
```

**The guarantee is structural, not behavioural.** The fork in `/api/chat` is
upstream of `build_agent()`, which is the thing that constructs the `LlmClient`.
The Shabbos path builds from `build_store_and_audit()` and is never handed a
model client. *You cannot call what you were never given.* This is why it is not
merely "a handler that is expected not to call the model".

Two consequences worth knowing:

- **Server-side, not cosmetic.** Even free-form text POSTed straight at
  `/api/chat` is answered deterministically. The UI is honesty, not enforcement.
- **The background sweeper is guarded.** `_compact_conversation_once()` sends
  stored conversations to the LLM for memory compaction. That is the one
  non-obvious leak, and it is blocked at that single chokepoint.

## Admin tasks

Excluding these users from the LLM sweeper would otherwise make them invisible in
`get_admin_task_summary`. Instead, the router writes task notes **at command
time**, when it knows exactly what happened, rather than having a model infer it
from prose afterwards:

- `/issue` → an `open` task (always — it needs a human).
- A failed `/request` or `/fix` → an `open` task.
- Anything successful → a `logged` note (auditable, doesn't spam the task list).

These land in `user_memory_notes`, which is what `get_admin_task_summary` reads,
and they resolve with the existing `resolve_admin_task`. This is *more* accurate
than the LLM path, because nothing is inferred.

## Failure behaviour: fail closed, always

| Situation | Response |
|---|---|
| Free-form text | "Shabbos Mode only accepts explicit commands." |
| Unknown command | `/help` |
| Bad syntax / arguments | The usage string, or the tool's own field-level validation error |
| Unsupported action | "That action is not available in Shabbos Mode." |
| A service is down | "…failed while talking to Ombi… Nothing was changed." |

It **never** falls back to the agent. There is no code path from the router to a
model.

## Adding a command — developer checklist

Commands live in `backend/shabbos/commands.py`. A new one must:

- [ ] **Parse deterministically.** No fuzzy matching, synonyms, or intent
      inference. See the note below.
- [ ] **Call only a catalog tool** (`tools/catalog.py`) — never a service client
      directly, and never a reimplementation of existing business logic.
- [ ] **Have a renderer** in `backend/shabbos/render.py`. A tool with no renderer
      fails closed rather than dumping a raw dict.
- [ ] **Whitelist fields when rendering.** Never serialize a result dict — several
      carry internal passthroughs (`raw`, `ombi_detail`, `grab_payload`, …).
- [ ] **Replicate any filtering the prompt used to do.** Read the tool's catalog
      description: if it tells the model to soften or hide part of the result
      (e.g. "these top titles are server-wide, not this user's"; "never mention
      indexers or seeders"), the renderer must do that, because no model will.
      An unfiltered renderer is an information-disclosure bug.
- [ ] **Enforce normal permissions** by relying on `visible_specs` — do not invent
      a second auth model.
- [ ] **Have no model or embedding dependency**, direct or transitive.
- [ ] **Be covered by the zero-AI test.** Add it to `COMMAND_SCRIPT` in
      `tests/test_shabbos_isolation.py`; a completeness test fails if you don't.
- [ ] **Fail closed.** Never add an AI fallback.

### Why the parser is allowed to be a parser

`AGENTS.md` says *"no regex/hardcoded NL parsing — lean on the LLM"*. **Shabbos
Mode is a deliberate, documented exception.** It is not parsing English; it parses
an explicit command grammar, which is the narrow mechanical-parsing case that rule
already permits. Do not "fix" it by routing it through the model — that would
destroy the entire point of the feature.

## Tests

- `tests/test_shabbos_isolation.py` — the load-bearing proof. A spy makes any
  model use (`build_llm_client`, `generate_response`, or raw `AsyncOpenAI` /
  `AsyncAnthropic` construction) **record itself**, and every command in the
  registry is driven through the real router and real handlers — in both the
  success path and the service-failure path — asserting zero model calls. It
  records rather than merely raising, because the router and the sweeper both wrap
  work in `except Exception` and would otherwise swallow the evidence. Also covers
  the sweeper guard, the endpoint fork, and an import tripwire.
- `tests/test_shabbos_commands.py` — parsing, confirmation tokens, permissions,
  admin tasks, and renderer whitelisting.

Run: `.venv/bin/python -m pytest tests/ -q`

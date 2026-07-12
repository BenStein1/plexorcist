"""The Shabbos Mode command registry.

Each command declares how to turn a ParsedCommand into an Invocation of ONE
catalog tool. The router validates that invocation against the tool's real
pydantic input model and calls the real handler, so this layer never duplicates
business logic and never invents a second permission model.

To add a command, see the developer checklist in SHABBOS_MODE.md. In short: it
must parse deterministically, call only a catalog tool that has a renderer, and
be covered by the zero-AI test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.shabbos.parser import ParsedCommand, UsageError, parse_episode_token, parse_id_token, parse_int

LAST_SEARCH_KEY = "shabbos_last_search"

ISSUE_TYPES = (
    "missing",
    "wrong-version",
    "bad-audio",
    "bad-video",
    "no-subtitles",
    "wrong-language",
    "playback-error",
    "incomplete",
    "other",
)


@dataclass(frozen=True)
class Invocation:
    """One catalog tool call, fully resolved. The router executes this."""

    tool: str
    kwargs: dict[str, Any]
    target: str = ""
    """Stable identifier for the audit log / task note (e.g. 'tmdb:1091')."""

    confirm: str | None = None
    """When set, the command does NOT run until /confirm <token>. The text
    describes exactly what would happen."""

    result_filter: str | None = None
    """Optional media-type filter applied to search candidates ('movie'/'show')."""

    writes_note: bool = False
    """State-changing / attention-worthy commands leave a note for the admin."""

    always_open_task: bool = False
    """/issue always needs Ben, even though the Prowl push itself succeeded."""

    note_text: str = ""


@dataclass(frozen=True)
class BuildContext:
    user_id: str
    support_context: dict[str, Any]

    def last_search(self) -> list[dict[str, Any]]:
        raw = self.support_context.get(LAST_SEARCH_KEY)
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


@dataclass(frozen=True)
class CommandSpec:
    name: str
    usage: str
    summary: str
    build: Callable[[BuildContext, ParsedCommand], Invocation] | None = None
    local: bool = False
    """Handled by the router itself (help/confirm/whoami/logout) — no tool call."""

    aliases: tuple[str, ...] = field(default_factory=tuple)


# -- builders ------------------------------------------------------------------


def _require_text(parsed: ParsedCommand, usage: str) -> str:
    text = parsed.joined()
    if not text:
        raise UsageError("That command needs a title.", usage)
    return text


def build_search(ctx: BuildContext, parsed: ParsedCommand) -> Invocation:
    usage = "/search [movie|show] <title>"
    args = list(parsed.args)
    media_type: str | None = None
    if args and args[0].lower() in {"movie", "show"}:
        media_type = args.pop(0).lower()
    query = " ".join(args).strip()
    if not query:
        raise UsageError("That command needs a title.", usage)
    return Invocation(
        tool="search_media",
        kwargs={"query": query},
        target=query,
        result_filter=media_type,
    )


def build_request(ctx: BuildContext, parsed: ParsedCommand) -> Invocation:
    usage = "/request <n> | /request tmdb:<id> | /request tvdb:<id> [--first|--latest|--all|s01e02]"
    args = list(parsed.args)
    if not args:
        raise UsageError("Tell me what to request.", usage)

    head = args.pop(0)
    title = ""

    # Form 1: an index into the last /search.
    if head.isdigit():
        results = ctx.last_search()
        if not results:
            raise UsageError("There are no search results to pick from. Run /search first.", usage)
        index = int(head)
        if not 1 <= index <= len(results):
            raise UsageError(f"Pick a number between 1 and {len(results)}.", usage)
        chosen = results[index - 1]
        kind = "tvdb" if str(chosen.get("type") or "").lower() == "show" else "tmdb"
        media_id = chosen.get("tvdb_id") if kind == "tvdb" else chosen.get("tmdb_id")
        if not media_id:
            raise UsageError(f"Result {index} has no usable {kind} id, so it can't be requested.", usage)
        media_id = int(media_id)
        title = str(chosen.get("title") or "")
    else:
        # Form 2: an explicit stable id.
        kind, media_id = parse_id_token(head)

    # Movies take no scope.
    if kind == "tmdb":
        if parsed.flags or args:
            raise UsageError("A movie takes no season/episode options.", usage)
        return Invocation(
            tool="request_movie_for_user",
            kwargs={"tmdb_id": media_id},
            target=f"tmdb:{media_id}",
            writes_note=True,
            note_text=f"Requested movie {title or f'tmdb:{media_id}'}",
        )

    # Shows: a single episode, or a scope.
    if args:
        season, episode = parse_episode_token(args[0])
        if len(args) > 1:
            raise UsageError("Too many arguments.", usage)
        return Invocation(
            tool="request_episode_for_user",
            kwargs={"tvdb_id": media_id, "season": season, "episode": episode},
            target=f"tvdb:{media_id} S{season:02d}E{episode:02d}",
            writes_note=True,
            note_text=f"Requested {title or f'tvdb:{media_id}'} S{season:02d}E{episode:02d}",
        )

    if parsed.flags.get("all"):
        scope = "full_series"
    elif parsed.flags.get("latest"):
        scope = "latest_season"
    elif parsed.flags.get("first"):
        scope = "first_season"
    else:
        # Deterministic safe default. The catalog warns against silently pulling a
        # whole series; /help says how to ask for more.
        scope = "first_season"

    return Invocation(
        tool="request_show_scope_for_user",
        kwargs={"tvdb_id": media_id, "scope": scope},
        target=f"tvdb:{media_id} {scope}",
        writes_note=True,
        note_text=f"Requested {title or f'tvdb:{media_id}'} ({scope.replace('_', ' ')})",
    )


def build_status(ctx: BuildContext, parsed: ParsedCommand) -> Invocation:
    usage = "/status movie|show <title>"
    args = list(parsed.args)
    if not args or args[0].lower() not in {"movie", "show"}:
        raise UsageError("Say whether it's a movie or a show.", usage)
    kind = args.pop(0).lower()
    query = " ".join(args).strip()
    if not query:
        raise UsageError("That command needs a title.", usage)
    tool = "check_movie_request_status" if kind == "movie" else "check_show_request_status"
    return Invocation(tool=tool, kwargs={"query": query}, target=query)


def build_seasons(ctx: BuildContext, parsed: ParsedCommand) -> Invocation:
    usage = "/seasons <show> [--season N]"
    query = _require_text(parsed, usage)
    kwargs: dict[str, Any] = {"query": query}
    if "season" in parsed.flags:
        kwargs["season"] = parse_int(parsed.flags["season"], "--season")
    return Invocation(tool="get_show_season_status", kwargs=kwargs, target=query)


def build_library(ctx: BuildContext, parsed: ParsedCommand) -> Invocation:
    query = _require_text(parsed, "/library <title>")
    return Invocation(tool="check_existing_media_status", kwargs={"query": query}, target=query)


def build_inventory(ctx: BuildContext, parsed: ParsedCommand) -> Invocation:
    query = _require_text(parsed, "/inventory <actor|director|franchise>")
    return Invocation(tool="check_library_inventory", kwargs={"query": query}, target=query)


def build_episode(ctx: BuildContext, parsed: ParsedCommand) -> Invocation:
    usage = "/episode <show> s01e02"
    args = list(parsed.args)
    if len(args) < 2:
        raise UsageError("That command needs a show and an episode.", usage)
    season, episode = parse_episode_token(args[-1])
    show = " ".join(args[:-1]).strip()
    if not show:
        raise UsageError("That command needs a show title.", usage)
    return Invocation(
        tool="check_episode_status",
        kwargs={"show": show, "season": season, "episode": episode},
        target=f"{show} S{season:02d}E{episode:02d}",
    )


def build_fix(ctx: BuildContext, parsed: ParsedCommand) -> Invocation:
    usage = "/fix movie <title> [--year Y]  |  /fix show <title> [--season N | --episode s01e02]"
    args = list(parsed.args)
    if not args or args[0].lower() not in {"movie", "show"}:
        raise UsageError("Say whether it's a movie or a show.", usage)
    kind = args.pop(0).lower()
    title = " ".join(args).strip()
    if not title:
        raise UsageError("That command needs a title.", usage)

    if kind == "movie":
        kwargs: dict[str, Any] = {"title": title, "issue": "redownload requested"}
        if "year" in parsed.flags:
            kwargs["year"] = parse_int(parsed.flags["year"], "--year")
        label = f"{title} ({kwargs['year']})" if "year" in kwargs else title
        return Invocation(
            tool="repair_requested_movie",
            kwargs=kwargs,
            target=title,
            confirm=f'This will ask Radarr to re-fetch "{label}".',
            writes_note=True,
            note_text=f"Repair requested for movie {label}",
        )

    kwargs = {"query": title, "scope": "show"}
    if "episode" in parsed.flags:
        season, episode = parse_episode_token(str(parsed.flags["episode"]))
        kwargs.update({"scope": "episode", "season": season, "episode": episode})
        label = f"{title} S{season:02d}E{episode:02d}"
    elif "season" in parsed.flags:
        kwargs.update({"scope": "season", "season": parse_int(parsed.flags["season"], "--season")})
        label = f"{title} season {kwargs['season']}"
    else:
        label = title

    return Invocation(
        tool="repair_requested_show",
        kwargs=kwargs,
        target=title,
        confirm=f'This will run the SickChill repair loop on "{label}".',
        writes_note=True,
        note_text=f"Repair requested for show {label}",
    )


def build_watching(ctx: BuildContext, parsed: ParsedCommand) -> Invocation:
    return Invocation(tool="get_user_watch_context", kwargs={}, target="")


def build_issue(ctx: BuildContext, parsed: ParsedCommand) -> Invocation:
    usage = "/issue <type> <what's wrong>\n  types: " + ", ".join(ISSUE_TYPES)
    args = list(parsed.args)
    if not args:
        raise UsageError("Tell me the issue type.", usage)
    issue_type = args.pop(0).lower()
    if issue_type not in ISSUE_TYPES:
        raise UsageError(f"'{issue_type}' is not a known issue type.", usage)

    note = " ".join(args).strip()
    if not note:
        raise UsageError("Add a short note saying what's wrong.", usage)

    # The note is opaque. It is passed through verbatim -- never classified,
    # summarized, or interpreted. EscalationTools prefixes the user's label.
    summary = f"[{issue_type}] {note}"
    return Invocation(
        tool="send_admin_prowl_notice",
        kwargs={"summary": summary, "priority": 0},
        target=issue_type,
        writes_note=True,
        always_open_task=True,
        note_text=summary,
    )


def build_name(ctx: BuildContext, parsed: ParsedCommand) -> Invocation:
    name = _require_text(parsed, "/name <what to call you>")
    return Invocation(tool="set_my_friendly_name", kwargs={"friendly_name": name}, target=name)


# -- registry ------------------------------------------------------------------

COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("help", "/help [command]", "Show this list, or details for one command.", local=True),
    CommandSpec("search", "/search [movie|show] <title>", "Find something. Returns numbered results.", build_search),
    CommandSpec(
        "request",
        "/request <n> | /request tmdb:<id> | /request tvdb:<id> [--first|--latest|--all|s01e02]",
        "Request a result from the last search, or by id. Shows default to the first season.",
        build_request,
    ),
    CommandSpec("status", "/status movie|show <title>", "Has it been requested, and where is it?", build_status),
    CommandSpec("seasons", "/seasons <show> [--season N]", "Episode-by-episode status for a show.", build_seasons),
    CommandSpec("library", "/library <title>", "Is it already in the library?", build_library),
    CommandSpec("inventory", "/inventory <actor|director|franchise>", "What we have vs. what's requestable.", build_inventory),
    CommandSpec("episode", "/episode <show> s01e02", "Status of one episode.", build_episode),
    CommandSpec(
        "fix",
        "/fix movie <title> [--year Y]  |  /fix show <title> [--season N | --episode s01e02]",
        "Re-fetch something that downloaded wrong or went missing. Asks you to confirm.",
        build_fix,
    ),
    CommandSpec("watching", "/watching", "Your recent watch history.", build_watching),
    CommandSpec(
        "issue",
        "/issue <type> <what's wrong>",
        "Report a problem to the admin. Types: " + ", ".join(ISSUE_TYPES),
        build_issue,
    ),
    CommandSpec("name", "/name <what to call you>", "Change what I call you.", build_name),
    CommandSpec("confirm", "/confirm", "Confirm the action you were just shown.", local=True),
    CommandSpec("whoami", "/whoami", "Who you're signed in as.", local=True),
    CommandSpec("logout", "/logout", "Sign out.", local=True),
)

COMMANDS_BY_NAME: dict[str, CommandSpec] = {}
for _spec in COMMANDS:
    COMMANDS_BY_NAME[_spec.name] = _spec
    for _alias in _spec.aliases:
        COMMANDS_BY_NAME[_alias] = _spec

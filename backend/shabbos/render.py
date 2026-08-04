"""Deterministic renderers: tool result dict -> plain text.

In the normal concierge path the LLM is what turns a tool's result dict into
something a human reads. Shabbos Mode has no model, so every exposed tool needs a
hand-written formatter here.

TWO HARD RULES -- there is no model in the loop to catch a mistake:

1. WHITELIST FIELDS. Never serialize a result dict. Several results carry
   internal passthroughs (`raw`, `ombi_detail`, `grab_payload`, `radarr_movie`,
   `selected_release`, ...) that must never reach a user.

2. REPLICATE THE FILTERING THE PROMPT USED TO DO. Some catalog descriptions
   instruct the model to soften or hide parts of a result -- e.g. the watch
   context's `top_movies_30d` is SERVER-WIDE and must never be described as
   titles this user personally watched. With no model, an unfiltered renderer is
   an information-disclosure bug. Read the tool's catalog description before
   writing its renderer.
"""

from __future__ import annotations

from typing import Any

MAX_LIST = 10


# -- small shared helpers ------------------------------------------------------


def _title_year(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "Unknown title").strip()
    year = item.get("year")
    return f"{title} ({year})" if year else title


def _id_token(item: dict[str, Any]) -> str:
    """The exact token the user can paste back into /request."""
    if str(item.get("type") or "").lower() == "show":
        tvdb = item.get("tvdb_id")
        return f"tvdb:{tvdb}" if tvdb else "tvdb:?"
    tmdb = item.get("tmdb_id")
    return f"tmdb:{tmdb}" if tmdb else "tmdb:?"


def _failure(result: dict[str, Any], fallback: str) -> str | None:
    """Deterministic failure text, or None when the result is not a failure.

    `user_summary` is already deterministic prose written for end users (see
    tools/error_helpers.py), so prefer it. Never leak `reason`/`error`, which can
    carry raw upstream payloads.
    """
    if result.get("ok") is False or result.get("resolved") is False or result.get("found") is False:
        summary = str(result.get("user_summary") or "").strip()
        return summary or fallback
    return None


def _episode_line(row: dict[str, Any]) -> str:
    season = row.get("season")
    episode = row.get("episode")
    ref = f"S{int(season):02d}E{int(episode):02d}" if isinstance(season, int) and isinstance(episode, int) else "?"
    status = str(row.get("status") or "unknown")
    title = str(row.get("title") or "").strip()
    suffix = f" — {title}" if title else ""
    return f"  {ref} — {status}{suffix}"


# -- renderers, keyed by catalog tool name -------------------------------------


def render_search_media(result: dict[str, Any]) -> str:
    candidates = [c for c in (result.get("candidates") or []) if isinstance(c, dict)]
    query = str(result.get("query") or "").strip()
    if not candidates:
        return f'No results for "{query}".'

    lines = [f'Results for "{query}":', ""]
    for index, item in enumerate(candidates, start=1):
        media_type = str(item.get("type") or "?").lower()
        extra = ""
        if media_type == "show" and item.get("seasons"):
            extra = f" — {item['seasons']} season(s)"
        lines.append(f"  {index}. {_title_year(item)} — {media_type} — {_id_token(item)}{extra}")

    plex = result.get("plex")
    if isinstance(plex, dict) and plex.get("available"):
        lines += ["", f"Plex already has: {plex.get('title') or candidates[0].get('title')}"]

    lines += ["", "Request with:  /request 1     (or /request " + _id_token(candidates[0]) + ")"]
    return "\n".join(lines)


def render_check_existing_media_status(result: dict[str, Any]) -> str:
    failure = _failure(result, f'Nothing found in Ombi for "{result.get("query")}".')
    if failure:
        return failure
    best = result.get("best_match") if isinstance(result.get("best_match"), dict) else result
    if not best.get("title"):
        return f'Nothing found in Ombi for "{result.get("query")}".'

    if result.get("fully_available") or best.get("fully_available"):
        state = "available in Plex"
    elif result.get("partly_available") or best.get("partly_available"):
        state = "partly available in Plex"
    elif result.get("requested") or best.get("requested"):
        state = "requested, not available yet"
    else:
        state = "not requested"
    return f"{_title_year(best)} — {str(best.get('type') or 'media').lower()} — {state}.\n{_id_token(best)}"


def render_check_library_inventory(result: dict[str, Any]) -> str:
    plex_matches = [m for m in (result.get("plex_matches") or []) if isinstance(m, dict)][:MAX_LIST]
    ombi_candidates = [m for m in (result.get("ombi_candidates") or []) if isinstance(m, dict)][:MAX_LIST]
    query = str(result.get("query") or "").strip()

    lines = [f'Inventory for "{query}":', "", "In Plex (watchable now):"]
    lines += [f"  - {_title_year(m)}" for m in plex_matches] or ["  (nothing)"]
    lines += ["", "In Ombi (requestable):"]
    lines += [f"  - {_title_year(m)} — {_id_token(m)}" for m in ombi_candidates] or ["  (nothing)"]
    return "\n".join(lines)


def _render_request(result: dict[str, Any], what: str) -> str:
    status = str(result.get("status") or "").lower()
    title = _title_year(result)

    if status == "already_available":
        return f"{title} is already in Plex — nothing to request."
    if status == "already_requested":
        return f"{title} was already requested. Nothing new was submitted."
    if status == "account_not_ready":
        return str(result.get("user_summary") or "Your account is still being set up. Try again in a moment.")
    if status == "unconfirmed":
        # Ombi answered without an error but without confirming either, and the request
        # was not in its list. It may still have landed -- do not claim it did not.
        # `title` is usually absent on this branch (Ombi's v2 search 204s on a TVDB id),
        # so prefer the client's user_summary, which degrades to the id, and never let
        # _title_year()'s "Unknown title" placeholder reach the user.
        summary = str(result.get("user_summary") or "").strip()
        if summary:
            return summary
        subject = f" for {title}" if result.get("title") else ""
        return f"Ombi did not confirm the request{subject}. Check Ombi before requesting it again."

    failure = _failure(result, f"The request for {title} was not submitted.")
    if failure:
        return failure
    return f"Requested {what}: {title}. Ombi has it."


def render_request_movie_for_user(result: dict[str, Any]) -> str:
    return _render_request(result, "movie")


def render_request_show_scope_for_user(result: dict[str, Any]) -> str:
    scope = str(result.get("scope") or "").replace("_", " ") or "show"
    return _render_request(result, f"show ({scope})")


def render_request_episode_for_user(result: dict[str, Any]) -> str:
    season, episode = result.get("season"), result.get("episode")
    ref = f"S{int(season):02d}E{int(episode):02d}" if isinstance(season, int) and isinstance(episode, int) else "episode"
    return _render_request(result, f"episode {ref}")


def _render_request_status(result: dict[str, Any], kind: str) -> str:
    failure = _failure(result, f'No {kind} request status was returned for "{result.get("query")}".')
    if failure:
        return failure
    if not result.get("exists_in_ombi"):
        return f'No {kind} request exists in Ombi for "{result.get("query")}".'
    status = str(result.get("status") or "unknown")
    return f"{_title_year(result)} — request status: {status}."


def render_check_movie_request_status(result: dict[str, Any]) -> str:
    return _render_request_status(result, "movie")


def render_check_show_request_status(result: dict[str, Any]) -> str:
    return _render_request_status(result, "show")


def render_get_show_season_status(result: dict[str, Any]) -> str:
    failure = _failure(result, f'No season status found for "{result.get("query")}".')
    if failure:
        return failure

    episodes = [row for row in (result.get("episodes") or []) if isinstance(row, dict)]
    if not episodes:
        return f"{_title_year(result)} — no episode rows returned."

    lines = [f"{_title_year(result)} — episode status:", ""]
    lines += [_episode_line(row) for row in episodes[:60]]
    missing = [row for row in (result.get("missing_episodes") or []) if isinstance(row, dict)]
    if missing:
        lines += ["", f"{len(missing)} missing. Repair with:  /fix show {result.get('title')}"]
    return "\n".join(lines)


def render_check_episode_status(result: dict[str, Any]) -> str:
    show = str(result.get("show") or "the show")
    season, episode = result.get("season"), result.get("episode")
    ref = f"S{int(season):02d}E{int(episode):02d}" if isinstance(season, int) and isinstance(episode, int) else "?"
    status = str(result.get("status") or "unknown")

    lines = [f"{show} {ref} — {status}."]
    lines.append(f"  In Plex: {'yes' if result.get('present_in_plex') else 'no'}")
    if result.get("aired") is False:
        lines.append("  Not aired yet — this is not a missing episode.")
    if result.get("backend_connected") is False:
        lines.append("  SickChill was unreachable, so its state is unknown.")
    return "\n".join(lines)


def _render_repair(result: dict[str, Any], fallback: str) -> str:
    """Repair tools already produce deterministic user_summary prose. Prefer it,
    and never expose grab_payload / selected_release / radarr_movie internals."""
    summary = str(result.get("user_summary") or "").strip()
    if summary:
        return summary
    failure = _failure(result, fallback)
    if failure:
        return failure
    action = str(result.get("action") or "repair").replace("_", " ")
    return f"{_title_year(result)} — {action}."


def render_repair_requested_movie(result: dict[str, Any]) -> str:
    return _render_repair(result, "The movie repair did not complete. Nothing was changed.")


def render_repair_requested_show(result: dict[str, Any]) -> str:
    return _render_repair(result, "The show repair did not complete. Nothing was changed.")


def render_get_user_watch_context(result: dict[str, Any]) -> str:
    failure = _failure(result, "No watch history is available for your account.")
    if failure:
        return failure

    def _titles(items: Any) -> list[str]:
        out: list[str] = []
        for item in items or []:
            if isinstance(item, dict):
                title = item.get("full_title") or item.get("title") or item.get("grandparent_title")
                if title:
                    out.append(str(title))
            elif isinstance(item, str):
                out.append(item)
        return out[:MAX_LIST]

    lines: list[str] = []
    recent = _titles(result.get("recently_watched"))
    if recent:
        lines += ["Recently watched (you):"] + [f"  - {t}" for t in recent]
    else:
        lines += ["No recent personal watch history."]

    summary = str(result.get("year_history_summary") or "").strip()
    if summary:
        lines += ["", summary]

    # NOTE: top_movies_30d / top_tv_30d are SERVER-WIDE trends, not this user's
    # history. The catalog description tells the model never to present them as
    # personal. With no model here, the label must do that work.
    server_wide = _titles(result.get("top_movies_30d")) + _titles(result.get("top_tv_30d"))
    if server_wide:
        lines += ["", "Popular on the server lately (everyone, not you):"]
        lines += [f"  - {t}" for t in server_wide[:MAX_LIST]]
    return "\n".join(lines)


def render_send_admin_prowl_notice(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return "Reported to the admin. He'll see it on his phone."
    return "Could not reach the admin notification service. Your report was NOT sent."


def render_set_my_friendly_name(result: dict[str, Any]) -> str:
    failure = _failure(result, "That name could not be set.")
    if failure:
        return failure
    return str(result.get("user_summary") or f"Done — you're {result.get('friendly_name')} now.")


RENDERERS = {
    "search_media": render_search_media,
    "check_existing_media_status": render_check_existing_media_status,
    "check_library_inventory": render_check_library_inventory,
    "request_movie_for_user": render_request_movie_for_user,
    "request_show_scope_for_user": render_request_show_scope_for_user,
    "request_episode_for_user": render_request_episode_for_user,
    "check_movie_request_status": render_check_movie_request_status,
    "check_show_request_status": render_check_show_request_status,
    "get_show_season_status": render_get_show_season_status,
    "check_episode_status": render_check_episode_status,
    "repair_requested_movie": render_repair_requested_movie,
    "repair_requested_show": render_repair_requested_show,
    "get_user_watch_context": render_get_user_watch_context,
    "send_admin_prowl_notice": render_send_admin_prowl_notice,
    "set_my_friendly_name": render_set_my_friendly_name,
}


def render(tool: str, result: dict[str, Any]) -> str:
    """Render a tool result. A tool with no renderer must never be exposed."""
    renderer = RENDERERS.get(tool)
    if renderer is None:
        # Fail closed: never fall back to dumping the dict.
        return "That action is not available in Shabbos Mode. No language model was used."
    try:
        return renderer(result)
    except Exception:  # noqa: BLE001 - a renderer bug must not leak the raw dict
        return "The service replied, but the response could not be displayed. Nothing was changed."

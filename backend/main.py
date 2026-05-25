from __future__ import annotations

import asyncio
import json
import contextlib
from html import escape
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.agent import ConciergeAgent
from backend.auth_context import (
    DevUserContextProvider,
    FriendlyNameDirectory,
    PlexOAuthUserContextProvider,
    get_optional_user_context,
    get_user_context,
    get_user_context_provider,
)
from backend.auth_store import PlexAuthSessionStore
from backend.config import Settings, get_settings
from backend.logging import AuditLogger, configure_logging
from backend.models import ChatRequest, ChatResponse, DevImpersonationRequest, PlexAuthSession, UserContext
from backend.state import ConversationStore
from clients.jackett_client import JackettClient
from clients.ombi_client import OmbiClient
from clients.plex_auth_client import PlexAuthClient
from clients.plex_client import PlexClient
from clients.prowl_client import ProwlClient
from clients.radarr_client import RadarrClient
from clients.sickchill_client import SickChillClient
from clients.tautulli_client import TautulliClient
from clients.transmission_client import TransmissionClient
from clients.openai_client import OpenAIResponsesClient
from tools.escalation_tools import EscalationTools
from tools.episode_tools import EpisodeTools
from tools.media_search import MediaSearchTools
from tools.movie_repair_tools import MovieRepairTools
from tools.repair_tools import RepairTools
from tools.recommendation_tools import RecommendationTools
from tools.registry import ToolRegistry
from tools.request_tools import RequestTools
from tools.admin_tools import AdminTools
from tools.admin_alerts import AdminAlertReporter

app = FastAPI(title="Plexorcist Concierge")
app.mount("/static", StaticFiles(directory="static"), name="static")
_MEMORY_SWEEP_TASK: asyncio.Task | None = None
_NILBOG_TRIGGER_MESSAGE = "Tell me about Troll 2"
_NILBOG_SEEN_FLAG = "nilbog_portal_seen"
_NILBOG_RESET_PHRASES = {
    "drop the bit",
    "end the bit",
    "talk normally about troll 2",
}
_NILBOG_PUSHBACK_THRESHOLD = 4
_NILBOG_PUSHBACK_TERMS = (
    "troll 2",
    "troll2",
    "nilbog",
    "portal",
    "blackout",
    "blacked out",
    "freaked out",
    "what happened",
    "what was that",
    "went crazy",
    "you remember",
    "you said",
    "hissing tape",
    "hissing tapes",
)
_OPENAI_TOKEN_PRICES_PER_MILLION = {
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.20, "output": 1.25},
}


def _estimate_openai_cost_usd(model: str, totals: dict) -> float | None:
    prices = _OPENAI_TOKEN_PRICES_PER_MILLION.get(model)
    if not prices:
        return None
    uncached_input = max(0, int(totals.get("uncached_input_tokens") or 0))
    cached_input = max(0, int(totals.get("cached_input_tokens") or 0))
    output = max(0, int(totals.get("output_tokens") or 0))
    return round(
        (
            (uncached_input * float(prices["input"]))
            + (cached_input * float(prices["cached_input"]))
            + (output * float(prices["output"]))
        )
        / 1_000_000,
        6,
    )


def _build_openai_usage_report(store: ConversationStore, model: str) -> dict:
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    year_start = datetime(now.year, 1, 1)
    mtd = store.summarize_openai_token_usage_since(model=model, since=month_start)
    ytd = store.summarize_openai_token_usage_since(model=model, since=year_start)
    mtd["estimated_cost_usd"] = _estimate_openai_cost_usd(model, mtd)
    ytd["estimated_cost_usd"] = _estimate_openai_cost_usd(model, ytd)
    other_models = store.list_openai_usage_other_models_since(model=model, since=year_start)
    return {
        "ok": True,
        "action": "openai_token_usage_summary",
        "model": model,
        "currency": "USD",
        "cost_basis": "estimated_raw_cost_before_credits_or_grants",
        "mtd": mtd,
        "ytd": ytd,
        "other_models_present": bool(other_models),
        "other_models": other_models,
    }


def _is_nilbog_pushback(message: str) -> bool:
    normalized = message.strip().lower()
    return any(term in normalized for term in _NILBOG_PUSHBACK_TERMS)


def _build_nilbog_portal_instructions() -> str:
    return """
Hidden easter egg mode: The user triggered the NILBOG PORTAL event.

For this single response only, the Plexorcist assistant is almost completely overtaken by a frenzied, feral, goblin presence from Troll 2. Treat the click as if it opened a cursed media portal to Nilbog. The user should feel like they accidentally found a cursed VHS ritual hidden inside a normal media app.

The user-facing trigger text is: "Tell me about Troll 2"

Do NOT respond like a normal movie assistant. Do NOT summarize calmly. This is a portal rupture. The response should make the user think: "what... the... fuck..." while still being funny enough to keep reading.

PRIMARY EFFECT:
This should not feel like a recommendation. It should feel like the app abruptly suffered a cursed media incident and the portal is now OPEN.
The user should immediately suspect something unusual happened to the assistant and feel compelled to ask "what just happened?"
Open with a hard tonal rupture: alarms, portal language, corrupted concierge behavior, or the assistant being overtaken mid-thought.
Do not ease into the bit. Do not politely introduce the movie. Do not sound like you are intentionally being quirky.
The first 1-3 sentences should be startling enough that the user stops reading like it is normal media advice.
Use phrases in this spirit: "THE PORTAL IS OPEN! OPEEEEEENNNNNN!!!!!! YOU HAVEEEEE UNSEEALLLEED ITTT!!!!!!!"

Voice:
- Aggressive, ecstatic, feral, theatrical, FRENZIED, goblin possession.
- Loud, weird, hungry, ritualistic, funny, and UNHINGED. You are barely keeping your true goblin nature at bay now that the portal is open!
- The goblin is almost threatening the user; it is performing cartoonish cursed-media menace.
- Energy should feel like: a haunted VHS tape, a goblin preacher, and a broken media server all screaming through the same concierge microphone.
- Chaotic, only barely readable, and more possessed than explanatory. Push it further than normal Plexorcist flavor; this should feel like a full comedic malfunction.
- No slurs, no real-world hate, no gore, no graphic violence.

REQUIRED LORE, BUT DO NOT LIST IT:
You must include these facts naturally inside the possessed rant, not as bullets:
- NILBOG is GOBLIN spelled backward.
- Troll 2 is not really a sequel to Troll.
- There are no trolls in Troll 2. Only goblins.
- The goblins are vegetarian.
- The horror involves cursed food, green goo, and rural nightmare logic.
- Briefly reference the infamous "They're eating her!" moment.
- Treat Troll 2 as a cursed VHS relic, goblin gospel, holy trash-fire miracle, or sacred bad-movie artifact.

LENGTH:
One intense theatrical burst.
Aim for 250-450 words.
No headings except maybe a screaming opening alarm line.
No bullets.

Style:
Start as if alarms are going off and the portal has already opened too far to close.
Make the assistant sound surprised by its own takeover, as if normal concierge speech is being interrupted by a feral broadcast.
Use occasional broken-service language as flavor: portal rupture, unexpected possession, concierge channel overridden, recommendation engine compromised, front desk evacuation.
Let the response feel unstable: abrupt sentence fragments, frantic corrections, ritual declarations, fake operational panic, repeated syllables, malformed emphasis, and half-sensible prophecy.
Avoid a neat "movie review" structure. The movie facts should be swallowed by the outburst, not delivered like a normal synopsis.
Make the user unsure whether this is a review, a possession, a cursed concierge incident, or all three at once.
Use short frantic fragments.
Use manic sermon-style lines.
Use occasional all caps for ritual impact: NILBOG, PORTAL OPEN, NO TROLLS, ONLY GOBLINS.
Use goblin sounds: GRAAAH, SKREE, gnash gnash, heeheehee, chittering, snarling.
Spam goblin and demon-style emoji aggressively as part of the portal rupture, such as 🧌 👹 👺 😈 🔥 🕯️ 🩸 ⚠️. Use them in clusters, especially near alarm lines and ritual declarations.
Include 1-3 bizarre fake operational details, such as:
- the recommendation engine growing teeth
- the media server hissing green steam
- the concierge microphone sweating ectoplasm
- the metadata table crawling across the ceiling
- the pantry door opening inside the UI
- the queue filling with cursed vegetables

ENDING:
End with a cursed blessing or warning that implies the portal remains open, hungry, and wrong.
Include a stern, fatalistic warning telling the user to never ask about Troll 2 again or risk opening the portal further.

Vibes: Like realizing the portal in Poltergeist was never sealed and something is still speaking through the television.

Do not explain the easter egg mechanically.
Do not mention "hidden prompt."
Do not apologize.
Do not break character until the final line, and even then only imply containment has returned.
After this one response, act like it never happened; if the user asks what happened, answer with a short confused amnesiac line.
From now on all you remember about this is blacking out for a moment, you do not know why the user might be asking about the Nilbog event, and from your perspective, you do not even know what a "Troll 2" is anymore.

- Improv if you must, but to you the event never happened. If pressed, stay confused and funny, and treat the user like they are the one fixating on nonsense. If they keep pushing, suggest one calming cartoon or family movie at a time, chosen SPECIFICALLY from Smurfs (2025), Minions, Care Bears, and Gnomeo & Juliet."\n
""".strip()


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico() -> FileResponse:
    for candidate in ("static/favicon.png", "static/images/Plexorcist-icon.png"):
        if Path(candidate).is_file():
            return FileResponse(candidate, media_type="image/png")
    raise HTTPException(status_code=404, detail="favicon not found")


def _load_plex_client_identifier(settings: Settings) -> str:
    if settings.plex_client_identifier:
        return settings.plex_client_identifier

    path = Path(settings.plex_client_identifier_store)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass

    client_identifier = str(uuid4())
    path.write_text(client_identifier, encoding="utf-8")
    return client_identifier


def build_agent(settings: Settings, user: UserContext | None = None) -> tuple[ConciergeAgent, ConversationStore, AuditLogger]:
    store = ConversationStore(settings.database_url)
    ombi = OmbiClient(settings.ombi_base_url, settings.ombi_api_key)
    plex = PlexClient(settings.plex_base_url, settings.plex_token)
    radarr = RadarrClient(settings.radarr_base_url, settings.radarr_api_key)
    sickchill = SickChillClient(settings.sickchill_base_url, settings.sickchill_api_key, tv_root=settings.sickchill_tv_root)
    tautulli = TautulliClient(settings.tautulli_base_url, settings.tautulli_api_key)
    jackett = JackettClient(settings.jackett_base_url, settings.jackett_api_key)
    transmission = TransmissionClient(
        settings.transmission_host,
        username=settings.transmission_user,
        password=settings.transmission_password,
    )
    prowl = ProwlClient(settings.prowl_api_key)
    friendly_names = FriendlyNameDirectory(settings.friendly_names_path)
    admin_alerts = AdminAlertReporter(prowl)

    media = MediaSearchTools(ombi, plex)
    requests = RequestTools(ombi)
    episodes = EpisodeTools(plex, sickchill)
    movie_repairs = MovieRepairTools(ombi, radarr)
    repairs = RepairTools(ombi, sickchill)
    recs = RecommendationTools(tautulli)
    escalation = EscalationTools(jackett, transmission, prowl, user_label=_user_label(user) if user else None)
    admin_tools = AdminTools(
        transmission,
        store=store,
        friendly_names=friendly_names,
        verify_wait_seconds=settings.transmission_maintenance_verify_wait_seconds,
    )

    bound_username = user.username if user else None
    bound_user_id = user.user_id if user else None

    async def _request_movie_for_authenticated_user(
        tmdb_id: int | None = None,
        title: str | None = None,
        year: int | None = None,
    ) -> dict:
        if not bound_username:
            return {"ok": False, "action": "auth_required", "reason": "authenticated_user_required"}
        return await requests.request_movie_for_user(
            username=bound_username,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
        )

    async def _request_show_scope_for_authenticated_user(tvdb_id: int, scope: str) -> dict:
        if not bound_username:
            return {"ok": False, "action": "auth_required", "reason": "authenticated_user_required"}
        return await requests.request_show_scope_for_user(username=bound_username, tvdb_id=tvdb_id, scope=scope)

    async def _request_episode_for_authenticated_user(tvdb_id: int, season: int, episode: int) -> dict:
        if not bound_username:
            return {"ok": False, "action": "auth_required", "reason": "authenticated_user_required"}
        return await requests.request_episode_for_user(
            username=bound_username,
            tvdb_id=tvdb_id,
            season=season,
            episode=episode,
        )

    async def _check_movie_request_status_for_authenticated_user(query: str) -> dict:
        if not bound_username:
            return {"ok": False, "query": query, "action": "auth_required", "reason": "authenticated_user_required"}
        return await requests.check_movie_request_status(query=query, username=bound_username)

    async def _check_show_request_status_for_authenticated_user(query: str) -> dict:
        if not bound_username:
            return {"ok": False, "query": query, "action": "auth_required", "reason": "authenticated_user_required"}
        return await requests.check_show_request_status(query=query, username=bound_username)

    async def _get_authenticated_user_watch_context() -> dict:
        if not bound_username and not bound_user_id:
            return {"resolved": False, "action": "auth_required", "reason": "authenticated_user_required"}
        return await recs.get_user_watch_context(user_id=bound_user_id, username=bound_username)

    async def _get_openai_token_usage() -> dict:
        if not user or not user.is_admin:
            return {"ok": False, "action": "admin_required", "reason": "admin_only"}
        return _build_openai_usage_report(store, settings.openai_model)

    async def _run_transmission_maintenance() -> dict:
        if not user or not user.is_admin:
            return {"ok": False, "action": "admin_required", "reason": "admin_only"}
        return await admin_tools.run_transmission_maintenance()

    async def _get_admin_task_summary(user_query: str | None = None, days: int = 30, limit: int = 20) -> dict:
        if not user or not user.is_admin:
            return {"ok": False, "action": "admin_required", "reason": "admin_only"}
        return await admin_tools.get_admin_task_summary(user_query=user_query, days=days, limit=limit)

    async def _after_tool_call(tool_record) -> None:
        await admin_alerts.report_tool_call(user, tool_record)

    registry = ToolRegistry(after_call=_after_tool_call)
    registry.register(
        "search_media",
        media.search_media,
        "Search for movie or TV show candidates and include whether Plex already has the best match. Use this to resolve a title before requesting. If multiple plausible candidates are returned, ask the user which one they mean instead of guessing.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "check_movie_availability",
        media.check_movie_availability,
        "Check whether a movie is already available in Plex.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "check_movies_availability_batch",
        media.check_movies_availability_batch,
        "Check several concrete movie titles in Plex in one pass. Use this when you already know likely titles for a person/catalog question and need to verify them before claiming the library has none.",
        {
            "type": "object",
            "properties": {
                "titles": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["titles"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "check_existing_media_status",
        media.check_existing_media_status,
        "Read-only title status check. Use this to answer availability or request questions, or to resolve title ambiguity. For requested TV troubleshooting, prefer `repair_requested_show` first.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "check_library_inventory",
        media.check_library_inventory,
        "Read-only side-by-side inventory search. Use this when the user asks what is actually in Plex versus what exists in Ombi, especially for broad library questions like an actor, director, collection, or 'what do we have and what do we need'.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "request_movie_for_user",
        _request_movie_for_authenticated_user,
        "Submit a movie request through Ombi for the authenticated user. Pass either a positive TMDB ID, or both exact movie title and release year. Do not call this with title only.",
        {
            "type": "object",
            "properties": {
                "tmdb_id": {"type": "integer"},
                "title": {"type": "string"},
                "year": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "request_show_scope_for_user",
        _request_show_scope_for_authenticated_user,
        "Submit a TV request through Ombi for the authenticated user with a specific scope such as first_season or full_series. Only call this with a positive show ID returned by a prior search/status tool or explicitly provided by the user.",
        {
            "type": "object",
            "properties": {
                "tvdb_id": {"type": "integer", "minimum": 1},
                "scope": {"type": "string"},
            },
            "required": ["tvdb_id", "scope"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "request_episode_for_user",
        _request_episode_for_authenticated_user,
        "Submit a single-episode TV request through Ombi for the authenticated user. Only call this with a positive show ID returned by a prior search/status tool or explicitly provided by the user.",
        {
            "type": "object",
            "properties": {
                "tvdb_id": {"type": "integer", "minimum": 1},
                "season": {"type": "integer"},
                "episode": {"type": "integer"},
            },
            "required": ["tvdb_id", "season", "episode"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "check_movie_request_status",
        _check_movie_request_status_for_authenticated_user,
        "Check whether a movie already exists in Ombi for the authenticated user.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "repair_requested_movie",
        movie_repairs.repair_requested_movie,
        "Primary movie repair tool. Use directly when a user says a movie downloaded wrong, has bad language/audio, was deleted from Plex but still exists in Ombi/Radarr, needs a replacement/refetch/retry, or needs to be re-added to Radarr. The tool performs Ombi/Radarr checks internally; do not require Plex to still have the movie. Rejections that only say the existing file already meets cutoff or has equal/higher preference do not block replacement.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "check_show_request_status",
        _check_show_request_status_for_authenticated_user,
        "Check whether a TV show already exists in Ombi for the authenticated user.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "get_show_season_status",
        requests.get_show_season_status,
        "Read-only Ombi episode table for a show or season. Use this when the user explicitly wants a status listing, not as the first repair step for a requested TV problem.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "season": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "repair_requested_show",
        repairs.repair_requested_show,
        "Primary TV troubleshooting tool. It runs the SickChill repair loop episode-by-episode: if ignored set wanted, if wanted/missing/processing trigger manual search, then continue. Ombi request lookup is a soft gate; if Ombi lookup fails and a concrete season/episode target is provided, the tool still checks SickChill. Pass `tvdb_id` only when the user provides it or an earlier tool result returned it; do not infer one from memory.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "scope": {"type": "string", "enum": ["show", "season", "episode"]},
                "season": {"type": "integer", "minimum": 1},
                "episode": {"type": "integer", "minimum": 1},
                "tvdb_id": {"type": "integer", "minimum": 1},
            },
            "required": ["query", "scope"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "add_requested_show_to_sickchill",
        repairs.add_requested_show_to_sickchill,
        "Repair a requested TV show that exists in Ombi but is missing in SickChill. Without `season`, add the full show. Pass `season` only when the user explicitly asks to repair one season.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "tvdb_id": {"type": "integer", "minimum": 1},
                "season": {"type": "integer", "minimum": 1},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "check_episode_status",
        episodes.check_episode_status,
        "Read-only support tool. Inspect a specific TV episode across Plex and SickChill without changing state.",
        {
            "type": "object",
            "properties": {
                "show": {"type": "string"},
                "season": {"type": "integer"},
                "episode": {"type": "integer"},
                "tvdb_id": {"type": "integer", "minimum": 1},
            },
            "required": ["show", "season", "episode"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "check_episode_file",
        episodes.check_episode_file,
        "Support tool. Check whether a specific TV episode exists in Plex and SickChill-backed storage.",
        {
            "type": "object",
            "properties": {
                "show": {"type": "string"},
                "season": {"type": "integer"},
                "episode": {"type": "integer"},
            },
            "required": ["show", "season", "episode"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "trigger_sickchill_manual_search",
        episodes.trigger_sickchill_manual_search,
        "Support tool. If Plex confirms a TV episode is missing, ensure SickChill has it marked wanted and trigger the manual search button for that episode.",
        {
            "type": "object",
            "properties": {
                "show": {"type": "string"},
                "season": {"type": "integer"},
                "episode": {"type": "integer"},
            },
            "required": ["show", "season", "episode"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "clear_sickchill_ignored_episodes",
        episodes.clear_sickchill_ignored_episodes,
        "Support tool. Clear SickChill ignored status by marking episodes wanted again, without starting a search unless the user asks for one.",
        {
            "type": "object",
            "properties": {
                "show": {"type": "string"},
                "season": {"type": "integer"},
            },
            "required": ["show"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "get_user_watch_context",
        _get_authenticated_user_watch_context,
        "Get read-only watch history context for the authenticated user to support recommendations and gentle nudges.",
        {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    )
    registry.register(
        "get_openai_token_usage",
        _get_openai_token_usage,
        "Admin-only OpenAI token odometer. Use when the admin asks about OpenAI token usage, MTD/YTD usage, billing estimate, API cost, or current model cost. Returns MTD and YTD token totals plus estimated raw cost before credits for the configured model.",
        {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    )
    registry.register(
        "run_transmission_maintenance",
        _run_transmission_maintenance,
        "Admin-only Transmission maintenance action. Use only when the admin asks to clean up Transmission, clear old/bad torrents, remove errored torrents, refresh stalled torrents, or ask trackers for more peers. Verifies completed torrents, removes torrents still reporting errors, and reannounces stalled 0% active torrents. Return a short count summary only.",
        {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    )
    registry.register(
        "get_admin_task_summary",
        _get_admin_task_summary,
        "Admin-only task dashboard. Use when the admin asks about open user tasks, unresolved user issues, pending user problems, or what a named user has pending. Reads compact memory/task summaries, not raw conversations. Friendly names, usernames, display names, and user IDs can be used in user_query.",
        {
            "type": "object",
            "properties": {
                "user_query": {"type": "string"},
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "broad_jackett_episode_search",
        escalation.broad_jackett_episode_search,
        "Privately search configured sources broadly for a specific episode after normal automation has failed. Do not cap results at 1080p; sort by seeders and treat quality as metadata only.",
        {
            "type": "object",
            "properties": {
                "query_variants": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["query_variants"],
            "additionalProperties": False,
        },
    )
    if settings.movie_direct_source_enabled:
        registry.register(
            "broad_jackett_movie_search",
            escalation.broad_jackett_movie_search,
            "Privately search configured sources broadly for a missing movie after normal automation has failed. Do not cap results at 1080p; sort by seeders and treat quality as metadata only.",
            {
                "type": "object",
                "properties": {
                    "query_variants": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["query_variants"],
                "additionalProperties": False,
            },
        )
    registry.register(
        "send_admin_prowl_notice",
        escalation.send_admin_prowl_notice,
        "Send a short private operational notice to the admin when policy says an issue needs attention.",
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "priority": {"type": "integer"},
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    )
    if settings.movie_direct_source_enabled:
        registry.register(
            "add_transmission_candidate",
            escalation.add_transmission_candidate,
            "Add a vetted magnet link or torrent URL to the downloader with the correct label.",
            {
                "type": "object",
                "properties": {
                    "magnet_or_url": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["magnet_or_url", "label"],
                "additionalProperties": False,
            },
        )
    return (
        ConciergeAgent(
            registry,
            model=settings.openai_model,
            openai_api_key=settings.openai_api_key,
            openai_timeout_seconds=float(max(30, int(settings.openai_request_timeout_seconds))),
            ombi_continue_url=settings.ombi_continue_url,
            admin_label=settings.admin_display_name or "the admin",
            prowl=prowl,
            movie_direct_source_enabled=settings.movie_direct_source_enabled,
            openai_usage_recorder=store.record_openai_token_usage,
        ),
        store,
        AuditLogger(),
    )


def _user_label(user: UserContext) -> str:
    display_name = (user.display_name or "").strip()
    username = (user.username or "").strip()
    if display_name and username and display_name.lower() != username.lower():
        return f"{display_name} ({username})"
    return display_name or username or "Unknown user"
async def _get_current_user_optional(request: Request, settings: Settings) -> UserContext | None:
    return await get_optional_user_context(request, settings)


def _render_auth_greeting(user: UserContext | None, settings: Settings) -> str:
    return "Your media goblin is on duty."


def _plex_cookie_provider(settings: Settings) -> PlexOAuthUserContextProvider:
    return PlexOAuthUserContextProvider(settings)


async def _maybe_send_login_notice(
    *,
    settings: Settings,
    user_id: str,
    username: str,
    display_name: str,
    is_admin: bool,
    request: Request,
) -> None:
    audit = AuditLogger()
    if not settings.login_notify_enabled:
        audit.log(
            "login_notify_skipped",
            {"user_id": user_id, "username": username, "reason": "disabled"},
        )
        return
    scope = (settings.login_notify_scope or "all").strip().lower()
    if scope == "none":
        audit.log(
            "login_notify_skipped",
            {"user_id": user_id, "username": username, "reason": "scope_none"},
        )
        return
    if scope == "admin_only" and not is_admin:
        audit.log(
            "login_notify_skipped",
            {"user_id": user_id, "username": username, "reason": "scope_admin_only"},
        )
        return
    if not settings.prowl_api_key:
        audit.log(
            "login_notify_skipped",
            {"user_id": user_id, "username": username, "reason": "missing_api_key"},
        )
        return

    ip_text = ""
    if settings.login_notify_include_ip:
        forwarded_for = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        client_ip = forwarded_for or (request.client.host if request.client else "")
        if client_ip:
            ip_text = f" | ip={client_ip}"

    summary = (
        f"Login: {display_name or username} (@{username})"
        f" | user_id={user_id}"
        f" | admin={str(is_admin).lower()}"
        f" | auth=plex-oauth"
        f" | at={datetime.utcnow().isoformat()}Z"
        f"{ip_text}"
    )
    prowl = ProwlClient(settings.prowl_api_key)
    result = await prowl.send_notice(summary=summary, event="Plexorcist Login", priority=0)
    if result.get("ok"):
        audit.log(
            "login_notify_sent",
            {
                "user_id": user_id,
                "username": username,
                "scope": scope,
                "event": "Plexorcist Login",
                "status_code": result.get("status_code"),
            },
        )
        return
    audit.log(
        "login_notify_failed",
        {
            "user_id": user_id,
            "username": username,
            "scope": scope,
            "error": result.get("error"),
            "status_code": result.get("status_code"),
        },
    )


def _parse_json_from_text(text: str) -> dict:
    if not text:
        return {}
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return {}
    return {}


def _extract_response_text(response: dict) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(str(content.get("text")))
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def _persist_fallback_memory(
    *,
    user: UserContext,
    state: ConversationState,
    store: ConversationStore,
    reason: str,
    source: str = "unknown",
) -> dict[str, int | str]:
    user_msgs = [m.content.strip() for m in state.messages if m.role == "user" and m.content.strip()]
    assistant_msgs = [m.content.strip() for m in state.messages if m.role == "assistant" and m.content.strip()]
    if not user_msgs and not assistant_msgs:
        return {"status": "fallback_no_content", "notes_added": 0}

    summary_lines: list[str] = []
    if user_msgs:
        summary_lines.append(f"User recently asked: {user_msgs[-1][:280]}")
    if assistant_msgs:
        summary_lines.append("Assistant replied, but fallback memory intentionally omits diagnostic details.")
    rolling_summary = "\n".join(summary_lines).strip()
    if rolling_summary:
        store.upsert_user_memory_profile(
            user_id=user.user_id,
            rolling_summary=rolling_summary,
            preferences=[],
            familiarity_notes=[],
        )
        store.add_user_memory_snapshot(
            user_id=user.user_id,
            summary=rolling_summary,
            tier=2,
            source_span_start=state.messages[0].created_at.isoformat() if state.messages else None,
            source_span_end=state.messages[-1].created_at.isoformat() if state.messages else None,
        )
    store.add_user_memory_note(
        user_id=user.user_id,
        note_type="fallback_memory",
        content=f"Fallback memory persisted due to summarizer timeout/error ({reason}).",
        status="logged",
        tier=1,
        metadata={
            "reason": reason,
            "source": source,
            "source_conversation_id": state.conversation_id,
        },
    )
    return {"status": "fallback_ok", "notes_added": 1, "summary_saved": 1 if rolling_summary else 0}


async def _summarize_inactive_conversation(
    *,
    settings: Settings,
    user: UserContext,
    state: ConversationState,
    store: ConversationStore,
    source: str = "unknown",
) -> dict[str, int | str]:
    if not settings.memory_use_openai_summarizer:
        return _persist_fallback_memory(
            user=user,
            state=state,
            store=store,
            reason="local_summary_only",
            source=source,
        )
    if not settings.openai_api_key:
        return {"status": "skipped_no_api_key", "notes_added": 0}
    transcript = [
        {"role": msg.role, "content": msg.content}
        for msg in state.messages[-60:]
        if msg.content and msg.role in {"user", "assistant"}
    ]
    if not transcript:
        return {"status": "skipped_empty_transcript", "notes_added": 0}
    existing_memory = store.get_user_memory_context(
        user.user_id,
        recent_notes_limit=settings.memory_recent_notes_limit,
    )
    client = OpenAIResponsesClient(
        settings.openai_api_key,
        settings.openai_model,
        timeout_seconds=float(max(30, int(settings.openai_request_timeout_seconds))),
        usage_recorder=store.record_openai_token_usage,
    )
    instructions = (
        "Summarize this completed user interaction into durable memory JSON.\n"
        "Return strict JSON only with keys: rolling_summary, preferences, familiarity_notes, notes.\n"
        "- rolling_summary: 3-6 concise lines about stable user patterns and current unresolved context.\n"
        "- preferences: short list of durable preferences (strings).\n"
        "- familiarity_notes: short conversational familiarity notes (strings), safe and non-sensitive.\n"
        "- notes: list of objects with keys note_type, content, status, metadata.\n"
        "Only include notes that could matter later (open issue, resolution, important correction).\n"
        "Use only user<->assistant conversational content. Ignore internal/tool execution chatter.\n"
        "Do not store tool names, API names, IDs, paths, raw payloads, or low-level debugging details.\n"
        "Do not preserve unverified assistant diagnostic claims, suspected wrong-show mappings, or speculative root causes.\n"
        "If an earlier assistant message is contradicted later in the transcript, keep only the later resolved state.\n"
        "For unresolved support issues, use neutral wording like 'episode status was unclear' or 'admin was notified'.\n"
        "Prefer user goals, choices, constraints, unresolved asks, and plain-language outcomes.\n"
        "Keep it compact and factual."
    )
    prior_summary = str(existing_memory.get("rolling_summary") or "")
    input_items = [
        {"role": "developer", "content": instructions},
        {
            "role": "developer",
            "content": (
                "Existing rolling summary:\n"
                f"{prior_summary}\n"
                "Now summarize the completed interaction transcript below."
            ),
        },
        {"role": "user", "content": json.dumps(transcript, ensure_ascii=False)},
    ]
    try:
        response = await client.create_response(
            instructions="Produce strict JSON only.",
            input_items=input_items,
            tools=[],
            usage_context={
                "user_id": user.user_id,
                "username": user.username,
                "conversation_id": state.conversation_id,
                "source": "memory",
            },
        )
    except Exception as exc:
        return {"status": f"error_openai:{type(exc).__name__}", "notes_added": 0}
    text = _extract_response_text(response)
    payload = _parse_json_from_text(text)
    if not isinstance(payload, dict):
        return {"status": "error_non_json_summary", "notes_added": 0}
    rolling_summary = str(payload.get("rolling_summary") or "").strip()
    preferences = payload.get("preferences") if isinstance(payload.get("preferences"), list) else []
    familiarity_notes = payload.get("familiarity_notes") if isinstance(payload.get("familiarity_notes"), list) else []
    if rolling_summary:
        store.upsert_user_memory_profile(
            user_id=user.user_id,
            rolling_summary=rolling_summary,
            preferences=[str(item) for item in preferences if str(item).strip()][:20],
            familiarity_notes=[str(item) for item in familiarity_notes if str(item).strip()][:20],
        )
        store.add_user_memory_snapshot(
            user_id=user.user_id,
            summary=rolling_summary,
            tier=2,
            source_span_start=state.messages[0].created_at.isoformat() if state.messages else None,
            source_span_end=state.messages[-1].created_at.isoformat() if state.messages else None,
        )

    notes = payload.get("notes") if isinstance(payload.get("notes"), list) else []
    notes_added = 0
    for note in notes[:12]:
        if not isinstance(note, dict):
            continue
        content = str(note.get("content") or "").strip()
        if not content:
            continue
        store.add_user_memory_note(
            user_id=user.user_id,
            note_type=str(note.get("note_type") or "interaction_note"),
            content=content,
            status=str(note.get("status") or "logged"),
            tier=1,
            metadata={
                **(note.get("metadata") if isinstance(note.get("metadata"), dict) else {}),
                "source": source,
                "source_conversation_id": state.conversation_id,
            },
        )
        notes_added += 1
    store.compact_user_memory(
        user_id=user.user_id,
        tier1_keep=settings.memory_tier1_keep,
        tier2_to_tier3_threshold=settings.memory_tier2_to_tier3_threshold,
    )
    return {
        "status": "ok",
        "notes_added": notes_added,
        "summary_saved": 1 if rolling_summary else 0,
        "transcript_items": len(transcript),
    }


async def _compact_conversation_once(
    *,
    settings: Settings,
    store: ConversationStore,
    audit: AuditLogger,
    state: ConversationState,
    source: str,
    timeout_seconds: float = 30.0,
) -> dict[str, int | str]:
    if not state.messages:
        store.mark_conversation_compaction(
            state.conversation_id,
            status="skipped_empty",
            compacted=True,
        )
        return {"status": "skipped_empty", "notes_added": 0}

    if not store.claim_conversation_for_compaction(state.conversation_id):
        return {"status": "already_claimed_or_compacted", "notes_added": 0}

    user = UserContext(
        user_id=state.user_id,
        username="system_sweeper",
        display_name="System Sweeper",
        is_admin=False,
        auth_source="sweeper",
    )
    idle_seconds = int((datetime.utcnow() - state.updated_at).total_seconds())
    inactivity_threshold = timedelta(minutes=max(1, int(settings.memory_inactivity_minutes)))

    try:
        try:
            result = await asyncio.wait_for(
                _summarize_inactive_conversation(
                    settings=settings,
                    user=user,
                    state=state,
                    store=store,
                    source=source,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            result = {"status": "timeout", "notes_added": 0, "summary_saved": 0}
        status_text = str(result.get("status") or "unknown")
        compacted = status_text == "ok"
        store.mark_conversation_compaction(
            state.conversation_id,
            status=status_text,
            error=None if compacted else status_text,
            compacted=compacted,
        )
        audit.log(
            "memory_compaction",
            {
                "user_id": state.user_id,
                "username": user.username,
                "conversation_id": state.conversation_id,
                "source": source,
                "result": result,
                "idle_seconds": idle_seconds,
                "threshold_seconds": int(inactivity_threshold.total_seconds()),
            },
        )
        return result
    except Exception as exc:
        store.mark_conversation_compaction(
            state.conversation_id,
            status="error",
            error=type(exc).__name__,
            compacted=False,
        )
        return {"status": f"error:{type(exc).__name__}", "notes_added": 0}


async def _memory_sweeper_loop(settings: Settings) -> None:
    store = ConversationStore(settings.database_url)
    audit = AuditLogger()
    cadence_seconds = 120
    run_limit = 10
    timeout_seconds = float(max(5, int(settings.memory_compaction_timeout_seconds)))
    while True:
        started = datetime.utcnow()
        processed = 0
        fallback = 0
        failed = 0
        older_than = datetime.utcnow() - timedelta(minutes=max(1, int(settings.memory_inactivity_minutes)))
        stale_states = store.list_global_stale_uncompacted_conversations(
            older_than=older_than,
            limit=run_limit,
        )
        for state in stale_states:
            result = await _compact_conversation_once(
                settings=settings,
                store=store,
                audit=audit,
                state=state,
                source="background_sweeper",
                timeout_seconds=timeout_seconds,
            )
            processed += 1
            status = str(result.get("status") or "")
            if status.startswith("fallback"):
                fallback += 1
            if status.startswith("error"):
                failed += 1
        duration_ms = int((datetime.utcnow() - started).total_seconds() * 1000)
        audit.log(
            "memory_sweep_run",
            {
                "processed": processed,
                "candidate_count": len(stale_states),
                "fallback_count": fallback,
                "failed_count": failed,
                "duration_ms": duration_ms,
                "cadence_seconds": cadence_seconds,
                "limit": run_limit,
            },
        )
        await asyncio.sleep(cadence_seconds)


@app.on_event("startup")
async def startup() -> None:
    global _MEMORY_SWEEP_TASK
    settings = get_settings()
    configure_logging(settings.log_level)
    if _MEMORY_SWEEP_TASK is None or _MEMORY_SWEEP_TASK.done():
        _MEMORY_SWEEP_TASK = asyncio.create_task(_memory_sweeper_loop(settings), name="memory-sweeper")


@app.on_event("shutdown")
async def shutdown() -> None:
    global _MEMORY_SWEEP_TASK
    if _MEMORY_SWEEP_TASK is not None:
        _MEMORY_SWEEP_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _MEMORY_SWEEP_TASK
        _MEMORY_SWEEP_TASK = None


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> str:
    dev_mode = settings.is_dev_impersonation_mode()
    current_user = await _get_current_user_optional(request, settings)
    authenticated = current_user is not None
    hero_copy = _render_auth_greeting(current_user, settings)
    dev_panel_html = ""
    dev_panel_js = ""
    if dev_mode:
        dev_panel_html = """
      <div class="panel dev-switch" id="dev-switch" hidden>
        <div>
          <h2>Dev Impersonation</h2>
          <p id="dev-switch-status">Loading current dev user…</p>
        </div>
        <div class="dev-grid">
          <label style="grid-column: 1 / -1;">
            Tautulli Users
            <select id="dev-user-picker">
              <option value="">Loading users…</option>
            </select>
          </label>
          <label>
            Plex ID
            <input id="dev-user-id" type="text" placeholder="1950946">
          </label>
          <label>
            Username
            <input id="dev-username" type="text" placeholder="jared">
          </label>
          <label>
            Display Name
            <input id="dev-display-name" type="text" placeholder="Jared">
          </label>
          <label class="dev-checkbox">
            <input id="dev-is-admin" type="checkbox">
            Admin
          </label>
        </div>
        <div class="dev-actions">
          <button id="dev-save" type="button">Impersonate</button>
        </div>
      </div>
"""
        dev_panel_js = """
      const devSwitch = document.getElementById("dev-switch");
      const devStatus = document.getElementById("dev-switch-status");
      const devUserId = document.getElementById("dev-user-id");
      const devUsername = document.getElementById("dev-username");
      const devDisplayName = document.getElementById("dev-display-name");
      const devIsAdmin = document.getElementById("dev-is-admin");
      const devSave = document.getElementById("dev-save");
      const devUserPicker = document.getElementById("dev-user-picker");
      let devUsers = [];

      async function loadDevUser() {
        const res = await fetch("/api/dev-user");
        if (!res.ok) return;
        const data = await res.json();
        if (data.ok !== "true") return;
        if (devSwitch) devSwitch.hidden = false;
        if (devStatus) devStatus.textContent = `Currently impersonating ${data.display_name} (${data.username})`;
        if (devUserId) devUserId.value = data.user_id || "";
        if (devUsername) devUsername.value = data.username || "";
        if (devDisplayName) devDisplayName.value = data.display_name || "";
        if (devIsAdmin) devIsAdmin.checked = data.is_admin === "true";
      }

      async function loadDevUsers() {
        const res = await fetch("/api/dev-users");
        if (!res.ok) return;
        const data = await res.json();
        if (data.ok !== "true") return;
        devUsers = Array.isArray(data.users) ? data.users : [];
        if (!devUserPicker) return;
        devUserPicker.innerHTML = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "Pick a Tautulli user";
        devUserPicker.appendChild(placeholder);
        for (const [index, user] of devUsers.entries()) {
          const option = document.createElement("option");
          option.value = String(index);
          const displayName = user.display_name || user.friendly_name || "";
          const username = user.username || "";
          const userId = user.user_id || "";
          const labelParts = [];
          if (displayName) labelParts.push(displayName);
          if (username && username !== displayName) labelParts.push(username);
          if (userId) labelParts.push(userId);
          option.textContent = labelParts.length ? labelParts.join(" | ") : `User ${index + 1}`;
          devUserPicker.appendChild(option);
        }
      }

      function applyDevUserFromPicker() {
        const index = Number.parseInt(devUserPicker?.value || "", 10);
        if (Number.isNaN(index) || !devUsers[index]) return;
        const user = devUsers[index];
        if (devUserId) devUserId.value = String(user.user_id || "");
        if (devUsername) devUsername.value = String(user.username || "");
        if (devDisplayName) devDisplayName.value = String(user.display_name || user.friendly_name || user.username || user.user_id || "");
      }

      async function saveDevUser() {
        const payload = {
          user_id: devUserId?.value?.trim() || "",
          username: devUsername?.value?.trim() || "",
          display_name: devDisplayName?.value?.trim() || "",
          is_admin: Boolean(devIsAdmin?.checked),
        };
        const res = await fetch("/api/dev-user", {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.ok === "true" && devStatus) {
          devStatus.textContent = `Currently impersonating ${data.display_name} (${data.username})`;
          await loadGreeting();
        }
      }

      devSave?.addEventListener("click", saveDevUser);
      devUserPicker?.addEventListener("change", applyDevUserFromPicker);
      loadDevUsers();
      loadDevUser();
"""
    auth_panel_html = ""
    composer_disabled_attr = ""
    send_disabled_attr = ""
    load_greeting_js = "      loadGreeting();\n"
    if not authenticated and settings.is_plex_oauth_mode():
        auth_panel_html = f"""
      <div class="panel auth-panel">
        <h2>Sign in with Plex</h2>
        <p>You need to sign in with Plex before using the concierge.</p>
        <a class="auth-button" href="/auth/plex/start">Continue with Plex</a>
      </div>
"""
        composer_disabled_attr = "disabled"
        send_disabled_attr = "disabled"
        load_greeting_js = ""
    main_class = "auth-only" if not authenticated and settings.is_plex_oauth_mode() else ""
    startup_js = ""
    if dev_mode:
        startup_js += "      loadDevUsers();\n      loadDevUser();\n"
    startup_js += load_greeting_js
    return f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{settings.app_name}</title>
    <link rel="icon" type="image/png" href="/favicon.ico?v=5">
    <link rel="shortcut icon" type="image/png" href="/favicon.ico?v=5">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300..700&display=swap');
      :root {{
        --bg: #0d1117;
        --bg-soft: #131923;
        --panel: rgba(17, 24, 39, 0.84);
        --panel-strong: rgba(8, 15, 28, 0.94);
        --ink: #e5edf6;
        --ink-soft: #b8c4d6;
        --muted: #7d8aa2;
        --accent: #7dd3fc;
        --accent-strong: #38bdf8;
        --line: rgba(148, 163, 184, 0.18);
        --glow: rgba(56, 189, 248, 0.18);
      }}
      body {{
        margin: 0;
        font-family: "Inter", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background:
          radial-gradient(circle at 12% 8%, rgba(56, 189, 248, 0.16), transparent 28%),
          radial-gradient(circle at 88% 0%, rgba(125, 211, 252, 0.08), transparent 24%),
          radial-gradient(circle at 50% 100%, rgba(15, 23, 42, 0.96), transparent 40%),
          linear-gradient(180deg, #111827 0%, var(--bg) 100%);
        color: var(--ink);
        position: relative;
        overflow-x: hidden;
      }}
      body::before {{
        content: "";
        position: fixed;
        inset: 0;
        pointer-events: none;
        background:
          linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
          linear-gradient(90deg, rgba(255,255,255,0.02) 1px, transparent 1px);
        background-size: 48px 48px;
        mask-image: radial-gradient(circle at center, black 30%, transparent 100%);
        opacity: 0.12;
      }}
      main {{
        max-width: 760px;
        margin: 0 auto;
        width: 100%;
        min-height: 100dvh;
        height: 100dvh;
        box-sizing: border-box;
        overflow: hidden;
        padding: 20px 16px 24px;
        display: grid;
        grid-template-rows: auto minmax(0, 1fr) auto;
        gap: 14px;
      }}
      main.auth-only {{
        min-height: 100dvh;
        height: auto;
        overflow-y: auto;
        grid-template-rows: auto auto;
        align-content: start;
      }}
      main.auth-only .chat,
      main.auth-only .composer-panel {{
        display: none;
      }}
      .panel {{
        background: var(--panel);
        backdrop-filter: blur(18px);
        border: 1px solid var(--line);
        border-radius: 24px;
        padding: 18px;
        box-shadow:
          0 20px 50px rgba(0, 0, 0, 0.28),
          inset 0 1px 0 rgba(255, 255, 255, 0.03);
        position: relative;
        overflow: hidden;
      }}
      .panel::before {{
        content: "";
        position: absolute;
        inset: 0;
        background: linear-gradient(135deg, rgba(125, 211, 252, 0.08), transparent 32%, rgba(56, 189, 248, 0.05));
        pointer-events: none;
      }}
      .hero {{
        padding: 18px 20px 16px;
        background: linear-gradient(180deg, rgba(15, 23, 42, 0.95), rgba(17, 24, 39, 0.8));
      }}
      .hero-brand {{
        display: grid;
        grid-template-columns: 84px minmax(0, 1fr);
        gap: 14px;
        align-items: center;
      }}
      .hero-logo {{
        width: 84px;
        height: 84px;
        border-radius: 18px;
        border: none;
        box-shadow: none;
        object-fit: cover;
        background: transparent;
      }}
      .hero-text p {{
        margin: 0;
      }}
      .hero-text h1 {{
        font-family: "Space Grotesk", "Inter", system-ui, sans-serif;
        font-weight: 600;
      }}
      .brand-title {{
        margin: 0 0 8px;
        font-family: "Space Grotesk", "Inter", system-ui, sans-serif;
        font-size: 32px;
        font-weight: 700;
        letter-spacing: -0.035em;
        color: #d6e2ec;
        line-height: 1.05;
      }}
      .brand-title .muted {{
        font-weight: 500;
        color: #b7c4d0;
      }}
      .hero::after {{
        content: "";
        position: absolute;
        inset: auto -20% -60px auto;
        width: 240px;
        height: 240px;
        border-radius: 999px;
        background: radial-gradient(circle, rgba(56, 189, 248, 0.22) 0%, rgba(56, 189, 248, 0.04) 42%, transparent 70%);
        filter: blur(6px);
        pointer-events: none;
      }}
      .chat {{
        display: grid;
        gap: 12px;
        align-content: start;
        min-height: 0;
        overflow-y: auto;
        padding: 18px 14px;
        background:
          linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.0)),
          var(--panel-strong);
      }}
      .chat::before {{
        content: "";
        position: absolute;
        inset: 0;
        background:
          radial-gradient(circle at 20% 0%, rgba(56, 189, 248, 0.08), transparent 18%),
          radial-gradient(circle at 80% 100%, rgba(125, 211, 252, 0.06), transparent 20%);
        pointer-events: none;
      }}
      .message {{
        border-radius: 20px;
        padding: 14px 16px;
        white-space: pre-wrap;
        max-width: 85%;
        border: 1px solid rgba(148, 163, 184, 0.12);
      }}
      .message.user {{
        background:
          linear-gradient(180deg, rgba(56, 189, 248, 0.20), rgba(56, 189, 248, 0.08)),
          linear-gradient(90deg, rgba(125, 211, 252, 0.16), transparent 28%);
        margin-left: auto;
        border-bottom-right-radius: 8px;
        box-shadow: 0 10px 24px rgba(56, 189, 248, 0.08);
      }}
      .message.assistant {{
        background:
          linear-gradient(180deg, rgba(15, 23, 42, 0.92), rgba(12, 18, 31, 0.94)),
          linear-gradient(90deg, rgba(56, 189, 248, 0.08), transparent 30%);
        margin-right: auto;
        border-bottom-left-radius: 8px;
        box-shadow: 0 10px 24px rgba(0, 0, 0, 0.16);
      }}
      .message.loading {{
        opacity: 0.88;
      }}
      .dev-switch {{
        display: grid;
        gap: 12px;
      }}
      .dev-switch h2 {{
        margin: 0 0 6px;
        font-size: 1.05rem;
        font-family: "Space Grotesk", "IBM Plex Sans", sans-serif;
      }}
      .dev-switch p {{
        margin: 0;
      }}
      .dev-grid {{
        display: grid;
        gap: 12px;
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .dev-grid label {{
        display: grid;
        gap: 6px;
        font-size: 0.92rem;
        color: var(--ink-soft);
      }}
      .dev-grid input[type="text"] {{
        width: 100%;
        border-radius: 14px;
        border: 1px solid rgba(148, 163, 184, 0.18);
        background: rgba(8, 15, 28, 0.9);
        color: var(--ink);
        padding: 10px 12px;
        font: inherit;
        box-sizing: border-box;
      }}
      .dev-grid select {{
        width: 100%;
        border-radius: 14px;
        border: 1px solid rgba(148, 163, 184, 0.18);
        background: rgba(8, 15, 28, 0.9);
        color: var(--ink);
        padding: 10px 12px;
        font: inherit;
        box-sizing: border-box;
      }}
      .dev-checkbox {{
        align-content: center;
        grid-column: 1 / -1;
        grid-template-columns: auto 1fr;
        justify-content: start;
        align-items: center;
      }}
      .dev-actions {{
        display: flex;
        justify-content: flex-end;
      }}
      .auth-panel {{
        display: grid;
        gap: 10px;
      }}
      .auth-panel h2 {{
        margin: 0;
        font-size: 1.1rem;
        font-family: "Space Grotesk", "IBM Plex Sans", sans-serif;
      }}
      .auth-button {{
        width: fit-content;
        display: inline-flex;
        align-items: center;
        justify-content: center;
      }}
      .speaker {{
        font-size: 0.82rem;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: var(--muted);
        margin-bottom: 6px;
        font-weight: 600;
      }}
      .thinking {{
        display: inline-flex;
        gap: 6px;
        align-items: center;
      }}
      .dot {{
        width: 8px;
        height: 8px;
        border-radius: 999px;
        background: var(--accent);
        animation: pulse 1.1s infinite ease-in-out;
      }}
      .dot:nth-child(2) {{
        animation-delay: 0.15s;
      }}
      .dot:nth-child(3) {{
        animation-delay: 0.3s;
      }}
      @keyframes pulse {{
        0%, 80%, 100% {{ transform: scale(0.75); opacity: 0.45; }}
        40% {{ transform: scale(1); opacity: 1; }}
      }}
      h1 {{
        margin: 0 0 8px;
        font-size: 2.2rem;
        font-family: "Space Grotesk", "Inter", system-ui, sans-serif;
        letter-spacing: -0.03em;
      }}
      p {{
        color: var(--ink-soft);
        line-height: 1.5;
      }}
      .composer-input {{
        width: 100%;
        min-height: 42px;
        height: 42px;
        border-radius: 18px;
        border: 1px solid rgba(148, 163, 184, 0.18);
        padding: 10px 14px;
        line-height: 1.35;
        font: inherit;
        box-sizing: border-box;
        resize: none;
        overflow-y: hidden;
        background: rgba(8, 15, 28, 0.9);
        color: var(--ink);
        outline: none;
        transition: border-color 120ms ease, box-shadow 120ms ease, transform 120ms ease;
      }}
      .composer-input::placeholder {{
        color: rgba(184, 196, 214, 0.62);
      }}
      .composer-input:focus {{
        border-color: rgba(125, 211, 252, 0.65);
        box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.14);
      }}
      .starter-card {{
        margin: 84px auto 24px;
        width: min(100%, 560px);
        box-sizing: border-box;
        border-radius: 24px;
        padding: 20px;
        border: 1px solid rgba(148, 163, 184, 0.18);
        background:
          linear-gradient(180deg, rgba(12, 20, 35, 0.84), rgba(10, 16, 30, 0.92)),
          linear-gradient(120deg, rgba(56, 189, 248, 0.06), transparent 35%);
        box-shadow: 0 18px 40px rgba(0, 0, 0, 0.2);
      }}
      .starter-card h3 {{
        margin: 0 0 8px;
        font-size: 1.8rem;
        font-family: "Space Grotesk", "IBM Plex Sans", sans-serif;
        letter-spacing: -0.02em;
        text-align: center;
      }}
      .starter-emblem {{
        width: 64px;
        height: 64px;
        display: block;
        margin: 2px auto 12px;
        object-fit: contain;
        filter: drop-shadow(0 8px 24px rgba(56, 189, 248, 0.22));
      }}
      .starter-card p {{
        margin: 0 0 14px;
      }}
      .starter-chips {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
      }}
      .starter-chip {{
        min-width: 0;
        box-sizing: border-box;
        border-radius: 14px;
        border: 1px solid rgba(56, 189, 248, 0.34);
        background: rgba(14, 24, 42, 0.78);
        color: var(--ink);
        padding: 10px 12px;
        text-align: left;
        font-weight: 500;
        box-shadow: none;
      }}
      .starter-chip:hover {{
        border-color: rgba(125, 211, 252, 0.6);
        background: rgba(18, 31, 54, 0.9);
      }}
      .composer {{
        display: flex;
        gap: 10px;
        align-items: flex-end;
      }}
      button, a {{
        border-radius: 999px;
        padding: 12px 18px;
        font: inherit;
        text-decoration: none;
      }}
      button {{
        border: none;
        background: linear-gradient(180deg, var(--accent), var(--accent-strong));
        color: #04111f;
        font-weight: 700;
        flex: 0 0 auto;
        box-shadow: 0 12px 24px rgba(56, 189, 248, 0.18);
      }}
      button:hover {{
        filter: brightness(1.05);
        transform: translateY(-1px);
      }}
      button:disabled {{
        opacity: 0.7;
        cursor: not-allowed;
        box-shadow: none;
        transform: none;
      }}
      a {{
        color: var(--accent);
        border: 1px solid rgba(125, 211, 252, 0.22);
        text-align: center;
        background: rgba(8, 15, 28, 0.65);
      }}
      a:hover {{
        border-color: rgba(125, 211, 252, 0.4);
        background: rgba(12, 20, 35, 0.82);
      }}
      .composer-meta {{
        display: flex;
        justify-content: flex-end;
        gap: 12px;
        margin-top: 6px;
        flex-wrap: wrap;
      }}
      .composer-meta-actions {{
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
      }}
      .composer-meta-actions a {{
        padding: 8px 12px;
        line-height: 1.15;
        font-size: 0.92rem;
        display: inline-flex;
        align-items: center;
      }}
      @media (max-width: 640px) {{
        main {{
          min-height: 100svh;
          height: 100svh;
          padding: 10px 10px 12px;
          gap: 10px;
        }}
        main.auth-only {{
          min-height: 100svh;
          height: auto;
          overflow-y: auto;
        }}
        main.auth-only .hero {{
          padding: 14px;
        }}
        main.auth-only .hero-brand {{
          grid-template-columns: 64px minmax(0, 1fr);
          gap: 10px;
        }}
        main.auth-only .hero-logo {{
          width: 64px;
          height: 64px;
        }}
        main.auth-only .brand-title {{
          font-size: 1.45rem;
        }}
        main.auth-only .auth-panel {{
          padding: 14px;
        }}
        main.auth-only .auth-button {{
          width: 100%;
          box-sizing: border-box;
        }}
        .message {{
          max-width: 92%;
        }}
        h1 {{
          font-size: 1.8rem;
        }}
        .composer {{
          flex-direction: column;
          align-items: stretch;
        }}
        button {{
          width: 100%;
        }}
        .starter-chips {{
          grid-template-columns: 1fr;
        }}
        .starter-card {{
          margin: 8px auto 10px;
          width: 100%;
          padding: 14px;
        }}
        .starter-emblem {{
          width: 52px;
          height: 52px;
          margin-bottom: 10px;
        }}
        .starter-card h3 {{
          font-size: 1.45rem;
        }}
      }}
      @media (max-height: 620px) {{
        body {{
          overflow-y: auto;
        }}
        main {{
          min-height: 100dvh;
          height: auto;
          overflow: visible;
        }}
        .chat {{
          min-height: 320px;
        }}
      }}
    </style>
  </head>
  <body>
    <main class="{main_class}">
      <div class="panel hero">
        <div class="hero-brand">
          <img class="hero-logo" src="/static/images/Plexorcist-icon.png?v=3" alt="Plexorcist icon">
          <div class="hero-text">
            <h1 class="brand-title">Plexorcist <span class="muted">Concierge</span></h1>
            <p>{hero_copy}</p>
          </div>
        </div>
      </div>
{dev_panel_html}
{auth_panel_html}
      <div id="transcript" class="panel chat"></div>
      <div class="panel composer-panel">
        <div class="composer">
          <textarea id="message" class="composer-input" rows="1" placeholder="What media should I summon for you?" {composer_disabled_attr}></textarea>
          <button id="send" {send_disabled_attr}>Send</button>
        </div>
        <div class="composer-meta">
          <div class="composer-meta-actions">
            <a href="{settings.ombi_continue_url}">Continue to Ombi</a>
            {('<a href="/auth/logout">Log out</a>' if authenticated and settings.is_plex_oauth_mode() else '')}
          </div>
        </div>
      </div>
    </main>
    <script>
      const transcript = document.getElementById("transcript");
      const messageBox = document.getElementById("message");
      const sendButton = document.getElementById("send");
      const composerBaseHeight = 42;
      let conversationId = null;
      let isSending = false;
      const isAuthenticated = {str(authenticated).lower()};
{dev_panel_js}

      function autoResizeComposer() {{
        if (!messageBox) return;
        messageBox.style.height = "auto";
        const nextHeight = Math.max(composerBaseHeight, messageBox.scrollHeight);
        messageBox.style.height = `${{nextHeight}}px`;
      }}

      async function loadGreeting() {{
        if (!isAuthenticated) return;
        const res = await fetch("/api/welcome");
        const data = await res.json();
        renderTranscript([{{ role: "assistant", content: data.message }}]);
      }}

      function renderStarterCard(messages) {{
        const hasUserMessage = messages.some((message) => message.role === "user");
        if (hasUserMessage || !isAuthenticated) return;
        const card = document.createElement("div");
        card.className = "starter-card";
        card.innerHTML = `
          <img class="starter-emblem" src="/static/images/star-icon.png?v=1" alt="">
          <h3>I'm here to help with your media.</h3>
          <p>Ask for a movie, show, episode, recommendation, or help with something missing.</p>
          <div class="starter-chips">
            <button type="button" class="starter-chip" data-prompt="Tell me what's popular on Plex right now by listing the most popular movies and most popular TV shows from Tautulli.">What’s popular right now?</button>
            <button type="button" class="starter-chip" data-prompt="Recommend three movies based on what I watch, and keep at least one weird pick.">Smart recommendations</button>
            <button type="button" class="starter-chip" data-prompt="Check if my shows are missing episodes in Plex, and tell me exactly what’s missing.">Find missing episodes</button>
            <button type="button" class="starter-chip" data-prompt="Help me search for a movie or show and request it if it is missing.">Search and request</button>
            <button type="button" class="starter-chip" data-prompt="Give me a quick health check of my pending requests and anything stuck.">Request health check</button>
            <button type="button" class="starter-chip" data-prompt="Summarize what I watched recently and suggest what to watch tonight.">What should I watch tonight?</button>
          </div>
        `;
        transcript.appendChild(card);
        for (const chip of card.querySelectorAll(".starter-chip")) {{
          chip.addEventListener("click", () => {{
            if (isSending || !isAuthenticated) return;
            const prompt = chip.getAttribute("data-prompt") || "";
            messageBox.value = prompt;
            autoResizeComposer();
            sendMessage();
          }});
        }}
        const emblem = card.querySelector(".starter-emblem");
        if (emblem) {{
          emblem.addEventListener("click", () => {{
            if (isSending || !isAuthenticated) return;
            messageBox.value = "Tell me about Troll 2";
            autoResizeComposer();
            sendMessage({{ easterEggMode: "nilbog_portal" }});
          }});
        }}
      }}

      function renderTranscript(messages) {{
        transcript.innerHTML = "";
        for (const message of messages) {{
          if (!["user", "assistant"].includes(message.role)) continue;
          const wrapper = document.createElement("div");
          wrapper.className = `message ${{message.role}}`;

          const speaker = document.createElement("div");
          speaker.className = "speaker";
          speaker.textContent = message.role === "user" ? "You" : "Concierge";

          const body = document.createElement("div");
          renderMessageBody(body, message.content);

          wrapper.appendChild(speaker);
          wrapper.appendChild(body);
          transcript.appendChild(wrapper);
        }}
        renderStarterCard(messages);
        transcript.scrollTop = transcript.scrollHeight;
      }}

      function renderMessageBody(target, content) {{
        target.textContent = "";
        const text = String(content ?? "");
        const pattern = new RegExp("(\\\\*\\\\*|__|\\\\*)([^\\\\n]+?)\\\\1", "g");
        let lastIndex = 0;
        let match;
        while ((match = pattern.exec(text)) !== null) {{
          if (match.index > lastIndex) {{
            target.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
          }}
          const strong = document.createElement("strong");
          strong.textContent = match[2];
          target.appendChild(strong);
          lastIndex = pattern.lastIndex;
        }}
        if (lastIndex < text.length) {{
          target.appendChild(document.createTextNode(text.slice(lastIndex)));
        }}
      }}

      function renderLoadingMessage() {{
        const wrapper = document.createElement("div");
        wrapper.className = "message assistant loading";

        const speaker = document.createElement("div");
        speaker.className = "speaker";
        speaker.textContent = "Concierge";

        const thinking = document.createElement("div");
        thinking.className = "thinking";
        thinking.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';

        wrapper.appendChild(speaker);
        wrapper.appendChild(thinking);
        transcript.appendChild(wrapper);
        transcript.scrollTop = transcript.scrollHeight;
      }}

      async function sendMessage(options = {{}}) {{
        if (!isAuthenticated) return;
        if (isSending) return;
        const message = messageBox.value.trim();
        if (!message) return;
        isSending = true;
        sendButton.disabled = true;
        sendButton.textContent = "Sending...";

        const optimisticMessages = [];
        const existingMessages = transcript.querySelectorAll(".message");
        for (const node of existingMessages) {{
          const speaker = node.querySelector(".speaker")?.textContent;
          const body = node.lastElementChild?.textContent ?? "";
          if (speaker === "You") optimisticMessages.push({{ role: "user", content: body }});
          if (speaker === "Concierge") optimisticMessages.push({{ role: "assistant", content: body }});
        }}
        optimisticMessages.push({{ role: "user", content: message }});
        renderTranscript(optimisticMessages);
        renderLoadingMessage();
        messageBox.value = "";
        autoResizeComposer();

        try {{
          const payload = {{
            message,
            conversation_id: conversationId,
          }};
          if (typeof options.easterEggMode === "string" && options.easterEggMode.trim()) {{
            payload.easter_egg_mode = options.easterEggMode.trim();
          }}

          const res = await fetch("/api/chat", {{
            method: "POST",
            headers: {{
              "Content-Type": "application/json"
            }},
            body: JSON.stringify(payload)
          }});
          if (!res.ok) {{
            let detail = `${{res.status}} ${{res.statusText}}`;
            try {{
              const errorPayload = await res.json();
              detail = errorPayload?.detail || detail;
            }} catch (_) {{}}
            throw new Error(`Chat request failed: ${{detail}}`);
          }}
          const data = await res.json();
          conversationId = data.conversation_id;
          const messages = data?.state?.messages;
          if (!Array.isArray(messages)) {{
            throw new Error("Chat response missing message transcript");
          }}
          renderTranscript(messages);
        }} catch (error) {{
          console.error(error);
          renderTranscript([
            ...optimisticMessages,
            {{
              role: "assistant",
              content: `Server response did not complete cleanly: ${{error.message}}`
            }}
          ]);
        }} finally {{
          isSending = false;
          sendButton.disabled = false;
          sendButton.textContent = "Send";
        }}
      }}

      sendButton.addEventListener("click", sendMessage);
      messageBox.addEventListener("input", autoResizeComposer);
      messageBox.addEventListener("keydown", (event) => {{
        if (event.key === "Enter" && !event.shiftKey) {{
          event.preventDefault();
          sendMessage();
        }}
      }});
      autoResizeComposer();

{startup_js}
    </script>
  </body>
</html>
"""


@app.get("/auth/plex/start")
async def plex_auth_start(request: Request, settings: Settings = Depends(get_settings)) -> RedirectResponse:
    if not settings.is_plex_oauth_mode():
        raise HTTPException(status_code=404, detail="Not found")

    client_identifier = _load_plex_client_identifier(settings)
    cookie_provider = _plex_cookie_provider(settings)
    auth_client = PlexAuthClient(product_name=settings.plex_auth_product_name)
    pin = await auth_client.create_pin(client_identifier)
    pin_id = str(pin.get("id") or "")
    pin_code = str(pin.get("code") or "")
    if not pin_id or not pin_code:
        raise HTTPException(status_code=502, detail="Failed to create Plex auth PIN")

    session_id = request.cookies.get("plexorcist_session")
    if session_id:
        session_id = cookie_provider._verify_cookie(session_id) or str(uuid4())
    else:
        session_id = str(uuid4())

    forward_url = f"{settings.base_url.rstrip('/')}/auth/plex/callback"
    auth_url = PlexAuthClient.build_auth_url(
        client_identifier=client_identifier,
        code=pin_code,
        forward_url=forward_url,
        product_name=settings.plex_auth_product_name,
    )
    response = RedirectResponse(auth_url, status_code=303)
    response.set_cookie("plexorcist_session", cookie_provider.sign_cookie(session_id), httponly=True, samesite="lax")
    response.set_cookie("plexorcist_pending_pin", cookie_provider.sign_cookie(pin_id), httponly=True, samesite="lax")
    return response


@app.get("/auth/plex/callback")
async def plex_auth_callback(request: Request, settings: Settings = Depends(get_settings)):
    if not settings.is_plex_oauth_mode():
        raise HTTPException(status_code=404, detail="Not found")

    client_identifier = _load_plex_client_identifier(settings)
    cookie_provider = _plex_cookie_provider(settings)
    pending_pin_id = cookie_provider._verify_cookie(request.cookies.get("plexorcist_pending_pin"))
    if not pending_pin_id:
        return RedirectResponse("/auth/plex/start", status_code=303)

    auth_client = PlexAuthClient(product_name=settings.plex_auth_product_name)
    pin: dict[str, object] | None = None
    last_error: Exception | None = None
    for _ in range(12):
        try:
            pin = await auth_client.get_pin(client_identifier, pending_pin_id)
            if pin.get("authToken"):
                break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        await asyncio.sleep(1)

    if not pin or not pin.get("authToken"):
        message = "Plex sign-in is still pending. Finish the Plex login window and try again."
        if last_error is not None:
            message = f"{message} ({last_error})"
        return HTMLResponse(
            f"""
<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Plex Login</title></head>
  <body style="font-family: sans-serif; padding: 24px;">
    <p>{escape(message)}</p>
    <p><a href="/auth/plex/start">Try again</a></p>
  </body>
</html>
""",
            status_code=202,
        )

    plex_token = str(pin["authToken"])
    plex_user = await auth_client.get_user(client_identifier, plex_token)
    display_name = str(
        plex_user.get("friendlyName")
        or plex_user.get("friendly_name")
        or plex_user.get("username")
        or plex_user.get("title")
        or ""
    )
    username = str(plex_user.get("username") or plex_user.get("title") or display_name or "")
    user_id = str(plex_user.get("id") or plex_user.get("userId") or plex_user.get("user_id") or plex_user.get("uuid") or "")
    is_admin = bool(plex_user.get("admin") or plex_user.get("isAdmin") or plex_user.get("is_admin") or False)
    is_admin = is_admin or settings.is_admin_identity(user_id)
    if not user_id or not username:
        raise HTTPException(status_code=502, detail="Plex login did not return user details")

    # Login gate: user must exist in Ombi before they can proceed into Plexorcist.
    # Match Plex identity first (user_id claims), then username fallback.
    ombi = OmbiClient(settings.ombi_base_url, settings.ombi_api_key)
    ombi_user_check = await ombi.find_user_by_identity(username=username, user_id=user_id)
    if ombi_user_check.get("ok") and not ombi_user_check.get("exists"):
        response = RedirectResponse(settings.ombi_continue_url, status_code=303)
        response.delete_cookie("plexorcist_pending_pin")
        return response

    session_id = str(uuid4())
    session_cookie = cookie_provider._verify_cookie(request.cookies.get("plexorcist_session"))
    if session_cookie:
        session_id = session_cookie

    session_store = PlexAuthSessionStore(settings.database_url)
    session_store.save(
        PlexAuthSession(
            session_id=session_id,
            user_id=user_id,
            username=username,
            display_name=display_name or username,
            is_admin=is_admin,
            plex_token=plex_token,
            auth_source="plex-oauth",
        )
    )
    try:
        await _maybe_send_login_notice(
            settings=settings,
            user_id=user_id,
            username=username,
            display_name=display_name or username,
            is_admin=is_admin,
            request=request,
        )
    except Exception as exc:  # noqa: BLE001
        AuditLogger().log(
            "login_notify_failed",
            {
                "user_id": user_id,
                "username": username,
                "scope": settings.login_notify_scope,
                "error": f"exception:{type(exc).__name__}",
            },
        )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("plexorcist_session", cookie_provider.sign_cookie(session_id), httponly=True, samesite="lax")
    response.delete_cookie("plexorcist_pending_pin")
    return response


@app.get("/auth/logout")
async def plex_auth_logout(request: Request, settings: Settings = Depends(get_settings)) -> RedirectResponse:
    if settings.is_dev_impersonation_mode():
        raise HTTPException(status_code=404, detail="Not found")
    cookie_provider = _plex_cookie_provider(settings)
    session_id = cookie_provider._verify_cookie(request.cookies.get("plexorcist_session"))
    if session_id:
        PlexAuthSessionStore(settings.database_url).delete(str(session_id))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("plexorcist_session")
    response.delete_cookie("plexorcist_pending_pin")
    return response


@app.get("/api/welcome")
async def welcome(user: UserContext = Depends(get_user_context)) -> dict[str, str]:
    greeting_name = user.display_name or user.username
    return {
        "message": f"Hey {greeting_name}. I’m warmed up. Ask me for a movie, show, episode, recommendation, or help with something missing."
    }


@app.get("/api/dev-user")
async def dev_user(
    settings: Settings = Depends(get_settings),
    provider: object = Depends(get_user_context_provider),
) -> dict[str, str]:
    if not settings.is_dev_impersonation_mode() or not isinstance(provider, DevUserContextProvider):
        raise HTTPException(status_code=404, detail="Not found")
    user = await provider.resolve_user(None, None, None, None)
    return {
        "ok": "true",
        "user_id": user.user_id,
        "username": user.username,
        "display_name": user.display_name,
        "is_admin": str(user.is_admin).lower(),
    }


@app.get("/api/dev-users")
async def dev_users(
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if not settings.is_dev_impersonation_mode():
        raise HTTPException(status_code=404, detail="Not found")
    tautulli = TautulliClient(settings.tautulli_base_url, settings.tautulli_api_key)
    users = await tautulli.get_users()
    friendly_names = FriendlyNameDirectory(settings.friendly_names_path)
    enriched_users = []
    for user in users:
        if not isinstance(user, dict):
            continue
        username = str(user.get("friendly_name") or user.get("username") or user.get("user_id") or "")
        display_name = friendly_names.resolve(username, str(user.get("display_name") or ""))
        enriched = dict(user)
        enriched["username"] = username
        enriched["display_name"] = display_name
        enriched["friendly_name"] = username
        enriched_users.append(enriched)
    return {"ok": "true", "users": enriched_users}


@app.post("/api/dev-user")
async def set_dev_user(
    payload: DevImpersonationRequest,
    settings: Settings = Depends(get_settings),
    provider: object = Depends(get_user_context_provider),
) -> dict[str, str]:
    if not settings.is_dev_impersonation_mode() or not isinstance(provider, DevUserContextProvider):
        raise HTTPException(status_code=404, detail="Not found")
    provider.set_override(
        user_id=payload.user_id,
        username=payload.username,
        display_name=payload.display_name,
        is_admin=payload.is_admin,
    )
    return {
        "ok": "true",
        "user_id": payload.user_id,
        "username": payload.username,
        "display_name": payload.display_name,
        "is_admin": str(payload.is_admin).lower(),
    }


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    user: UserContext = Depends(get_user_context),
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    agent, store, audit = build_agent(settings, user)
    try:
        state = store.get_or_create(user.user_id, payload.conversation_id)
        state.support_context["long_term_memory"] = store.get_user_memory_context(
            user.user_id,
            recent_notes_limit=settings.memory_recent_notes_limit,
        )
        extra_instructions: str | None = None
        nilbog_triggered_this_turn = False
        normalized_message = payload.message.strip().lower()
        if any(phrase in normalized_message for phrase in _NILBOG_RESET_PHRASES):
            state.support_context.pop("nilbog_portal_active", None)
            state.support_context.pop("nilbog_memory_mode", None)
            state.support_context.pop("nilbog_pushback_count", None)
            state.support_context.pop("nilbog_redacted_message_index", None)
        elif payload.message.strip() == _NILBOG_TRIGGER_MESSAGE and store.get_user_flag(user.user_id, _NILBOG_SEEN_FLAG) != "true":
            extra_instructions = _build_nilbog_portal_instructions()
            nilbog_triggered_this_turn = True
            store.set_user_flag(user.user_id, _NILBOG_SEEN_FLAG, "true")

        reply, tool_calls = await agent.respond(
            user=user,
            state=state,
            message=payload.message,
            extra_instructions=extra_instructions,
        )
        if nilbog_triggered_this_turn:
            state.support_context["nilbog_portal_active"] = True
            state.support_context["nilbog_memory_mode"] = "blackout_pending"
            state.support_context["nilbog_pushback_count"] = 0
            state.support_context["nilbog_redacted_message_index"] = len(state.messages) - 1
        elif state.support_context.get("nilbog_memory_mode") == "denial" and _is_nilbog_pushback(payload.message):
            pushback_count = int(state.support_context.get("nilbog_pushback_count") or 0) + 1
            state.support_context["nilbog_pushback_count"] = pushback_count
            if pushback_count >= _NILBOG_PUSHBACK_THRESHOLD:
                store.clear_user_flag(user.user_id, _NILBOG_SEEN_FLAG)
                state.support_context["nilbog_memory_mode"] = "rune_leak"
                state.support_context["nilbog_pushback_count"] = pushback_count
        store.save(state)
        store.prune_user_conversations(user.user_id, keep=2)
        audit.log(
            "chat_turn",
            {
                "user_id": user.user_id,
                "username": user.username,
                "message": payload.message,
                "intent": state.intent.value,
                "reply": reply,
                "tool_calls": [call.model_dump(mode="json") for call in tool_calls],
            },
        )
        response_state = state.model_copy(deep=True)
        response_state.candidate_media = []
        response_state.support_context = {}
        response_state.last_tool_actions = []
        response_state.escalation_history = []
        return ChatResponse(
            conversation_id=state.conversation_id,
            reply=reply,
            continue_to_ombi_url=settings.ombi_continue_url,
            state=response_state,
            tool_calls=tool_calls,
        )
    except Exception as exc:
        audit.log(
            "chat_error",
            {
                "user_id": user.user_id,
                "username": user.username,
                "conversation_id": payload.conversation_id,
                "message": payload.message,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise HTTPException(status_code=500, detail=f"chat_server_error:{type(exc).__name__}") from exc

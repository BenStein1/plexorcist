from __future__ import annotations

import asyncio
import json
import contextlib
from html import escape
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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

app = FastAPI(title="Plexorcist Concierge")
_MEMORY_SWEEP_TASK: asyncio.Task | None = None


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

    media = MediaSearchTools(ombi, plex)
    requests = RequestTools(ombi)
    episodes = EpisodeTools(plex, sickchill)
    movie_repairs = MovieRepairTools(ombi, radarr)
    repairs = RepairTools(ombi, sickchill)
    recs = RecommendationTools(tautulli)
    escalation = EscalationTools(jackett, transmission, prowl, user_label=_user_label(user) if user else None)

    bound_username = user.username if user else None
    bound_user_id = user.user_id if user else None

    async def _request_movie_for_authenticated_user(tmdb_id: int) -> dict:
        if not bound_username:
            return {"ok": False, "action": "auth_required", "reason": "authenticated_user_required"}
        return await requests.request_movie_for_user(username=bound_username, tmdb_id=tmdb_id)

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

    registry = ToolRegistry()
    registry.register(
        "search_media",
        media.search_media,
        "Search for a movie or TV show candidate and include whether Plex already has it. When you already know the concrete title, search the plain exact title first before trying embellished variants.",
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
        "Submit a movie request through Ombi for the authenticated user.",
        {
            "type": "object",
            "properties": {
                "tmdb_id": {"type": "integer"},
            },
            "required": ["tmdb_id"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "request_show_scope_for_user",
        _request_show_scope_for_authenticated_user,
        "Submit a TV request through Ombi for the authenticated user with a specific scope such as first_season or full_series.",
        {
            "type": "object",
            "properties": {
                "tvdb_id": {"type": "integer"},
                "scope": {"type": "string"},
            },
            "required": ["tvdb_id", "scope"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "request_episode_for_user",
        _request_episode_for_authenticated_user,
        "Submit a single-episode TV request through Ombi for the authenticated user.",
        {
            "type": "object",
            "properties": {
                "tvdb_id": {"type": "integer"},
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
        "Primary movie repair tool. Use Ombi as the first source of truth for movie request and availability state; after Ombi identifies the movie, let Radarr handle the retry by searching managed releases and grabbing the top torrent by seeders that Radarr allows. Rejections that only say the existing file already meets cutoff or has equal/higher preference do not block replacement.",
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
        "Primary TV troubleshooting tool. It runs the repair loop episode-by-episode in scope: check status, if ignored set wanted, if wanted/missing/processing trigger manual search, then continue to the next episode. Use `scope=show` for vague broken-show complaints.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "scope": {"type": "string", "enum": ["show", "season", "episode"]},
                "season": {"type": "integer", "minimum": 1},
                "episode": {"type": "integer", "minimum": 1},
            },
            "required": ["query", "scope"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "add_requested_show_to_sickchill",
        repairs.add_requested_show_to_sickchill,
        "Repair a requested TV show that exists in Ombi but is missing in SickChill. Without `season`, add the full show. With `season`, limit the repair to that season so only that season is activated/requested.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "tvdb_id": {"type": "integer"},
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
            ombi_continue_url=settings.ombi_continue_url,
            admin_label=settings.admin_display_name or "the admin",
            prowl=prowl,
            movie_direct_source_enabled=settings.movie_direct_source_enabled,
        ),
        ConversationStore(settings.database_url),
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
        summary_lines.append(f"Assistant last replied: {assistant_msgs[-1][:280]}")
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
    client = OpenAIResponsesClient(settings.openai_api_key, settings.openai_model)
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
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
    <style>
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
        font-family: "IBM Plex Sans", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
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
        min-height: 100vh;
        padding: 20px 16px 24px;
        display: grid;
        grid-template-rows: auto minmax(0, 1fr) auto;
        gap: 14px;
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
        font-family: "Space Grotesk", "IBM Plex Sans", sans-serif;
        letter-spacing: -0.03em;
      }}
      p {{
        color: var(--ink-soft);
        line-height: 1.5;
      }}
      textarea {{
        width: 100%;
        min-height: 54px;
        max-height: 180px;
        border-radius: 18px;
        border: 1px solid rgba(148, 163, 184, 0.18);
        padding: 14px;
        font: inherit;
        resize: vertical;
        box-sizing: border-box;
        background: rgba(8, 15, 28, 0.9);
        color: var(--ink);
        outline: none;
        transition: border-color 120ms ease, box-shadow 120ms ease, transform 120ms ease;
      }}
      textarea::placeholder {{
        color: rgba(184, 196, 214, 0.62);
      }}
      textarea:focus {{
        border-color: rgba(125, 211, 252, 0.65);
        box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.14);
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
        justify-content: space-between;
        gap: 12px;
        margin-top: 10px;
        flex-wrap: wrap;
      }}
      @media (max-width: 640px) {{
        main {{
          padding: 12px 10px 16px;
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
      }}
    </style>
  </head>
  <body>
    <main>
      <div class="panel hero">
        <h1>Plexorcist Concierge</h1>
        <p>{hero_copy}</p>
      </div>
{dev_panel_html}
{auth_panel_html}
      <div id="transcript" class="panel chat"></div>
      <div class="panel">
        <div class="composer">
          <textarea id="message" placeholder="Add Breaking Bad.&#10;&#10;Oak Island broken?" {composer_disabled_attr}></textarea>
          <button id="send" {send_disabled_attr}>Send</button>
        </div>
        <div class="composer-meta">
          <p>{("Press Enter to send. Shift+Enter adds a new line." if authenticated else "Sign in with Plex to start chatting.")}</p>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
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
      let conversationId = null;
      let isSending = false;
      const isAuthenticated = {str(authenticated).lower()};
{dev_panel_js}

      async function loadGreeting() {{
        if (!isAuthenticated) return;
        const res = await fetch("/api/welcome");
        const data = await res.json();
        renderTranscript([{{ role: "assistant", content: data.message }}]);
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
          body.textContent = message.content;

          wrapper.appendChild(speaker);
          wrapper.appendChild(body);
          transcript.appendChild(wrapper);
        }}
        transcript.scrollTop = transcript.scrollHeight;
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

      async function sendMessage() {{
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

        try {{
          const res = await fetch("/api/chat", {{
            method: "POST",
            headers: {{
              "Content-Type": "application/json"
            }},
            body: JSON.stringify({{ message, conversation_id: conversationId }})
          }});
          if (!res.ok) {{
            throw new Error(`Chat request failed: ${{res.status}} ${{res.statusText}}`);
          }}
          const data = await res.json();
          conversationId = data.conversation_id;
          const messages = data?.state?.messages;
          if (!Array.isArray(messages)) {{
            throw new Error("Chat response missing message transcript");
          }}
          renderTranscript(messages);
          messageBox.value = "";
        }} catch (error) {{
          console.error(error);
          renderTranscript([
            ...optimisticMessages,
            {{
              role: "assistant",
              content: "Server response did not complete cleanly. Please retry."
            }}
          ]);
        }} finally {{
          isSending = false;
          sendButton.disabled = false;
          sendButton.textContent = "Send";
        }}
      }}

      sendButton.addEventListener("click", sendMessage);
      messageBox.addEventListener("keydown", (event) => {{
        if (event.key === "Enter" && !event.shiftKey) {{
          event.preventDefault();
          sendMessage();
        }}
      }});

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
    state = store.get_or_create(user.user_id, payload.conversation_id)
    inactivity_delta = datetime.utcnow() - state.updated_at
    inactivity_threshold = timedelta(minutes=max(1, int(settings.memory_inactivity_minutes)))
    stale_cutoff = datetime.utcnow() - inactivity_threshold

    timeout_seconds = float(max(5, int(settings.memory_compaction_timeout_seconds)))
    stale_states = store.list_stale_conversations(
        user.user_id,
        older_than=stale_cutoff,
        exclude_conversation_id=state.conversation_id,
        limit=1,
    )
    for stale_state in stale_states:
        await _compact_conversation_once(
            settings=settings,
            store=store,
            audit=audit,
            state=stale_state,
            source="request_stale_sweep",
            timeout_seconds=timeout_seconds,
        )

    state.support_context["long_term_memory"] = store.get_user_memory_context(
        user.user_id,
        recent_notes_limit=settings.memory_recent_notes_limit,
    )
    reply, tool_calls = await agent.respond(user=user, state=state, message=payload.message)
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

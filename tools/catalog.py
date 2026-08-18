"""The concierge tool catalog: one source of truth for every tool.

Each entry pairs a typed input model (tools/schemas.py) with a handler binding
and a description that tells the model WHEN to use the tool, not just what it
does. The in-process agent bridge and the external MCP server both build from
this catalog, so tool behavior can never drift between the two surfaces.

Gating: tags decide which tools a given principal sees at all (admins see
admin tools, escalation tools appear only when enabled). Handlers keep their
own auth checks as defense in depth for the external MCP surface.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from backend.config import Settings
from backend.models import UserContext
from backend.state import ConversationStore
from backend.usage_report import build_token_usage_report
from clients.jackett_client import JackettClient
from clients.ombi_client import OmbiClient
from clients.plex_client import PlexClient
from clients.prowl_client import ProwlClient
from clients.radarr_client import RadarrClient
from clients.sickchill_client import SickChillClient
from clients.tautulli_client import TautulliClient
from clients.transmission_client import TransmissionClient
from tools import schemas
from tools.admin_alerts import AdminAlertReporter
from tools.admin_tools import AdminTools
from tools.episode_tools import EpisodeTools
from tools.escalation_tools import EscalationTools
from tools.media_search import MediaSearchTools
from tools.movie_repair_tools import MovieRepairTools
from tools.recommendation_tools import RecommendationTools
from tools.repair_tools import RepairTools
from tools.request_tools import RequestTools

ToolHandler = Callable[..., Awaitable[dict[str, Any]]]

# Tags
READONLY = "readonly"
REQUEST = "request"
REPAIR = "repair"
ADMIN = "admin"
ESCALATION = "escalation"
DIRECT_SOURCE_GATED = "direct_source_gated"


def _user_label(user: UserContext | None) -> str | None:
    if user is None:
        return None
    display_name = (user.display_name or "").strip()
    username = (user.username or "").strip()
    if display_name and username and display_name.lower() != username.lower():
        return f"{display_name} ({username})"
    return display_name or username or "Unknown user"


@dataclass
class Toolkit:
    """Per-request bundle of service toolsets bound to the authenticated user."""

    settings: Settings
    store: ConversationStore
    user: UserContext | None
    media: MediaSearchTools
    requests: RequestTools
    episodes: EpisodeTools
    movie_repairs: MovieRepairTools
    repairs: RepairTools
    recs: RecommendationTools
    escalation: EscalationTools
    admin_tools: AdminTools
    admin_alerts: AdminAlertReporter
    friendly_names: "FriendlyNameDirectory"

    # -- auth-bound wrappers --------------------------------------------

    def _auth_required(self, **extra: Any) -> dict[str, Any]:
        return {"ok": False, "action": "auth_required", "reason": "authenticated_user_required", **extra}

    def _admin_required(self) -> dict[str, Any]:
        return {"ok": False, "action": "admin_required", "reason": "admin_only"}

    @property
    def _username(self) -> str | None:
        return self.user.username if self.user else None

    @property
    def is_admin(self) -> bool:
        return bool(self.user and self.user.is_admin)

    async def request_movie(self, tmdb_id: int | None = None, title: str | None = None, year: int | None = None) -> dict:
        if not self._username:
            return self._auth_required()
        return await self.requests.request_movie_for_user(username=self._username, tmdb_id=tmdb_id, title=title, year=year)

    async def request_show_scope(self, tvdb_id: int, scope: str) -> dict:
        if not self._username:
            return self._auth_required()
        return await self.requests.request_show_scope_for_user(username=self._username, tvdb_id=tvdb_id, scope=scope)

    async def request_episode(self, tvdb_id: int, season: int, episode: int) -> dict:
        if not self._username:
            return self._auth_required()
        return await self.requests.request_episode_for_user(username=self._username, tvdb_id=tvdb_id, season=season, episode=episode)

    async def check_movie_request_status(self, query: str) -> dict:
        if not self._username:
            return self._auth_required(query=query)
        return await self.requests.check_movie_request_status(query=query, username=self._username)

    async def check_show_request_status(self, query: str) -> dict:
        if not self._username:
            return self._auth_required(query=query)
        return await self.requests.check_show_request_status(query=query, username=self._username)

    async def get_user_watch_context(self) -> dict:
        if not self.user or (not self.user.username and not self.user.user_id):
            return {"resolved": False, "action": "auth_required", "reason": "authenticated_user_required"}
        return await self.recs.get_user_watch_context(user_id=self.user.user_id, username=self.user.username)

    async def get_token_usage(self) -> dict:
        if not self.is_admin:
            return self._admin_required()
        return build_token_usage_report(self.store, self.settings.effective_llm_model)

    async def run_transmission_maintenance(self) -> dict:
        if not self.is_admin:
            return self._admin_required()
        return await self.admin_tools.run_transmission_maintenance()

    async def get_admin_task_summary(
        self,
        user_query: str | None = None,
        scope: str = "all_users",
        days: int | None = None,
        limit: int | None = None,
    ) -> dict:
        if not self.is_admin:
            return self._admin_required()
        return await self.admin_tools.get_admin_task_summary(user_query=user_query, scope=scope, days=days, limit=limit)

    async def send_admin_message(self, message: str, user_query: str | None = None, task_query: str | None = None) -> dict:
        if not self.is_admin:
            return self._admin_required()
        return await self.admin_tools.send_admin_message(
            user_query=user_query,
            task_query=task_query,
            message=message,
            sender_user_id=self.user.user_id,
            sender_name=self.user.display_name or self.user.username or "Ben",
        )

    async def set_admin_motd(self, message: str) -> dict:
        if not self.is_admin:
            return self._admin_required()
        return await self.admin_tools.set_admin_motd(
            message=message,
            sender_user_id=self.user.user_id,
            sender_name=self.user.display_name or self.user.username or "Ben",
        )

    async def clear_admin_motd(self) -> dict:
        if not self.is_admin:
            return self._admin_required()
        return await self.admin_tools.clear_admin_motd()

    async def resolve_admin_task(
        self,
        note_id: int | None = None,
        task_query: str | None = None,
        resolve_all_matches: bool = False,
    ) -> dict:
        if not self.is_admin:
            return self._admin_required()
        return await self.admin_tools.resolve_admin_task(
            note_id=note_id,
            task_query=task_query,
            resolve_all_matches=resolve_all_matches,
        )

    async def exit_admin_task_mode(self) -> dict:
        if not self.is_admin:
            return self._admin_required()
        return {
            "ok": True,
            "action": "admin_task_mode_exited",
            "user_summary": "Left admin task mode without changing any tasks.",
        }

    async def set_user_friendly_name(self, user_query: str, friendly_name: str) -> dict:
        if not self.is_admin:
            return self._admin_required()
        return await self.admin_tools.set_user_friendly_name(user_query=user_query, friendly_name=friendly_name)

    async def find_users(self, query: str | None = None, limit: int = 25) -> dict:
        if not self.is_admin:
            return self._admin_required()
        return await self.admin_tools.find_users(query=query, limit=limit)

    async def set_shabbos_mode(self, user_query: str, enabled: bool) -> dict:
        if not self.is_admin:
            return self._admin_required()
        return await self.admin_tools.set_shabbos_mode(user_query=user_query, enabled=enabled)

    async def get_shabbos_diagnostics(self, user_query: str | None = None) -> dict:
        if not self.is_admin:
            return self._admin_required()
        return await self.admin_tools.get_shabbos_diagnostics(user_query=user_query)

    async def set_my_friendly_name(self, friendly_name: str) -> dict:
        if not self.user or not self.user.username:
            return self._auth_required()
        try:
            self.friendly_names.set_friendly_name(self.user.username, friendly_name)
        except ValueError as exc:
            return {"ok": False, "action": "set_my_friendly_name", "reason": "invalid_input", "user_summary": str(exc)}
        return {
            "ok": True,
            "action": "set_my_friendly_name",
            "friendly_name": friendly_name.strip(),
            "user_summary": f"Done — you're {friendly_name.strip()} now.",
        }

    async def set_admin_nickname(self, friendly_name: str) -> dict:
        if not self.user:
            return self._auth_required()
        cleaned = friendly_name.strip()
        if cleaned.lower() in {"", "default", "reset"}:
            self.store.clear_user_flag(self.user.user_id, "admin_alias")
            return {
                "ok": True,
                "action": "set_admin_nickname",
                "friendly_name": None,
                "user_summary": "Done — back to the default name for the admin.",
            }
        self.store.set_user_flag(self.user.user_id, "admin_alias", cleaned)
        return {
            "ok": True,
            "action": "set_admin_nickname",
            "friendly_name": cleaned,
            "user_summary": f'Done — you\'ll see the owner as "{cleaned}" now.',
        }


def build_toolkit(settings: Settings, store: ConversationStore, user: UserContext | None) -> Toolkit:
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

    from backend.auth_context import FriendlyNameDirectory

    friendly_names = FriendlyNameDirectory(settings.friendly_names_path)

    return Toolkit(
        settings=settings,
        store=store,
        user=user,
        media=MediaSearchTools(ombi, plex),
        requests=RequestTools(ombi),
        episodes=EpisodeTools(plex, sickchill),
        movie_repairs=MovieRepairTools(ombi, radarr),
        repairs=RepairTools(ombi, sickchill),
        recs=RecommendationTools(tautulli),
        escalation=EscalationTools(jackett, transmission, prowl, user_label=_user_label(user)),
        admin_tools=AdminTools(
            transmission,
            store=store,
            friendly_names=friendly_names,
            verify_wait_seconds=settings.transmission_maintenance_verify_wait_seconds,
        ),
        admin_alerts=AdminAlertReporter(prowl),
        friendly_names=friendly_names,
    )


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[schemas.ToolInput]
    resolve: Callable[[Toolkit], ToolHandler]
    tags: frozenset[str] = field(default_factory=frozenset)

    def json_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return schema


def _tags(*values: str) -> frozenset[str]:
    return frozenset(values)


CATALOG: list[ToolSpec] = [
    # --- Media lookup (read-only) ---------------------------------------
    ToolSpec(
        name="search_media",
        description=(
            "Search for movie or TV show candidates and see whether Plex already has the best match. "
            "Use this FIRST to resolve a vague, fuzzy, or half-remembered title before requesting anything. "
            "If an embellished query fails (e.g. 'M.I.A. 2026 Peacock Shannon'), retry with the plain likely title ('M.I.A.') before saying it cannot be found. "
            "If multiple plausible candidates return, ask the user which one they mean using title + year — do not guess."
        ),
        input_model=schemas.SearchMediaInput,
        resolve=lambda tk: tk.media.search_media,
        tags=_tags(READONLY),
    ),
    ToolSpec(
        name="check_movie_availability",
        description="Check whether one movie is already available in Plex. Plex is library truth (what can be watched now); Ombi is request truth. Do not use this to answer request-status questions.",
        input_model=schemas.CheckMovieAvailabilityInput,
        resolve=lambda tk: tk.media.check_movie_availability,
        tags=_tags(READONLY),
    ),
    ToolSpec(
        name="check_movies_availability_batch",
        description=(
            "Check several concrete movie titles in Plex in one pass. "
            "Use when you already know likely titles for a person/director/catalog question and must verify them before claiming the library has none. "
            "Never answer 'nothing in Plex' for a filmography question without running this or check_library_inventory first."
        ),
        input_model=schemas.CheckMoviesAvailabilityBatchInput,
        resolve=lambda tk: tk.media.check_movies_availability_batch,
        tags=_tags(READONLY),
    ),
    ToolSpec(
        name="check_existing_media_status",
        description=(
            "Read-only title status check across Ombi. Use FIRST when the user asks whether something is already added, requested, available, or partly available. "
            "A returned best_match or exact_matches entry is the authoritative Ombi answer — do not contradict it. "
            "Ombi state is not Plex state: never use request status as proof something is or is not watchable in Plex. "
            "For troubleshooting a requested TV show, prefer repair_requested_show instead."
        ),
        input_model=schemas.CheckExistingMediaStatusInput,
        resolve=lambda tk: tk.media.check_existing_media_status,
        tags=_tags(READONLY),
    ),
    ToolSpec(
        name="check_library_inventory",
        description=(
            "Read-only side-by-side inventory: what is actually in Plex versus what exists in Ombi. "
            "Use for broad library questions — an actor, director, collection, franchise, or 'what do we have and what do we need'. "
            "Answer 'have' from the Plex side and 'need/requestable' from the Ombi side, and if the two disagree, say they disagree."
        ),
        input_model=schemas.CheckLibraryInventoryInput,
        resolve=lambda tk: tk.media.check_library_inventory,
        tags=_tags(READONLY),
    ),
    # --- Requests (Ombi) -------------------------------------------------
    ToolSpec(
        name="request_movie_for_user",
        description=(
            "Submit a movie request through Ombi for the authenticated user. "
            "Requires either a positive TMDB id (authoritative when the user gives one) or BOTH exact title and release year — title alone is rejected. "
            "If you only have a title, ask the user for the year or resolve it via search_media first. "
            "If Ombi returns ok: false, report the failure plainly; never imply the request succeeded."
        ),
        input_model=schemas.RequestMovieInput,
        resolve=lambda tk: tk.request_movie,
        tags=_tags(REQUEST),
    ),
    ToolSpec(
        name="request_show_scope_for_user",
        description=(
            "Submit a TV request through Ombi with a scope (first_season / latest_season / full_series). "
            "Requires a positive TVDB id from a prior search/status tool result or explicitly from the user — never call this with only a title in hand; search first. "
            "For long shows (many seasons, anime, reality, nostalgia picks), confirm before requesting full_series; suggest starting with first_season. Skip specials unless asked."
        ),
        input_model=schemas.RequestShowScopeInput,
        resolve=lambda tk: tk.request_show_scope,
        tags=_tags(REQUEST),
    ),
    ToolSpec(
        name="request_episode_for_user",
        description=(
            "Submit a single-episode TV request through Ombi. "
            "Requires a positive TVDB id from a prior tool result or explicitly from the user — if you only have a title, search/status-check first."
        ),
        input_model=schemas.RequestEpisodeInput,
        resolve=lambda tk: tk.request_episode,
        tags=_tags(REQUEST),
    ),
    ToolSpec(
        name="check_movie_request_status",
        description=(
            "Check whether a movie request already exists in Ombi for the authenticated user. "
            "Trust this request record over shallow search fields: a search result saying requested: false is NOT enough to claim nobody requested it."
        ),
        input_model=schemas.QueryInput,
        resolve=lambda tk: tk.check_movie_request_status,
        tags=_tags(READONLY, REQUEST),
    ),
    ToolSpec(
        name="check_show_request_status",
        description="Check whether a TV show request already exists in Ombi for the authenticated user.",
        input_model=schemas.QueryInput,
        resolve=lambda tk: tk.check_show_request_status,
        tags=_tags(READONLY, REQUEST),
    ),
    ToolSpec(
        name="get_show_season_status",
        description=(
            "Read-only Ombi episode table for a show or one season. "
            "Use when the user asks which episodes are missing/available — answer directly from the returned table instead of guessing episode numbers one at a time. "
            "Do not count future-airing episodes as missing. This is a listing tool, not a repair tool: for fixing requested TV problems use repair_requested_show."
        ),
        input_model=schemas.ShowSeasonStatusInput,
        resolve=lambda tk: tk.requests.get_show_season_status,
        tags=_tags(READONLY),
    ),
    # --- Repair -----------------------------------------------------------
    ToolSpec(
        name="repair_requested_movie",
        description=(
            "PRIMARY movie repair tool (Radarr lane). Use directly when a user says a movie downloaded wrong, is a bad copy, has wrong language/audio (admin only — for normal users send_admin_prowl_notice instead), "
            "needs replacement/refetch/retry, was deleted from Plex, or needs re-adding to Radarr. Do not block on Plex availability — the bad copy may already be deleted. "
            "Put ONLY the clean movie title in title (plus year), and the complaint/desired action in issue — never stuff the user's sentence into title. "
            "It performs Ombi/Radarr checks internally, never deletes files, and Radarr owns all quality/release judgment — do not reason about codecs or release ranking yourself. "
            "'Cutoff already met' / 'equal or higher preference' rejections are acceptable replacement overrides, not failures. "
            "If the grab still fails, report that and stop — there is no hidden force mode."
        ),
        input_model=schemas.RepairMovieInput,
        resolve=lambda tk: tk.movie_repairs.repair_requested_movie,
        tags=_tags(REPAIR),
    ),
    ToolSpec(
        name="repair_requested_show",
        description=(
            "PRIMARY TV troubleshooting tool for shows already present in SickChill. Call it in the same turn you identify a confident requested-show match — do not ask permission to inspect first. "
            "Runs the SickChill repair loop episode-by-episode: ignored → set wanted; wanted/missing/processing → trigger manual search; not aired → reported plainly. "
            "Ombi lookup is a soft gate: if it fails but a concrete season/episode target exists, SickChill is still checked (request_gate_soft_failed — not a full failure). "
            "Use scope='show' with only query for vague complaints; narrow scope only when the user did. "
            "NOT for shows missing from SickChill entirely — that Ombi→SickChill handoff failure is add_requested_show_to_sickchill's job."
        ),
        input_model=schemas.RepairShowInput,
        resolve=lambda tk: tk.repairs.repair_requested_show,
        tags=_tags(REPAIR),
    ),
    ToolSpec(
        name="add_requested_show_to_sickchill",
        description=(
            "Repair a requested TV show that exists in Ombi but is MISSING from SickChill (the show-level handoff failure, e.g. requested while SickChill was down). "
            "Only the confirmed show identity is needed; resolve duplicate-title ambiguity (original vs reboot) to a TVDB id first when possible. "
            "Omit season to add the full show — pass it only when the user explicitly asked to repair one season. "
            "For shows already IN SickChill, use repair_requested_show instead."
        ),
        input_model=schemas.AddShowToSickchillInput,
        resolve=lambda tk: tk.repairs.add_requested_show_to_sickchill,
        tags=_tags(REPAIR),
    ),
    ToolSpec(
        name="check_episode_status",
        description=(
            "Read-only inspection of one TV episode across Plex and SickChill, no state changes. "
            "Use for non-requested playback or file questions before taking action. Future air dates are not missing episodes. "
            "SickChill matching is strict: if it returns show_not_found/show_ambiguous/mismatch codes, say the show could not be safely matched — never use another show's episode data."
        ),
        input_model=schemas.EpisodeStatusInput,
        resolve=lambda tk: tk.episodes.check_episode_status,
        tags=_tags(READONLY),
    ),
    ToolSpec(
        name="check_episode_file",
        description="Check whether one TV episode's file exists in Plex and SickChill-backed storage. Read-only support tool.",
        input_model=schemas.EpisodeRefInput,
        resolve=lambda tk: tk.episodes.check_episode_file,
        tags=_tags(READONLY),
    ),
    ToolSpec(
        name="trigger_sickchill_manual_search",
        description=(
            "Ensure SickChill marks one episode wanted and trigger its manual search. "
            "Use after Plex confirms the episode is missing. Prefer repair_requested_show for requested-show complaints — it runs this loop for you."
        ),
        input_model=schemas.EpisodeRefInput,
        resolve=lambda tk: tk.episodes.trigger_sickchill_manual_search,
        tags=_tags(REPAIR),
    ),
    ToolSpec(
        name="clear_sickchill_ignored_episodes",
        description=(
            "Clear SickChill 'ignored' status by marking episodes wanted again. "
            "Use when the user says an episode is ignored or asks to fix the ignore. Does NOT start a search — only trigger one if the user asks."
        ),
        input_model=schemas.ClearIgnoredInput,
        resolve=lambda tk: tk.episodes.clear_sickchill_ignored_episodes,
        tags=_tags(REPAIR),
    ),
    # --- Recommendations --------------------------------------------------
    ToolSpec(
        name="get_user_watch_context",
        description=(
            "Read-only watch history context for the authenticated user, for recommendations. "
            "Prefer year_history_summary, then recently_watched. top_movies_30d/top_tv_30d are SERVER-WIDE trends — never describe them as titles this user personally watched. "
            "If personal history is thin, say so; do not invent watched titles."
        ),
        input_model=schemas.EmptyInput,
        resolve=lambda tk: tk.get_user_watch_context,
        tags=_tags(READONLY),
    ),
    ToolSpec(
        name="set_my_friendly_name",
        description=(
            "Set how the CURRENT authenticated user is addressed by name in chat and in admin notices. "
            "Use only when the user explicitly asks to change their own name/nickname (e.g. 'call me X', 'change my name to X'). "
            "Never use this to rename anyone else — for that, an admin uses set_user_friendly_name."
        ),
        input_model=schemas.SetMyFriendlyNameInput,
        resolve=lambda tk: tk.set_my_friendly_name,
    ),
    ToolSpec(
        name="set_admin_nickname",
        description=(
            "Set what the CURRENT authenticated user calls the ADMIN/OWNER (Ben) in their own chats — e.g. 'call the owner X', "
            "'I want to call Ben X', 'rename the admin to X'. Only changes what this user sees; it does not rename the user "
            "themselves (that's set_my_friendly_name) and does not affect what anyone else sees. Pass 'default' or 'reset' to clear it."
        ),
        input_model=schemas.SetAdminNicknameInput,
        resolve=lambda tk: tk.set_admin_nickname,
    ),
    # --- Admin ------------------------------------------------------------
    ToolSpec(
        name="get_openai_token_usage",
        description="Admin-only LLM token odometer. Use when the admin asks about token usage, MTD/YTD usage, billing estimate, or API cost. Returns MTD/YTD totals plus estimated raw cost before credits.",
        input_model=schemas.EmptyInput,
        resolve=lambda tk: tk.get_token_usage,
        tags=_tags(ADMIN, READONLY),
    ),
    ToolSpec(
        name="run_transmission_maintenance",
        description=(
            "Admin-only Transmission cleanup: verifies completed torrents, removes torrents still reporting errors, reannounces stalled 0% torrents. "
            "Use only when the admin asks to clean up Transmission / clear bad torrents / refresh stalled ones. Reply with a short count summary."
        ),
        input_model=schemas.EmptyInput,
        resolve=lambda tk: tk.run_transmission_maintenance,
        tags=_tags(ADMIN),
    ),
    ToolSpec(
        name="get_admin_task_summary",
        description=(
            "Admin-only LIVE task dashboard over compact memory/task records (not raw conversations or remembered lists). "
            "Use when the admin asks about open user tasks, unresolved issues, or what a named user has pending. "
            "For EVERY list/show/check-again request, call this tool fresh in that same turn; never answer from a prior result or infer the board from a resolve action. "
            "scope='all_users' for broad questions ('any open tasks?', 'anything new?'); scope='specific_user' only when a user is named. "
            "Omit days to include every open task regardless of age. Each task includes exact stored content and a note_id. "
            "When presenting a task list, preserve every returned task's exact content and use the compact '- [note_id] Friendly name (username): content' format from user_summary. "
            "Do not print internal user IDs, merge, deduplicate, paraphrase, or supplement the list from chat memory. "
            "Use note_id with resolve_admin_task once a task is fixed."
        ),
        input_model=schemas.AdminTaskSummaryInput,
        resolve=lambda tk: tk.get_admin_task_summary,
        tags=_tags(ADMIN, READONLY),
    ),
    ToolSpec(
        name="resolve_admin_task",
        description=(
            "Admin-only: mark an open task/issue as resolved so it stops appearing in get_admin_task_summary. "
            "Use when the admin says a task/issue is fixed, done, resolved, handled, or no longer needed. "
            "Prefer note_id from a get_admin_task_summary result seen earlier in this conversation — it's unambiguous. "
            "Otherwise pass task_query in the admin's natural wording; backend matching covers user labels, exact task text, metadata, and close paraphrases. "
            "If matching tasks are duplicates for the same person/title, they are closed together automatically. Set resolve_all_matches=true when the admin says all/every/both or supplies a bulk close instruction. "
            "resolve_all_matches means every task matching task_query, never every task on the board. "
            "The result includes remaining_open_task_count; never claim the board is empty unless that value is zero or a fresh summary has task_count=0. "
            "Never claim success unless ok=true and verified_closed=true; after failure, use returned candidates/note_ids instead of asking the admin to reword the same task repeatedly."
        ),
        input_model=schemas.ResolveAdminTaskInput,
        resolve=lambda tk: tk.resolve_admin_task,
        tags=_tags(ADMIN),
    ),
    ToolSpec(
        name="exit_admin_task_mode",
        description=(
            "Admin-only task-mode routing control. Use only when the admin's latest message is NOT asking to list, "
            "re-check, close, resolve, or otherwise act on admin tasks AND is NOT asking to tell, message, ask, or notify another user. "
            "Use send_admin_message for any outbound user message. This changes no data and lets the conversation "
            "continue normally. Never use it for a task request merely to avoid calling the task tool."
        ),
        input_model=schemas.EmptyInput,
        resolve=lambda tk: tk.exit_admin_task_mode,
        tags=_tags(ADMIN),
    ),
    ToolSpec(
        name="set_user_friendly_name",
        description=(
            "Admin-only: set how a specific user is addressed by name in chat and in admin notices. "
            "Use when the admin asks to rename/relabel a user or set someone else's nickname. "
            "Resolve the target with user_query (friendly name, username, display name, or user ID) — if ambiguous, ask which user before guessing."
        ),
        input_model=schemas.SetUserFriendlyNameInput,
        resolve=lambda tk: tk.set_user_friendly_name,
        tags=_tags(ADMIN),
    ),
    ToolSpec(
        name="find_users",
        description=(
            "Admin-only: look up or browse the people on file — searches friendly names, usernames and display names, "
            "and covers everyone in the friendly-names ledger, not just people who have logged in. "
            "Use when the admin asks who someone is, asks to see the friendly names for a partial name, or when a "
            "send_admin_message / rename lookup came back not-found or ambiguous and you need to show them the options. "
            "Results marked [no account yet] exist only in the name ledger and cannot receive an admin message."
        ),
        input_model=schemas.FindUsersInput,
        resolve=lambda tk: tk.find_users,
        tags=_tags(ADMIN),
    ),
    ToolSpec(
        name="send_admin_message",
        description=(
            "Admin-only: deliver a note to a user. Apply recipient perspective before calling: imagine the recipient sees only "
            "the message argument and none of this conversation. Resolve pronouns, shorthand, omitted subjects, and implied context "
            "so the note stands on its own. For example, after discussing Top Chef, 'tell Don it is fixed' must become a complete "
            "recipient-facing message such as 'The Top Chef issue is fixed', never merely 'Fixed.' This applies to every topic, not only media or fixes. "
            "Preserve the admin's meaning, facts, crude/ribald/affectionate humor, profanity, and tone; do not sanitize or invent details. "
            "Named recipient → user_query. "
            "'Whoever requested X' → task_query with the title/issue and user_query empty (the backend resolves the user from open tasks). "
            "Do not guess recipients from prior chat prose, do not validate media titles, and do not call media tools for this."
        ),
        input_model=schemas.SendAdminMessageInput,
        resolve=lambda tk: tk.send_admin_message,
        tags=_tags(ADMIN),
    ),
    ToolSpec(
        name="set_admin_motd",
        description="Admin-only: set the system-wide MOTD / issue notice. Do not validate media titles or call media tools for this.",
        input_model=schemas.SetMotdInput,
        resolve=lambda tk: tk.set_admin_motd,
        tags=_tags(ADMIN),
    ),
    ToolSpec(
        name="clear_admin_motd",
        description="Admin-only: clear the MOTD when the admin says the issue is over.",
        input_model=schemas.EmptyInput,
        resolve=lambda tk: tk.clear_admin_motd,
        tags=_tags(ADMIN),
    ),
    ToolSpec(
        name="set_shabbos_mode",
        description=(
            "Admin-only: turn Shabbos Mode on or off for one user. "
            "Shabbos Mode gives that account a deterministic slash-command interface and routes it AWAY from the language model entirely — "
            "no model sees their messages, at request time or in any background job. Use when the admin says someone doesn't want AI, "
            "or asks to put a named user on the command interface (or take them off it)."
        ),
        input_model=schemas.SetShabbosModeInput,
        resolve=lambda tk: tk.set_shabbos_mode,
        tags=_tags(ADMIN),
    ),
    ToolSpec(
        name="get_shabbos_diagnostics",
        description=(
            "Admin-only: report what the Shabbos Mode audit log actually recorded — commands run and how many invoked a language model (must be zero). "
            "Use when the admin asks whether Shabbos Mode is really AI-free, or wants to verify a specific user's route."
        ),
        input_model=schemas.ShabbosDiagnosticsInput,
        resolve=lambda tk: tk.get_shabbos_diagnostics,
        tags=_tags(ADMIN, READONLY),
    ),
    # --- Escalation ---------------------------------------------------------
    ToolSpec(
        name="broad_jackett_episode_search",
        description=(
            "Privately search configured sources broadly for a specific episode AFTER normal automation (request + repair/manual search) has already failed. "
            "Never mention indexers, seeders, or tiers to normal users — describe it as 'I searched a little more broadly'. "
            "Do not cap results at 1080p; sort by seeders, quality is metadata only."
        ),
        input_model=schemas.JackettSearchInput,
        resolve=lambda tk: tk.escalation.broad_jackett_episode_search,
        tags=_tags(ESCALATION),
    ),
    ToolSpec(
        name="broad_jackett_movie_search",
        description=(
            "Privately search configured sources broadly for a missing movie AFTER normal automation has failed. "
            "Use only when the normal request already exists and the item is still missing. Same visibility rules as the episode search."
        ),
        input_model=schemas.JackettSearchInput,
        resolve=lambda tk: tk.escalation.broad_jackett_movie_search,
        tags=_tags(ESCALATION, DIRECT_SOURCE_GATED),
    ),
    ToolSpec(
        name="send_admin_prowl_notice",
        description=(
            "Send a short private operational notice to the admin. Use when policy says an issue needs human attention: "
            "a normal user reports wrong language/audio on a movie (notify and stop — do not run movie repair for them), a repair failed, SickChill is unreachable, or a meaningful corrective action was taken outside Ombi. "
            "Do not notify for routine searches or ordinary status checks, and do not send twice for the same issue in one conversation."
        ),
        input_model=schemas.ProwlNoticeInput,
        resolve=lambda tk: tk.escalation.send_admin_prowl_notice,
        tags=_tags(ESCALATION),
    ),
    ToolSpec(
        name="add_transmission_candidate",
        description=(
            "Add a vetted magnet link or torrent URL to the downloader with the correct label. "
            "Use only after a broad source search found a candidate and the user asked to proceed. Describe it to normal users as 'I found a likely match and added it'."
        ),
        input_model=schemas.TransmissionCandidateInput,
        resolve=lambda tk: tk.escalation.add_transmission_candidate,
        tags=_tags(ESCALATION, DIRECT_SOURCE_GATED),
    ),
]

_CATALOG_BY_NAME = {spec.name: spec for spec in CATALOG}


def get_spec(name: str) -> ToolSpec | None:
    return _CATALOG_BY_NAME.get(name)


def visible_specs(user: UserContext | None, settings: Settings) -> list[ToolSpec]:
    """Which tools this principal is allowed to see and call."""
    is_admin = bool(user and user.is_admin)
    specs: list[ToolSpec] = []
    for spec in CATALOG:
        if ADMIN in spec.tags and not is_admin:
            continue
        if DIRECT_SOURCE_GATED in spec.tags and not settings.movie_direct_source_enabled:
            continue
        specs.append(spec)
    return specs
